#!/usr/bin/env python3
"""Regression harness: replay the test set from cached transcripts and cached model outputs.

    python3 scripts/regress.py            # all videos in server/testset.json
    python3 scripts/regress.py SOQynHuWPeI dU7gCt-7kKk
    python3 scripts/regress.py --allow-model   # permit Ollama calls when a cached output is missing/outdated

Runs the server pipeline in-process (no HTTP). With matching DETECT/REFINE versions every model call is served
from server/cache/<id>.json, so a run takes a second or two and makes zero Ollama calls; a prompt change will
need the model (pass --allow-model, or just run the videos once through the real server first).
Stats go to a throwaway SQLite DB, results.local.md is not touched. Exit code 1 on any failure.
"""

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import server  # noqa: E402
import stats  # noqa: E402
import intel  # noqa: E402

DEFAULT_TOL = 1.5


def load_testset():
    with open(os.path.join(ROOT, "server", "testset.json"), "r", encoding="utf-8") as f:
        return json.load(f)["videos"]


def run_video(entry, allow_model):
    vid = entry["video"]
    cached = server.load_cache(vid)
    if not cached or not cached.get("transcript"):
        return None, "no cached transcript (watch it once with the extension)"
    lines = cached["transcript"]
    if not allow_model:
        # Refuse to touch Ollama: any call not served from the cache raises and the video is reported as not run.
        def no_model(*a, **k):
            raise RuntimeError("model call needed but --allow-model not given")
        server.ollama_chat = no_model
    server.cache_valid = lambda *a, **k: False  # always re-process (reusing cached calls)
    job = server.start_or_get_job(vid, cached.get("lang", "en"), lines, cached.get("title", ""),
                                  {"creator": entry.get("creator", ""), "kind": "", "tag": "REGRESS"},
                                  None, None, 0.0, "")
    t0 = time.time()
    while job["status"] not in ("done", "error"):
        server.touch(job)
        try:
            server.worker_step()
        except server.JobError as je:
            server.fail_job(je.job, je.cause)
        if time.time() - t0 > 900:
            return None, "timeout"
    if job["status"] == "error":
        if not allow_model:
            return None, "a model output is missing or outdated in the cache; run the video through the server once, or pass --allow-model"
        return None, f"job error: {job.get('error')}"
    return job["segments"], None


def check(entry, segments):
    """Return list of (ok, message)."""
    out = []
    skips = [s for s in segments if s["action"] == "skip"]
    if entry.get("must_not_skip"):
        out.append((not skips, "no skip" if not skips else f"UNEXPECTED skip(s): {[(server.fmt_time(s['start']), server.fmt_time(s['end']), s['brand']) for s in skips]}"))
    matched = set()
    for e in entry.get("expect", []):
        tol = e.get("tol", DEFAULT_TOL)
        hit = None
        for i, s in enumerate(segments):
            if s["action"] == e["action"] and abs(s["start"] - e["start"]) <= tol and abs(s["end"] - e["end"]) <= tol:
                hit = i
                break
        if hit is not None:
            matched.add(hit)
            out.append((True, f"{e['action']} {server.fmt_time(e['start'])}-{server.fmt_time(e['end'])} ok"))
        else:
            near = [s for s in segments if s["action"] == e["action"] and s["start"] <= e["end"] and s["end"] >= e["start"]]
            detail = f"got {server.fmt_time(near[0]['start'])}-{server.fmt_time(near[0]['end'])} (off by {near[0]['start'] - e['start']:+.1f}s / {near[0]['end'] - e['end']:+.1f}s)" if near else "nothing there"
            out.append((False, f"{e['action']} {server.fmt_time(e['start'])}-{server.fmt_time(e['end'])} MISSING: {detail}"))
    for i, s in enumerate(skips):
        idx = segments.index(s)
        if idx not in matched and not entry.get("must_not_skip"):
            out.append((False, f"UNEXPECTED skip {server.fmt_time(s['start'])}-{server.fmt_time(s['end'])} {s['brand']!r}"))
    for fr in entry.get("forbid", []):
        bad = [s for s in segments if s["start"] < fr["end"] and s["end"] > fr["start"]]
        out.append((not bad, f"nothing at {server.fmt_time(fr['start'])}-{server.fmt_time(fr['end'])}" if not bad
                    else f"FORBIDDEN segment at {server.fmt_time(fr['start'])}-{server.fmt_time(fr['end'])}: {[(s['action'], s['category'], s['brand']) for s in bad]}"))
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    allow_model = "--allow-model" in sys.argv
    tmp = tempfile.mkdtemp(prefix="ss-regress-")
    stats.DB_PATH = os.path.join(tmp, "stats.db")
    server.RESULTS_MD = os.path.join(tmp, "results.md")
    intel._embed_available = False  # no embedding calls during regression
    server.log = lambda *a: None  # quiet; set SS_REGRESS_VERBOSE=1 to see the pipeline log
    if os.environ.get("SS_REGRESS_VERBOSE"):
        server.log = lambda *a: print("   ", *a)
    stats.init()
    intel.init()
    intel.set_logger(server.log)
    entries = load_testset()
    if args:
        entries = [e for e in entries if e["video"] in args]
    failures = 0
    rows = []
    for e in entries:
        t0 = time.time()
        segs, err = run_video(e, allow_model)
        if err:
            rows.append((e["video"], e.get("creator", ""), "SKIP", [f"not run: {err}"], time.time() - t0))
            continue
        results = check(e, segs)
        ok = all(r[0] for r in results)
        failures += 0 if ok else 1
        rows.append((e["video"], e.get("creator", ""), "PASS" if ok else "FAIL", [m for _, m in results], time.time() - t0))
    print(f"{'video':12} {'creator':14} {'result':6} {'time':>6}  details")
    print("-" * 100)
    for vid, cr, res, details, dt in rows:
        print(f"{vid:12} {cr[:14]:14} {res:6} {dt:5.1f}s  {details[0]}")
        for d in details[1:]:
            print(f"{'':12} {'':14} {'':6} {'':>6}  {d}")
    skipped = sum(1 for r in rows if r[2] == "SKIP")
    print("-" * 100)
    print(f"{len(rows) - failures - skipped} passed, {failures} failed, {skipped} not run"
          f"  (versions detect={server.DETECT_VERSION} refine={server.REFINE_VERSION} post={server.POST_VERSION})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
