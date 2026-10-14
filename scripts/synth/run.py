#!/usr/bin/env python3
"""Run the synthetic suite through the real pipeline and score it against the scripted truth.

    python3 scripts/synth/run.py dev                    # every DEV file, clean and noisy
    python3 scripts/synth/run.py dev s05 s09_d2         # only these scenarios / files
    python3 scripts/synth/run.py dev --variants clean
    python3 scripts/synth/run.py held --final           # the one HELD run (aggregates only, never transcript text)

Starts a second server instance (SR_PORT=8766, SR_DATA_DIR = a fresh temp dir, never server/cache) with the real
model, POSTs each built transcript like the extension does, polls until the job is done, and scores:
  false-skip seconds  skipped time outside every 'skip' truth segment (content, integrations, look-alikes)
  miss                a 'skip' segment less than half covered by skips
  wrong               a 'keep' without a keep marker, a 'label' without a label of its category, anything marked on a
                      'none' segment (rule labels like 'subscribe' aside), a skip on a 'noskip' segment
  boundary            start/end of the skip covering a 'skip' segment minus the truth (s); + = later
A run passes with no miss, nothing wrong, false skips <= 1.5 s, and every covering skip starting at most 4 s late,
ending at most 4 s early, and leaving no more than 4 s unskipped between its first and last skip. Results: one JSON line per run in scripts/synth/results/<split>-<stamp>.jsonl.
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import build  # noqa: E402

PORT = int(os.environ.get("SYNTH_PORT", "8766"))
API = f"http://127.0.0.1:{PORT}"
FALSE_SKIP_MAX = 1.5
LATE_START_MAX = 4.0
EARLY_END_MAX = 4.0
HOLE_MAX = 4.0
RULE_CATS = {"interaction", "intro", "outro", "recap"}
LABEL_ALIKE = {"selfpromo": {"selfpromo", "membership"}, "membership": {"selfpromo", "membership"}}


def http(method, path, body=None, timeout=30):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-SR-Client": "extension"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def start_server(data_dir, log_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SR_", "SS_"))}
    env.update(SR_PORT=str(PORT), SR_DATA_DIR=data_dir)
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server", "server.py")], env=env,
                            stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            if http("GET", "/health", timeout=2).get("ok"):
                return proc
        except (OSError, ValueError):
            pass
        time.sleep(1)
    proc.kill()
    sys.exit(f"synth: the test server did not come up (log: {log_path})")


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def score(truth, segs):
    skips = [s for s in segs if s["action"] == "skip"]
    skip_truth = [t for t in truth if t["expect"] == "skip"]
    res = {"false_skip_s": 0.0, "misses": [], "wrong": [], "boundary": []}
    for s in skips:   # skipped time outside every skip segment
        inside = sum(overlap(s["start"], s["end"], t["start"], t["end"]) for t in skip_truth)
        res["false_skip_s"] += max(0.0, (s["end"] - s["start"]) - inside)
    res["false_skip_s"] = round(res["false_skip_s"], 2)
    for t in truth:
        span = t["end"] - t["start"]
        over = [s for s in segs if overlap(s["start"], s["end"], t["start"], t["end"]) > 0]
        name = f"{t['key']} {t['expect']} {t['category']}"
        if t["expect"] == "skip":
            cov = [s for s in over if s["action"] == "skip"]
            got = sum(overlap(s["start"], s["end"], t["start"], t["end"]) for s in cov)
            if got < 0.5 * span:
                res["misses"].append(f"{name}: {got:.0f} of {span:.0f} s skipped")
                continue
            ds, de = round(min(s["start"] for s in cov) - t["start"], 2), round(max(s["end"] for s in cov) - t["end"], 2)
            res["boundary"].append([ds, de])
            if ds > LATE_START_MAX or de < -EARLY_END_MAX:
                res["wrong"].append(f"{name}: boundary start {ds:+.1f} s, end {de:+.1f} s")
            a, b = max(t["start"], min(s["start"] for s in cov)), min(t["end"], max(s["end"] for s in cov))
            hole = (b - a) - got
            if hole > HOLE_MAX:
                res["wrong"].append(f"{name}: {hole:.0f} s inside it not skipped")
        elif t["expect"] == "keep":
            if not any(s["action"] == "keep" and overlap(s["start"], s["end"], t["start"], t["end"]) >= 0.5 * span for s in over):
                res["wrong"].append(f"{name}: no keep marker ({[(s['action'], s['category']) for s in over]})")
        elif t["expect"] == "label":
            ok = LABEL_ALIKE.get(t["category"], {t["category"]})
            if not any(s["action"] == "show" and s["category"] in ok for s in over):
                res["wrong"].append(f"{name}: no {t['category']} label ({[(s['action'], s['category']) for s in over]})")
        elif t["expect"] == "none":
            bad = [s for s in over if s["category"] not in RULE_CATS]
            if bad:
                res["wrong"].append(f"{name}: marked {[(s['action'], s['category'], s.get('brand')) for s in bad]}")
        elif t["expect"] == "noskip":
            if any(s["action"] == "skip" for s in over):
                res["wrong"].append(f"{name}: skipped")
    res["pass"] = not res["misses"] and not res["wrong"] and res["false_skip_s"] <= FALSE_SKIP_MAX
    return res


def run_one(d, variant):
    payload, truth = build.build(d, noisy=(variant == "noisy"))
    vid = "sy" + hashlib.sha1(f"{d['id']}|{variant}|{build.BUILD_VERSION}".encode()).hexdigest()[:9]
    payload["videoId"] = vid
    t0 = time.time()
    job = http("POST", "/analyze", payload, timeout=60)
    while job.get("status") not in ("done", "error"):
        if time.time() - t0 > 1200:
            return {"error": "timeout"}, truth
        time.sleep(1.5)
        job = http("GET", f"/analyze?v={vid}")
    return {"status": job["status"], "error": job.get("error"), "segments": job.get("segments") or [],
            "seconds": round(time.time() - t0, 1)}, truth


def hints_of(description):
    """Scenario 19: the description must give no sponsor hint. Uses the server's own parser (no model)."""
    os.environ.setdefault("SR_DATA_DIR", tempfile.mkdtemp(prefix="sr-synth-hints-"))
    sys.path.insert(0, os.path.join(ROOT, "server"))
    import server  # noqa: E402
    h = server.extract_hints(description)
    return {k: h[k] for k in ("brands", "codes", "domains")}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    split = args[0] if args else "dev"
    only = args[1:]
    variants = ["clean", "noisy"]
    for a in sys.argv:
        if a.startswith("--variants="):
            variants = a.split("=", 1)[1].split(",")
    held = split == "held"
    if held and "--final" not in sys.argv:
        sys.exit("synth: the HELD split runs once, with --final")
    paths = sorted(glob.glob(os.path.join(HERE, split, "s*.json")))
    if only:
        paths = [p for p in paths if any(os.path.basename(p).startswith(o) for o in only)]
    # DEV may keep one data dir across runs (SYNTH_DATA_DIR): model calls are then reused whenever their inputs are
    # unchanged, so a change to filters or rules re-scores in seconds. HELD always starts from an empty one.
    keep_dir = os.environ.get("SYNTH_DATA_DIR") if not held else None
    data_dir = keep_dir or tempfile.mkdtemp(prefix="sr-synth-")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(HERE, "results", f"{split}-{stamp}.jsonl")
    log_path = os.path.join(HERE, "results", f"{split}-{stamp}.server.log")
    proc = start_server(data_dir, log_path)
    print(f"synth: {len(paths)} file(s) x {len(variants)} variant(s) on {API}, data dir {data_dir}, results {out_path}", flush=True)
    t_all = time.time()
    try:
        with open(out_path, "w", encoding="utf-8") as out:
            for p in paths:
                d = json.load(open(p, encoding="utf-8"))
                for v in variants:
                    try:
                        job, truth = run_one(d, v)
                    except (OSError, ValueError) as e:
                        job, truth = {"error": f"request failed: {e!r}"}, []
                    res = score(truth, job.get("segments") or []) if not job.get("error") else \
                        {"pass": False, "false_skip_s": 0.0, "misses": [], "wrong": [f"job error: {job['error']}"], "boundary": []}
                    if d["scenario"] == 19:
                        h = hints_of(d.get("description") or "")
                        if any(h.values()):
                            res["wrong"].append(f"description hint from a disclaimer: {h}")
                            res["pass"] = False
                    row = {"id": d["id"], "scenario": d["scenario"], "variant": v, **res, "seconds": job.get("seconds")}
                    if not held:   # HELD rows keep scores only
                        row.update(segments=[{k: s.get(k) for k in ("action", "category", "style", "start", "end", "brand",
                                                                     "confidence", "evidence", "source")} for s in job.get("segments") or []],
                                   truth=truth)
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    out.flush()
                    detail = "" if held else "  " + "; ".join(res["misses"] + res["wrong"])[:200]
                    print(f"{d['id']:8} {v:5} {'PASS' if res['pass'] else 'FAIL'}  false-skip {res['false_skip_s']:5.1f}s  "
                          f"{job.get('seconds') or 0:6.1f}s{detail}", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not keep_dir:
            shutil.rmtree(data_dir, ignore_errors=True)
    print(f"synth: done in {(time.time() - t_all) / 60:.0f} min")
    report(out_path)


def report(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    tax = {s["id"]: s for s in json.load(open(os.path.join(HERE, "taxonomy.json")))["scenarios"]}
    print(f"\n{'#':>2}  {'scenario':40} {'expect':7} {'clean':>5} {'noisy':>5}  false-skip s  misses  wrong")
    for sid in sorted({r["scenario"] for r in rows}):
        rs = [r for r in rows if r["scenario"] == sid]
        c = [r for r in rs if r["variant"] == "clean"]
        n = [r for r in rs if r["variant"] == "noisy"]
        print(f"{sid:>2}  {tax[sid]['name'][:40]:40} {tax[sid]['expect']:7} "
              f"{sum(r['pass'] for r in c)}/{len(c):<3} {sum(r['pass'] for r in n)}/{len(n):<3}  "
              f"{sum(r['false_skip_s'] for r in rs):11.1f}  {sum(len(r['misses']) for r in rs):6}  {sum(len(r['wrong']) for r in rs):5}")
    for v in ("clean", "noisy"):
        rs = [r for r in rows if r["variant"] == v]
        b = [x for r in rs for x in r["boundary"]]
        mean = (sum(abs(x[0]) for x in b) / len(b), sum(abs(x[1]) for x in b) / len(b)) if b else (0, 0)
        print(f"{v}: {sum(r['pass'] for r in rs)}/{len(rs)} pass, false skips {sum(r['false_skip_s'] for r in rs):.1f} s in "
              f"{sum(1 for r in rs if r['false_skip_s'] > 0)} run(s), {sum(len(r['misses']) for r in rs)} miss(es), "
              f"mean |boundary| start {mean[0]:.1f} s / end {mean[1]:.1f} s")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--report":
        report(sys.argv[2])
    else:
        main()
