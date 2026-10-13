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
import shutil
import sqlite3
import subprocess
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
    lines = server.clean_lines(cached["transcript"])  # exactly what POST /analyze does with the client's lines
    if not allow_model:
        # Refuse to touch Ollama: any call not served from the cache raises and the video is reported as not run.
        def no_model(*a, **k):
            raise RuntimeError("model call needed but --allow-model not given")
        server.ollama_chat = no_model
    server.cache_valid = lambda *a, **k: False  # always re-process (reusing cached calls)
    server.jobs.clear()  # entries may share a video (with and without metadata)
    m = entry.get("meta") or {}
    meta = {"creator": m.get("creator", entry.get("creator", "")), "kind": m.get("kind", ""), "tag": "REGRESS",
            "channel_id": m.get("channelId", ""), "duration": m.get("duration"),
            "paid_promotion": m.get("paidPromotion") if isinstance(m.get("paidPromotion"), bool) else None,
            "heat": [], "client": None}
    job = server.start_or_get_job(vid, cached.get("lang", "en"), lines, m.get("title", cached.get("title", "")),
                                  meta, None, None, 0.0, m.get("description", ""))
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
    return job, None


def check_meta(entry, job):
    """expect_meta: what the server must have derived from the request metadata (hints, canonical brand, stats row)."""
    out = []
    em = entry.get("expect_meta") or {}
    if not em:
        return out
    hints = job.get("hints") or {}
    if "hints" in em:
        got = bool(hints.get("brands") or hints.get("codes") or hints.get("domains"))
        out.append((got == em["hints"], f"hints {'found' if got else 'none'}: brands={hints.get('brands')} domains={hints.get('domains')}"
                    + ("" if got == em["hints"] else f" (expected {'some' if em['hints'] else 'none'})")))
    skips = [s for s in job["segments"] if s["action"] == "skip"]
    if "brand" in em:
        got = skips[0].get("brand_canonical") if skips else None
        out.append((got == em["brand"], f"brand {skips[0].get('brand')!r} -> {got!r}" if skips else "brand: no skip segment"
                    if got == em["brand"] else f"brand {skips[0].get('brand') if skips else None!r} -> {got!r}, expected {em['brand']!r}"))
    c = sqlite3.connect(stats.DB_PATH)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM videos WHERE video_id = ?", (job["video_id"],)).fetchone()
    c.close()
    if "creator" in em:
        got = row["channel_name"] if row else None
        out.append((got == em["creator"], f"stats creator {got!r}" + ("" if got == em["creator"] else f", expected {em['creator']!r}")))
    if em.get("duration"):
        got = row["duration_s"] if row else None
        out.append((bool(got), f"stats duration {got}"))
    if "disclosure" in em:
        got = sorted({s.get("disclosure") for s in job["segments"] if s.get("category") == "sponsor" and s.get("style") == "read"})
        out.append((got == [em["disclosure"]], f"disclosure {got}" + ("" if got == [em["disclosure"]] else f", expected {em['disclosure']!r}")))
    return out


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
            # start may be later than expected by start_late_ok (a later start is the safe direction), never earlier
            start_ok = -tol <= s["start"] - e["start"] <= tol + e.get("start_late_ok", 0)
            if s["action"] == e["action"] and start_ok and abs(s["end"] - e["end"]) <= tol \
                    and (not e.get("category") or s.get("category") == e["category"]):
                hit = i
                break
        if hit is not None:
            matched.add(hit)
            out.append((True, f"{e['action']}{' ' + e['category'] if e.get('category') else ''} {server.fmt_time(e['start'])}-{server.fmt_time(e['end'])} ok"))
        else:
            near = [s for s in segments if s["action"] == e["action"] and s["start"] <= e["end"] and s["end"] >= e["start"]]
            detail = f"got {server.fmt_time(near[0]['start'])}-{server.fmt_time(near[0]['end'])} (off by {near[0]['start'] - e['start']:+.1f}s / {near[0]['end'] - e['end']:+.1f}s)" if near else "nothing there"
            out.append((False, f"{e['action']}{' ' + e['category'] if e.get('category') else ''} {server.fmt_time(e['start'])}-{server.fmt_time(e['end'])} MISSING: {detail}"))
    for i, s in enumerate(skips):
        idx = segments.index(s)
        if idx not in matched and not entry.get("must_not_skip"):
            out.append((False, f"UNEXPECTED skip {server.fmt_time(s['start'])}-{server.fmt_time(s['end'])} {s['brand']!r}"))
    for fr in entry.get("forbid", []):
        bad = [s for s in segments if s["start"] < fr["end"] and s["end"] > fr["start"]]
        out.append((not bad, f"nothing at {server.fmt_time(fr['start'])}-{server.fmt_time(fr['end'])}" if not bad
                    else f"FORBIDDEN segment at {server.fmt_time(fr['start'])}-{server.fmt_time(fr['end'])}: {[(s['action'], s['category'], s['brand']) for s in bad]}"))
    return out


def unit_checks():
    """Pure-function guards for rules that have regressed before. Returns (ok, message) pairs."""
    out = []

    def eq(name, got, want):
        out.append((got == want, f"{name}" if got == want else f"{name}: got {got!r}, expected {want!r}"))
    # transcript cleaning is idempotent, including consecutive speaker markers
    for t in (">> >> So it needed insights.", "expensive, >> especially >> in the US", "[Music] >> [Applause] >> hello"):
        once = server.strip_noise(t)
        eq(f"strip_noise({t!r}) has no '>>' and is stable", (">>" in once, server.strip_noise(once) == once), (False, True))
    # description hints: only sponsor lines count; the creator's own links, socials and name do not
    h = server.extract_hints("Learn to code with Coddy (ad): https://fern.deals/coddy")
    eq("hints: (ad) line -> brand + link", (h["brands"], h["domains"]), (["Coddy"], ["fern.deals/coddy"]))
    eq("hint terms drop the creator's own name", server.hint_terms(h, "fern"), ["Coddy", "coddy"])
    h = server.extract_hints("Order food at: https://www.eatsides.com/\nMerch: https://www.sidemenclothing.com\nhttps://www.instagram.com/Sidemen")
    eq("hints: own shop and social links are not sponsor hints", (h["brands"], h["domains"]), ([], []))
    h = server.extract_hints("Get 60% off Incogni with code HARRIS: https://incogni.com/harris")
    eq("hint terms: creator's code/name excluded, brand kept", server.hint_terms(h, "Johnny Harris"), ["incogni.com/harris", "incogni"])
    h = server.extract_hints("This video is sponsored by Odoo.\nhttps://www.odoo.com/r/fern")
    eq("hints: link on the line after a sponsor line", (h["brands"], h["domains"]), (["Odoo"], ["odoo.com/r"]))
    eq("ad lines: a denial is not a sponsorship marker",
       [server.is_ad_line(l) for l in ("Coddy (ad): https://fern.deals/coddy", "This video is sponsored by Odoo.",
                                       "This video is not sponsored.", "No sponsor today.")], [True, True, False, False])
    # halls: a brand is ranked only from HALL_MIN_SPOTS spots
    def spot(i, brand, style="read", action="skip"):
        vid = f"hall{brand[:3]}{i:04d}"[:11].ljust(11, "0")
        stats.record_video({"video_id": vid, "channel_id": "UCh", "creator": "hall test", "title": "", "lang": "en", "duration": 600.0,
                            "hints": {"brands": [brand]}, "lines": []},
                           [{"start": 100.0, "end": 160.0, "category": "sponsor", "style": style, "action": action, "brand": brand,
                             "confidence": 0.95, "evidence": "", "reason": "", "source": "detect"}], {})
        if action == "skip":
            stats.record_event({"kind": "skip", "videoId": vid, "segment": {"start": 100.0, "end": 160.0}, "playhead": 100.0, "saved": 60.0})
    for i in range(3):
        spot(i, "Thricebrand")
        spot(i, "Integrated", "integration", "keep")
    for i in range(2):
        spot(i, "Twicebrand")
    d = stats.compute()
    eq(f"halls: ranked only from {stats.HALL_MIN_SPOTS} spots",
       ([b["brand"] for b in d["hall_of_shame"]], [b["brand"] for b in d["hall_of_fame"]]), (["Thricebrand"], ["Integrated"]))
    # disclosure is 'undisclosed' only when both signals are known and negative
    table = {(p, d): server.disclosure_state(p, d) for p in (True, False, None) for d in (True, False, None)}
    eq("disclosure: only (False, False) is undisclosed", sorted(k for k, v in table.items() if v == "undisclosed"), [(False, False)])
    eq("disclosure: any missing signal without a positive one is unknown",
       [table[(None, None)], table[(False, None)], table[(None, False)]], ["unknown"] * 3)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    allow_model = "--allow-model" in sys.argv
    tmp = tempfile.mkdtemp(prefix="sr-regress-")
    # Nothing the harness does may land in the user's local data: stats, results log and feedback go to a temp dir.
    stats.DB_PATH = os.path.join(tmp, "stats.db")
    server.RESULTS_MD = os.path.join(tmp, "results.md")
    server.FEEDBACK_JSONL = os.path.join(tmp, "feedback.jsonl")
    intel._embed_available = False  # no embedding calls during regression
    server.log = lambda *a: None  # quiet; set SR_REGRESS_VERBOSE=1 to see the pipeline log
    if os.environ.get("SR_REGRESS_VERBOSE") or os.environ.get("SS_REGRESS_VERBOSE"):
        server.log = lambda *a: print("   ", *a)
    stats.init()
    intel.init()
    intel.set_logger(server.log)
    entries = load_testset()
    if args:
        entries = [e for e in entries if e["video"] in args or e.get("id") in args]
    failures = 0
    rows = []
    if not args:
        # Extension wiring first: metadata must survive page.js -> content.js -> background.js -> POST /analyze.
        t0 = time.time()
        node = shutil.which("node")
        if node:
            r = subprocess.run([node, os.path.join(ROOT, "scripts", "wiring_test.js")], capture_output=True, text=True)
            out = [l.strip() for l in (r.stdout + r.stderr).strip().splitlines() if l.strip()] or ["(no output)"]
            failures += 0 if r.returncode == 0 else 1
            rows.append(("wiring", "extension", "PASS" if r.returncode == 0 else "FAIL", out, time.time() - t0))
        else:
            rows.append(("wiring", "extension", "SKIP", ["not run: node not found"], 0.0))
    if not args:
        t0 = time.time()
        results = unit_checks()
        bad = [m for ok, m in results if not ok]
        failures += 1 if bad else 0
        rows.append(("rules", "server", "FAIL" if bad else "PASS", bad or [f"{len(results)} rule checks ok (cleaning, hints, halls, disclosure)"], time.time() - t0))
    for e in entries:
        t0 = time.time()
        label = e.get("id", e["video"])
        job, err = run_video(e, allow_model)
        if err:
            rows.append((label, e.get("creator", ""), "SKIP", [f"not run: {err}"], time.time() - t0))
            continue
        results = check(e, job["segments"]) + check_meta(e, job)
        ok = all(r[0] for r in results)
        failures += 0 if ok else 1
        rows.append((label, e.get("creator", ""), "PASS" if ok else "FAIL", [m for _, m in results], time.time() - t0))
    print(f"{'video':22} {'creator':14} {'result':6} {'time':>6}  details")
    print("-" * 110)
    for vid, cr, res, details, dt in rows:
        print(f"{vid:22} {cr[:14]:14} {res:6} {dt:5.1f}s  {details[0]}")
        for d in details[1:]:
            print(f"{'':22} {'':14} {'':6} {'':>6}  {d}")
    skipped = sum(1 for r in rows if r[2] == "SKIP")
    print("-" * 110)
    print(f"{len(rows) - failures - skipped} passed, {failures} failed, {skipped} not run"
          f"  (versions detect={server.DETECT_VERSION} refine={server.REFINE_VERSION} post={server.POST_VERSION})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
