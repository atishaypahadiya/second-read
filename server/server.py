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

Status: queued -> classifying -> done | error  (+ paused:true while the video isn't being polled)
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

# Threat model: the transcript, title and words come from whatever the YouTube page handed the
# extension, so they are untrusted. They are only ever (a) hashed into the cache key, so a bogus
# transcript cannot overwrite the result for the real one, and (b) placed in the model prompt, where
# the output is constrained by a JSON schema, line indices are validated against the window, and a
# skip needs a verbatim quote from the transcript itself. Prompt text can at most mislabel its own video.
HOST, PORT = "127.0.0.1", 8765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "127.0.0.1", "localhost"}
# Optional: restrict to one extension. Unpacked extension IDs differ per machine, so this is opt-in:
#   SS_EXTENSION_ID=abcdefghijklmnopabcdefghijklmnop python3 server/server.py
EXTENSION_ID = os.environ.get("SS_EXTENSION_ID", "").strip()
OLLAMA = "http://localhost:11434"
MODEL = "qwen3:14b"                                   # refine + word pass (and detect, by default)
# Optional smaller model for pass-1 detection, e.g. SS_DETECT_MODEL=qwen3:8b. Both models then stay loaded
# (keep_alive -1): 14b + 8b = ~18 GB of GPU memory, which thrashed and timed out on a 24 GB Mac. Only use it
# with >= 32 GB. Default: one model for everything.
DETECT_MODEL_PREF = os.environ.get("SS_DETECT_MODEL", "").strip() or MODEL
DETECT_MODEL = MODEL                                  # resolved at startup
KEEP_ALIVE = -1                                       # keep the model loaded between calls
# Staged cache versions. Each model call's raw output is cached per video and keyed by its inputs, so a
# change only re-runs what it affects:
#   DETECT_VERSION  pass 1 (SYSTEM_PROMPT rubric, OUTPUT_SCHEMA, window size/overlap)
#   REFINE_VERSION  pass 2 (SYSTEM_PROMPT, REFINE_INSTRUCTIONS, REFINE_CONTEXT_S, WORD_PROMPT, WORD_SCHEMA)
#   POST_VERSION    everything after the model: evidence gate, thresholds, snapping, merge, filters
# A POST-only change re-processes a cached video from stored model outputs with zero Ollama calls.
# Rubric changes in SYSTEM_PROMPT affect both passes: bump DETECT and REFINE.
DETECT_VERSION = 2
REFINE_VERSION = 1
POST_VERSION = 1
NUM_CTX = 8192
LLM_TIMEOUT_S = 600
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_LINES = 20000
MAX_ACTIVE_JOBS = 4          # unfinished jobs kept in memory; beyond this the least-recently-polled is evicted
MAX_JOBS_IN_MEMORY = 200     # finished jobs kept in memory (results stay on disk)
PAUSE_AFTER_S = 10.0         # a job not polled for this long is paused between Ollama calls
SKIP_THRESHOLD = 0.85
SHOW_THRESHOLD = 0.7        # drop 'show' segments below this: low-confidence noise
WINDOW_S, OVERLAP_S = 240.0, 30.0
REFINE_CONTEXT_S = 90.0     # context on each side of a candidate in pass 2
BOUNDARY_LINES = 1          # lines on each side of a boundary line offered as numbered words
END_INSET_S = 0.3           # round the end inward
MERGE_GAP_S = 1.0           # merge same-action, same-brand segments this close
MIN_SKIP_S = 3.0            # a skip shorter than this after inward fallback is too uncertain -> show
SNAP_GAP_S = 0.25           # an inter-word gap this long counts as a pause
SNAP_FORWARD_MAX_S = 5.0    # never move a start later than this to find a clean cut
SNAP_END_WINDOW_S = 1.0     # look this far after the last ad word for a pause to end on
TOPIC_MENTIONS = 3          # brand mentioned in this many lines outside the segment -> it's the subject
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
RESULTS_MD = os.path.join(CACHE_DIR, "results.local.md")  # gitignored: it records which videos were watched
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


# Sponsor cues. A window with none of these (nor any description hint) is never sent to the model:
# the evidence gate requires a verbatim cue, so such a window could never produce a skip anyway.
CUE_PATTERNS = [
    r"sponsor", r"brought to you", r"thanks? to \w+", r"special thanks to", r"use (?:the |my |promo |discount )?code",
    r"promo ?code", r"discount", r"\d+ ?(?:%|percent) off", r"link(?:'s| is)? in the description", r"link below",
    r"\.com/", r"\.com\b", r"check (?:them |it )?out", r"sign ?up", r"free trial", r"partner(?:ed|ing)? with",
    r"in partnership with", r"supported by", r"made possible by", r"today'?s sponsor", r"go to \w+", r"head (?:over )?to",
    r"\bvisit\b", r"today'?s video", r"this video is", r"first \d+ (?:people|months)",
]
CUE_RE = re.compile("|".join(CUE_PATTERNS), re.I)


def window_cues(text, hint_terms):
    """Matched cue strings in a window's text (built-in cues + description hints)."""
    found = [m.group(0) for m in CUE_RE.finditer(text)]
    low = text.lower()
    found += [h for h in hint_terms if h.lower() in low]
    return found


CODE_RE = re.compile(r"\bcode[:\s]+([A-Z0-9]{3,20})\b")
DOMAIN_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|net|org|io|co|deals|gg|ai|app|tv|me|ly|fm|shop|store|xyz|dev|to)(?:/[\w.-]+)?)\b", re.I)
AD_LINE_RE = re.compile(r"\(ad\)|\bsponsor|\bpartner|#ad\b|\bpaid promotion", re.I)
CHAPTER_RE = re.compile(r"^\s*(?:\d+:)?\d{1,2}:\d{2}\s*[-–—:]?\s*(.+)$")
STOPWORDS = {"the", "this", "video", "sponsored", "sponsor", "by", "thanks", "to", "for", "and", "with", "check", "out",
             "get", "use", "code", "off", "free", "trial", "link", "description", "below", "today", "ad", "our", "my",
             "try", "learn", "click", "visit", "download", "start", "join", "sign", "head", "watch", "subscribe", "thank"}


def extract_hints(description):
    """Brands, promo codes, domains and chapter titles from the video description."""
    hints = {"brands": [], "codes": [], "domains": [], "chapters": []}
    if not description:
        return hints
    for line in description.splitlines():
        line = line.strip()
        if not line:
            continue
        m = CHAPTER_RE.match(line)
        if m:
            hints["chapters"].append(m.group(1).strip()[:60])
        for c in CODE_RE.findall(line):
            hints["codes"].append(c)
        for d in DOMAIN_RE.findall(line):
            if not d.lower().startswith(("youtube.", "youtu.be", "instagram.", "twitter.", "x.com", "tiktok.", "facebook.",
                                         "patreon.", "discord.", "twitch.", "reddit.", "spotify.", "apple.", "bit.ly", "linktr.ee")):
                hints["domains"].append(d)
        if AD_LINE_RE.search(line):
            caps = re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", line)
            first_tok = line.split()[0].strip(".,:;!?()[]\"'") if line.split() else ""
            for w in caps:
                if w.lower() in STOPWORDS or w in hints["codes"]:
                    continue
                if w == first_tok and len(line.split()) > 3:
                    continue  # sentence-initial capital ("Learn to code with...") is usually not a brand
                hints["brands"].append(w)
    for k in hints:
        seen = []
        for x in hints[k]:
            if x not in seen:
                seen.append(x)
        hints[k] = seen[:12]
    return hints


def hint_terms(hints):
    terms = list(hints["brands"]) + list(hints["codes"])
    for d in hints["domains"]:
        terms.append(d)
        host = d.split("/")[0]
        label = host.split(".")[-2] if host.count(".") >= 1 else host
        if len(label) >= 4 and label.lower() not in STOPWORDS:
            terms.append(label)
        if "/" in d:
            path = d.split("/", 1)[1].split("/")[0]
            if len(path) >= 4:
                terms.append(path)
    out = []
    for t in terms:
        if t and t not in out:
            out.append(t)
    return out


def hints_header(hints):
    parts = []
    if hints["brands"]:
        parts.append("brands: " + ", ".join(hints["brands"]))
    if hints["codes"]:
        parts.append("codes: " + ", ".join(hints["codes"]))
    if hints["domains"]:
        parts.append("links: " + ", ".join(hints["domains"]))
    if hints["chapters"]:
        parts.append("chapters: " + " | ".join(hints["chapters"][:20]))
    return "Sponsor hints from the video description (brand names may be misheard in the transcript): " + "; ".join(parts) if parts else ""


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


def format_window(lines, first, last, lang, title="", header=""):
    out = [f"Transcript language: {lang}"]
    if title:
        out.append(f"Video title: {title}")
    if header:
        out.append(header)
    for i in range(first, last + 1):
        out.append(f"L{i} [{fmt_time(lines[i]['start'])}] {lines[i]['text']}")
    return "\n".join(out)


def ollama_chat(user_content, schema=None, model=None):
    payload = {
        "model": model or MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
        "stream": False,
        "format": schema or OUTPUT_SCHEMA,
        "think": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = (data.get("message") or {}).get("content", "")
    return json.loads(content), data


def ollama_chat_retry(user_content, schema=None, label="", model=None):
    try:
        return ollama_chat(user_content, schema, model)
    except json.JSONDecodeError:
        log(f"  {label}: model returned non-JSON, retrying once")
        return ollama_chat(user_content, schema, model)


def warm_models():
    """Load the models into memory at startup so the first real call doesn't pay for it."""
    for m in sorted({DETECT_MODEL, MODEL}):
        t0 = time.time()
        try:
            req = urllib.request.Request(f"{OLLAMA}/api/generate", data=json.dumps(
                {"model": m, "prompt": "hi", "stream": False, "keep_alive": KEEP_ALIVE,
                 "options": {"num_predict": 1}}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300):
                pass
            log(f"warmed {m} in {time.time() - t0:.1f}s (keep_alive={KEEP_ALIVE})")
        except (OSError, ValueError) as e:
            log(f"warm-up of {m} failed: {e}")


def segments_of(result):
    segs = result.get("segments") if isinstance(result, dict) else None
    return [x for x in (segs or []) if isinstance(x, dict)]


def classify_window(lines, first, last, lang, title, calls, header=""):
    """Pass 1: raw model segments (global line indices) for lines[first..last]."""
    t0 = time.time()
    rng = f"{fmt_time(lines[first]['start'])}-{fmt_time(lines[last]['end'])}"

    def compute():
        with llm_lock:
            result, meta = ollama_chat_retry(format_window(lines, first, last, lang, title, header),
                                             label=f"window L{first}-L{last}", model=DETECT_MODEL)
        return {"segs": segments_of(result), "tokens": f"{meta.get('prompt_eval_count', '?')}+{meta.get('eval_count', '?')}"}

    out, reused = calls.get(f"detect|{first}|{last}|{lang}|{title}|{header}", DETECT_VERSION, compute, model=DETECT_MODEL)
    segs = [x for x in out.get("segs", []) if isinstance(x, dict)]
    how = "REUSED from cache" if reused else f"in {time.time() - t0:.1f}s, {out.get('tokens', '?')} tokens"
    log(f"  detect  L{first}-L{last} ({rng}): {len(segs)} raw segment(s) {how}")
    return segs


REFINE_INSTRUCTIONS = """

A candidate {category}/{style} segment for brand {brand!r} was detected at L{a}-L{b} in a narrower
window. Re-examine it with this fuller context. Return the FULL extent of that one promotion as a
single segment (two segments only if an integration ends in a separate promo-code pitch). The start
is the first line that transitions into the promotion; the end is the last promotional line. If on
reflection this is not a promotion at all, return no segments. Do not label anything unrelated to
this candidate."""


def refine_candidate_lines(lines, cand, lang, title, calls, header=""):
    """Pass 2a: re-ask with ~REFINE_CONTEXT_S of context either side. Returns raw segs + window bounds."""
    lo, hi = cand["start"] - REFINE_CONTEXT_S, cand["end"] + REFINE_CONTEXT_S
    idx = [i for i, l in enumerate(lines) if lo <= l["start"] <= hi]
    first, last = min(idx[0], cand["start_line"]), max(idx[-1], cand["end_line"])
    prompt = format_window(lines, first, last, lang, title, header) + REFINE_INSTRUCTIONS.format(
        category=cand["category"], style=cand["style"], brand=cand["brand"],
        a=cand["start_line"], b=cand["end_line"])
    t0 = time.time()

    def compute():
        with llm_lock:
            result, meta = ollama_chat_retry(prompt, label=f"refine L{cand['start_line']}-L{cand['end_line']}")
        return {"segs": segments_of(result), "tokens": f"{meta.get('prompt_eval_count', '?')}+{meta.get('eval_count', '?')}"}

    key = f"refine|{first}|{last}|{cand['start_line']}|{cand['end_line']}|{cand['category']}|{cand['style']}|{cand['brand']}|{header}"
    out, reused = calls.get(key, REFINE_VERSION, compute)
    segs = [x for x in out.get("segs", []) if isinstance(x, dict)]
    how = "REUSED from cache" if reused else f"in {time.time() - t0:.1f}s, {out.get('tokens', '?')} tokens"
    log(f"  refine  L{cand['start_line']}-L{cand['end_line']} in L{first}-L{last}: {len(segs)} segment(s) {how}")
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


SENTENCE_END_RE = re.compile(r"[.!?…][)\"'”’]*$")


def all_words(lines, lo, hi):
    """Flattened [start, end, text, line_idx] for lines[lo..hi]."""
    out = []
    for i in range(lo, hi + 1):
        for w in lines[i]["words"]:
            out.append([w[0], w[1], w[2], i])
    return out


def is_sentence_start(words, i):
    return i == 0 or bool(SENTENCE_END_RE.search(words[i - 1][2]))


def gap_before(words, i):
    return words[i][0] - words[i - 1][1] if i > 0 else 0.0


def sentence_start_index(words, p):
    """Index of the first word of the sentence containing words[p] (sentence start or pause boundary)."""
    i = p
    while i > 0 and not is_sentence_start(words, i) and gap_before(words, i) < SNAP_GAP_S:
        i -= 1
    return i


def sentence_end_index(words, q):
    """Index of the last word of the sentence containing words[q]."""
    i = q
    while i + 1 < len(words) and not is_sentence_start(words, i + 1) and gap_before(words, i + 1) < SNAP_GAP_S:
        i += 1
    return i


def snap_start(lines, s_line, e_line, picked_t):
    """Move the start to a natural cut.
    1. If the sentence containing the pick begins inside the ad lines (refine says the ad starts there), snap
       BACKWARD to that sentence start: it's still ad, and the viewer won't hear half a sentence.
    2. Else forward (later) to the nearest sentence start, else the largest pause >= SNAP_GAP_S, within
       SNAP_FORWARD_MAX_S and inside the ad lines.
    Returns (time, note)."""
    words = all_words(lines, max(0, s_line - 1), e_line)
    idx = [i for i, w in enumerate(words) if w[0] >= picked_t - 1e-6]
    if not idx:
        return picked_t, "no words at/after pick"
    p = idx[0]
    if is_sentence_start(words, p) or gap_before(words, p) >= SNAP_GAP_S:
        return words[p][0], "pick is already a sentence start/pause"
    i = sentence_start_index(words, p)
    if words[i][3] >= s_line:
        return words[i][0], f"backward to sentence start {words[i][2]!r} (-{picked_t - words[i][0]:.2f}s, inside ad lines)"
    limit_t = picked_t + SNAP_FORWARD_MAX_S
    for j in range(p + 1, len(words)):
        if words[j][0] > limit_t or words[j][3] > e_line:
            break
        if words[j][3] < s_line:
            continue  # forward targets must be inside the ad lines
        if is_sentence_start(words, j):
            return words[j][0], f"forward to sentence start {words[j][2]!r} (+{words[j][0] - picked_t:.2f}s)"
    best, best_gap = None, 0.0
    for j in range(p + 1, len(words)):
        if words[j][0] > limit_t or words[j][3] > e_line:
            break
        if words[j][3] < s_line:
            continue
        g = gap_before(words, j)
        if g >= SNAP_GAP_S and g > best_gap:
            best, best_gap = j, g
    if best is not None:
        return words[best][0], f"forward to pause {best_gap:.2f}s before {words[best][2]!r} (+{words[best][0] - picked_t:.2f}s)"
    return picked_t, "sentence starts before the ad and no clean cut ahead; keeping pick"


def snap_end(lines, s_line, e_line, picked_end_t):
    """Move the end to a natural cut.
    1. If the sentence containing the last ad word ENDS inside the ad lines, extend to that sentence end
       (still ad; avoids cutting mid-sentence).
    2. Then end on the largest pause (>= SNAP_GAP_S) that begins within SNAP_END_WINDOW_S after that word;
       the end is the start of the pause (no inset needed).
    Returns (time, note); time None if nothing to snap to (caller keeps pick - inset)."""
    words = all_words(lines, s_line, min(len(lines) - 1, e_line + 1))
    if not words:
        return None, "no words"
    q = min(range(len(words)), key=lambda i: abs(words[i][1] - picked_end_t))
    notes = []
    se = sentence_end_index(words, q)
    if se > q and words[se][3] <= e_line:
        notes.append(f"extended to sentence end {words[se][2]!r} (+{se - q} word(s), inside ad lines)")
        q = se
    best, best_gap = None, 0.0
    for i in range(q, len(words) - 1):
        if words[i][1] > words[q][1] + SNAP_END_WINDOW_S:
            break
        g = words[i + 1][0] - words[i][1]
        if g >= SNAP_GAP_S and g > best_gap:
            best, best_gap = i, g
    if best is not None:
        extra = f", +{best - q} word(s)" if best > q else ""
        notes.append(f"pause {best_gap:.2f}s after {words[best][2]!r}{extra}")
        return words[best][1], "; ".join(notes) + "; no inset needed"
    if notes:  # extended but no pause: end at the sentence's last word, keep the inset
        return max(words[q][0], words[q][1] - END_INSET_S), "; ".join(notes) + "; no pause, inset applied"
    return None, "no pause found; keeping pick"


def refine_words(lines, s_line, e_line, seg, calls):
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

    def compute():
        """Up to two attempts; returns the last answer (resolved or not) so it can be cached."""
        answer = None
        for attempt in (1, 2):
            with llm_lock:
                result, _meta = ollama_chat_retry(prompt, WORD_SCHEMA, label=f"words L{s_line}/L{e_line}")
            answer = {k: result.get(k) for k in ("first", "first_word", "last", "last_word")}
            a, _ = resolve_pick(answer["first"], answer["first_word"], s_words, "S")
            b, _ = resolve_pick(answer["last"], answer["last_word"], e_words, "E")
            if a is not None and b is not None:
                break
            log(f"  words   L{s_line}/L{e_line}: unresolved pick (attempt {attempt})")
        return answer

    key = f"words|{s_line}|{e_line}|{seg['category']}|{seg['style']}|{seg['brand']}"
    try:
        result, reused = calls.get(key, REFINE_VERSION, compute)
    except (OSError, ValueError) as e:
        log(f"  words   L{s_line}/L{e_line}: model call failed ({e!r})")
        result, reused = None, False
    if result:
        fw, rule_s = resolve_pick(result.get("first"), result.get("first_word"), s_words, "S")
        lw, rule_e = resolve_pick(result.get("last"), result.get("last_word"), e_words, "E")
        ok_s, ok_e = fw is not None, lw is not None
        log(f"  words   L{s_line}/L{e_line}: answer first={result.get('first')!r}/{result.get('first_word')!r} "
            f"last={result.get('last')!r}/{result.get('last_word')!r} -> start rule: {rule_s}; end rule: {rule_e}"
            f"{' (REUSED from cache)' if reused else ''}")

    start = s_words[fw][0] if ok_s else inward_start
    end = (e_words[lw][1] - END_INSET_S) if ok_e else inward_end
    log(f"  words   L{s_line}/L{e_line} in {time.time() - t0:.1f}s")
    log(f"    start: {describe_boundary('S', s_words, fw, ok_s, lines, s_line)} -> {start:.2f}"
        f"{'' if ok_s else ' (INWARD FALLBACK: end of first ad line)'}")
    log(f"    end:   {describe_boundary('E', e_words, lw, ok_e, lines, e_line)} -> {end:.2f}"
        f"{' (inset %.1fs)' % END_INSET_S if ok_e else ' (INWARD FALLBACK: start of last ad line)'}")
    if ok_s:
        snapped, note = snap_start(lines, s_line, e_line, start)
        log(f"    snap start: {start:.2f} -> {snapped:.2f} ({note})")
        start = snapped
    if ok_e:
        snapped, note = snap_end(lines, s_line, e_line, e_words[lw][1])
        if snapped is not None:
            log(f"    snap end:   {end:.2f} -> {snapped:.2f} ({note})")
            end = snapped
        else:
            log(f"    snap end:   kept {end:.2f} ({note})")
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
    cues = window_cues(seg.get("evidence", ""), seg.get("hint_terms", []))
    if match and cues:
        log(f"            {match}; cue(s): {cues[:4]}")
        return seg
    if match and not cues:
        why = "evidence contains no sponsor cue"
    else:
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


def map_to_original(seg, alines, lines):
    """Analysis-line indices -> original-line indices by time overlap (identity when same track)."""
    if alines is lines:
        return seg["start_line"], seg["end_line"]
    a_start, a_end = alines[seg["start_line"]]["start"], alines[seg["end_line"]]["end"]
    # lines whose midpoint falls in the analysis range (auto-translations keep the cue timing, so this is
    # usually exact); fall back to any overlap, then to the nearest line.
    idx = [i for i, l in enumerate(lines) if a_start <= (l["start"] + l["end"]) / 2 < a_end]
    if not idx:
        idx = [i for i, l in enumerate(lines) if l["end"] > a_start and l["start"] < a_end]
    if not idx:
        idx = [min(range(len(lines)), key=lambda i: abs(lines[i]["start"] - a_start))]
    return idx[0], idx[-1]


def refine_candidate(alines, lines, cand, lang, title, calls, header="", terms=()):
    """Pass 2 for one sponsor candidate -> list of refined segments (possibly empty).
    Detection/refinement/evidence run on the analysis track (alines); the word pass and snapping run on
    the original track (lines) so cuts are exact in the spoken language."""
    raw, first, last = refine_candidate_lines(alines, cand, lang, title, calls, header)
    refined = []
    for r in raw:
        seg = to_segment(r, alines, first, last)
        if not seg:
            log("  refine: dropped segment with out-of-window line indices")
            continue
        if not overlaps(seg, cand, REFINE_CONTEXT_S / 2):
            log(f"  refine: dropped unrelated segment at {fmt_time(seg['start'])}-{fmt_time(seg['end'])}")
            continue
        seg["hint_terms"] = list(terms)
        seg["line_start"], seg["line_end"] = seg["start"], seg["end"]
        require_evidence(seg, alines)
        o_s, o_e = map_to_original(seg, alines, lines)
        if alines is not lines:
            log(f"  map: analysis L{seg['start_line']}-L{seg['end_line']} -> original L{o_s}-L{o_e} "
                f"({fmt_time(lines[o_s]['start'])}-{fmt_time(lines[o_e]['end'])})")
        if seg["action"] == "skip":
            start, end, sure = refine_words(lines, o_s, o_e, seg, calls)
            if not sure and end - start < MIN_SKIP_S:
                log(f"  refine: boundaries uncertain and only {end - start:.1f}s left after inward fallback -> show")
                seg["action"] = "show"
                seg["reason"] = "[downgraded: uncertain boundaries] " + seg["reason"]
            else:
                seg["start"], seg["end"] = start, end
        seg.pop("hint_terms", None)
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


def brand_terms(brand):
    """Normalized phrase plus the longest token (>= 4 chars) of a brand, for mention counting."""
    toks = normalize(brand)
    if not toks:
        return []
    terms = [" ".join(toks)]
    longest = max(toks, key=len)
    if len(longest) >= 4 and longest not in terms:
        terms.append(longest)
    return terms


def mentions_outside(seg, lines):
    """Number of lines outside the segment that mention its brand."""
    terms = brand_terms(seg["brand"])
    if not terms:
        return 0
    n = 0
    for i, l in enumerate(lines):
        if seg["start_line"] <= i <= seg["end_line"]:
            continue
        t = " " + " ".join(normalize(l["text"])) + " "
        if any(f" {term} " in t for term in terms):
            n += 1
    return n


def post_filter(segments, title, lines, verbose=True):
    """Final safety net for keep/show markers (skips are left alone and only logged):
    - drop keep/show whose evidence/reason reads like a sponsor disclaimer;
    - drop keep/show whose brand is the video's subject (in the title, or mentioned in >= TOPIC_MENTIONS
      lines outside the segment);
    - drop show below SHOW_THRESHOLD."""
    out = []
    say = log if verbose else (lambda *a: None)
    for s in segments:
        rng = f"{fmt_time(s['start'])}-{fmt_time(s['end'])}"
        if s["action"] in ("keep", "show"):
            text = f"{s.get('evidence', '')} {s.get('reason', '')}"
            m = DISCLAIMER_RE.search(text)
            if m:
                say(f"  filter: dropped {s['action']} {rng} {s['brand']!r}: reads like a disclaimer ({m.group(0)!r})")
                continue
            if brand_in_title(s["brand"], title):
                say(f"  filter: dropped {s['action']} {rng} {s['brand']!r}: brand is the video's subject (in title)")
                continue
            n = mentions_outside(s, lines) if "start_line" in s else 0
            if n >= TOPIC_MENTIONS:
                say(f"  filter: dropped {s['action']} {rng} {s['brand']!r}: brand mentioned in {n} lines outside the segment (subject)")
                continue
            if s["action"] == "show" and s["confidence"] < SHOW_THRESHOLD:
                say(f"  filter: dropped show {rng} {s['brand']!r}: conf {s['confidence']:.2f} < {SHOW_THRESHOLD}")
                continue
        elif brand_in_title(s["brand"], title):
            say(f"  filter: NOTE skip {rng} {s['brand']!r} brand appears in the title; kept (has verbatim evidence)")
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


def md_cell(text):
    """Plain text only: no table pipes, line breaks, HTML or code spans (values come from the page)."""
    t = re.sub(r"[<>`\r\n]", " ", str(text)).replace("|", "\\|")
    return re.sub(r"\s+", " ", t).strip()[:400]


def append_result(job, segments, job_time, calls, ttfs):
    """One row per finished job in results.md (verdict left blank for a human)."""
    segs = "; ".join(f"{s['action'].upper()} {fmt_time(s['start'])}-{fmt_time(s['end'])} {s['brand'] or '?'} ({s['confidence']:.2f})"
                     for s in segments) or "none"
    notes = (f"{job.get('tag', '')} v{DETECT_VERSION}/{REFINE_VERSION}/{POST_VERSION} detect={DETECT_MODEL} refine={MODEL}; "
             f"windows {job.get('n_sent', '?')}/{job.get('n_windows', '?')}; {calls.reused} reused").strip()
    row = "| " + " | ".join(md_cell(x) for x in [
        job["video_id"], job.get("creator", ""), job["lang"], job.get("kind", ""), "", segs, "", notes,
        f"{job_time:.0f}s", calls.computed, f"{ttfs:.0f}s" if ttfs is not None else "-",
        time.strftime("%Y-%m-%d %H:%M")]) + " |\n"
    try:
        new_file = not os.path.exists(RESULTS_MD)
        with open(RESULTS_MD, "a", encoding="utf-8") as f:
            if new_file:
                f.write("# Sponsor Skipper — local results log (gitignored; one row per finished job)\n\n"
                        "| video | creator | lang | kind | expected | actual | verdict | notes | job time | calls | first skip | when |\n"
                        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
            f.write(row)
    except OSError as e:
        log(f"results.md append failed: {e}")


def public_job(job):
    out = {"status": job["status"], "segments": job["segments"]}
    if job.get("cached"):
        out["cached"] = True
    if job.get("error"):
        out["error"] = job["error"]
    if job.get("paused"):
        out["paused"] = True
    if job.get("progress"):
        out["progress"] = job["progress"]
    return out


def versions():
    return {"detect": DETECT_VERSION, "refine": REFINE_VERSION, "post": POST_VERSION}


def cache_valid(cached, thash):
    """Final segments can be served as-is only if nothing changed."""
    return (bool(cached) and cached.get("hash") == thash and cached.get("model") == MODEL
            and cached.get("versions") == versions())


class CallCache:
    """Per-video cache of raw model outputs keyed by call inputs. Counts reuse for the log."""

    def __init__(self, stored):
        self.calls = dict(stored or {})
        self.reused = 0
        self.computed = 0

    def get(self, key, version, compute, model=None):
        model = model or MODEL
        hit = self.calls.get(key)
        if hit and hit.get("v") == version and hit.get("model") == model:
            self.reused += 1
            return hit["out"], True
        out = compute()
        self.calls[key] = {"v": version, "model": model, "out": out}
        self.computed += 1
        return out, False


def order_windows(wins, alines, position, terms):
    """Playhead first: the window containing the playhead, then later windows, then earlier ones;
    within each group, windows with description hints first."""
    def key(w):
        first, last = w
        w_start, w_end = alines[first]["start"], alines[last]["end"]
        contains = w_start <= position < w_end
        after = w_end >= position
        text = " ".join(alines[i]["text"] for i in range(first, last + 1)).lower()
        hinted = any(t.lower() in text for t in terms)
        return (0 if contains else (1 if after else 2), 0 if hinted else 1, w_start)
    return sorted(wins, key=key)


def plan_job(job):
    """Build the ordered list of windows to run (prefiltered), done once per job."""
    alines, position, terms = job["alines"], job.get("position", 0.0), job["terms"]
    wins = windows(alines)
    sent = []
    brand_terms_only = [t for t in terms if t in job["hints"]["brands"]]
    for first, last in wins:
        text = " ".join(alines[i]["text"] for i in range(first, last + 1))
        if window_cues(text, terms):
            sent.append((first, last))
        else:
            low = text.lower()
            named = [b for b in brand_terms_only if b.lower() in low]
            if named:  # can't happen while brands are cue terms, but guards future cue-list changes
                log(f"[{job['video_id']}] prefilter: POSSIBLE MISS, skipped window "
                    f"{fmt_time(alines[first]['start'])}-{fmt_time(alines[last]['end'])} mentions hint brand {named}")
    with jobs_lock:
        job["n_windows"] = len(wins)
        job["n_sent"] = len(sent)
        job["pending"] = order_windows(sent, alines, position, terms)
    log(f"[{job['video_id']}] prefilter: {len(sent)} of {len(wins)} windows sent"
        f"{'; hints: ' + ', '.join(terms[:8]) if terms else ''}; playhead {fmt_time(position)}; "
        f"order: {[fmt_time(alines[f]['start']) for f, _ in job['pending']]}")


def run_window(job, first, last):
    """One unit of work: detect one window, refine its sponsor candidates."""
    alines, lines, lang, title, calls = job["alines"], job["lines"], job["alang"], job["title"], job["calls"]
    header, terms, final = job["header"], job["terms"], job["final"]
    cands = []
    for r in classify_window(alines, first, last, lang, title, calls, header):
        seg = to_segment(r, alines, first, last)
        if seg:
            cands.append(seg)
        else:
            log("  detect: dropped segment with out-of-window line indices")
    for cand in merge_candidates(cands):
        if any(overlaps(cand, f) for f in final):
            continue  # already covered by another window
        if cand["category"] != "sponsor":
            final.append(cand)  # selfpromo/interaction are 'show'; boundaries don't matter
            continue
        final.extend(refine_candidate(alines, lines, cand, lang, title, calls, header, terms))


def finalize(job, verbose):
    return [public_segment(x) for x in post_filter(merge_segments(job["final"]), job["title"], job["alines"], verbose)]


def publish(job):
    with jobs_lock:
        if jobs.get(job["video_id"]) is not job:
            return
        job["segments"] = finalize(job, verbose=False)
        job["progress"] = f"{job['n_sent'] - len(job['pending'])}/{job['n_sent']}"
        if job["ttfs"] is None and any(x["action"] == "skip" for x in job["segments"]):
            job["ttfs"] = time.time() - job["t_start"]
            log(f"[{job['video_id']}] first skip published after {job['ttfs']:.1f}s")


def save_partial(job):
    """Persist model outputs so a paused/evicted job resumes without repeating Ollama calls."""
    save_cache(job["video_id"], {"videoId": job["video_id"], "versions": None, "hash": job["hash"], "lang": job["lang"],
                                 "title": job["title"], "model": MODEL, "segments": job["segments"],
                                 "calls": job["calls"].calls, "transcript": job["lines"], "partial": True})


def finish_job(job):
    video_id = job["video_id"]
    segments = finalize(job, verbose=True)
    with jobs_lock:
        if jobs.get(video_id) is not job:
            log(f"[{video_id}] job superseded; discarding result")
            return
    log_segments(video_id, segments)
    save_cache(video_id, {"videoId": video_id, "versions": versions(), "hash": job["hash"], "lang": job["lang"],
                          "title": job["title"], "model": MODEL, "segments": segments,
                          "calls": job["calls"].calls, "transcript": job["lines"]})
    job_time = time.time() - job["t_start"]
    if job["ttfs"] is None and any(x["action"] == "skip" for x in segments):
        job["ttfs"] = job_time
    with jobs_lock:
        job["segments"] = segments
        job["status"] = "done"
        job["paused"] = False
    calls = job["calls"]
    log(f"[{video_id}] done: {len(segments)} segment(s), {job['n_sent']}/{job['n_windows']} window(s) sent, "
        f"model calls: {calls.reused} reused / {calls.computed} computed, {job_time:.1f}s wall "
        f"({job['work_s']:.1f}s working), first skip at {'%.1fs' % job['ttfs'] if job['ttfs'] is not None else '-'}, cached")
    append_result(job, segments, job_time, calls, job["ttfs"])


def fail_job(job, e):
    video_id = job["video_id"]
    if isinstance(e, urllib.error.URLError):
        log(f"[{video_id}] error: cannot reach Ollama at {OLLAMA}: {e.reason}")
        err = "ollama offline"
    else:
        log(f"[{video_id}] error: {e!r}")  # full detail stays in the terminal only
        err = "model error" if isinstance(e, (json.JSONDecodeError, KeyError, TypeError, ValueError)) else "internal error"
    with jobs_lock:
        job["status"] = "error"
        job["error"] = err
        job["paused"] = False


work_available = threading.Condition(jobs_lock)


def pick_job():
    """Most recently polled unfinished job that is still being watched. Marks others paused."""
    now = time.time()
    best = None
    for job in jobs.values():
        if job["status"] in ("done", "error"):
            continue
        watched = now - job["last_poll"] <= PAUSE_AFTER_S
        if not watched:
            if not job.get("paused"):
                job["paused"] = True
                left = f"{len(job['pending'])} window(s) left" if job["pending"] is not None else "not started"
                log(f"[{job['video_id']}] paused (not polled for {PAUSE_AFTER_S:.0f}s); {left}, partial results cached")
            continue
        if best is None or job["last_poll"] > best["last_poll"]:
            best = job
    return best


def worker_step():
    """One scheduling step. Returns the job it worked on (or None if idle)."""
    job = None
    with work_available:
        job = pick_job()
        if job is None:
            work_available.wait(timeout=1.0)
            return None
        if job.get("paused"):
            job["paused"] = False
            log(f"[{job['video_id']}] resumed")
        job["status"] = "classifying"
        needs_plan = job["pending"] is None
        unit = None if needs_plan else (job["pending"].pop(0) if job["pending"] else None)
    # Everything below runs without the lock so HTTP handlers never wait on model or planning work.
    try:
        if needs_plan:
            plan_job(job)  # the next step pops the first window
        elif unit is None:
            finish_job(job)
        else:
            t0 = time.time()
            run_window(job, *unit)
            job["work_s"] += time.time() - t0
            publish(job)
            save_partial(job)
    except Exception as e:  # noqa: BLE001
        raise JobError(job, e) from e
    return job


class JobError(Exception):
    def __init__(self, job, cause):
        super().__init__(repr(cause))
        self.job, self.cause = job, cause


def worker():
    """Single worker: one Ollama call at a time, always for the video being watched right now.
    Never dies: any error is logged, the job it was working on is marked error, and the loop goes on."""
    while True:
        try:
            worker_step()
        except JobError as je:
            fail_job(je.job, je.cause)
        except Exception as e:  # noqa: BLE001
            log(f"worker error outside any job: {e!r}")
            time.sleep(1.0)


def touch(job):
    job["last_poll"] = time.time()


def start_or_get_job(video_id, lang, lines, title="", meta=None, alines=None, alang=None, position=0.0, description=""):
    meta = meta or {}
    thash = transcript_hash(lines)
    with work_available:
        job = jobs.get(video_id)
        if job and job["hash"] == thash and job["status"] != "error":
            touch(job)
            if job["status"] == "done":
                job["cached"] = True  # reused, not computed for this request
            else:
                job["position"] = position
            work_available.notify()
            return job
        cached = load_cache(video_id)
        if cache_valid(cached, thash):
            job = {"status": "done", "segments": cached.get("segments", []), "hash": thash, "lang": lang,
                   "lines": lines, "cached": True, "video_id": video_id, "last_poll": time.time()}
            jobs[video_id] = job
            log(f"[{video_id}] cache hit ({len(job['segments'])} segment(s))")
            return job
        why = ""
        if job:
            why = " (transcript changed; replacing running job)" if job["status"] != "error" else " (previous attempt errored)"
        elif cached:
            why = (" (cache versions %s/%s; now %s/%s: re-processing, reusing model calls where valid)"
                   % (cached.get("versions"), cached.get("model"), versions(), MODEL)
                   if cached.get("hash") == thash else " (cached transcript hash differs)")
        # Admission: never refuse the video being watched; evict the least-recently-polled unfinished job.
        active = [j for v, j in jobs.items() if j["status"] not in ("done", "error") and v != video_id]
        while len(active) >= MAX_ACTIVE_JOBS:
            victim = min(active, key=lambda j: j["last_poll"])
            active.remove(victim)
            log(f"[{victim['video_id']}] evicted (least recently polled, {time.time() - victim['last_poll']:.0f}s ago); "
                f"partial results stay on disk")
            del jobs[victim["video_id"]]
        finished = [v for v, j in jobs.items() if j["status"] in ("done", "error")]
        if len(finished) >= MAX_JOBS_IN_MEMORY:
            for v in finished[: len(finished) - MAX_JOBS_IN_MEMORY + 1]:
                del jobs[v]
        prior_calls = cached.get("calls") if cached and cached.get("hash") == thash and cached.get("model") == MODEL else None
        hints = extract_hints(description)
        terms = hint_terms(hints)
        same = alines is None
        job = {"status": "queued", "segments": [], "hash": thash, "lang": lang, "lines": lines, "title": title,
               "alines": lines if same else alines, "alang": lang if same else (alang or "en"),
               "video_id": video_id, "kind": meta.get("kind", ""), "creator": meta.get("creator", ""),
               "tag": meta.get("tag", ""), "position": position, "hints": hints, "terms": terms,
               "header": hints_header(hints), "calls": CallCache(prior_calls), "final": [], "pending": None,
               "n_windows": 0, "n_sent": 0, "ttfs": None, "t_start": time.time(), "work_s": 0.0,
               "last_poll": time.time(), "paused": False}
        jobs[video_id] = job
        work_available.notify()
    log(f"[{video_id}] new job: {len(lines)} lines{'' if same else f' + {len(alines)} analysis lines ({alang})'}, "
        f"lang={lang}, kind={job['kind']}, hash={thash}, title={title!r}{why}")
    if prior_calls:
        log(f"[{video_id}] {len(prior_calls)} cached model call(s) available")
    if any(hints.values()):
        log(f"[{video_id}] hints: brands={hints['brands']} codes={hints['codes']} domains={hints['domains']} chapters={len(hints['chapters'])}")
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
                if job:
                    touch(job)
            if not job:
                cached = load_cache(vid)
                if cached and cached.get("versions") == versions() and cached.get("model") == MODEL:
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
        meta = {"kind": str(data.get("kind") or "")[:10], "creator": str(data.get("creator") or "")[:100],
                "tag": str(data.get("tag") or "")[:40]}  # free-text note for results.md (e.g. BASELINE)
        description = str(data.get("description") or "")[:5000]
        try:
            position = max(0.0, float(data.get("position") or 0.0))
        except (TypeError, ValueError):
            position = 0.0
        alines = clean_lines(data.get("analysisLines")) if data.get("analysisLines") else None
        if alines is not None and (len(alines) == 0 or len(alines) > MAX_LINES):
            alines = None
        alang = str(data.get("analysisLang") or "en")[:12]
        job = start_or_get_job(vid, lang, lines, title, meta, alines, alang, position, description)
        with jobs_lock:
            return self.send_json(200, public_job(job))


def model_present(name):
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=2) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        return any(m.get("name") == name for m in tags.get("models", []))
    except (OSError, ValueError):
        return False


def main():
    global DETECT_MODEL
    os.makedirs(CACHE_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    DETECT_MODEL = DETECT_MODEL_PREF if model_present(DETECT_MODEL_PREF) else MODEL
    if DETECT_MODEL != MODEL:
        try:
            gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
        except (ValueError, OSError, AttributeError):
            gb = 0
        log(f"WARNING: two models ({DETECT_MODEL} + {MODEL}) will stay loaded; needs >= 32 GB"
            f"{f', this machine has {gb:.0f} GB' if gb else ''}")
    log(f"Sponsor Skipper server on http://{HOST}:{PORT}  cache={CACHE_DIR}")
    log(f"models: detect={DETECT_MODEL} refine/words={MODEL} via Ollama "
        f"({'reachable' if ollama_available() else 'NOT available'}); versions {versions()}")
    threading.Thread(target=warm_models, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
