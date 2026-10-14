#!/usr/bin/env python3
"""Check synthetic transcript files against FORMAT.md without printing their content.

    python3 scripts/synth/validate.py held      # one line per file: ok, or the rule it breaks (never the text)
    python3 scripts/synth/validate.py --hash held   # sha256 over the split's files (sorted by name)

The HELD split must not be read while tuning: this script reports only file names, counts and rule names.
"""
import glob
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXPECTS = {"skip", "keep", "label", "none", "noskip"}
CATEGORIES = {"sponsor", "selfpromo", "membership", "affiliate", "charity", "giveaway", "crosspromo"}


def problems(d, name, scenarios):
    out = []
    want = re.match(r"s(\d\d)_([dh])(\d)\.json$", name)
    if not want:
        return ["file name is not sNN_<d|h><n>.json"]
    if d.get("id") != name[:-5]:
        out.append("id does not match the file name")
    if d.get("scenario") != int(want.group(1)) or d.get("scenario") not in scenarios:
        out.append("scenario number")
    if d.get("split") != {"d": "dev", "h": "held"}[want.group(2)]:
        out.append("split")
    if d.get("lang") not in ("en", "hi") or (d.get("lang") == "hi") != (d.get("scenario") == 18):
        out.append("lang")
    for k in ("title", "creator"):
        if not isinstance(d.get(k), str) or not d[k].strip():
            out.append(f"missing {k}")
    if not isinstance(d.get("description"), str):
        out.append("description must be a string")
    if d.get("paid_promotion") not in (True, False, None):
        out.append("paid_promotion")
    sents = d.get("sentences")
    segs = d.get("segments")
    if not isinstance(sents, list) or len(sents) < 20:
        return out + ["fewer than 20 sentences"]
    if not isinstance(segs, dict) or not segs:
        return out + ["no segments"]
    words = 0
    seen, last = [], None
    for s in sents:
        if not isinstance(s, dict) or not isinstance(s.get("t"), str) or not s["t"].strip():
            out.append("a sentence without text")
            break
        words += len(s["t"].split())
        if d.get("lang") == "hi" and not (isinstance(s.get("en"), str) and s["en"].strip()):
            out.append("a Hindi sentence without 'en'")
            break
        if "gap" in s and not (isinstance(s["gap"], (int, float)) and 0 <= s["gap"] <= 30):
            out.append("gap")
        tag = s.get("seg")
        if tag is not None:
            if tag not in segs:
                out.append(f"sentence tagged with an undeclared segment")
            if tag != last and tag in seen:
                out.append("a segment's sentences are not contiguous")
            if tag not in seen:
                seen.append(tag)
        last = tag
    for k, v in segs.items():
        if k not in seen:
            out.append("a declared segment tags no sentence")
        if not isinstance(v, dict) or v.get("expect") not in EXPECTS or v.get("category") not in CATEGORIES:
            out.append("segment expect/category")
    if not (300 <= words <= 2200):
        out.append(f"length {words} words (want ~500-700, scenario 9 longer)")
    swaps = (d.get("noisy") or {}).get("swaps")
    if not isinstance(swaps, list) or not (1 <= len(swaps) <= 3) or not all(
            isinstance(x, list) and len(x) == 2 and all(isinstance(y, str) and y for y in x) for x in swaps):
        out.append("noisy.swaps")
    return sorted(set(out))


def files(split):
    return sorted(glob.glob(os.path.join(HERE, split, "s*.json")))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    split = args[0] if args else "dev"
    paths = files(split)
    if "--hash" in sys.argv:
        h = hashlib.sha256()
        for p in paths:
            h.update(os.path.basename(p).encode() + b"\0" + open(p, "rb").read() + b"\0")
        print(f"{split}: {len(paths)} files, sha256 {h.hexdigest()}")
        return
    scenarios = {s["id"] for s in json.load(open(os.path.join(HERE, "taxonomy.json")))["scenarios"]}
    bad = 0
    per = {}
    for p in paths:
        name = os.path.basename(p)
        try:
            d = json.load(open(p, encoding="utf-8"))
            errs = problems(d, name, scenarios)
        except ValueError:
            errs = ["not valid JSON"]
        per[name[:3]] = per.get(name[:3], 0) + 1
        if errs:
            bad += 1
            print(f"{name}: {'; '.join(errs)}")
    missing = [f"s{i:02d}" for i in sorted(scenarios) if per.get(f"s{i:02d}", 0) != 3]
    print(f"{split}: {len(paths)} files, {bad} with problems; scenarios without exactly 3 files: {missing or 'none'}")
    sys.exit(1 if bad or missing else 0)


if __name__ == "__main__":
    main()
