#!/usr/bin/env python3
"""Second Read server — localhost:8765, stdlib only.

Stage 4: classification with a local Qwen model via Ollama (/api/chat, JSON schema output,
temperature 0), in two passes:
  pass 1 (detect): 240s windows with 30s overlap -> candidate segments. First window first.
  pass 2 (refine): each sponsor candidate is re-asked with ~90s of context on both sides so
                   window edges never cut an ad, then the exact first/last ad word is picked
                   from word-level caption timing. end = last word end - 0.3s (round inward).

API
  POST /analyze        {videoId, lang, lines:[{start,end,text}]}  -> job {status, segments}
  GET  /analyze?v=ID                                              -> job or 404 {"error":"unknown"}
  GET  /health                                                    -> {ok, model, ollama, loaded}
  POST /feedback       {videoId, kind: false_skip|missed_ad, playhead, segment?} -> {ok}
  GET  /feedback                                                  -> {count, recent}
  POST /event          {videoId, kind: skip|undo|seek_into|allowlisted, segment, playhead, saved} -> {ok}
  GET  /stats.json, GET /dashboard                                local stats (server/stats.py)
  GET  /ui/tokens.css | components.css | fonts.css | fonts/*.woff2  the design system, for the dashboard
  GET  /ads.json                                                  ad intelligence per segment + insights (server/intel.py)
  GET  /codes.json?q=                                             promo code / link / offer vault
  python3 server/server.py --feedback-report                      prints a summary for tuning sessions

Status: queued -> classifying -> done | error  (+ paused:true while the video isn't being polled)
"""

import hashlib
import http.client
import json
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import intel  # server/intel.py: ad intelligence (tier-1 metrics, idle-only tier-2 Qwen analysis)
import stats  # server/stats.py: SQLite store, brand normalization, metrics, dashboard

# Threat model: the transcript, title and words come from whatever the YouTube page handed the
# extension, so they are untrusted. They are only ever (a) hashed into the cache key, so a bogus
# transcript cannot overwrite the result for the real one, and (b) placed in the model prompt, where
# the output is constrained by a JSON schema, line indices are validated against the window, and a
# skip needs a verbatim quote from the transcript itself. Prompt text can at most mislabel its own video.
def env(name, default=""):
    """SR_<NAME>, falling back to the pre-rename SS_<NAME> so old launch commands keep working."""
    return os.environ.get(f"SR_{name}") or os.environ.get(f"SS_{name}") or default


HOST, PORT = "127.0.0.1", int(env("PORT", "8765"))  # SR_PORT: run a second instance for testing
CLIENT_BUILD = 21  # extension build this server expects (page.js / content.js / background.js BUILD)
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "127.0.0.1", "localhost"}
# Optional: restrict to one extension. Unpacked extension IDs differ per machine, so this is opt-in:
#   SR_EXTENSION_ID=abcdefghijklmnopabcdefghijklmnop python3 server/server.py
EXTENSION_ID = env("EXTENSION_ID").strip()
OLLAMA = "http://localhost:11434"
MODEL = "qwen3:14b"                                   # refine + word pass (and detect, by default)
# Optional smaller model for pass-1 detection, e.g. SR_DETECT_MODEL=qwen3:8b. Both models then stay loaded
# (keep_alive -1): 14b + 8b = ~18 GB of GPU memory, which thrashed and timed out on a 24 GB Mac. Only use it
# with >= 32 GB. Default: one model for everything.
DETECT_MODEL_PREF = env("DETECT_MODEL").strip() or MODEL
DETECT_MODEL = MODEL                                  # resolved at startup
KEEP_ALIVE = -1                                       # keep the model loaded between calls
# Staged cache versions. Each model call's raw output is cached per video and keyed by its inputs, so a
# change only re-runs what it affects:
#   DETECT_VERSION  pass 1 (SYSTEM_PROMPT rubric, OUTPUT_SCHEMA, window size/overlap)
#   REFINE_VERSION  pass 2 (SYSTEM_PROMPT, REFINE_INSTRUCTIONS, REFINE_CONTEXT_S, WORD_PROMPT, WORD_SCHEMA)
#   POST_VERSION    everything after the model: evidence gate, thresholds, snapping, merge, filters
# A POST-only change re-processes a cached video from stored model outputs with zero Ollama calls.
# Rubric changes in SYSTEM_PROMPT affect both passes: bump DETECT and REFINE.
DETECT_VERSION = 3
REFINE_VERSION = 3
POST_VERSION = 10
LEADIN_VERSION = 2   # the lead-in check: LEADIN_PROMPT / LEADIN_SYSTEM, the sentences it is shown, its caps
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
MIN_SKIP_S = 5.0            # a skip shorter than this is a one-line disclosure, not a read: labeled instead
TOPIC_MENTIONS = 3          # brand mentioned in this many lines outside the segment -> it's the subject
HINT_CLUSTER_GAP_S = 120.0  # transcript mentions of a description hint closer than this form one region
HINT_REGION_PAD_S = 60.0    # detect windows overlapping a hint region (+- this) are skipped: refine runs directly
ANCHOR_CUE_WORDS = 25       # a start anchor is trusted only if a sponsor cue follows within this many words
LEADIN_MAX_S = 6.0          # the lead-in check may move a skip's start back by at most this much (largest seen: 5 s)
LEADIN_MAX_SENTENCES = 3    # ... and by at most this many whole sentences
LEADIN_TIMEOUT_S = 60       # ... and gives up after this long: the start then stays where it is
LEADIN_AD_WORDS = 45        # how much of the ad's opening the check is shown
MAX_HINT_LINES = 8          # a hint term matching more lines than this is too common to be a sponsor marker
MAX_HINT_REGION_S = 240.0   # a hint region longer than this is split (keeps refine windows inside the context)
MAX_CHAPTER_AD_S = 240.0    # a "sponsor" chapter longer than this is not trusted as the ad's bounds
CHAPTER_AD_RE = re.compile(r"\b(sponsor|sponsored|sponsorship|advert|advertisement|\(ad\)|\bad\b|ad break|paid promotion|"
                           r"promo|partner|partnership|brought to you|werbung|publicité|anuncio|patrocin|広告|スポンサー|विज्ञापन|प्रायोजक)", re.I)
# SR_DATA_DIR redirects every local file (result cache, stats DB, results log, feedback) for an isolated test instance.
CACHE_DIR = env("DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
RESULTS_MD = os.path.join(CACHE_DIR, "results.local.md")  # gitignored: it records which videos were watched
FEEDBACK_JSONL = os.path.join(CACHE_DIR, "feedback.local.jsonl")  # gitignored: wrong-skip / missed-ad reports
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extension", "ui")
UI_FILE_RE = re.compile(r"^(?:tokens\.css|components\.css|fonts\.css|fonts/[A-Za-z0-9-]+\.woff2)$")

jobs = {}                 # videoId -> job dict
jobs_lock = threading.Lock()
llm_lock = threading.Lock()  # one LLM call at a time


CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x1b]")


def scrub(value, depth=0):
    """Strip control/escape characters from every string in page-supplied JSON (they end up in the
    terminal, results.md and the model prompt); drop non-finite numbers."""
    if depth > 6:
        return None
    if isinstance(value, str):
        return CONTROL_RE.sub("", value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [scrub(v, depth + 1) for v in value[:50000]]
    if isinstance(value, dict):
        return {str(k)[:64]: scrub(v, depth + 1) for k, v in list(value.items())[:200]}
    return value


def finite(x, default=None):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


# ---------------------------------------------------------------------------
# Transcript handling
# ---------------------------------------------------------------------------
# ">>" speaker markers and [Music]/[Applause]/(laughs)-style tags carry no meaning for the model.
NOISE_TAG_RE = re.compile(r"\[[^\]]{1,40}\]|\((?:music|applause|laugh\w*|cheer\w*|inaudible|crosstalk)[^)]*\)", re.I)
# Lookbehind instead of consuming the leading whitespace: with "(^|\s)>>+\s*" the first of two consecutive markers
# (">> >> So it needed") ate the space the second one needed, and one ">>" survived into the model input.
SPEAKER_RE = re.compile(r"(?:^|(?<=\s))>{2,}\s*")


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
        if not wt:
            continue
        # Both ends inside the line, start <= end. A track without word timing has its words spread over each cue's
        # display time, which runs past the next cue's start: capping only the end left words that "start" after
        # they end, and the gaps between them read as pauses.
        ws = min(max(start, ws), end)
        out.append([round(ws, 2), round(max(ws, min(end, we)), 2), wt])
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


MODEL_CATEGORIES = ("sponsor", "affiliate", "selfpromo", "membership", "crosspromo", "charity", "giveaway")
RULE_CATEGORIES = ("interaction", "intro", "outro", "recap")
ALL_CATEGORIES = MODEL_CATEGORIES + RULE_CATEGORIES

# Rule-based categories (no model call). Phrases are matched per line; the matched phrase is the evidence.
INTERACTION_RE = re.compile(
    r"\b(like (?:this video|the video|and subscribe)|(?:hit|smash|press|click) (?:that |the )?(?:like|subscribe|bell|thumbs)|"
    r"subscribe (?:to|for|if|so)|leave a (?:like|comment|thumbs)|ring the bell|notification bell|turn on notifications|"
    r"comment (?:below|down below)|let me know in the comments|drop a (?:like|comment)|give (?:this|it) a (?:like|thumbs))\b", re.I)
OUTRO_RE = re.compile(
    r"\b(thanks? (?:so much |you )?for watching|see you (?:guys |all )?(?:in the )?next (?:time|video|one)|until next time|"
    r"that'?s (?:it|all) for (?:today|this video|now|this one)|i'?ll see you (?:guys |all )?(?:next|in the|soon)|catch you (?:next|later|in the)|"
    r"peace out|take care|bye[- ]?bye|goodbye|stay safe)\b", re.I)
RECAP_RE = re.compile(
    r"\b(previously on|last time (?:we|i|you)|coming up|in this video (?:we'?ll|i'?ll|we'?re going to|i'?m going to|we will|i will)|"
    r"today (?:we'?ll|i'?ll|we'?re going to|i'?m going to|we will|i will)|here'?s what'?s coming|in today'?s video|"
    r"by the end of this video|quick recap|to recap)\b", re.I)
OUTRO_WINDOW_S = 60.0     # outro phrases only count in the final minute
INTRO_MIN_GAP_S = 4.0     # no speech at the start for at least this long -> intro segment
RULE_MERGE_GAP_S = 10.0   # matched lines closer than this form one segment


def rule_segments(job):
    """interaction / intro / outro / recap from rules; label-only (action 'show'), source 'rule'."""
    lines, alines = job["lines"], job["alines"]
    duration = job.get("duration") or (lines[-1]["end"] if lines else 0)
    out = []
    if lines and lines[0]["start"] >= INTRO_MIN_GAP_S:
        out.append({"start": 0.0, "end": round(lines[0]["start"], 2), "start_line": 0, "end_line": 0,
                    "category": "intro", "style": "read", "brand": "", "confidence": 1.0,
                    "evidence": f"no speech 0:00-{fmt_time(lines[0]['start'])}", "reason": "no spoken line before this point",
                    "line_start": 0.0, "line_end": lines[0]["start"], "source": "rule", "action": "show"})
    hits = {"interaction": [], "outro": [], "recap": []}
    for i, l in enumerate(alines):
        m = INTERACTION_RE.search(l["text"])
        if m:
            hits["interaction"].append((i, m.group(0)))
        m = RECAP_RE.search(l["text"])
        if m:
            hits["recap"].append((i, m.group(0)))
        if duration and l["end"] >= duration - OUTRO_WINDOW_S:
            m = OUTRO_RE.search(l["text"])
            if m:
                hits["outro"].append((i, m.group(0)))
    for cat, items in hits.items():
        group = []
        for i, phrase in items + [(None, None)]:
            if i is not None and group and alines[i]["start"] - alines[group[-1][0]]["end"] <= RULE_MERGE_GAP_S:
                group.append((i, phrase))
                continue
            if group:
                a, b = group[0][0], group[-1][0]
                out.append({"start": alines[a]["start"], "end": alines[b]["end"], "start_line": a, "end_line": b,
                            "category": cat, "style": "read", "brand": "", "confidence": 1.0,
                            "evidence": group[0][1], "reason": f"rule: {cat} phrase{'s' if len(group) > 1 else ''} "
                            + ", ".join(sorted({g[1].lower() for g in group}))[:120],
                            "line_start": alines[a]["start"], "line_end": alines[b]["end"], "source": "rule", "action": "show"})
            group = [(i, phrase)] if i is not None else []
    if cat_counts := {c: sum(1 for x in out if x["category"] == c) for c in RULE_CATEGORIES if any(x["category"] == c for x in out)}:
        log(f"[{job['video_id']}] rules: {cat_counts}")
    return out


# Sponsor cues. A window with none of these (nor any description hint) is never sent to the model:
# the evidence gate requires a verbatim cue, so such a window could never produce a skip anyway.
CUE_PATTERNS = [
    r"sponsor", r"brought to you", r"thanks? to \w+", r"special thanks to", r"use (?:the |my |promo |discount )?code",
    r"promo ?code", r"discount", r"\d+ ?(?:%|percent) off", r"link(?:'s| is)? in the description", r"link below",
    r"\.com/", r"\.com\b", r"check (?:them |it )?out", r"sign ?up", r"free trial", r"partner(?:ed|ing)? with",
    r"in partnership with", r"supported by", r"made possible by", r"today'?s sponsor", r"go to \w+", r"head (?:over )?to",
    r"\bvisit\b", r"today'?s video", r"this video is", r"first \d+ (?:people|months)",
    r"it'?s an ad\b", r"this is an ad\b", r"an ad for\b", r"\bad break\b", r"try (?:it|them) (?:out )?(?:for )?free",
    r"start your free", r"get started (?:for free|today)", r"free for life",
]
CUE_RE = re.compile("|".join(CUE_PATTERNS), re.I)
# Strong cues: unambiguous sponsorship language. Used when the evidence quote itself has no cue and we look at the
# segment's own lines instead; generic phrases (check out, visit, go to, sign up, .com) do not count there.
STRONG_CUE_RE = re.compile(
    r"sponsor|brought to you|promo ?code|use (?:the |my |your )?code|\bcode [A-Z0-9]{3,}|discount|\d+ ?(?:%|percent) off|"
    r"link(?:'s| is)? in the description|partner(?:ed|ing)? with|in partnership with|supported by|made possible by|"
    r"it'?s an ad\b|this is an ad\b|an ad for\b|ad break|free trial|free for life|first \d+ (?:people|months)", re.I)
# Ad markers: what separates an ad from a brand that merely comes up in the story ("we're shooting for Fossil
# today"). Sponsorship language, a code, an offer, a link or a call to action. A sponsor label that is not a skip
# (an integration, or a read that was downgraded) needs one; a skip already needs a cue in its evidence.
# The bare word "sponsor" is not one: "we got all kinds of sponsors" in an interview about a community project is the
# story (FBeMrUWX73s); only sponsorship of this video or episode counts.
SPONSORING_THIS_RE = (r"sponsored by|(?:today'?s|our|this (?:video'?s|episode'?s)) sponsor\b|for sponsoring|"
                      r"sponsor(?:s|ed|ing)? (?:of )?(?:this|today'?s) (?:video|episode|vlog|stream|podcast)|"
                      r"(?:video|episode|vlog|podcast) is sponsored")
AD_MARKER_RE = re.compile(
    STRONG_CUE_RE.pattern.replace("sponsor|", SPONSORING_THIS_RE + "|", 1)
    + r"|link (?:below|in (?:the|my) bio)|use (?:my|the|this|our) link|\.com\b|sign ?up|check (?:them|it) out", re.I)
# Cues for the other model categories (affiliate, selfpromo, membership, crosspromo, charity, giveaway): they get
# a window to the model, but they never count as evidence for a sponsor skip.
OTHER_CUE_PATTERNS = [
    r"patreon", r"membership", r"\bmembers?\b", r"join (?:the|my|our) channel", r"\bmerch\b", r"my (?:new )?(?:course|book|app|newsletter|podcast|game|album)",
    r"giveaway", r"\bgive away\b", r"donat", r"fundrais", r"charity", r"affiliate", r"amazon (?:link|store|storefront)",
    r"links? (?:below|in the description) (?:for|to)", r"my other (?:video|channel)", r"check out (?:my|their|his|her) (?:channel|video)",
    r"friend'?s channel", r"\bcollab", r"go watch", r"our (?:store|shop)", r"\bshop\b", r"super ?thanks", r"ko-?fi", r"buy me a coffee",
]
OTHER_CUE_RE = re.compile("|".join(OTHER_CUE_PATTERNS), re.I)


def window_cues(text, hint_terms, sponsor_only=False):
    """Matched cue strings in a window's text (built-in cues + description hints). With sponsor_only, only sponsor
    cues and hints count (used for the sponsor-skip evidence gate)."""
    found = [m.group(0) for m in CUE_RE.finditer(text)]
    if not sponsor_only:
        found += [m.group(0) for m in OTHER_CUE_RE.finditer(text)]
    low = text.lower()
    found += [h for h in hint_terms if h.lower() in low]
    return found


CODE_RE = re.compile(r"\bcode[:\s]+([A-Z0-9]{3,20})\b")
DOMAIN_RE = re.compile(r"\b([a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){0,4}\.(?:com|net|org|io|co|deals|gg|ai|app|tv|me|ly|fm|shop|store|xyz|dev|to)(?:/[\w.-]{1,80})?)\b", re.I)
AD_LINE_RE = re.compile(r"\(ad\)|\bsponsor|\bpartner|#ad\b|\bpaid promotion", re.I)
NOT_AD_LINE_RE = re.compile(r"\b(?:not|no|non|never|isn'?t|aren'?t|wasn'?t|without)\b[^.!?\n]{0,20}\b(?:sponsor|paid promotion|partner)", re.I)
# A standing legal disclaimer about sponsors in general ("Viewer's discretion is advised w.r.t. the products/services of
# the sponsors advertised on our platforms. We do not claim any responsibility..."), pasted under every video of a
# channel. It names no sponsor of this video; its capitalized first word became the brand hint "Viewer" (lyMTD1eAZAw).
DISCLAIMER_LINE_RE = re.compile(r"\bdiscretion\b|\bdisclaimer\b|\bresponsibilit(?:y|ies)\b|\bnot (?:be )?(?:held )?(?:liable|responsible)\b"
                                r"|\b(?:do|does|did)(?: not|n'?t) (?:claim|endorse|accept|guarantee)\b", re.I)
# ... unless the line also names this video's own sponsorship outright
STRONG_AD_LINE_RE = re.compile(r"\(ad\)|#ad\b|\bsponsored by\b|\bbrought to you by\b|\bin partnership with\b|\btoday'?s sponsor\b", re.I)


def is_ad_line(line):
    """A line that marks a sponsorship: '(ad)', 'sponsored by', 'in partnership with', '#ad'. A denial
    ('this video is not sponsored', 'no sponsor') is not one, and neither is a disclaimer about sponsors in general."""
    if not AD_LINE_RE.search(line) or NOT_AD_LINE_RE.search(line):
        return False
    return bool(STRONG_AD_LINE_RE.search(line)) or not DISCLAIMER_LINE_RE.search(line)
CHAPTER_RE = re.compile(r"^\s*(?:\d+:)?\d{1,2}:\d{2}\s*[-–—:]?\s*(.+)$")
STOPWORDS = {"the", "this", "video", "sponsored", "sponsor", "by", "thanks", "to", "for", "and", "with", "check", "out",
             "get", "use", "code", "off", "free", "trial", "link", "description", "below", "today", "ad", "our", "my",
             "try", "learn", "click", "visit", "download", "start", "join", "sign", "head", "watch", "subscribe", "thank"}


SOCIAL_HOSTS = ("youtube.", "youtu.be", "instagram.", "twitter.", "x.com", "tiktok.", "facebook.", "patreon.", "discord.",
                "twitch.", "reddit.", "spotify.", "apple.", "bit.ly", "linktr.ee", "threads.", "snapchat.", "linkedin.",
                "soundcloud.", "google.", "goo.gl", "amzn.to", "amazon.")
OFFER_LINE_RE = re.compile(r"\d+ ?(?:%|percent) off|discount|free trial|promo ?code|coupon|use (?:my|the|our) link", re.I)


def is_social(domain):
    d = domain.lower()
    if d.startswith("www."):
        d = d[4:]
    return d.startswith(SOCIAL_HOSTS)


def extract_hints(description):
    """Brands, promo codes, sponsor links and chapter titles from the video description.

    Only a SPONSOR LINE contributes brands and links: a line with an (ad)/sponsor/partner marker, a promo code or an
    offer, or the line right after such a line (a "Sponsor:" header followed by the link). Every other link in the
    description (the creator's own shop, socials, sources) is ignored: hint terms count as sponsor cues for the skip
    gate, so they must not include the creator's own things."""
    hints = {"brands": [], "codes": [], "domains": [], "chapters": [], "chapter_times": []}
    if not description:
        return hints
    prev_sponsor = False
    for line in description.splitlines():
        line = line.strip()
        if not line:
            prev_sponsor = False
            continue
        m = CHAPTER_RE.match(line)
        if m:
            hints["chapters"].append(m.group(1).strip()[:60])
            tm = re.match(r"^\s*((?:\d+:)?\d{1,2}:\d{2})", line)
            if tm:
                parts = [int(x) for x in tm.group(1).split(":")]
                secs = parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]
                hints.setdefault("chapter_times", []).append([secs, m.group(1).strip()[:60]])
        codes = [c for c in CODE_RE.findall(line) if stats.looks_like_code(c, line)]  # "code BELOW" is not a code
        hints["codes"].extend(codes)
        marked = is_ad_line(line)
        sponsor_line = marked or bool(codes) or bool(OFFER_LINE_RE.search(line))
        if sponsor_line or prev_sponsor:
            for d in DOMAIN_RE.findall(line):
                if not is_social(d):
                    hints["domains"].append(d[4:] if d.lower().startswith("www.") else d)
        if marked:
            caps = re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", line)
            first_tok = line.split()[0].strip(".,:;!?()[]\"'") if line.split() else ""
            for w in caps:
                if w.lower() in STOPWORDS or w in hints["codes"] or stats.is_generic(w):
                    continue  # "Coddy Pro Plan": Pro and Plan are not brands, and as hint terms they would be sponsor cues
                if w == first_tok and len(line.split()) > 3 and not re.match(rf"{re.escape(w)}\s*(\(ad\)|[:—–-])", line) \
                        and w.lower() not in line.lower()[len(w):]:
                    continue  # sentence-initial capital ("Learn to code with...") is usually not a brand;
                    # but "Coddy (ad): ..." or "Odoo: odoo.com/x" keeps it
                hints["brands"].append(w)
        prev_sponsor = sponsor_line
    for k in ("brands", "codes", "domains", "chapters"):
        seen = []
        for x in hints[k]:
            if x not in seen:
                seen.append(x)
        hints[k] = seen[:12]
    hints["chapter_times"] = sorted(hints.get("chapter_times", []))[:60]
    return hints


def hint_terms(hints, creator=""):
    """Words that, when they occur in the transcript, point at the sponsor: brands, codes, and the brand-bearing
    parts of sponsor links. Anything that is really the CREATOR's own name is dropped ('fern' from fern.deals/coddy,
    'HARRIS' / 'harris' for Johnny Harris): creators say their own name in ordinary content, and hint terms count
    as sponsor cues for the skip gate. A spoken promo code is still caught by the generic "code XYZ" cue."""
    cn = re.sub(r"[^a-z0-9]", "", (creator or "").lower())

    def own(term):
        tn = re.sub(r"[^a-z0-9]", "", term.lower())
        return bool(cn) and len(tn) >= 3 and (tn in cn or cn in tn)

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
        if t and t not in out and not own(t):
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
- affiliate: the creator pointing to affiliate/referral links for third-party products they are NOT being paid
  to feature in the video ("links below are affiliate links", "my Amazon storefront", "if you buy through my link").
- selfpromo: the creator's OWN products: merch, their own course, book, app, newsletter, podcast, game, their
  shop.
- membership: Patreon, channel membership, "join", Ko-fi, Super Thanks, Buy Me a Coffee: recurring support asks.
- crosspromo: promoting other videos, playlists, the creator's other channels or a friend's/collaborator's channel.
- charity: the creator's own fundraiser or donation appeal for a cause. RULE: if a charity or NGO is PAYING for the
  spot (sponsored by a charity), that is sponsor, not charity.
- giveaway: contests, giveaways, "enter to win".
(interaction, intro, outro and recaps are handled by rules, do not label them.)
Every segment of every category needs an evidence quote copied verbatim from the lines.

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
- first_words: the first 3-5 words of the promotion, copied EXACTLY from the transcript (the words right where the
  ad begins, e.g. "but first this video is"). last_words: the last 3-5 words of the promotion, copied exactly (e.g.
  "for 20 percent off"). Words after last_words are normal content. Use "" if not applicable.
- Keep evidence under 25 words and reason under 30 words.
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
                    "category": {"type": "string", "enum": ["sponsor", "affiliate", "selfpromo", "membership", "crosspromo", "charity", "giveaway"]},
                    "style": {"type": "string", "enum": ["read", "integration"]},
                    "brand": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                    "reason": {"type": "string"},
                    "first_words": {"type": "string"},
                    "last_words": {"type": "string"},
                },
                "required": ["start_line", "end_line", "category", "style", "brand", "confidence", "evidence", "reason",
                             "first_words", "last_words"],
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


def ollama_chat(user_content, schema=None, model=None, system=None, timeout=None):
    payload = {
        "model": model or MODEL,
        "messages": [{"role": "system", "content": system or SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
        "stream": False,
        "format": schema or OUTPUT_SCHEMA,
        "think": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout or LLM_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = (data.get("message") or {}).get("content", "")
    return json.loads(content), data


def ollama_chat_retry(user_content, schema=None, label="", model=None, system=None):
    try:
        return ollama_chat(user_content, schema, model, system)
    except json.JSONDecodeError:
        log(f"  {label}: model returned non-JSON, retrying once")
        return ollama_chat(user_content, schema, model, system)


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
    """Move the end to a natural cut, never past the sentence of the picked last ad word.
    1. If the sentence containing the last ad word ENDS inside the ad lines, extend to that sentence end
       (still ad; avoids cutting mid-sentence).
    2. If a pause (>= SNAP_GAP_S) follows that word directly, end at the start of the pause (no inset needed).
       A pause after any later word is never used: that word already belongs to the next sentence, which is content.
    Returns (time, note); time None if nothing to snap to (caller keeps pick - inset)."""
    words = all_words(lines, s_line, min(len(lines) - 1, e_line + 1))
    upto = [i for i, w in enumerate(words) if w[1] <= picked_end_t + 1e-6]
    if not upto:
        return None, "no words at/before pick"
    q = min(upto, key=lambda i: abs(words[i][1] - picked_end_t))
    notes = []
    se = sentence_end_index(words, q)
    if se > q and words[se][3] <= e_line:
        notes.append(f"extended to sentence end {words[se][2]!r} (+{se - q} word(s), inside ad lines)")
        q = se
    gap = words[q + 1][0] - words[q][1] if q + 1 < len(words) else 0.0
    if gap >= SNAP_GAP_S:
        notes.append(f"pause {gap:.2f}s after {words[q][2]!r}")
        return words[q][1], "; ".join(notes) + "; no inset needed"
    if notes:  # extended but no pause: end at the sentence's last word, keep the inset
        return max(words[q][0], words[q][1] - END_INSET_S), "; ".join(notes) + "; no pause, inset applied"
    return None, "no pause right after the pick; keeping pick"


def find_phrase(words, phrase):
    """Index range (i, j) of a verbatim phrase in a word list, if it occurs exactly once; else None."""
    ph = normalize(phrase)
    if len(ph) < 2:
        return None
    toks = [normalize(w[2]) for w in words]
    flat, owner = [], []
    for i, t in enumerate(toks):
        flat.extend(t)
        owner.extend([i] * len(t))
    n = len(ph)
    hits = [k for k in range(len(flat) - n + 1) if flat[k:k + n] == ph]
    if len(hits) != 1:
        return None
    return owner[hits[0]], owner[hits[0] + n - 1]


def apply_picks(lines, s_line, e_line, s_words, fw, e_words, lw, how):
    """Turn word picks (indices into the boundary contexts, or None) into snapped times. Returns (start, end, sure)."""
    ok_s, ok_e = fw is not None, lw is not None
    inward_start, inward_end = lines[s_line]["end"], lines[e_line]["start"]
    start = s_words[fw][0] if ok_s else inward_start
    end = (e_words[lw][1] - END_INSET_S) if ok_e else inward_end
    log(f"    start: {describe_boundary('S', s_words, fw, ok_s, lines, s_line)} -> {start:.2f}"
        f"{'' if ok_s else ' (INWARD FALLBACK: end of first ad line)'} [{how}]")
    log(f"    end:   {describe_boundary('E', e_words, lw, ok_e, lines, e_line)} -> {end:.2f}"
        f"{' (inset %.1fs)' % END_INSET_S if ok_e else ' (INWARD FALLBACK: start of last ad line)'} [{how}]")
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
    sure = ok_s and ok_e
    if end <= start:
        log("    end <= start after word picks; using inward line fallback")
        start, end, sure = inward_start, inward_end, False
    return round(start, 2), round(max(start, end), 2), sure


def trim_anchor(phrase, keep):
    """The model sometimes quotes across a sentence boundary ("...percent off. Okay, so back to..."). Keep only the
    ad side: for the end anchor the part before the first internal sentence end, for the start anchor the part
    after the last internal sentence end (a later start and an earlier end are the safe directions)."""
    toks = phrase.split()
    ends = [i for i, t in enumerate(toks[:-1]) if SENTENCE_END_RE.search(t)]
    if not ends:
        return phrase
    if keep == "head":
        toks = toks[:ends[0] + 1]
    else:
        toks = toks[ends[-1] + 1:]
    return " ".join(toks)


def anchor_words(lines, s_line, e_line, seg):
    """Merged path: locate the refine call's first_words / last_words in the boundary contexts.
    Returns (start, end, sure) or None if either anchor is missing or ambiguous (caller runs the word pass)."""
    n = len(lines)
    s_words, e_words = boundary_words(lines, s_line, n), boundary_words(lines, e_line, n)
    first_phrase = trim_anchor(seg.get("first_words", ""), "tail")
    last_phrase = trim_anchor(seg.get("last_words", ""), "head")
    if first_phrase != seg.get("first_words", "") or last_phrase != seg.get("last_words", ""):
        log(f"  anchors: trimmed at sentence boundary -> first={first_phrase!r} last={last_phrase!r}")
    a = find_phrase(s_words, first_phrase)
    b = find_phrase(e_words, last_phrase)
    log(f"  anchors L{s_line}/L{e_line}: first_words={seg.get('first_words', '')!r} -> "
        f"{'S%d' % a[0] if a else 'no unique match'}; last_words={seg.get('last_words', '')!r} -> "
        f"{'E%d' % b[1] if b else 'no unique match'}")
    if a:
        # A start that is too early skips real content, so the anchor must be followed quickly by a sponsor cue
        # (or a description hint). The refine call once anchored on a content sentence two lines before the ad.
        opener = " ".join(w[2] for w in s_words[a[0]:a[0] + ANCHOR_CUE_WORDS])
        cues = window_cues(opener, seg.get("hint_terms", []))
        if not cues:
            log(f"  anchors: start rejected, no sponsor cue within {ANCHOR_CUE_WORDS} words of {s_words[a[0]][2]!r}")
            a = None
        else:
            log(f"  anchors: start accepted, cue(s) {cues[:3]} follow {s_words[a[0]][2]!r}")
    if a and not b:
        seg["_start_anchor"] = a[0]   # the start is good; the word pass is only needed for the end (refine_words)
    if not a or not b:
        return None
    return apply_picks(lines, s_line, e_line, s_words, a[0], e_words, b[1], "anchors from refine call")


def prefer_start_anchor(lines, s_line, e_line, s_words, fw, anchor):
    """Which start word to use when the refine call's own start phrase was accepted (a sponsor cue follows it) but
    its end phrase was not unique, so the word pass ran. The word pass tends to answer with the first cue word and
    not with the segue that leads to it ("Do you actually know about today's sponsor already?"), and the snap then
    moves forward to the next sentence. The refine start is kept when it is earlier and close: in the same caption
    line as the start the word pass would give, or in the line right before it. Returns the word index."""
    n = len(lines)
    line_of = [i for i in range(max(0, s_line - BOUNDARY_LINES), min(n - 1, s_line + BOUNDARY_LINES) + 1)
               for _ in lines[i]["words"]]
    cur_t = snap_start(lines, s_line, e_line, s_words[fw][0])[0] if fw is not None else lines[s_line]["end"]
    cur_line = max((i for i, l in enumerate(lines) if l["start"] <= cur_t + 1e-6), default=s_line)
    a_t, a_line = s_words[anchor][0], line_of[anchor]
    if a_t < cur_t - 1e-6 and a_line in (cur_line, cur_line - 1):
        log(f"    start: keeping the refine call's start {s_words[anchor][2]!r} at {a_t:.2f} (L{a_line}) over the word "
            f"pass's {cur_t:.2f} (L{cur_line})")
        return anchor
    log(f"    start: refine's start {s_words[anchor][2]!r} at {a_t:.2f} (L{a_line}) not used "
        f"({'not earlier than' if a_t >= cur_t - 1e-6 else 'more than one line before'} the word pass's {cur_t:.2f}, L{cur_line})")
    return fw


def refine_words(lines, s_line, e_line, seg, calls, start_anchor=None):
    """Pass 2b: word-level boundaries. Returns (start, end, sure). start_anchor: the word index (in the start
    context) of the refine call's accepted start phrase, if it had one; see prefer_start_anchor.
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
        log(f"  words   L{s_line}/L{e_line}: answer first={result.get('first')!r}/{result.get('first_word')!r} "
            f"last={result.get('last')!r}/{result.get('last_word')!r} -> start rule: {rule_s}; end rule: {rule_e}"
            f"{' (REUSED from cache)' if reused else ''}")
    log(f"  words   L{s_line}/L{e_line} in {time.time() - t0:.1f}s")
    log(f"    S ctx: {start_ctx}")
    log(f"    E ctx: {end_ctx}")
    how = "labeled word pass"
    if start_anchor is not None and 0 <= start_anchor < len(s_words):
        kept = prefer_start_anchor(lines, s_line, e_line, s_words, fw, start_anchor)
        if kept != fw:
            fw, how = kept, "start from the refine call, end from the labeled word pass"
    return apply_picks(lines, s_line, e_line, s_words, fw, e_words, lw, how)


def model_loaded(name):
    """Is the model currently resident in Ollama (GET /api/ps)?"""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=2) as resp:
            ps = json.loads(resp.read().decode("utf-8"))
        return any(m.get("name") == name for m in ps.get("models", []))
    except (OSError, ValueError):
        return False


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
    """Server default: only sponsor reads skip; integrations keep; every other category is label-only ('show').
    The user re-maps categories client-side (skip / label / off) without re-analysis."""
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
        "category": raw.get("category") if raw.get("category") in MODEL_CATEGORIES else "sponsor",
        "style": raw.get("style") if raw.get("style") in ("read", "integration") else "read",
        "brand": scrub(str(raw.get("brand") or "")).strip()[:80],
        "confidence": conf,
        "evidence": scrub(str(raw.get("evidence") or "")).strip()[:1000],
        "reason": scrub(str(raw.get("reason") or "")).strip()[:1000],
        "first_words": scrub(str(raw.get("first_words") or "")).strip()[:200],
        "last_words": scrub(str(raw.get("last_words") or "")).strip()[:200],
    }
    if out["category"] == "charity" and paid_spot(out, lines):
        # "This episode is brought to you by Cancer Research UK": a charity PAYING for the spot is a sponsor read (the
        # rubric says so, but the model labels it by who the brand is). A charity cannot be an integration.
        out.update(category="sponsor", style="read", reason=f"[charity paying for the spot: sponsor] {out['reason']}"[:1000])
    out["action"] = action_for(out)
    return out


PAID_SPOT_RE = re.compile(r"\b(?:brought to you by|sponsored by|(?:supported|made possible|presented) by|in partnership with|"
                          r"partnered with|today'?s sponsor|thanks to [\w .'&-]{1,40} for sponsoring)\b", re.I)


def paid_spot(seg, lines):
    """Sponsorship language in the evidence quote or the segment's first lines: someone paid for this spot."""
    s, e = seg["start_line"], seg["end_line"]
    text = " ".join([seg.get("evidence", "")] + [lines[i]["text"] for i in range(s, min(e, s + 2) + 1)])
    return bool(PAID_SPOT_RE.search(text))


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
    """Every model-labeled segment needs verbatim evidence from the transcript. A sponsor skip additionally needs
    a sponsor cue in the quote; without it the skip is downgraded to show. Any other segment whose evidence is
    missing or not verbatim is dropped (seg['action'] = 'drop'). Rule and chapter segments are exempt."""
    if seg.get("source") in ("chapter", "rule"):
        return seg
    norm = " ".join(normalize_evidence(seg.get("evidence", "")))
    match = evidence_found(seg, lines)
    log(f"  evidence: raw={seg.get('evidence', '')!r}")
    log(f"            normalized={norm!r}")
    if not match:
        why = "no evidence given" if not seg.get("evidence") else "evidence not found verbatim in segment lines"
        if seg["action"] == "skip":
            log(f"  refine: downgraded skip -> show ({why})")
            seg["action"] = "show"
            seg["reason"] = f"[downgraded: {why}] " + seg["reason"]
            return seg
        log(f"  evidence: dropped {seg['category']} {fmt_time(seg['start'])}-{fmt_time(seg['end'])} ({why})")
        seg["action"] = "drop"
        return seg
    if seg["action"] != "skip":
        log(f"            {match}")
        return seg
    cues = window_cues(seg.get("evidence", ""), seg.get("hint_terms", []), sponsor_only=True)
    if cues:
        log(f"            {match}; cue(s) in quote: {cues[:4]}")
        return seg
    # The quote is verbatim but not itself a cue (the model quoted the pitch, not "it's an ad"): accept a STRONG
    # sponsor cue, or a description hint, anywhere in the segment's own lines instead.
    seg_text = " ".join(lines[i]["text"] for i in range(seg["start_line"], seg["end_line"] + 1))
    cues = [m.group(0) for m in STRONG_CUE_RE.finditer(seg_text)]
    low = seg_text.lower()
    cues += [h for h in seg.get("hint_terms", []) if h.lower() in low]
    if cues:
        log(f"            {match}; strong cue(s) in segment lines: {cues[:4]}")
        return seg
    why = "no sponsor cue in the quote or the segment"
    log(f"  refine: downgraded skip -> show ({why})")
    seg["action"] = "show"
    seg["reason"] = f"[downgraded: {why}] " + seg["reason"]
    return seg


def overlaps(a, b, gap=0.0):
    return a["start"] <= b["end"] + gap and b["start"] <= a["end"] + gap


def same_brand(a, b):
    return a["brand"].strip().lower() == b["brand"].strip().lower() and a.get("category") == b.get("category")


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
    keys = ("start", "end", "category", "style", "brand", "confidence", "evidence", "reason", "action", "line_start", "line_end",
            "source", "undisclosed", "disclosure", "brand_canonical", "lead_in_s", "lead_in")
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
        if seg["action"] == "drop":
            continue
        o_s, o_e = map_to_original(seg, alines, lines)
        if alines is not lines:
            log(f"  map: analysis L{seg['start_line']}-L{seg['end_line']} -> original L{o_s}-L{o_e} "
                f"({fmt_time(lines[o_s]['start'])}-{fmt_time(lines[o_e]['end'])})")
        if seg["action"] == "skip":
            picked = anchor_words(lines, o_s, o_e, seg) if alines is lines else None
            if picked is None:
                log("  anchors unresolved; running the labeled word pass")
                picked = refine_words(lines, o_s, o_e, seg, calls, seg.get("_start_anchor"))
            start, end, sure = picked
            if not sure and end - start < MIN_SKIP_S:
                log(f"  refine: boundaries uncertain and only {end - start:.1f}s left after inward fallback -> show")
                seg["action"] = "show"
                seg["reason"] = "[downgraded: uncertain boundaries] " + seg["reason"]
            else:
                seg["start"], seg["end"] = start, end
        seg.pop("hint_terms", None)
        seg.pop("_start_anchor", None)
        seg["source"] = cand.get("source", "detect")
        refined.append(seg)
    if not refined:
        log(f"  refine: candidate {fmt_time(cand['start'])}-{fmt_time(cand['end'])} {cand['brand']!r} withdrawn by model")
    return refined


LEADIN_SYSTEM = ("You decide where the lead-in to a sponsor read begins in a YouTube transcript. "
                 "Answer only with the JSON schema.")
LEADIN_PROMPT = """Video title: {title}

A sponsor read for {brand!r} is being cut out of this video. The cut starts at the text marked AD. Read what leads
up to it.

EARLIER (context only; it stays in the video):
... {context}

CANDIDATES, the sentences spoken right before the cut, in order:
{sentences}
AD: {opening} ...

Label every candidate as "content" or "lead-in".

content: the sentence is about the video's own subject. Explanation, story, facts, a conclusion, or the statement
of a problem are content, even when the ad then builds on them ("...there are just so many of these companies." and
then the ad "That's why I use X": the first sentence is content). So is a sentence that finishes a thought, and any
sentence that would still belong in the video if the ad were deleted.

lead-in: the sentence is only there because the ad follows. It is about the sponsor, or it stages the ad:
- an announcement of, or a question about, the sponsor ("but first...", "do you know today's sponsor?");
- a bridge whose only job is to turn toward the product ("speaking of which...", "so it's a happy coincidence that...");
- a staged setup for a testimonial: introducing the person or the situation from which the product is then
  recommended ("Look at Sam, one of our editors." / "Hey, I'm travelling right now." and then the ad begins
  "When I travel I always use ..."): both of those sentences are lead-in;
- small talk or an aside away from the video's subject that sets the product up.
If the ad were deleted, a lead-in sentence would be left hanging, pointing at nothing.

A candidate marked (in the ad's lines) was already counted as part of the promotion by an earlier pass that read
the whole passage: label it content only if it is clearly about the video's subject. For a candidate without that
mark, when it could be either, label it content.
reason: one short sentence."""


def sentences_before(lines, cut_t):
    """The whole sentences that end where the cut begins, oldest first: [(start_time, text)]. At most
    LEADIN_MAX_SENTENCES, each one starting no more than LEADIN_MAX_S before the cut. A sentence is what
    sentence_start_index says it is (end punctuation, or a pause on tracks without punctuation). If the cut is
    in the middle of a sentence, the part of that sentence before the cut is the last entry."""
    hi = max((i for i, l in enumerate(lines) if l["start"] <= cut_t + 1e-6), default=None)
    if hi is None:
        return []
    lo = min((i for i, l in enumerate(lines) if l["end"] > cut_t - 2 * LEADIN_MAX_S), default=hi)
    words = all_words(lines, lo, hi)
    end = next((i for i, w in enumerate(words) if w[0] >= cut_t - 1e-6), len(words))
    out = []
    while end > 0 and len(out) < LEADIN_MAX_SENTENCES:
        i = sentence_start_index(words, end - 1)
        if (i == 0 and lo > 0) or words[i][0] < cut_t - LEADIN_MAX_S - 1e-6:
            break   # its real start is outside what we looked at, or further back than the cap
        out.append((words[i][0], " ".join(w[2] for w in words[i:end])))
        end = i
    return out[::-1]


def check_leadin(job, seg):
    """One model call about the sentences right before a skip: are they a lead-in to the ad? If so the start moves
    back to the earliest lead-in sentence, by whole sentences and by at most LEADIN_MAX_S. The segment was already
    published with the start it had, so this can only improve a skip, never delay it. Anything short of a clear
    answer (a failed or timed-out call, an unparseable or unknown label, "none") leaves the start where it is.
    Returns True if the start moved."""
    lines, cut = job["lines"], seg["start"]
    rng = f"{fmt_time(seg['start'])}-{fmt_time(seg['end'])}"
    seg["leadin"] = "checked"
    if seg.get("action") != "skip":
        return False
    if job.get("alang") != job.get("lang"):
        log(f"  lead-in {rng}: not checked (the analysis runs on a translated track; the cut is on the original)")
        return False
    sents = sentences_before(lines, cut)
    if not sents:
        log(f"  lead-in {rng}: no whole sentence within {LEADIN_MAX_S:.0f}s before the cut")
        return False
    labels = "ABC"[:len(sents)]
    after = [w[2] for l in lines if l["end"] > cut for w in l["words"] if w[0] >= cut - 1e-6][:LEADIN_AD_WORDS]
    before = [w[2] for l in lines if l["start"] < sents[0][0] and l["end"] > sents[0][0] - 30 for w in l["words"]
              if w[0] < sents[0][0] - 1e-6][-LEADIN_AD_WORDS:]
    # The refine pass gave the promotion a line range that starts before the word-level cut. A candidate that lies
    # in those lines (by its midpoint) was already called promotion once, by a pass that read a wider context.
    ends = [t for t, _ in sents[1:]] + [cut]
    in_ad = [(t + e) / 2 >= seg.get("line_start", cut) - 1e-6 for (t, _), e in zip(sents, ends)]
    prompt = LEADIN_PROMPT.format(title=job.get("title", ""), brand=seg.get("brand", ""), context=" ".join(before),
                                  sentences="\n".join(f"{lab}{' (in the ad\'s lines)' if inside else ''}: {text}"
                                                      for lab, inside, (_, text) in zip(labels, in_ad, sents)),
                                  opening=" ".join(after))
    kinds = {"type": "string", "enum": ["content", "lead-in"]}
    schema = {"type": "object", "properties": {**{lab: kinds for lab in labels}, "reason": {"type": "string"}},
              "required": list(labels) + ["reason"]}
    t0 = time.time()

    def compute():
        with llm_lock:
            result, _meta = ollama_chat(prompt, schema, system=LEADIN_SYSTEM, timeout=LEADIN_TIMEOUT_S)
        return {**{lab: result.get(lab) for lab in labels}, "reason": str(result.get("reason") or "")[:200]}

    try:
        out, reused = job["calls"].get("leadin|" + hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:16], LEADIN_VERSION, compute)
    except (OSError, ValueError, AttributeError) as e:   # offline, timed out, not JSON, not an object
        log(f"  lead-in {rng}: check failed ({e!r}); start stays at {cut:.2f}")
        return False
    how = "REUSED from cache" if reused else f"in {time.time() - t0:.1f}s"
    verdicts = [out.get(lab) for lab in labels] if isinstance(out, dict) else []
    shown = " | ".join(f"{lab} [{v}]: {text}" for lab, v, (_, text) in zip(labels, verdicts or [None] * len(labels), sents))
    why = out.get("reason", "") if isinstance(out, dict) else ""
    if any(v not in ("content", "lead-in") for v in verdicts) or not verdicts:   # a missing or made-up label
        log(f"  lead-in {rng}: unusable answer {out!r} {how}; start stays at {cut:.2f}")
        return False
    # the lead-in is the unbroken run of lead-in sentences that ends at the cut; a content sentence stops it
    run = 0
    while run < len(verdicts) and verdicts[-1 - run] == "lead-in":
        run += 1
    if not run:
        log(f"  lead-in {rng}: none {how}; start stays at {cut:.2f}. {shown} -- {why}")
        return False
    pick = labels[len(labels) - run]
    new_start = sents[labels.index(pick)][0]
    floor = max([f["end"] for f in job["final"] if f is not seg and f.get("action") != "drop" and f["end"] <= cut] + [0.0])
    if not (cut - LEADIN_MAX_S - 1e-6 <= new_start < cut) or new_start < floor:
        log(f"  lead-in {rng}: answer {pick!r} would start at {new_start:.2f}, outside what is allowed; start stays at {cut:.2f}")
        return False
    added = [text for _, text in sents[labels.index(pick):]]
    seg["start"] = round(new_start, 2)
    seg["lead_in_s"] = round(cut - new_start, 1)
    seg["lead_in"] = {"from": round(cut, 2), "sentences": added}
    log(f"  lead-in {rng}: from {pick} {how}; start {cut:.2f} -> {new_start:.2f} (-{cut - new_start:.1f}s). {shown} -- {why}")
    return True


def queue_leadin_checks(job):
    """Every new skip gets a lead-in check as the job's very next unit. It was published before this is called, so
    the check never delays a skip; it can only republish it with an earlier start. Chapter skips keep the creator's
    own bounds."""
    new = [s for s in job["final"] if s.get("action") == "skip" and s.get("source") != "chapter" and "leadin" not in s]
    for s in reversed(new):
        s["leadin"] = "queued"
        job["pending"].insert(0, ("leadin", s))


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


def ad_markers(seg, lines, terms=(), paid=None):
    """The ad markers that back a sponsor label, [] if there are none: YouTube's paid-promotion flag when it was
    received and is true (the creator declared the video as paid, so an integration needs no code or link of its
    own); an ad marker or a description hint in the evidence quote or in the segment's lines (+-1); or the same
    brand next to an ad marker elsewhere in the video ("sponsored by X" at the top, X used later). Without any, the
    brand is just part of what the creator is telling. A flag that is false or was not received changes nothing."""
    if paid is True:
        return ["YouTube paid-promotion flag"]
    s0, s1 = seg.get("line_start", seg["start"]), seg.get("line_end", seg["end"])
    idx = [i for i, l in enumerate(lines) if l["end"] > s0 and l["start"] < s1]
    lo, hi = (max(0, idx[0] - 1), min(len(lines) - 1, idx[-1] + 1)) if idx else (0, -1)
    text = seg.get("evidence", "") + " " + " ".join(lines[i]["text"] for i in range(lo, hi + 1))
    found = [m.group(0) for m in AD_MARKER_RE.finditer(text)]
    found += [h for h in terms if h.lower() in text.lower()]
    brand = brand_terms(seg.get("brand", ""))
    if found or not brand:
        return found
    for i, l in enumerate(lines):
        if lo <= i <= hi or not any(f" {b} " in " " + " ".join(normalize(l["text"])) + " " for b in brand):
            continue
        m = AD_MARKER_RE.search(" ".join(lines[k]["text"] for k in range(max(0, i - 1), min(len(lines), i + 2))))
        if m:
            return [f"{m.group(0)} (with the brand at {fmt_time(l['start'])})"]
    return []


def post_filter(segments, title, lines, verbose=True, terms=(), paid=None):
    """Final safety net for keep/show markers (skips are left alone and only logged):
    - drop keep/show whose evidence/reason reads like a sponsor disclaimer;
    - drop keep/show whose brand is the video's subject (in the title, or mentioned in >= TOPIC_MENTIONS
      lines outside the segment);
    - drop a sponsor keep/show that has no ad marker (ad_markers): the creator's own brand work told as story;
    - drop show below SHOW_THRESHOLD."""
    out = []
    say = log if verbose else (lambda *a: None)
    for s in segments:
        rng = f"{fmt_time(s['start'])}-{fmt_time(s['end'])}"
        if s.get("source") == "rule":
            out.append(s)
            continue
        if s["action"] in ("keep", "show"):
            reason = re.sub(r"^\[downgraded:[^\]]*\]\s*", "", s.get("reason", ""))  # ignore our own annotations
            text = f"{s.get('evidence', '')} {reason}"
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
            if s.get("category") == "sponsor" and not ad_markers(s, lines, terms, paid):
                say(f"  filter: dropped {s['action']} {rng} {s['brand']!r}: sponsor label with no ad marker (no sponsorship "
                    f"language, call to action, link, code or offer, no description hint, and the video is not flagged "
                    f"as a paid promotion): story, not an ad")
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
                f.write("# Second Read — local results log (gitignored; one row per finished job)\n\n"
                        "| video | creator | lang | kind | expected | actual | verdict | notes | job time | calls | first skip | when |\n"
                        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
            f.write(row)
    except OSError as e:
        log(f"results.md append failed: {e}")


# ---------------------------------------------------------------------------
# Feedback (wrong skip / missed ad), appended to a gitignored jsonl with the pipeline context
# ---------------------------------------------------------------------------
FEEDBACK_KINDS = ("false_skip", "missed_ad")


def segment_context(video_id, seg):
    """Evidence/reason for the reported segment from the cached result (matched by time overlap)."""
    cached = load_cache(video_id)
    if not cached or not seg:
        return {}
    for s in cached.get("segments", []):
        if s["start"] <= seg.get("end", 0) and seg.get("start", 0) <= s["end"]:
            return {k: s.get(k) for k in ("evidence", "reason", "line_start", "line_end", "source")}
    return {}


def record_feedback(data):
    """Validate and append one report. A false_skip must carry the skipped segment; a missed_ad must carry the
    playhead. Anything less is rejected, so the file never holds rows that cannot be acted on."""
    kind = str(data.get("kind") or "")
    if kind not in FEEDBACK_KINDS:
        return None, "bad kind"
    vid = str(data.get("videoId") or "")
    if not VIDEO_ID_RE.match(vid):
        return None, "bad video id"
    seg = data.get("segment") if isinstance(data.get("segment"), dict) else None
    client_ctx = {}
    if seg:
        st, en = finite(seg.get("start")), finite(seg.get("end"))
        if st is None or en is None or en <= st or st < 0:
            seg = None
        else:
            client_ctx = {"evidence": str(seg.get("evidence") or "")[:1000], "reason": str(seg.get("reason") or "")[:1000],
                          "source": str(seg.get("source") or "")[:20],
                          "line_start": finite(seg.get("line_start")), "line_end": finite(seg.get("line_end"))}
            seg = {"start": st, "end": en,
                   "category": str(seg.get("category") or "")[:20], "style": str(seg.get("style") or "")[:20],
                   "brand": str(seg.get("brand") or "")[:80], "brand_canonical": str(seg.get("brand_canonical") or "")[:80],
                   "action": str(seg.get("action") or "")[:10],
                   "confidence": max(0.0, min(1.0, finite(seg.get("confidence"), 0.0)))}
    playhead = finite(data.get("playhead"))
    if kind == "false_skip" and not seg:
        return None, "false_skip needs the skipped segment (start < end)"
    if playhead is None or playhead < 0:
        return None, "playhead missing"
    playhead = round(playhead, 2)
    # What the pipeline said about this segment: the cached result first, the client's copy as a fallback.
    ctx = {k: v for k, v in segment_context(vid, seg).items() if v not in (None, "")}
    for k, v in client_ctx.items():
        if v not in (None, "") and k not in ctx:
            ctx[k] = v
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "videoId": vid,
        "channelId": str(data.get("channelId") or "")[:40], "channelName": str(data.get("channelName") or "")[:100],
        "title": str(data.get("title") or "")[:200], "playhead": playhead, "segment": seg,
        "context": ctx, "versions": versions(), "model": MODEL,
    }
    os.makedirs(os.path.dirname(FEEDBACK_JSONL), exist_ok=True)
    with open(FEEDBACK_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    where = f"{fmt_time(seg['start'])}-{fmt_time(seg['end'])} {seg['brand']!r} (playhead {fmt_time(playhead)})" if seg else f"at {fmt_time(playhead)}"
    log(f"[{vid}] FEEDBACK {kind}: {where}")
    try:
        stats.record_event({"kind": kind, "videoId": vid, "channelId": entry["channelId"], "segment": seg,
                            "playhead": playhead, "channelName": entry["channelName"], "title": entry["title"]})
    except Exception as e:  # noqa: BLE001
        log(f"stats: event failed: {e!r}")
    return entry, None


def feedback_complete(e):
    """Rows written before validation existed may lack what makes them usable."""
    seg = e.get("segment")
    if e.get("kind") == "false_skip":
        return bool(seg) and finite(seg.get("start")) is not None and finite(seg.get("end")) is not None
    return finite(e.get("playhead")) is not None


def load_feedback():
    out = []
    try:
        with open(FEEDBACK_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def feedback_report():
    """Summary table for tuning sessions."""
    everything = load_feedback()
    fb = [e for e in everything if feedback_complete(e)]
    incomplete = len(everything) - len(fb)
    if incomplete:
        print(f"{incomplete} incomplete row(s) ignored (no segment / no playhead; written before validation)")
    if not fb:
        print(f"no feedback yet ({FEEDBACK_JSONL})")
        return
    by_kind = {}
    for e in fb:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    print(f"{len(fb)} report(s): " + ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))
    print()
    print(f"{'when':16} {'kind':10} {'video':11} {'where':13} {'at':>6} {'brand':14} {'conf':5} {'src':6} {'ver':7} evidence / reason")
    print("-" * 128)
    for e in fb:
        seg, ctx = e.get("segment") or {}, e.get("context") or {}
        if seg and (finite(seg.get("start")) is None or finite(seg.get("end")) is None):
            seg = {}
        where = f"{fmt_time(seg['start'])}-{fmt_time(seg['end'])}" if seg else f"@{fmt_time(finite(e.get('playhead'), 0.0))}"
        ver = "/".join(str(e.get("versions", {}).get(k, "?")) for k in ("detect", "refine", "post"))
        text = (ctx.get("evidence") or "")[:50] + (" | " + (ctx.get("reason") or "")[:60] if ctx.get("reason") else "")
        print(f"{e['ts'][:16]:16} {e['kind']:10} {e['videoId']:11} {where:13} {fmt_time(finite(e.get('playhead'), 0.0)):>6} "
              f"{(seg.get('brand_canonical') or seg.get('brand') or '')[:14]:14} "
              f"{(('%.2f' % seg['confidence']) if seg else ''):5} {(ctx.get('source') or '')[:6]:6} {ver:7} {text}")
    print()
    videos = {}
    for e in fb:
        videos.setdefault(e["videoId"], []).append(e["kind"])
    print("per video: " + ", ".join(f"{v} ({', '.join(sorted(set(k)))})" for v, k in videos.items()))
    brands = {}
    for e in fb:
        b = (e.get("segment") or {}).get("brand_canonical") or (e.get("segment") or {}).get("brand")
        if e.get("kind") == "false_skip" and b:
            brands[b] = brands.get(b, 0) + 1
    if brands:
        print("wrong skips by brand: " + ", ".join(f"{b}={n}" for b, n in sorted(brands.items(), key=lambda x: -x[1])))


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
    return {"detect": DETECT_VERSION, "refine": REFINE_VERSION, "post": POST_VERSION, "leadin": LEADIN_VERSION}


def inputs_digest(thash, title, description, paid, alines, lines=()):
    """Everything besides the transcript text that changes the result: title and description (hints, chapters,
    disclosure), the paid-promotion flag, the analysis track, and the word timings (the cuts are made of them, and
    the transcript hash only covers line starts and text). A POST that brings a different digest for the same
    transcript is re-processed (model calls are reused where their own inputs are unchanged), so a job or cache
    entry created without metadata, or from an older build's word estimates, can never mask a later request."""
    words = hashlib.sha1()
    for l in lines:
        words.update(json.dumps(l.get("words") or []).encode("utf-8"))
    h = hashlib.sha1()
    h.update(json.dumps([thash, title, description, paid, transcript_hash(alines) if alines else "", words.hexdigest()],
                        ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:12]


instant_served = {}   # video id -> signature of the segments served by GET /stored, until the POST validates them
INSTANT_SERVED_MAX = 64
# GET /stored answers "has this video been analyzed?" by id and remembers that it answered. A request with no Origin
# is normally let in (curl, the dashboard), and a web page can make one: <img> and <script> loads carry no Origin,
# and whether they load or fail would tell the page which videos were watched. So this endpoint also wants a header
# that only a scripted request can carry. A page cannot put it on an <img>/<script> load, and a fetch() with it
# needs a CORS preflight, which this server never answers.
EXTENSION_HEADER, EXTENSION_HEADER_VALUE = "X-SR-Client", "extension"


def extension_request(headers):
    return (headers.get(EXTENSION_HEADER) or "") == EXTENSION_HEADER_VALUE


def segments_signature(segments):
    return sorted((s.get("action"), s.get("category"), round(s.get("start", 0), 1), round(s.get("end", 0), 1)) for s in segments or [])


def stored_result(video_id, build):
    """The finished result for a video, by id alone, for the instant revisit: (segments, where) or (None, why).
    Only a complete result made by the current detect/refine/post/leadin versions and model is served, and only
    to a client on this server's build. The transcript and the request metadata are not checked here: the POST
    that follows the caption read does that and confirms or replaces what was served."""
    if build != CLIENT_BUILD:
        return None, f"client build {build} is not {CLIENT_BUILD}"
    with jobs_lock:
        job = jobs.get(video_id)
        if job and job.get("status") == "done":
            return list(job.get("segments") or []), "memory"
    cached = load_cache(video_id)
    if not cached:
        return None, "nothing stored"
    if cached.get("partial") or cached.get("versions") != versions() or cached.get("model") != MODEL:
        return None, "stored by other versions"
    return list(cached.get("segments") or []), "disk"


STORED_DURATION_TOL_S = 2.0   # the page's lengthSeconds is whole seconds, the stored duration is the player's float


def stored_matches(video_id, duration, desc_hash):
    """Is the stored result still for this video as the page reports it now? (matches, why). The check that lets
    the extension skip the caption read altogether: same video id (the lookup), same duration (within
    STORED_DURATION_TOL_S) and the same description (by SHA-1 of its text, first 12 hex digits). A stored result
    without a duration never matches."""
    with jobs_lock:
        job = jobs.get(video_id)
        known = (job.get("duration"), job.get("description") or "") if job and job.get("status") == "done" else None
    if known is None:
        meta = (load_cache(video_id) or {}).get("meta") or {}
        known = (meta.get("duration"), meta.get("description") or "")
    stored_duration, stored_description = known
    if not stored_duration:
        return False, "the stored result has no duration"
    if duration is None or abs(float(stored_duration) - duration) > STORED_DURATION_TOL_S:
        return False, f"duration {'?' if duration is None else '%.0f s' % duration}, stored {float(stored_duration):.0f} s"
    if hashlib.sha1(stored_description.encode("utf-8")).hexdigest()[:12] != desc_hash:
        return False, "the description changed"
    return True, "video id, duration and description match the stored result"


def settle_instant(video_id, segments, how):
    """The validated answer for a video whose stored result was served instantly: say whether it held."""
    served = instant_served.pop(video_id, None)
    if served is None:
        return
    now = segments_signature(segments)
    if now == served:
        log(f"[{video_id}] cache: confirmed ({how})")
    else:
        log(f"[{video_id}] cache: replaced ({how}): served {served}, now {now}")


def cache_valid(cached, thash, inputs=None):
    """Final segments can be served as-is only if nothing changed."""
    return (bool(cached) and cached.get("hash") == thash and cached.get("model") == MODEL
            and cached.get("versions") == versions() and cached.get("inputs") == inputs)


def clean_capture(raw):
    """page.js's report of how it got the captions (track, path, request parameters, counts). Diagnostics only:
    kept small and flat, never interpreted."""
    if not isinstance(raw, dict):
        return None

    def text(v, n=40):
        return re.sub(r"[^\w .:,/=+-]", "?", str(v))[:n]
    out = {k: text(raw.get(k)) for k in ("path", "via", "how", "retry", "picked") if raw.get(k) not in (None, "")}
    if raw.get("timing"):
        out["timing"] = text(raw["timing"], 60)
    for k in ("events", "withSegs", "withOffset", "multiSeg", "lines", "timed", "lastEnd", "videoSeconds", "timingLines"):
        if isinstance(raw.get(k), (int, float)) and not isinstance(raw.get(k), bool):
            out[k] = int(raw[k])
    if isinstance(raw.get("tracks"), list):
        out["tracks"] = [text(t, 12) for t in raw["tracks"][:16]]
    req = raw.get("request") if isinstance(raw.get("request"), dict) else {}
    if isinstance(req.get("params"), dict):
        out["params"] = {text(k, 12): text(v, 24) for k, v in list(req["params"].items())[:12]}
    if isinstance(req.get("others"), list):
        out["others"] = [text(k, 16) for k in req["others"][:40]]
    return out or None


def log_capture(video_id, cap):
    """One line saying which caption track was read and how, so two different transcripts for one video can be
    told apart afterwards (lang and kind alone could not: both reads of vpn3VmGvIXA were 'en asr')."""
    if not cap:
        log(f"[{video_id}] captions: no capture report from page.js (older build)")
        return
    params = " ".join(f"{k}={v}" for k, v in (cap.get("params") or {}).items())
    log(f"[{video_id}] captions: path {cap.get('path', '?')} via {cap.get('via', '?')} ({cap.get('how', '?')}); "
        f"picked {cap.get('picked', '?')} of [{', '.join(cap.get('tracks') or [])}]; request {params or '?'} "
        f"(+{len(cap.get('others') or [])} other params); {cap.get('events', '?')} events, "
        f"{cap.get('withOffset', '?')} with word offsets, {cap.get('multiSeg', '?')} multi-seg; "
        f"{cap.get('lines', '?')} lines ({cap.get('timed', '?')} timed), ends {fmt_time(cap.get('lastEnd') or 0)} of "
        f"{fmt_time(cap.get('videoSeconds') or 0)}{'; second read: ' + cap['retry'] if cap.get('retry') else ''}"
        f"; cut placement: {cap.get('timing', 'not reported')}"
        f"{' (%d lines)' % cap['timingLines'] if cap.get('timingLines') else ''}")


def log_meta(video_id, title, description, meta, position, hints):
    """One line per POST with the presence (lengths only) of every metadata field, plus loud warnings when the
    extension did not deliver something it always should."""
    client = meta.get("client") or {}
    builds = ", ".join(f"{k} {client[k] if client.get(k) is not None else '?'}" for k in ("page", "content", "background"))
    dur = meta.get("duration")
    paid = meta.get("paid_promotion")
    log(f"[{video_id}] meta (lengths): client=[{builds}] title={len(title)} creator={len(meta.get('creator', ''))} "
        f"channelId={len(meta.get('channel_id', ''))} kind={meta.get('kind') or 'MISSING'} description={len(description)} "
        f"chapters={len(hints.get('chapter_times', []))} duration={'%.0fs' % dur if dur else 'MISSING'} "
        f"heat={len(meta.get('heat') or [])} paidPromotion={'MISSING' if paid is None else paid} position={fmt_time(position)}")
    missing = [name for name, ok in (("title", title), ("creator", meta.get("creator")), ("channelId", meta.get("channel_id")),
                                     ("kind", meta.get("kind")), ("duration", dur), ("paidPromotion", paid is not None),
                                     ("description", description)) if not ok]
    if missing:
        log(f"[{video_id}] WARNING: metadata missing: {', '.join(missing)}. Hints, brand names, creator stats and the "
            f"disclosure check depend on it.")
    log_capture(video_id, meta.get("capture"))
    seen = [client.get(k) for k in ("page", "content", "background")]
    if any(b != CLIENT_BUILD for b in seen):
        log(f"[{video_id}] WARNING: extension builds [{builds}] do not all match this server's CLIENT_BUILD {CLIENT_BUILD}: "
            f"reload the extension on chrome://extensions, then refresh the YouTube tab (or restart the server if it is the older one).")
    return missing


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


def hint_candidates(job):
    """Fast path: description hints (brand/code/domain) that the transcript mentions. Mentions closer than
    HINT_CLUSTER_GAP_S form one region, which goes straight to refine (no pass-1 detect)."""
    alines, terms, hints = job["alines"], job["terms"], job["hints"]
    if not terms:
        return []
    lows = [" " + " ".join(normalize(l["text"])) + " " for l in alines]
    usable, too_common = [], []
    for t in terms:
        tn = " ".join(normalize(t))
        if len(tn) < 4:
            continue
        n = sum(1 for low in lows if f" {tn} " in low)
        if n > MAX_HINT_LINES:
            log(f"[{job['video_id']}] hint term {t!r} matches {n} lines: too common, dropped as a hint and as a cue")
            too_common.append(t)
            continue
        if n:
            usable.append((t, tn))
    if too_common:
        job["terms"] = [t for t in terms if t not in too_common]
    hits = []  # (line index, term)
    for i, low in enumerate(lows):
        for t, tn in usable:
            if f" {tn} " in low:
                hits.append((i, t))
                break
    cands = []
    for i, t in hits:
        if cands and alines[i]["start"] - alines[cands[-1]["end_line"]]["end"] <= HINT_CLUSTER_GAP_S \
                and alines[i]["end"] - cands[-1]["start"] <= MAX_HINT_REGION_S:
            c = cands[-1]
            c["end_line"] = i
            c["end"] = alines[i]["end"]
            c["terms"].append(t)
        else:
            cands.append({"start_line": i, "end_line": i, "start": alines[i]["start"], "end": alines[i]["end"], "terms": [t]})
    out = []
    for c in cands:
        brand = next((t for t in c["terms"] if t in hints["brands"]), None) or \
                next((t for t in c["terms"] if t not in hints["codes"]), c["terms"][0])
        out.append({"start_line": c["start_line"], "end_line": c["end_line"], "start": c["start"], "end": c["end"],
                    "category": "sponsor", "style": "read", "brand": brand, "confidence": 0.0,
                    "evidence": "", "reason": f"description hint(s) {sorted(set(c['terms']))} mentioned in transcript",
                    "action": "show", "source": "hint"})
    return out


def chapter_segments(job):
    """7a: a chapter titled like an ad/sponsor becomes a skip segment from its bounds, immediately, no model call.
    Bounds are snapped to pauses on the original track. Over MAX_CHAPTER_AD_S the chapter is ignored (the model
    path still runs there)."""
    lines, alines = job["lines"], job["alines"]
    chapters = job.get("chapter_times") or []
    duration = job.get("duration") or (lines[-1]["end"] if lines else 0)
    out = []
    for i, (cs, title) in enumerate(chapters):
        if not CHAPTER_AD_RE.search(title):
            continue
        ce = chapters[i + 1][0] if i + 1 < len(chapters) else duration
        if ce <= cs or ce - cs > MAX_CHAPTER_AD_S:
            log(f"[{job['video_id']}] chapter {title!r} {fmt_time(cs)}-{fmt_time(ce)} ignored ({'empty' if ce <= cs else 'longer than %ds' % MAX_CHAPTER_AD_S})")
            continue
        o_idx = [k for k, l in enumerate(lines) if l["end"] > cs and l["start"] < ce]
        a_idx = [k for k, l in enumerate(alines) if l["end"] > cs and l["start"] < ce]
        if not o_idx or not a_idx:
            continue
        o_s, o_e = o_idx[0], o_idx[-1]
        start, s_note = snap_start(lines, o_s, o_e, cs)
        end, e_note = snap_end(lines, o_s, o_e, ce)
        if end is None or end > ce:
            end, e_note = ce, "chapter end (snap would extend past it)" if end is not None else "chapter end"
        start = max(cs - 1.0, min(start, cs + 5.0))  # never earlier than 1s before, nor later than 5s after, the chapter start
        if end - start < MIN_SKIP_S:
            continue
        brands = job["hints"].get("brands") or []
        seg = {"start": round(start, 2), "end": round(end, 2), "start_line": a_idx[0], "end_line": a_idx[-1],
               "category": "sponsor", "style": "read", "brand": brands[0] if len(brands) == 1 else "",
               "confidence": 1.0, "evidence": f"chapter: {title}", "reason": f"creator-labeled chapter {title!r} {fmt_time(cs)}-{fmt_time(ce)}",
               "line_start": cs, "line_end": ce, "source": "chapter"}
        seg["action"] = action_for(seg)
        out.append(seg)
        log(f"[{job['video_id']}] chapter fast path: {title!r} {fmt_time(cs)}-{fmt_time(ce)} -> SKIP {fmt_time(start)}-{fmt_time(end)} "
            f"(snap start: {s_note}; snap end: {e_note}); no model call")
    return out


def plan_job(job):
    """Build the ordered list of units to run, done once per job: chapter fast path (no model call), hint regions
    (straight to refine), then prefiltered detect windows in playhead order."""
    alines, position = job["alines"], job.get("position", 0.0)
    wins = windows(alines)
    chapter_segs = chapter_segments(job)
    if chapter_segs:
        job["final"].extend(chapter_segs)  # published below, once the plan exists (still before any model call)
    job["final"].extend(rule_segments(job))
    hint_cands = hint_candidates(job)  # may drop too-common terms from job["terms"]
    terms = job["terms"]
    regions = [(c["start"] - HINT_REGION_PAD_S, c["end"] + HINT_REGION_PAD_S) for c in hint_cands]
    regions += [(c["start"] - HINT_REGION_PAD_S, c["end"] + HINT_REGION_PAD_S) for c in chapter_segs]
    sent, deferred = [], []
    brand_terms_only = [t for t in terms if t in job["hints"]["brands"]]
    for first, last in wins:
        w_start, w_end = alines[first]["start"], alines[last]["end"]
        text = " ".join(alines[i]["text"] for i in range(first, last + 1))
        if not window_cues(text, terms):
            low = text.lower()
            named = [b for b in brand_terms_only if b.lower() in low]
            if named:
                log(f"[{job['video_id']}] prefilter: POSSIBLE MISS, skipped window "
                    f"{fmt_time(w_start)}-{fmt_time(w_end)} mentions hint brand {named}")
            continue
        if any(w_start < hi and w_end > lo for lo, hi in regions):
            deferred.append((first, last))  # a hint region covers it: runs last, and only if the hint refine found nothing there
        else:
            sent.append((first, last))
    units = ([("hint", c) for c in hint_cands]
             + [("win", f, l, False) for f, l in order_windows(sent, alines, position, terms)]
             + [("win", f, l, True) for f, l in order_windows(deferred, alines, position, terms)])
    with jobs_lock:
        job["n_windows"] = len(wins)
        job["n_sent"] = len(sent) + len(deferred)
        job["pending"] = units
        job["units_all"] = list(units)
    log(f"[{job['video_id']}] prefilter: {len(sent) + len(deferred)} of {len(wins)} windows sent"
        f"{f', {len(deferred)} deferred behind {len(hint_cands)} hint region(s)' if hint_cands else ''}"
        f"{'; hints: ' + ', '.join(terms[:8]) if terms else ''}; playhead {fmt_time(position)}; "
        f"order: {[('hint ' if u[0] == 'hint' else '') + fmt_time(alines[u[1]['start_line'] if u[0] == 'hint' else u[1]]['start']) for u in units]}")
    for c in hint_cands:
        log(f"[{job['video_id']}] hint region {fmt_time(c['start'])}-{fmt_time(c['end'])} ({c['reason']}) -> refine directly")
    if job["final"]:
        publish(job)


def run_unit(job, unit):
    if unit[0] == "leadin":
        check_leadin(job, unit[1])
        return
    if unit[0] == "hint":
        cand = unit[1]
        if any(overlaps(cand, f) for f in job["final"]):
            return
        job["final"].extend(refine_candidate(job["alines"], job["lines"], cand, job["alang"], job["title"],
                                             job["calls"], job["header"], job["terms"]))
        return
    _, first, last, deferred = unit
    if deferred:
        alines = job["alines"]
        w = {"start": alines[first]["start"], "end": alines[last]["end"]}
        if any(overlaps(w, f) and f["category"] == "sponsor" for f in job["final"]):
            log(f"  detect  L{first}-L{last}: skipped, hint-region refine already found a sponsor segment here")
            return
        log(f"  detect  L{first}-L{last}: hint region produced nothing here; running detect after all")
    run_window(job, first, last)


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
            cand["hint_terms"] = list(terms)
            require_evidence(cand, alines)  # label-only categories still need a verbatim quote
            cand.pop("hint_terms", None)
            if cand["action"] != "drop":
                cand["source"] = "detect"
                final.append(cand)
            continue
        final.extend(refine_candidate(alines, lines, cand, lang, title, calls, header, terms))


def disclosure_state(paid, described):
    """'disclosed' if either signal is positive; 'undisclosed' only when BOTH are known and negative; otherwise
    'unknown'. Missing data must never read as an accusation."""
    if paid is True or described is True:
        return "disclosed"
    if paid is False and described is False:
        return "undisclosed"
    return "unknown"


def finalize(job, verbose):
    out = []
    kept = [x for x in job["final"] if x.get("action") != "drop"]
    state = disclosure_state(job.get("paid_promotion"), job.get("disclosed"))
    for x in post_filter(merge_segments(kept), job["title"], job["alines"], verbose, job.get("terms") or (),
                         job.get("paid_promotion")):
        if x.get("action") == "skip" and x["end"] - x["start"] < MIN_SKIP_S:
            # "Part of this video is sponsored by Shopify." (bCmwCkNY85g, 2 s) is a disclosure line, not a read: a seek
            # that short gains nothing and jars. A label instead. (A copy: the lead-in check may still lengthen it.)
            if verbose:
                log(f"  filter: {fmt_time(x['start'])}-{fmt_time(x['end'])} {x.get('brand')!r} is {x['end'] - x['start']:.1f}s "
                    f"(< {MIN_SKIP_S:.0f}s): a disclosure line, labeled instead of skipped")
            x = {**x, "action": "show", "reason": f"[under {MIN_SKIP_S:.0f}s: a disclosure, not a read] {x.get('reason', '')}"[:1000]}
        x.pop("undisclosed", None)
        if x.get("category") == "sponsor" and x.get("style") == "read":
            x["disclosure"] = state
            if state == "undisclosed":
                x["undisclosed"] = True
        out.append(x)
    return [public_segment(x) for x in out]


def publish(job):
    with jobs_lock:
        if jobs.get(job["video_id"]) is not job:
            return
        job["segments"] = finalize(job, verbose=False)
        total = job["n_sent"] + sum(1 for u in job.get("units_all", []) if u[0] == "hint")
        left = len(job["pending"]) if job.get("pending") is not None else total
        job["progress"] = f"{max(0, total - left)}/{total}"
        if job["ttfs"] is None and any(x["action"] == "skip" for x in job["segments"]):
            job["ttfs"] = time.time() - job["t_start"]
            log(f"[{job['video_id']}] first skip published after {job['ttfs']:.1f}s")


def save_partial(job):
    """Persist model outputs so a paused/evicted job resumes without repeating Ollama calls."""
    save_cache(job["video_id"], {"videoId": job["video_id"], "versions": None, "hash": job["hash"], "inputs": job.get("inputs"),
                                 "lang": job["lang"], "title": job["title"], "meta": job_meta(job), "model": MODEL,
                                 "segments": job["segments"], "calls": job["calls"].calls, "transcript": job["lines"],
                                 "partial": True})


def finish_job(job):
    video_id = job["video_id"]
    segments = finalize(job, verbose=True)
    with jobs_lock:
        if jobs.get(video_id) is not job:
            log(f"[{video_id}] job superseded; discarding result")
            return
    log_segments(video_id, segments)
    settle_instant(video_id, segments, "re-analyzed: the transcript, the metadata or the versions differ from the stored result")
    sync_stats(job, segments)  # also sets brand_canonical on each segment
    save_cache(video_id, {"videoId": video_id, "versions": versions(), "hash": job["hash"], "inputs": job.get("inputs"),
                          "lang": job["lang"], "title": job["title"], "meta": job_meta(job), "model": MODEL,
                          "segments": segments, "calls": job["calls"].calls, "transcript": job["lines"]})
    job_time = time.time() - job["t_start"]
    if job["ttfs"] is None and any(x["action"] == "skip" for x in segments):
        job["ttfs"] = job_time
    with jobs_lock:
        job["segments"] = segments
        job["status"] = "done"
        job["paused"] = False
    calls = job["calls"]
    if any(x.get("disclosure") for x in segments):
        paid, described = job.get("paid_promotion"), job.get("disclosed")
        state = disclosure_state(paid, described)
        log(f"[{video_id}] disclosure: {state} (YouTube paid-promotion flag: {'not reported' if paid is None else paid}; "
            f"(ad)/sponsor line in description: {'no description received' if described is None else described})")
    log(f"[{video_id}] done: {len(segments)} segment(s), {job['n_sent']}/{job['n_windows']} window(s) sent, "
        f"model calls: {calls.reused} reused / {calls.computed} computed, {job_time:.1f}s wall "
        f"({job['work_s']:.1f}s working), first skip at {'%.1fs' % job['ttfs'] if job['ttfs'] is not None else '-'}, cached")
    append_result(job, segments, job_time, calls, job["ttfs"])
    with work_available:
        work_available.notify()  # wake the worker for idle-time tier-2 analysis


def job_meta(job):
    """Request metadata kept in the cache file (gitignored) so a video can be replayed exactly as it was sent."""
    return {"creator": job.get("creator", ""), "channelId": job.get("channel_id", ""), "kind": job.get("kind", ""),
            "duration": job.get("duration"), "paidPromotion": job.get("paid_promotion"),
            "description": job.get("description", ""), "client": job.get("client"), "capture": job.get("capture")}


def sync_stats(job, segments):
    """Record the video, its segments (canonical brands), codes and tier-1 ad metrics. Idempotent: segment ids are
    stable, so it also runs for cache hits and when a later request brings metadata the first one lacked."""
    video_id = job["video_id"]
    try:
        seg_ids = stats.record_video(job, segments, versions())
        n_codes = stats.record_codes(job, segments)
        if n_codes:
            log(f"[{video_id}] codes: {n_codes} new code/link/offer row(s) in the vault")
        intel.compute_tier1(job, segments, seg_ids, job.get("terms", []))
    except Exception as e:  # noqa: BLE001 - stats must never break analysis
        log(f"[{video_id}] stats/intel: failed: {e!r}")


META_FIELDS = ("creator", "channel_id", "kind", "duration", "heat", "client")


def apply_meta(job, meta):
    """Fill metadata an existing job is missing from a newer request. Returns the names that changed."""
    changed = []
    for k in META_FIELDS:
        new = meta.get(k)
        if new and not job.get(k):
            job[k] = new
            changed.append(k)
    return changed


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
                left = f"{len(job['pending'])} unit(s) left" if job["pending"] is not None else "not started"
                log(f"[{job['video_id']}] paused (not polled for {PAUSE_AFTER_S:.0f}s); {left}, partial results cached")
            continue
        if best is None or job["last_poll"] > best["last_poll"]:
            best = job
    return best


def worker_step():
    """One scheduling step. Returns the job it worked on (or None if idle)."""
    pending = None
    with work_available:
        job = pick_job()
        if job is None:
            pending = intel.pending_analysis(1)
            if not pending:
                work_available.wait(timeout=1.0)
                return None
        else:
            if job.get("paused"):
                job["paused"] = False
                log(f"[{job['video_id']}] resumed")
            job["status"] = "classifying"
            needs_plan = job["pending"] is None
            unit = None if needs_plan else (job["pending"].pop(0) if job["pending"] else None)
    if job is None:
        # Idle: one tier-2 ad analysis. Real jobs win again as soon as one is being polled.
        try:
            intel.run_analysis(pending[0], analysis_chat, load_transcript)
        except Exception as e:  # noqa: BLE001
            log(f"intel: tier-2 step failed: {e!r}")
        return None
    # Everything below runs without the lock so HTTP handlers never wait on model or planning work.
    try:
        if needs_plan:
            plan_job(job)  # the next step pops the first window
        elif unit is None:
            finish_job(job)
        else:
            t0 = time.time()
            run_unit(job, unit)
            job["work_s"] += time.time() - t0
            publish(job)
            save_partial(job)
            if unit[0] != "leadin":
                queue_leadin_checks(job)   # after the publish: a skip is out before its lead-in is looked at
    except Exception as e:  # noqa: BLE001
        raise JobError(job, e) from e
    return job


class JobError(Exception):
    def __init__(self, job, cause):
        super().__init__(repr(cause))
        self.job, self.cause = job, cause


ANALYSIS_SYSTEM = "You are a concise analyst of sponsorship segments in YouTube videos. Answer only with the JSON schema."


class Preempted(Exception):
    """An idle-time model call was abandoned because a video being watched needs the model."""
    preempted = True


def watched_job_waiting():
    now = time.time()
    with jobs_lock:
        return any(j["status"] not in ("done", "error") and now - j["last_poll"] <= PAUSE_AFTER_S for j in jobs.values())


def ollama_chat_stream(user_content, schema, system, should_abort, model=None):
    """Like ollama_chat, but abandonable: a watchdog shuts the socket down as soon as should_abort() is true.
    That unblocks the read immediately, also while the prompt is still being evaluated and nothing has come back
    (closing the response object from another thread does not), and Ollama stops generating when the client is
    gone. Raises Preempted in that case."""
    payload = {
        "model": model or MODEL,
        "messages": [{"role": "system", "content": system or SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
        "stream": True, "format": schema or OUTPUT_SCHEMA, "think": False, "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }
    u = urlparse(OLLAMA)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=LLM_TIMEOUT_S)
    finished, aborted = threading.Event(), threading.Event()

    def watchdog():
        while not finished.wait(0.2):
            if should_abort():
                aborted.set()
                try:
                    if conn.sock:
                        conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    parts = []
    try:
        conn.connect()
        threading.Thread(target=watchdog, daemon=True).start()
        conn.request("POST", "/api/chat", body=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()  # blocks until the first token; the watchdog can cut it short
        if resp.status != 200:
            raise OSError(f"ollama http {resp.status}")
        for raw in resp:
            if aborted.is_set():
                break
            raw = raw.strip()
            if not raw:
                continue
            obj = json.loads(raw.decode("utf-8"))
            parts.append((obj.get("message") or {}).get("content") or "")
            if obj.get("done"):
                break
    except Exception:  # noqa: BLE001 - reads on a socket the watchdog shut down fail in several ways
        if not aborted.is_set():
            raise
    finally:
        finished.set()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    if aborted.is_set():
        raise Preempted()
    return json.loads("".join(parts))


def analysis_chat(prompt, schema):
    """Tier-2 model call: only ever runs while idle, and gives the model up the moment a watched video needs it."""
    with llm_lock:
        return ollama_chat_stream(prompt, schema, ANALYSIS_SYSTEM, watched_job_waiting)


def load_transcript(video_id):
    cached = load_cache(video_id)
    return cached.get("transcript") if cached else None


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
    paid = meta.get("paid_promotion")
    inputs = inputs_digest(thash, title, description, paid, alines, lines)
    hints = extract_hints(description)
    terms = hint_terms(hints, meta.get("creator", ""))
    same = alines is None
    # Fields every job has, whether it is computed or served from the cache (stats need them either way).
    base = {"hash": thash, "inputs": inputs, "lang": lang, "lines": lines, "title": title, "description": description,
            "alines": lines if same else alines, "alang": lang if same else (alang or "en"),
            "video_id": video_id, "kind": meta.get("kind", ""), "creator": meta.get("creator", ""),
            "tag": meta.get("tag", ""), "channel_id": meta.get("channel_id", ""), "duration": meta.get("duration"),
            "client": meta.get("client"), "capture": meta.get("capture"), "heat": meta.get("heat") or [],
            "chapter_times": hints.get("chapter_times", []),
            # Both disclosure signals are tri-state. None = we were not told (no description / no flag from the client).
            "paid_promotion": paid, "disclosed": any(is_ad_line(l) for l in description.splitlines()) if description else None,
            "position": position, "hints": hints, "terms": terms, "header": hints_header(hints),
            "last_poll": time.time(), "paused": False}
    resync = None
    with work_available:
        job = jobs.get(video_id)
        if job and job["hash"] == thash and job.get("inputs") == inputs and job["status"] != "error":
            touch(job)
            changed = apply_meta(job, meta)
            if job["status"] == "done":
                settle_instant(video_id, job["segments"], "same transcript and metadata, result still in memory")
                job["cached"] = True  # reused, not computed for this request
                if changed:
                    resync = (job, job["segments"], f"metadata arrived later: {', '.join(changed)}")
            else:
                job["position"] = position
            work_available.notify()
        else:
            cached = load_cache(video_id)
            if cache_valid(cached, thash, inputs):
                job = {**base, "status": "done", "segments": cached.get("segments", []), "cached": True}
                jobs[video_id] = job
                log(f"[{video_id}] cache hit ({len(job['segments'])} segment(s))")
                settle_instant(video_id, job["segments"], "same transcript and metadata, result on disk")
                resync = (job, job["segments"], "cache hit")
            else:
                why = ""
                if job and job["hash"] == thash and job["status"] != "error":
                    why = " (title/description/paid flag/word timing differ from the existing job: re-processing with them)"
                elif job:
                    why = " (transcript changed; replacing running job)" if job["status"] != "error" else " (previous attempt errored)"
                elif cached and cached.get("hash") == thash and cached.get("versions") == versions() and cached.get("model") == MODEL:
                    why = " (cached result was made with different title/description/paid flag/word timing: re-processing, reusing model calls where valid)"
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
                job = {**base, "status": "queued", "segments": [], "calls": CallCache(prior_calls), "final": [], "pending": None,
                       "n_windows": 0, "n_sent": 0, "ttfs": None, "t_start": time.time(), "work_s": 0.0}
                jobs[video_id] = job
                work_available.notify()
                log(f"[{video_id}] new job: {len(lines)} lines{'' if same else f' + {len(alines)} analysis lines ({alang})'}, "
                    f"lang={lang}, kind={job['kind'] or 'MISSING'}, hash={thash}, inputs={inputs}, title={title!r}{why}")
                if prior_calls:
                    log(f"[{video_id}] {len(prior_calls)} cached model call(s) available")
                if any(hints.values()):
                    log(f"[{video_id}] hints: brands={hints['brands']} codes={hints['codes']} domains={hints['domains']} chapters={len(hints['chapters'])}")
                else:
                    log(f"[{video_id}] hints: none ({'description is empty' if not description else 'nothing sponsor-like in the description'})")
    if resync:
        # Outside the lock and off the request thread: keeps POST fast.
        j, segs, reason = resync
        log(f"[{video_id}] stats: syncing ({reason})")
        threading.Thread(target=sync_stats, args=(j, segs), daemon=True).start()
    return job


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "SecondRead/0.5"

    def log_message(self, fmt, *args):  # quiet default access log
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_ui(self, name):
        """The design system's stylesheets and fonts for the dashboard, straight from extension/ui/ (the same
        files the popup and the player use). Only the names matched here are served."""
        if not UI_FILE_RE.match(name):
            return self.send_json(404, {"error": "not found"})
        path = os.path.join(UI_DIR, *name.split("/"))
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self.send_json(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", "font/woff2" if name.endswith(".woff2") else "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def allowed_origin(self):
        """Only the extension (host_permissions, no CORS needed) or non-browser clients like curl.
        - Host must be localhost/127.0.0.1: defeats DNS rebinding (evil.example -> 127.0.0.1).
        - Web pages send an http(s) Origin; refuse them so sites can't read or poison the cache.
        - With SR_EXTENSION_ID set, only that extension's origin is accepted."""
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
        if u.path == "/feedback":
            fb = load_feedback()
            return self.send_json(200, {"count": len(fb), "recent": fb[-20:]})
        if u.path == "/ads.json":
            try:
                return self.send_json(200, intel.ads_json())
            except Exception as e:  # noqa: BLE001
                log(f"intel: ads_json failed: {e!r}")
                return self.send_json(500, {"error": "intel error"})
        if u.path == "/codes.json":
            try:
                return self.send_json(200, stats.codes_json((parse_qs(u.query).get("q") or [""])[0][:100]))
            except Exception as e:  # noqa: BLE001
                log(f"stats: codes failed: {e!r}")
                return self.send_json(500, {"error": "stats error"})
        if u.path == "/stats.json":
            try:
                return self.send_json(200, stats.compute())
            except Exception as e:  # noqa: BLE001
                log(f"stats: compute failed: {e!r}")
                return self.send_json(500, {"error": "stats error"})
        if u.path.startswith("/ui/"):
            return self.send_ui(u.path[4:])
        if u.path in ("/dashboard", "/dashboard/"):
            body = stats.DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/health":
            ok = ollama_available()
            # build: so the popup can say when the running server is older than the extension's files
            return self.send_json(200, {"ok": True, "model": MODEL, "ollama": ok, "loaded": ok and model_loaded(MODEL),
                                        "build": CLIENT_BUILD})
        if u.path == "/stored":
            if not extension_request(self.headers):
                return self.send_json(403, {"error": "extension only"})
            q = parse_qs(u.query)
            vid = (q.get("v") or [""])[0]
            if not VIDEO_ID_RE.match(vid):
                return self.send_json(400, {"error": "bad video id"})
            try:
                build = int((q.get("build") or [""])[0])
            except ValueError:
                build = None
            segments, where = stored_result(vid, build)
            if segments is None:
                return self.send_json(404, {"error": "not stored", "why": where})
            if "desc" in q or "dur" in q:
                # second question, once the page's metadata is known: may the caption read be skipped altogether?
                try:
                    duration = float((q.get("dur") or [""])[0])
                except ValueError:
                    duration = None
                match, why = stored_matches(vid, duration, (q.get("desc") or [""])[0][:12])
                if match:
                    instant_served.pop(vid, None)   # settled: nothing further will come to confirm or replace it
                    log(f"[{vid}] cache: instant (no re-read): {why}")
                else:
                    log(f"[{vid}] cache: reading captions to validate the stored result ({why})")
                return self.send_json(200, {"match": match, "why": why})
            instant_served.pop(vid, None)
            while len(instant_served) >= INSTANT_SERVED_MAX:   # never grows without bound
                instant_served.pop(next(iter(instant_served)))
            instant_served[vid] = segments_signature(segments)
            skips = sum(1 for s in segments if s.get("action") == "skip")
            log(f"[{vid}] cache: instant ({len(segments)} segment(s), {skips} to skip, from {where}), before the captions were read")
            return self.send_json(200, {"status": "done", "segments": segments, "cached": True, "instant": True})
        if u.path == "/analyze":
            if not extension_request(self.headers):   # answers per video id, like /stored: the same gate
                return self.send_json(403, {"error": "extension only"})
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
        if u.path not in ("/analyze", "/feedback", "/event"):
            return self.send_json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY_BYTES:
                return self.send_json(413, {"error": "body too large"})
            data = scrub(json.loads(self.rfile.read(n) or b"{}"))
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad json"})
        if u.path == "/event":
            if not isinstance(data, dict) or not VIDEO_ID_RE.match(str(data.get("videoId") or "")):
                return self.send_json(400, {"error": "bad event"})
            try:
                ok, err = stats.record_event(data)
            except Exception as e:  # noqa: BLE001
                log(f"stats: event failed: {e!r}")
                ok, err = False, "stats error"
            return self.send_json(200 if ok else 400, {"ok": ok, **({"error": err} if err else {})})
        if u.path == "/feedback":
            if not isinstance(data, dict):
                return self.send_json(400, {"error": "bad json"})
            entry, err = record_feedback(data)
            if err:
                return self.send_json(400, {"error": err})
            return self.send_json(200, {"ok": True})
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
                "tag": str(data.get("tag") or "")[:40],  # free-text note for results.md (e.g. BASELINE)
                "channel_id": str(data.get("channelId") or "")[:40], "duration": finite(data.get("duration")),
                "heat": [[finite(p[0], 0.0), finite(p[1], 0.0)] for p in (data.get("heat") or [])[:200]
                         if isinstance(p, list) and len(p) == 2],
                # tri-state: True / False as reported by the page, None when the client did not say
                "paid_promotion": data.get("paidPromotion") if isinstance(data.get("paidPromotion"), bool) else None,
                "capture": clean_capture(data.get("capture")),
                "client": {k: (v if isinstance(v, int) and not isinstance(v, bool) else None)
                           for k, v in ((k, (data.get("client") or {}).get(k) if isinstance(data.get("client"), dict) else None)
                                        for k in ("page", "content", "background"))}}
        description = str(data.get("description") or "")[:5000]
        try:
            position = max(0.0, float(data.get("position") or 0.0))
        except (TypeError, ValueError):
            position = 0.0
        alines = clean_lines(data.get("analysisLines")) if data.get("analysisLines") else None
        if alines is not None and (len(alines) == 0 or len(alines) > MAX_LINES):
            alines = None
        alang = str(data.get("analysisLang") or "en")[:12]
        log_meta(vid, title, description, meta, position, extract_hints(description))
        job = start_or_get_job(vid, lang, lines, title, meta, alines, alang, position, description)
        with jobs_lock:
            return self.send_json(200, public_job(job))


def backfill_durations():
    """Videos recorded before the client sent a duration have none; use the end of their cached transcript."""
    fixed = 0
    try:
        for vid in stats.videos_without_duration():
            cached = load_cache(vid)
            lines = (cached or {}).get("transcript") or []
            end = finite(lines[-1].get("end")) if lines and isinstance(lines[-1], dict) else None
            if end and end > 0:
                stats.set_duration(vid, end)
                fixed += 1
    except Exception as e:  # noqa: BLE001
        log(f"stats: duration backfill failed: {e!r}")
    if fixed:
        log(f"stats: filled in the length of {fixed} video(s) from cached transcripts")
    try:
        channels, reset = stats.repair_legacy_rows(POST_VERSION)
        if channels or reset:
            log(f"stats: repaired older rows: {channels} video(s) got their channel from events, "
                f"{reset} disclosure value(s) recorded without data reset to unknown")
    except Exception as e:  # noqa: BLE001
        log(f"stats: legacy repair failed: {e!r}")
    try:
        stats.purge_bad_codes()  # logs each row it drops
    except Exception as e:  # noqa: BLE001
        log(f"stats: code vault clean-up failed: {e!r}")
    try:
        redone = stats.renormalize_brands(fresh_hints)
        if redone:
            log(f"stats: brands re-derived under rules v{stats.BRAND_RULES_VERSION}: {redone[0]} segment(s) changed, "
                f"{redone[1]} learned alias(es) dropped and re-learned where still valid")
    except Exception as e:  # noqa: BLE001
        log(f"stats: brand re-normalization failed: {e!r}")


def fresh_hints(video_id):
    """The description hints of a cached video under the current rules (None if no description was stored), so that a
    brand re-derivation after a rules change does not reuse hints the old rules extracted."""
    meta = (load_cache(video_id) or {}).get("meta") or {}
    return extract_hints(meta["description"]) if meta.get("description") else None


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
    stats.init()
    intel.init()
    intel.set_logger(log)
    stats.set_logger(log)
    backfill_durations()
    DETECT_MODEL = DETECT_MODEL_PREF if model_present(DETECT_MODEL_PREF) else MODEL
    if DETECT_MODEL != MODEL:
        try:
            gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
        except (ValueError, OSError, AttributeError):
            gb = 0
        log(f"WARNING: two models ({DETECT_MODEL} + {MODEL}) will stay loaded; needs >= 32 GB"
            f"{f', this machine has {gb:.0f} GB' if gb else ''}")
    log(f"Second Read server on http://{HOST}:{PORT}  cache={CACHE_DIR}")
    log(f"models: detect={DETECT_MODEL} refine/words={MODEL} via Ollama "
        f"({'reachable' if ollama_available() else 'NOT available'}); versions {versions()}")
    threading.Thread(target=warm_models, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if "--feedback-report" in sys.argv[1:]:
        feedback_report()
        sys.exit(0)
    sys.exit(main())
