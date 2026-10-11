#!/usr/bin/env python3
"""Sponsor Skipper server — localhost:8765, stdlib only.

Stage 3: API skeleton with a STUB classifier. The stub emits one fake sponsor segment
so the extension's relay, polling, cache file and logging can be exercised end to end.
Stage 4 replaces `classify_window` with the real Ollama call.

API
  POST /analyze        {videoId, lang, lines:[{start,end,text}]}  -> job {status, segments}
  GET  /analyze?v=ID                                              -> job or 404 {"error":"unknown"}
  GET  /health                                                    -> {ok, model}

Status: queued -> classifying -> classifying_rest -> done | error
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST, PORT = "127.0.0.1", 8765
MODEL = "qwen3:14b"
SKIP_THRESHOLD = 0.75
WINDOW_S, OVERLAP_S = 240.0, 30.0
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

jobs = {}                 # videoId -> job dict
jobs_lock = threading.Lock()
llm_lock = threading.Lock()  # one LLM call at a time


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


# ---------------------------------------------------------------------------
# Transcript handling
# ---------------------------------------------------------------------------
def clean_lines(raw):
    lines = []
    for item in raw or []:
        try:
            start, end, text = float(item["start"]), float(item["end"]), str(item["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not text or start < 0 or end < start:
            continue
        lines.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    lines.sort(key=lambda l: l["start"])
    return lines


def transcript_hash(lines):
    h = hashlib.sha1()
    for l in lines:
        h.update(f"{l['start']}|{l['text']}\n".encode("utf-8"))
    return h.hexdigest()[:12]


def fmt_time(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def windows(lines):
    """Yield (first_index, last_index_inclusive) over lines, WINDOW_S long with OVERLAP_S overlap.
    First window is 0-WINDOW_S; it is classified before the rest."""
    if not lines:
        return []
    out, total = [], lines[-1]["end"]
    w_start = 0.0
    while True:
        w_end = w_start + WINDOW_S
        idx = [i for i, l in enumerate(lines) if l["start"] >= w_start and l["start"] < w_end]
        if idx:
            out.append((idx[0], idx[-1]))
        if w_end >= total:
            break
        w_start = w_end - OVERLAP_S
    return out


# ---------------------------------------------------------------------------
# Classifier (STUB — replaced by Ollama in stage 4)
# ---------------------------------------------------------------------------
def classify_window(lines, first, last, lang):
    """Return raw model-style segments with global line indices for lines[first..last]."""
    time.sleep(1.0)  # make status transitions observable
    if first != 0:
        return []
    end_line = min(last, first + 3)
    return [{
        "start_line": first + 1 if last > first else first,
        "end_line": end_line,
        "category": "sponsor",
        "style": "read",
        "brand": "StubBrand",
        "confidence": 0.9,
        "reason": "stub classifier: fake segment to exercise the pipeline",
    }]


# ---------------------------------------------------------------------------
# Post-processing (minimal here; stage 5 completes it)
# ---------------------------------------------------------------------------
def action_for(seg):
    if seg["category"] == "sponsor" and seg["style"] == "read" and seg["confidence"] >= SKIP_THRESHOLD:
        return "skip"
    if seg["category"] == "sponsor" and seg["style"] == "integration":
        return "keep"
    return "show"


def to_seconds(seg, lines, first, last):
    s, e = seg.get("start_line"), seg.get("end_line")
    if not isinstance(s, int) or not isinstance(e, int) or s > e or s < first or e > last:
        return None  # hallucinated indices: outside the window it was given
    start = lines[s]["start"]
    end = lines[e + 1]["start"] if e + 1 < len(lines) else lines[e]["end"]
    out = {
        "start": start, "end": end,
        "category": seg.get("category", "sponsor"),
        "style": seg.get("style", "read"),
        "brand": seg.get("brand") or "",
        "confidence": float(seg.get("confidence", 0)),
        "reason": seg.get("reason", ""),
    }
    out["action"] = action_for(out)
    return out


def log_segments(video_id, segments):
    for s in segments:
        log(f"[{video_id}] {s['action'].upper():4} {fmt_time(s['start'])}-{fmt_time(s['end'])} "
            f"{s['category']}/{s['style']} brand={s['brand']!r} conf={s['confidence']:.2f} — {s['reason']}")


# ---------------------------------------------------------------------------
# Jobs and cache
# ---------------------------------------------------------------------------
def cache_path(video_id):
    return os.path.join(CACHE_DIR, f"{video_id}.json")


def load_cache(video_id):
    try:
        with open(cache_path(video_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_cache(video_id, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = cache_path(video_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, cache_path(video_id))


def public_job(job):
    return {"status": job["status"], "segments": job["segments"]}


def run_job(video_id):
    with jobs_lock:
        job = jobs[video_id]
        lines, lang, thash = job["lines"], job["lang"], job["hash"]
    try:
        wins = windows(lines)
        segments = []
        for n, (first, last) in enumerate(wins):
            with jobs_lock:
                job["status"] = "classifying" if n == 0 else "classifying_rest"
            with llm_lock:
                raw = classify_window(lines, first, last, lang)
            for seg in raw:
                conv = to_seconds(seg, lines, first, last)
                if conv:
                    segments.append(conv)
            with jobs_lock:
                job["segments"] = list(segments)
        log_segments(video_id, segments)
        save_cache(video_id, {"videoId": video_id, "hash": thash, "lang": lang,
                              "model": MODEL, "segments": segments, "transcript": lines})
        with jobs_lock:
            job["status"] = "done"
        log(f"[{video_id}] done: {len(segments)} segment(s), {len(wins)} window(s), cached")
    except Exception as e:  # noqa: BLE001
        log(f"[{video_id}] error: {e!r}")
        with jobs_lock:
            job["status"] = "error"
            job["error"] = repr(e)


def start_or_get_job(video_id, lang, lines):
    thash = transcript_hash(lines)
    with jobs_lock:
        job = jobs.get(video_id)
        if job and job["hash"] == thash and job["status"] != "error":
            return job
        cached = load_cache(video_id)
        if cached and cached.get("hash") == thash:
            job = {"status": "done", "segments": cached.get("segments", []),
                   "hash": thash, "lang": lang, "lines": lines}
            jobs[video_id] = job
            log(f"[{video_id}] cache hit ({len(job['segments'])} segment(s))")
            return job
        job = {"status": "queued", "segments": [], "hash": thash, "lang": lang, "lines": lines}
        jobs[video_id] = job
    log(f"[{video_id}] new job: {len(lines)} lines, lang={lang}, hash={thash}")
    threading.Thread(target=run_job, args=(video_id,), daemon=True).start()
    return job


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "SponsorSkipper/0.3"

    def log_message(self, fmt, *args):  # quiet default access log
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_json(204, {})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self.send_json(200, {"ok": True, "model": MODEL})
        if u.path == "/analyze":
            vid = (parse_qs(u.query).get("v") or [""])[0]
            if not VIDEO_ID_RE.match(vid):
                return self.send_json(400, {"error": "bad video id"})
            with jobs_lock:
                job = jobs.get(vid)
            if not job:
                cached = load_cache(vid)
                if cached:
                    return self.send_json(200, {"status": "done", "segments": cached.get("segments", [])})
                return self.send_json(404, {"error": "unknown"})
            return self.send_json(200, public_job(job))
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/analyze":
            return self.send_json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad json"})
        vid = str(data.get("videoId", ""))
        if not VIDEO_ID_RE.match(vid):
            return self.send_json(400, {"error": "bad video id"})
        lines = clean_lines(data.get("lines"))
        if not lines:
            return self.send_json(400, {"error": "no lines"})
        lang = str(data.get("lang") or "und")[:12]
        job = start_or_get_job(vid, lang, lines)
        with jobs_lock:
            return self.send_json(200, public_job(job))


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"Sponsor Skipper server on http://{HOST}:{PORT}  model={MODEL}  cache={CACHE_DIR}")
    log("classifier: STUB (stage 3)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
