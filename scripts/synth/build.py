#!/usr/bin/env python3
"""Turn a synthetic transcript file (FORMAT.md) into what the extension would POST, plus the truth in seconds.

    python3 scripts/synth/build.py dev/s05_d1.json [--noisy]   # prints the built payload (DEV files only)

clean: one caption line per sentence (split at 8 s), word timing on every word, punctuation and capitals kept.
noisy: lowercase, no punctuation, the file's swaps applied, cues of 6-10 words merged into lines of up to 8 s, no
word timing (words spread evenly inside each line, timed: false), like an auto track without word offsets.
Timings are deterministic per file: words take 0.22-0.75 s by length, sentences are 0.2-0.6 s apart unless the
file sets a gap.
"""
import hashlib
import json
import random
import re
import sys

BUILD_VERSION = 1
LINE_MAX_S = 8.0


def word_seconds(w):
    return max(0.22, min(0.75, 0.16 + 0.045 * len(w)))


def timed_sentences(d):
    """[(sentence dict, [[start, end, word], ...])] on one timeline."""
    rng = random.Random(hashlib.sha1(d["id"].encode()).hexdigest())
    t, out = 0.6, []
    for s in d["sentences"]:
        t += s["gap"] if isinstance(s.get("gap"), (int, float)) else rng.uniform(0.2, 0.6)
        words = []
        for w in s["t"].split():
            dur = word_seconds(w) * rng.uniform(0.9, 1.1)
            words.append([round(t, 2), round(t + dur, 2), w])
            t += dur
        out.append((s, words))
    return out


def clean_lines(timed):
    lines = []
    for _, words in timed:
        chunk = []
        for w in words:
            if chunk and w[1] - chunk[0][0] > LINE_MAX_S:
                lines.append(chunk)
                chunk = []
            chunk.append(w)
        if chunk:
            lines.append(chunk)
    return [{"start": c[0][0], "end": c[-1][1], "text": " ".join(w[2] for w in c), "words": c, "timed": True} for c in lines]


PUNCT_RE = re.compile(r"[.,!?;:\"“”‘’()\[\]…।—–]|(?<!\w)'|'(?!\w)")


def noisy_text(text, swaps):
    t = text.lower()
    for a, b in swaps:
        t = re.sub(r"(?<!\w)" + re.escape(a.lower()) + r"(?!\w)", b.lower(), t)
    return PUNCT_RE.sub(" ", t).split()


def noisy_lines(timed, swaps, seed):
    rng = random.Random(seed)
    toks = []
    for s, words in timed:
        new = noisy_text(s["t"], swaps)
        if not new:
            continue
        s0, s1 = words[0][0], words[-1][1]
        step = (s1 - s0) / len(new)
        toks += [[round(s0 + i * step, 2), round(s0 + (i + 1) * step, 2), w] for i, w in enumerate(new)]
    cues, i = [], 0
    while i < len(toks):
        n = rng.randint(6, 10)
        cues.append(toks[i:i + n])
        i += n
    lines, cur = [], []
    for c in cues:
        if cur and c[-1][1] - cur[0][0] > LINE_MAX_S:
            lines.append(cur)
            cur = []
        cur += c
    if cur:
        lines.append(cur)
    out = []
    for c in lines:
        start, end = c[0][0], c[-1][1]
        step = (end - start) / len(c)
        out.append({"start": start, "end": end, "text": " ".join(w[2] for w in c), "timed": False,
                    "words": [[round(start + k * step, 2), round(start + (k + 1) * step, 2), w[2]] for k, w in enumerate(c)]})
    return out


def analysis_lines(timed, noisy, swaps):
    """Hindi files: the English translation per sentence, on the sentence's span (YouTube's auto-translation)."""
    out = []
    for s, words in timed:
        text = " ".join(noisy_text(s["en"], swaps)) if noisy else s["en"]
        if text:
            out.append({"start": words[0][0], "end": words[-1][1], "text": text})
    return out


def truth(d, timed):
    segs = {}
    for s, words in timed:
        k = s.get("seg")
        if k:
            a = segs.setdefault(k, {"key": k, **d["segments"][k], "start": words[0][0], "end": words[-1][1]})
            a["end"] = words[-1][1]
    return sorted(segs.values(), key=lambda x: x["start"])


def build(d, noisy=False):
    timed = timed_sentences(d)
    swaps = (d.get("noisy") or {}).get("swaps") or []
    lines = noisy_lines(timed, swaps, d["id"] + "noisy") if noisy else clean_lines(timed)
    payload = {"lang": d["lang"], "lines": lines, "title": d["title"], "creator": d["creator"],
               "description": d.get("description") or "", "paidPromotion": d.get("paid_promotion"),
               "kind": "asr" if noisy else "manual", "duration": round(timed[-1][1][-1][1] + 2.0, 2), "position": 0}
    if d["lang"] == "hi":
        payload.update(analysisLines=analysis_lines(timed, noisy, swaps), analysisLang="en")
    return payload, truth(d, timed)


if __name__ == "__main__":
    path = sys.argv[1]
    if "/held/" in path or path.startswith("held/"):
        sys.exit("build.py: refusing to print a HELD file")
    d = json.load(open(path, encoding="utf-8"))
    p, t = build(d, "--noisy" in sys.argv)
    print(json.dumps({"truth": t, "lines": p["lines"][:6], "n_lines": len(p["lines"]), "duration": p["duration"]},
                     ensure_ascii=False, indent=1))
