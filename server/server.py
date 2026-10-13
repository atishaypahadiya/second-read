#!/usr/bin/env python3
"""Sponsor Skipper server — localhost:8765, stdlib only.

Stage 4: classification with a local Qwen model via Ollama (/api/chat, JSON schema output,
temperature 0), in two passes:
  pass 1 (detect): 240s windows with 30s overlap -> candidate segments. First window first.
  pass 2 (refine): each sponsor candidate is re-asked with ~90s of context on both sides so
                   window edges never cut an ad, then the exact first/last ad word is picked
                   from word-level caption timing. end = last word end - 0.3s (round inward).

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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST, PORT = "127.0.0.1", 8765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "127.0.0.1", "localhost"}
# Optional: restrict to one extension. Unpacked extension IDs differ per machine, so this is opt-in:
#   SS_EXTENSION_ID=abcdefghijklmnopabcdefghijklmnop python3 server/server.py
EXTENSION_ID = os.environ.get("SS_EXTENSION_ID", "").strip()
OLLAMA = "http://localhost:11434"
MODEL = "qwen3:14b"
# Bump whenever the prompt, schema, threshold or post-processing changes: cached results from an
# older pipeline are then ignored and the video is re-analyzed.
PIPELINE_VERSION = 7
NUM_CTX = 8192
LLM_TIMEOUT_S = 600
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_LINES = 20000
SKIP_THRESHOLD = 0.85
SHOW_THRESHOLD = 0.7        # drop 'show' segments below this: low-confidence noise
WINDOW_S, OVERLAP_S = 240.0, 30.0
REFINE_CONTEXT_S = 90.0     # context on each side of a candidate in pass 2
BOUNDARY_LINES = 1          # lines on each side of a boundary line offered as numbered words
END_INSET_S = 0.3           # round the end inward
MERGE_GAP_S = 1.0           # merge same-action, same-brand segments this close
MIN_SKIP_S = 3.0            # a skip shorter than this after inward fallback is too uncertain -> show
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
# ">>" speaker markers and [Music]/[Applause]/(laughs)-style tags carry no meaning for the model.
NOISE_TAG_RE = re.compile(r"\[[^\]]{1,40}\]|\((?:music|applause|laugh\w*|cheer\w*|inaudible|crosstalk)[^)]*\)", re.I)
SPEAKER_RE = re.compile(r"(^|\s)>>+\s*")


def strip_noise(text):
    text = NOISE_TAG_RE.sub(" ", text)
    text = SPEAKER_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_words(raw, start, end):
    """[[s, e, text], ...] -> cleaned triples inside [start, end]; [] if unusable."""
    out = []
    if not isinstance(raw, list):
        return out
    for w in raw:
        try:
            ws, we, wt = float(w[0]), float(w[1]), strip_noise(str(w[2]))
        except (TypeError, ValueError, IndexError):
            continue
        if not wt or we < ws:
            continue
        out.append([round(max(start, ws), 2), round(min(end, max(ws, we)), 2), wt])
    return out


def clean_lines(raw):
    lines = []
    for item in raw or []:
        try:
            start, end, text = float(item["start"]), float(item["end"]), strip_noise(str(item["text"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not text or start < 0 or end < start:
            continue
        words = clean_words(item.get("words"), start, end)
        timed = bool(item.get("timed")) and bool(words)
        if not words:  # no words sent: every word gets the line's span (line-level fallback)
            words = [[round(start, 2), round(end, 2), w] for w in text.split()]
        lines.append({"start": round(start, 2), "end": round(end, 2), "text": text, "words": words, "timed": timed})
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
# Classifier: Qwen via Ollama
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You label sponsorship in YouTube video transcripts. You receive numbered transcript lines
("L12 [3:05] text") and return JSON segments with global line indices.

Categories:
- sponsor: a paid promotion of a THIRD-PARTY product or service (any company the creator does not own).
  A creator-specific discount code ("code FERN20"), a creator-branded link ("fern.deals/x", "link in the
  description") or the creator saying they use the product does NOT make it selfpromo: that is the standard
  sponsor pattern. If the brand is not something the creator owns, it is sponsor.
- selfpromo: ONLY things the creator owns: their own merch, Patreon/membership, their own course, app,
  newsletter, podcast, other videos or channel.
- interaction: asking to like, subscribe, comment, hit the bell.

Never label (return no segment for):
- disclosures or disclaimers about sponsors ("VPN providers sometimes sponsor the channel but aren't
  involved in this video", "this video is not sponsored"): a disclaimer is not a promotion.
- the product, company or app that is the SUBJECT of the video or of the current discussion (a video
  about Facebook's Onavo app discussing Onavo; a review; a news story about a company). Discussing a
  product is not promoting it, even at length and even if the creator likes it.

Style (for every segment):
- read: a scripted sponsor read. Signals: "this video is sponsored by", "thanks to X for sponsoring",
  promo codes, discounts, "link in the description", generic benefit claims about the product,
  a switch away from the video's topic and later back to it.
- integration: the product is genuinely used to do the video's actual task, it advances the content,
  and there is no hard topic break. Letting integrations play is the whole point; do not call them reads.

Rules:
- Mixed case: an integration that ends in a promo-code pitch becomes TWO segments; only the pitch is a read.
- Start a read at the transition line ("but first, a word from...", "before we continue..."). End it at the
  last promotional line (promo code, link, "back to the video"), not where the topic has already resumed.
- Transcripts may be auto-generated: no punctuation, misheard brand names, odd line breaks. Infer the brand
  as best you can; use "" if unknown.
- Transcripts may be in any language. Write reasons in English.
- confidence is 0..1: how sure you are this is a sponsor read with these boundaries. Be conservative;
  when unsure whether something is a read, lower the confidence instead of widening the segment.
- Only use line indices that appear in the input. Return an empty list if there is nothing to label.
- Never label ordinary content. Mentioning a company while discussing the topic is not a sponsor.
- NOT sponsorships: casual mentions of paying for things, buying things, shops, restaurants, brands, apps,
  places or people's businesses in daily life; a vlogger saying what they paid, where they ate, which phone
  they use, or that they'll "go back tomorrow". Example of ordinary content that must NOT be labeled:
  "paid 2000 rupees to Sahil bhai at his shop, great guy, will come back tomorrow for the rest".
  A sponsor read is scripted and promotional: the brand pays the creator and the creator pitches it.
- evidence: quote VERBATIM (any language, copied exactly from the lines) the single strongest sponsor cue:
  "sponsored by" / "thanks to X for sponsoring", a promo or discount code, "link in the description" / a URL
  call-to-action, or a scripted product pitch with an offer. Quote the transcript words only: no line
  numbers, no "L12", no timestamps, no quotation marks around it. If no such cue exists in the lines, the
  segment is not a sponsor read: use style integration or category selfpromo, or do not label it."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "category": {"type": "string", "enum": ["sponsor", "selfpromo", "interaction"]},
                    "style": {"type": "string", "enum": ["read", "integration"]},
                    "brand": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["start_line", "end_line", "category", "style", "brand", "confidence", "evidence", "reason"],
            },
        }
    },
    "required": ["segments"],
}


def format_window(lines, first, last, lang, title=""):
    out = [f"Transcript language: {lang}"]
    if title:
        out.append(f"Video title: {title}")
    for i in range(first, last + 1):
        out.append(f"L{i} [{fmt_time(lines[i]['start'])}] {lines[i]['text']}")
    return "\n".join(out)


def ollama_chat(user_content, schema=None):
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
        "stream": False,
        "format": schema or OUTPUT_SCHEMA,
        "think": False,
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = (data.get("message") or {}).get("content", "")
    return json.loads(content), data


def ollama_chat_retry(user_content, schema=None, label=""):
    try:
        return ollama_chat(user_content, schema)
    except json.JSONDecodeError:
        log(f"  {label}: model returned non-JSON, retrying once")
        return ollama_chat(user_content, schema)


def segments_of(result):
    segs = result.get("segments") if isinstance(result, dict) else None
    return [x for x in (segs or []) if isinstance(x, dict)]


def classify_window(lines, first, last, lang, title=""):
    """Pass 1: raw model segments (global line indices) for lines[first..last]."""
    t0 = time.time()
    result, meta = ollama_chat_retry(format_window(lines, first, last, lang, title), label=f"window L{first}-L{last}")
    segs = segments_of(result)
    log(f"  detect  L{first}-L{last} ({fmt_time(lines[first]['start'])}-{fmt_time(lines[last]['end'])}): "
        f"{len(segs)} raw segment(s) in {time.time() - t0:.1f}s, "
        f"{meta.get('prompt_eval_count', '?')}+{meta.get('eval_count', '?')} tokens")
    return segs


REFINE_INSTRUCTIONS = """

A candidate {category}/{style} segment for brand {brand!r} was detected at L{a}-L{b} in a narrower
window. Re-examine it with this fuller context. Return the FULL extent of that one promotion as a
single segment (two segments only if an integration ends in a separate promo-code pitch). The start
is the first line that transitions into the promotion; the end is the last promotional line. If on
reflection this is not a promotion at all, return no segments. Do not label anything unrelated to
this candidate."""


def refine_candidate_lines(lines, cand, lang, title=""):
    """Pass 2a: re-ask with ~REFINE_CONTEXT_S of context either side. Returns raw segs + window bounds."""
    lo, hi = cand["start"] - REFINE_CONTEXT_S, cand["end"] + REFINE_CONTEXT_S
    idx = [i for i, l in enumerate(lines) if lo <= l["start"] <= hi]
    first, last = min(idx[0], cand["start_line"]), max(idx[-1], cand["end_line"])
    prompt = format_window(lines, first, last, lang, title) + REFINE_INSTRUCTIONS.format(
        category=cand["category"], style=cand["style"], brand=cand["brand"],
        a=cand["start_line"], b=cand["end_line"])
    t0 = time.time()
    result, meta = ollama_chat_retry(prompt, label=f"refine L{cand['start_line']}-L{cand['end_line']}")
    segs = segments_of(result)
    log(f"  refine  L{cand['start_line']}-L{cand['end_line']} in L{first}-L{last}: "
        f"{len(segs)} segment(s) in {time.time() - t0:.1f}s, "
        f"{meta.get('prompt_eval_count', '?')}+{meta.get('eval_count', '?')} tokens")
    return segs, first, last


WORD_SCHEMA = {
    "type": "object",
    "properties": {
        "first": {"type": "string"}, "first_word": {"type": "string"},
        "last": {"type": "string"}, "last_word": {"type": "string"},
    },
    "required": ["first", "first_word", "last", "last_word"],
}

WORD_PROMPT = """You are fixing the exact boundaries of a {category} {style} for brand {brand!r} in a YouTube transcript.

The promotion begins somewhere in the START CONTEXT and ends somewhere in the END CONTEXT below.
Words are labeled S0, S1, ... in the START CONTEXT and E0, E1, ... in the END CONTEXT. Answer with the
LABEL (copied exactly, e.g. "S9" or "E28") and the WORD at that label (copied exactly):
- first / first_word: the label and text of the FIRST word that belongs to the promotion, e.g. the first word
  of "but first, this video is sponsored by...". Words before it are normal content.
- last / last_word: the label and text of the LAST word that is still part of the promotion, e.g. the last word
  of the promo code / link / "thanks to X for sponsoring" sentence. The words after it, where the creator
  returns to the video's own topic, are NOT part of the promotion even if they are in the same line.
Use only labels that appear below. Do not use line numbers or any other numbering.
Skipping real content is far worse than leaving a little of the ad, so when unsure choose a LATER
first_word and an EARLIER last_word. If the context is auto-generated it may lack punctuation; judge by meaning.

The promotion text (middle may be cut):
{ad_text}

START CONTEXT:
{start_words}

END CONTEXT:
{end_words}"""


def boundary_words(lines, center, n):
    """Flattened words of lines[center-BOUNDARY_LINES .. center+BOUNDARY_LINES] within [0, n)."""
    out = []
    for i in range(max(0, center - BOUNDARY_LINES), min(n - 1, center + BOUNDARY_LINES) + 1):
        out.extend(lines[i]["words"])
    return out


def describe_boundary(label, words, idx, ok, lines, line_idx):
    """One debug line per boundary: timing source, context size, chosen word and its own timestamps."""
    timed = lines[line_idx]["timed"]
    src = "word timing" if timed else "estimated (track has no word timing)"
    if ok:
        w = words[idx]
        pick = f"{label}{idx} {w[2]!r} [{w[0]:.2f}-{w[1]:.2f}]"
    else:
        pick = f"invalid index {idx!r}"
    return f"{src}, {len(words)} words in context, pick={pick}"


def resolve_pick(label_value, word_value, words, prefix):
    """Turn the model's (label, word) answer into a context index.
    Returns (index or None, rule). Rules: 'label', 'label-int', 'text-unique', or a failure reason."""
    idx = None
    if isinstance(label_value, int):
        idx, rule = label_value, "label-int"
    elif isinstance(label_value, str):
        m = re.fullmatch(r"\s*([SE])?\s*(\d+)\s*", label_value, re.I)
        if m:
            idx, rule = int(m.group(2)), "label"
    if idx is not None and 0 <= idx < len(words):
        if isinstance(word_value, str) and word_value.strip() and normalize(word_value) != normalize(words[idx][2]):
            # label and word disagree; trust the word if it is unique in the context
            hits = [i for i, w in enumerate(words) if normalize(w[2]) == normalize(word_value)]
            if len(hits) == 1:
                return hits[0], f"text-unique (label {prefix}{idx} said {words[idx][2]!r})"
        return idx, rule
    if isinstance(word_value, str) and word_value.strip():
        hits = [i for i, w in enumerate(words) if normalize(w[2]) == normalize(word_value)]
        if len(hits) == 1:
            return hits[0], f"text-unique (label {label_value!r} invalid)"
        return None, f"label {label_value!r} invalid, word {word_value!r} matches {len(hits)} words"
    return None, f"label {label_value!r} invalid, no word text"


def refine_words(lines, s_line, e_line, seg):
    """Pass 2b: word-level boundaries. Returns (start, end, sure).
    Invalid picks are retried once, then fall back INWARD only: start -> end of the first ad line,
    end -> start of the last ad line. Never outward to a line end."""
    n = len(lines)
    s_words, e_words = boundary_words(lines, s_line, n), boundary_words(lines, e_line, n)
    ad_lines = [lines[i]["text"] for i in range(s_line, e_line + 1)]
    ad_text = " ".join(ad_lines) if len(ad_lines) <= 4 else " ".join(ad_lines[:2]) + " [...] " + " ".join(ad_lines[-2:])
    start_ctx = " ".join(f"S{i}:{w[2]}" for i, w in enumerate(s_words))
    end_ctx = " ".join(f"E{i}:{w[2]}" for i, w in enumerate(e_words))
    prompt = WORD_PROMPT.format(category=seg["category"], style=seg["style"], brand=seg["brand"],
                                ad_text=ad_text, start_words=start_ctx, end_words=end_ctx)
    inward_start, inward_end = lines[s_line]["end"], lines[e_line]["start"]

    fw = lw = None
    rule_s = rule_e = "no answer"
    ok_s = ok_e = False
    t0 = time.time()
    for attempt in (1, 2):
        try:
            result, _meta = ollama_chat_retry(prompt, WORD_SCHEMA, label=f"words L{s_line}/L{e_line}")
        except (OSError, ValueError) as e:
            log(f"  words   L{s_line}/L{e_line}: model call failed ({e!r})")
            break
        fw, rule_s = resolve_pick(result.get("first"), result.get("first_word"), s_words, "S")
        lw, rule_e = resolve_pick(result.get("last"), result.get("last_word"), e_words, "E")
        ok_s, ok_e = fw is not None, lw is not None
        log(f"  words   L{s_line}/L{e_line}: answer first={result.get('first')!r}/{result.get('first_word')!r} "
            f"last={result.get('last')!r}/{result.get('last_word')!r} -> start rule: {rule_s}; end rule: {rule_e}")
        if ok_s and ok_e:
            break
        log(f"  words   L{s_line}/L{e_line}: unresolved pick (attempt {attempt})")

    start = s_words[fw][0] if ok_s else inward_start
    end = (e_words[lw][1] - END_INSET_S) if ok_e else inward_end
    log(f"  words   L{s_line}/L{e_line} in {time.time() - t0:.1f}s")
    log(f"    start: {describe_boundary('S', s_words, fw, ok_s, lines, s_line)} -> {start:.2f}"
        f"{'' if ok_s else ' (INWARD FALLBACK: end of first ad line)'}")
    log(f"    end:   {describe_boundary('E', e_words, lw, ok_e, lines, e_line)} -> {end:.2f}"
        f"{' (inset %.1fs)' % END_INSET_S if ok_e else ' (INWARD FALLBACK: start of last ad line)'}")
    log(f"    S ctx: {start_ctx}")
    log(f"    E ctx: {end_ctx}")
    sure = ok_s and ok_e
    if end <= start:
        log("    end <= start after word picks; using inward line fallback")
        start, end, sure = inward_start, inward_end, False
    return round(start, 2), round(max(start, end), 2), sure


def ollama_available():
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=2) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        return any(m.get("name") == MODEL for m in tags.get("models", []))
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------
def action_for(seg):
    if seg["category"] == "sponsor" and seg["style"] == "read" and seg["confidence"] >= SKIP_THRESHOLD:
        return "skip"
    if seg["category"] == "sponsor" and seg["style"] == "integration":
        return "keep"
    return "show"


def to_segment(raw, lines, first, last):
    """Model segment with line indices -> segment with line-level seconds. None if indices are
    outside the window it was given (hallucinated)."""
    s, e = raw.get("start_line"), raw.get("end_line")
    if not isinstance(s, int) or not isinstance(e, int) or s > e or s < first or e > last:
        return None
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    out = {
        "start": lines[s]["start"], "end": lines[e]["end"],
        "start_line": s, "end_line": e,
        "category": raw.get("category") if raw.get("category") in ("sponsor", "selfpromo", "interaction") else "sponsor",
        "style": raw.get("style") if raw.get("style") in ("read", "integration") else "read",
        "brand": str(raw.get("brand") or "").strip(),
        "confidence": conf,
        "evidence": str(raw.get("evidence") or "").strip(),
        "reason": str(raw.get("reason") or "").strip(),
    }
    out["action"] = action_for(out)
    return out


LINE_LABEL_RE = re.compile(r"\bL\d+\s*(\[\d+:\d{2}(?::\d{2})?\])?\s*|\[\d+:\d{2}(?::\d{2})?\]")
EVIDENCE_MIN_RUN = 6    # a verbatim run of this many words from the evidence is enough
EVIDENCE_MIN_WORDS = 3  # shorter quotes ("the sponsor") are not evidence


def normalize(text):
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def normalize_evidence(text):
    """Drop line labels/timestamps the model may have copied, quotes and ellipses, then normalize."""
    text = LINE_LABEL_RE.sub(" ", text)
    text = re.sub(r"[\"“”„«»'‘’`]|\.{3}|…", " ", text)
    return normalize(text)


def find_run(ev, hay, n):
    """First n-word run of ev that appears in hay, as (ev_offset, hay_offset) or None."""
    for i in range(0, len(ev) - n + 1):
        window = ev[i:i + n]
        for j in range(0, len(hay) - n + 1):
            if hay[j:j + n] == window:
                return i, j
    return None


def evidence_found(seg, lines):
    """Evidence must match the segment's lines (+-1 line) verbatim: the whole quote, or any run of
    EVIDENCE_MIN_RUN words from it. Returns a description of the match, or None."""
    ev = normalize_evidence(seg.get("evidence", ""))
    if len(ev) < EVIDENCE_MIN_WORDS:
        return None
    lo, hi = max(0, seg["start_line"] - 1), min(len(lines) - 1, seg["end_line"] + 1)
    hay_words = []
    for i in range(lo, hi + 1):
        for w in normalize(lines[i]["text"]):
            hay_words.append((w, i))
    hay = [w for w, _ in hay_words]
    hit = find_run(ev, hay, len(ev))
    kind = "full"
    if hit is None and len(ev) >= EVIDENCE_MIN_RUN:
        hit = find_run(ev, hay, EVIDENCE_MIN_RUN)
        kind = f"{EVIDENCE_MIN_RUN}-word run"
    if hit is None:
        return None
    ei, hj = hit
    n = len(ev) if kind == "full" else EVIDENCE_MIN_RUN
    line_a, line_b = hay_words[hj][1], hay_words[hj + n - 1][1]
    matched = " ".join(ev[ei:ei + n])
    span = f"L{line_a}" if line_a == line_b else f"L{line_a}-L{line_b}"
    return f"{kind} match at {span}: {matched!r}"


def require_evidence(seg, lines):
    """A skip needs verbatim sponsor evidence from the transcript; otherwise downgrade to show."""
    if seg["action"] != "skip":
        return seg
    norm = " ".join(normalize_evidence(seg.get("evidence", "")))
    match = evidence_found(seg, lines)
    log(f"  evidence: raw={seg.get('evidence', '')!r}")
    log(f"            normalized={norm!r}")
    if match:
        log(f"            {match}")
        return seg
    why = "no evidence given" if not seg.get("evidence") else "evidence not found verbatim in segment lines"
    log(f"  refine: downgraded skip -> show ({why})")
    seg["action"] = "show"
    seg["reason"] = f"[downgraded: {why}] " + seg["reason"]
    return seg


def overlaps(a, b, gap=0.0):
    return a["start"] <= b["end"] + gap and b["start"] <= a["end"] + gap


def same_brand(a, b):
    return a["brand"].strip().lower() == b["brand"].strip().lower()


def merge_candidates(cands):
    """Pass-1 candidates from overlapping windows that cover the same span -> one candidate."""
    cands = sorted(cands, key=lambda c: c["start"])
    out = []
    for c in cands:
        if out and out[-1]["category"] == c["category"] and overlaps(out[-1], c, MERGE_GAP_S):
            m = out[-1]
            best = c if c["confidence"] > m["confidence"] else m
            m.update({"start": min(m["start"], c["start"]), "end": max(m["end"], c["end"]),
                      "start_line": min(m["start_line"], c["start_line"]),
                      "end_line": max(m["end_line"], c["end_line"]),
                      "style": best["style"], "brand": best["brand"] or m["brand"],
                      "confidence": best["confidence"], "reason": best["reason"]})
            m["action"] = action_for(m)
        else:
            out.append(dict(c))
    return out


def merge_segments(segs):
    """Final merge: overlapping or touching (gap <= MERGE_GAP_S) segments with same action and brand."""
    segs = sorted(segs, key=lambda x: x["start"])
    out = []
    for x in segs:
        if out and out[-1]["action"] == x["action"] and same_brand(out[-1], x) and overlaps(out[-1], x, MERGE_GAP_S):
            m = out[-1]
            m["end"] = max(m["end"], x["end"])
            m["line_end"] = max(m.get("line_end", m["end"]), x.get("line_end", x["end"]))
            m["confidence"] = max(m["confidence"], x["confidence"])
        else:
            out.append(dict(x))
    return out


def public_segment(seg):
    keys = ("start", "end", "category", "style", "brand", "confidence", "evidence", "reason", "action", "line_start", "line_end")
    return {k: seg[k] for k in keys if k in seg}


def refine_candidate(lines, cand, lang, title=""):
    """Pass 2 for one sponsor candidate -> list of refined segments (possibly empty)."""
    raw, first, last = refine_candidate_lines(lines, cand, lang, title)
    refined = []
    for r in raw:
        seg = to_segment(r, lines, first, last)
        if not seg:
            log("  refine: dropped segment with out-of-window line indices")
            continue
        if not overlaps(seg, cand, REFINE_CONTEXT_S / 2):
            log(f"  refine: dropped unrelated segment at {fmt_time(seg['start'])}-{fmt_time(seg['end'])}")
            continue
        seg["line_start"], seg["line_end"] = seg["start"], seg["end"]
        require_evidence(seg, lines)
        if seg["action"] == "skip":
            start, end, sure = refine_words(lines, seg["start_line"], seg["end_line"], seg)
            if not sure and end - start < MIN_SKIP_S:
                log(f"  refine: boundaries uncertain and only {end - start:.1f}s left after inward fallback -> show")
                seg["action"] = "show"
                seg["reason"] = "[downgraded: uncertain boundaries] " + seg["reason"]
            else:
                seg["start"], seg["end"] = start, end
        refined.append(seg)
    if not refined:
        log(f"  refine: candidate {fmt_time(cand['start'])}-{fmt_time(cand['end'])} {cand['brand']!r} withdrawn by model")
    return refined


DISCLAIMER_RE = re.compile(
    r"disclaim|disclosure|not sponsored|isn'?t sponsored|aren'?t involved|not involved|no sponsor|"
    r"sometimes sponsor|transparen|conflict of interest", re.I)


def brand_in_title(brand, title):
    b = normalize(brand)
    if not b or not title:
        return False
    t = " ".join(normalize(title))
    phrase = " ".join(b)
    return len(phrase) >= 3 and f" {phrase} " in f" {t} "


def post_filter(segments, title):
    """Final safety net for keep/show markers (skips are left alone and only logged):
    - drop keep/show whose evidence/reason reads like a sponsor disclaimer;
    - drop keep/show whose brand is the video's subject (appears in the title);
    - drop show below SHOW_THRESHOLD."""
    out = []
    for s in segments:
        rng = f"{fmt_time(s['start'])}-{fmt_time(s['end'])}"
        if s["action"] in ("keep", "show"):
            text = f"{s.get('evidence', '')} {s.get('reason', '')}"
            m = DISCLAIMER_RE.search(text)
            if m:
                log(f"  filter: dropped {s['action']} {rng} {s['brand']!r}: reads like a disclaimer ({m.group(0)!r})")
                continue
            if brand_in_title(s["brand"], title):
                log(f"  filter: dropped {s['action']} {rng} {s['brand']!r}: brand is the video's subject (in title)")
                continue
            if s["action"] == "show" and s["confidence"] < SHOW_THRESHOLD:
                log(f"  filter: dropped show {rng} {s['brand']!r}: conf {s['confidence']:.2f} < {SHOW_THRESHOLD}")
                continue
        elif brand_in_title(s["brand"], title):
            log(f"  filter: NOTE skip {rng} {s['brand']!r} brand appears in the title; kept (has verbatim evidence)")
        out.append(s)
    return out


def log_segments(video_id, segments):
    for s in segments:
        line_part = ""
        if "line_start" in s and (s["line_start"] != s["start"] or s["line_end"] != s["end"]):
            line_part = f" (lines {fmt_time(s['line_start'])}-{fmt_time(s['line_end'])})"
        log(f"[{video_id}] {s['action'].upper():4} {fmt_time(s['start'])}-{fmt_time(s['end'])}{line_part} "
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
    out = {"status": job["status"], "segments": job["segments"]}
    if job.get("cached"):
        out["cached"] = True
    if job.get("error"):
        out["error"] = job["error"]
    return out


def cache_valid(cached, thash):
    return (cached and cached.get("hash") == thash and cached.get("version") == PIPELINE_VERSION
            and cached.get("model") == MODEL)


def run_job(video_id):
    with jobs_lock:
        job = jobs[video_id]
        lines, lang, thash, title = job["lines"], job["lang"], job["hash"], job.get("title", "")
    try:
        wins = windows(lines)
        final = []

        def finalize():
            return [public_segment(x) for x in post_filter(merge_segments(final), title)]

        def publish():
            with jobs_lock:
                if jobs.get(video_id) is job:
                    job["segments"] = finalize()

        def phase(win_slice, status):
            with jobs_lock:
                job["status"] = status
            cands = []
            for first, last in win_slice:
                with llm_lock:
                    raw = classify_window(lines, first, last, lang, title)
                for r in raw:
                    seg = to_segment(r, lines, first, last)
                    if seg:
                        cands.append(seg)
                    else:
                        log("  detect: dropped segment with out-of-window line indices")
            for cand in merge_candidates(cands):
                if any(overlaps(cand, f) for f in final):
                    continue  # already covered by an earlier phase
                if cand["category"] != "sponsor":
                    final.append(cand)  # selfpromo/interaction are 'show'; boundaries don't matter
                    continue
                with llm_lock:
                    final.extend(refine_candidate(lines, cand, lang, title))
            publish()

        phase(wins[:1], "classifying")
        if len(wins) > 1:
            phase(wins[1:], "classifying_rest")

        segments = finalize()
        with jobs_lock:
            superseded = jobs.get(video_id) is not job
        if superseded:
            log(f"[{video_id}] job superseded by a newer transcript; discarding result")
            return
        log_segments(video_id, segments)
        save_cache(video_id, {"videoId": video_id, "version": PIPELINE_VERSION, "hash": thash, "lang": lang,
                              "title": title, "model": MODEL, "segments": segments, "transcript": lines})
        with jobs_lock:
            job["segments"] = segments
            job["status"] = "done"
        log(f"[{video_id}] done: {len(segments)} segment(s), {len(wins)} window(s), cached")
    except urllib.error.URLError as e:
        log(f"[{video_id}] error: cannot reach Ollama at {OLLAMA}: {e.reason}")
        with jobs_lock:
            job["status"] = "error"
            job["error"] = "ollama offline"
    except Exception as e:  # noqa: BLE001
        log(f"[{video_id}] error: {e!r}")  # full detail stays in the terminal only
        with jobs_lock:
            job["status"] = "error"
            job["error"] = "model error" if isinstance(e, (json.JSONDecodeError, KeyError, TypeError, ValueError)) else "internal error"


def start_or_get_job(video_id, lang, lines, title=""):
    thash = transcript_hash(lines)
    with jobs_lock:
        job = jobs.get(video_id)
        if job and job["hash"] == thash and job["status"] != "error":
            if job["status"] == "done":
                job["cached"] = True  # reused, not computed for this request
            return job
        cached = load_cache(video_id)
        if cache_valid(cached, thash):
            job = {"status": "done", "segments": cached.get("segments", []),
                   "hash": thash, "lang": lang, "lines": lines, "cached": True}
            jobs[video_id] = job
            log(f"[{video_id}] cache hit ({len(job['segments'])} segment(s))")
            return job
        why = ""
        if job:
            why = " (transcript changed; replacing running job)" if job["status"] != "error" else " (previous attempt errored)"
        elif cached:
            why = (" (cache is from pipeline v%s/%s; now v%s/%s)" % (cached.get("version"), cached.get("model"), PIPELINE_VERSION, MODEL)
                   if cached.get("hash") == thash else " (cached transcript hash differs)")
        job = {"status": "queued", "segments": [], "hash": thash, "lang": lang, "lines": lines, "title": title}
        jobs[video_id] = job
    log(f"[{video_id}] new job: {len(lines)} lines, lang={lang}, hash={thash}, title={title!r}{why}")
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
        self.end_headers()
        self.wfile.write(body)

    def allowed_origin(self):
        """Only the extension (host_permissions, no CORS needed) or non-browser clients like curl.
        - Host must be localhost/127.0.0.1: defeats DNS rebinding (evil.example -> 127.0.0.1).
        - Web pages send an http(s) Origin; refuse them so sites can't read or poison the cache.
        - With SS_EXTENSION_ID set, only that extension's origin is accepted."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in ALLOWED_HOSTS:
            self.send_json(403, {"error": "forbidden host"})
            return False
        origin = self.headers.get("Origin")
        if origin:
            if not origin.startswith("chrome-extension://"):
                self.send_json(403, {"error": "forbidden origin"})
                return False
            if EXTENSION_ID and origin != f"chrome-extension://{EXTENSION_ID}":
                self.send_json(403, {"error": "forbidden extension"})
                return False
        return True

    def do_GET(self):
        if not self.allowed_origin():
            return
        u = urlparse(self.path)
        if u.path == "/health":
            return self.send_json(200, {"ok": True, "model": MODEL, "ollama": ollama_available()})
        if u.path == "/analyze":
            vid = (parse_qs(u.query).get("v") or [""])[0]
            if not VIDEO_ID_RE.match(vid):
                return self.send_json(400, {"error": "bad video id"})
            with jobs_lock:
                job = jobs.get(vid)
            if not job:
                cached = load_cache(vid)
                if cached and cached.get("version") == PIPELINE_VERSION and cached.get("model") == MODEL:
                    return self.send_json(200, {"status": "done", "segments": cached.get("segments", []), "cached": True})
                return self.send_json(404, {"error": "unknown"})
            return self.send_json(200, public_job(job))
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed_origin():
            return
        u = urlparse(self.path)
        if u.path != "/analyze":
            return self.send_json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY_BYTES:
                return self.send_json(413, {"error": "body too large"})
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad json"})
        vid = str(data.get("videoId", ""))
        if not VIDEO_ID_RE.match(vid):
            return self.send_json(400, {"error": "bad video id"})
        lines = clean_lines(data.get("lines"))
        if not lines:
            return self.send_json(400, {"error": "no lines"})
        if len(lines) > MAX_LINES:
            return self.send_json(413, {"error": "too many lines"})
        lang = str(data.get("lang") or "und")[:12]
        title = re.sub(r"\s+", " ", str(data.get("title") or "")).strip()[:300]
        job = start_or_get_job(vid, lang, lines, title)
        with jobs_lock:
            return self.send_json(200, public_job(job))


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"Sponsor Skipper server on http://{HOST}:{PORT}  model={MODEL}  cache={CACHE_DIR}")
    log(f"classifier: {MODEL} via Ollama ({'reachable, model present' if ollama_available() else 'NOT available'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
