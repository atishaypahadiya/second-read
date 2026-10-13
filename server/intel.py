"""Second Read — ad intelligence. Local only; never blocks skipping.

Tier 1 (deterministic, computed at job end, stored in stats.local.db / ad_metrics):
  position, hook distance, chapter placement, heatmap relation, segue type, reading pace, CTA count,
  offers, template similarity across creators, topic fit (embeddings via Ollama, optional).
Tier 2 (Qwen, idle-only queue, stored in ad_analysis with its own ANALYSIS_VERSION):
  persuasion tags with quotes, structure beats, better placement suggestion.
"""

import json
import math
import re
import time
import urllib.request

import stats

ANALYSIS_VERSION = 2  # 2: persuasion quotes matched like evidence (v1 dropped every quote that crossed a line break)
EMBED_MODEL = "nomic-embed-text"
OLLAMA = "http://localhost:11434"
HOOK_S = 60.0
CHAPTER_BOUNDARY_S = 3.0
PEAK_WINDOW_S = 60.0
TEMPLATE_THRESHOLD = 0.7

SCHEMA = """
CREATE TABLE IF NOT EXISTS ad_metrics (
  segment_id INTEGER PRIMARY KEY, computed_at TEXT, metrics TEXT
);
CREATE TABLE IF NOT EXISTS ad_text (
  segment_id INTEGER PRIMARY KEY, brand TEXT, video_id TEXT, channel_id TEXT, text TEXT, shingles TEXT
);
CREATE TABLE IF NOT EXISTS ad_analysis (
  segment_id INTEGER PRIMARY KEY, version INTEGER, created_at TEXT, analysis TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS video_context (
  video_id TEXT PRIMARY KEY, heat TEXT, chapters TEXT, paid_promotion INTEGER, disclosed INTEGER
);
"""

_log = print
_embed_available = None


def set_logger(fn):
    global _log
    _log = fn


def init():
    with stats._lock, stats.connect() as c:
        c.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
STOP = set("""the a an and or but of to in on at for with from by as is are was were be been it its this that these those
i you we they he she them us our your my me his her their what which who when where why how not no yes so if then than
there here just also very really like get got have has had do does did can could would should will about into over out up
down more most some any all each every one two three first new now today video channel guys okay ok yeah right well go
going gonna want wanted know think thing things way time lot much many make made use using used see look say said""".split())
SENT_END = re.compile(r"[.!?…][)\"'”’]*$")
HARD_CUT = re.compile(r"\b(but first|before (?:we|i) (?:get|go|continue|dive|jump|start)|quick (?:word|break|note|message)|"
                      r"let'?s take a (?:quick |short )?(?:break|second|moment)|real quick|a word from|time for|speaking of which|"
                      r"(?:we'?ll|i'?ll|let'?s) (?:get |come )?back to (?:that|this|it)|more on that (?:later|in a)|"
                      r"hold that thought|pause (?:for|here))\b", re.I)
FAKE_OUT = re.compile(r"\b(psych|just kidding|gotcha|plot twist|it'?s an ad|that'?s right,? it'?s|you thought|surprise)\b", re.I)
OFFER_PATTERNS = [
    (r"(\d{1,3})\s*(?:%|percent)\s*off", "percent off"), (r"free (?:trial|month|week|year)", "free trial"),
    (r"(\d+)[- ]day(?:s)? (?:free|trial)", "free trial"), (r"money[- ]back guarantee", "money-back guarantee"),
    (r"free shipping", "free shipping"), (r"first (\d+) (?:people|months|users|customers)", "limited offer"),
    (r"\b(exclusive|limited time|only (?:today|this week))\b", "urgency"), (r"\bfor free\b|\bcompletely free\b", "free"),
    (r"(\d+)\s*(?:extra )?months? free", "months free"), (r"\$\s?\d+(?:\.\d+)?", "price"),
]


def words_of(text):
    return [w for w in re.sub(r"[^\w\s]", " ", text.lower()).split() if w]


QUOTE_MIN_RUN = 6  # as the server's evidence gate: the whole quote, or any verbatim run of this many words
LINE_LABEL_RE = re.compile(r"\bL\d+\b")  # the model sometimes copies the "L3" labels of the prompt into a quote


def quote_in(quote, hay):
    """Is the model's quote really in the text (hay = words_of(text), no line labels)? Matched like evidence:
    normalized, then the whole quote or any run of QUOTE_MIN_RUN words from it must appear verbatim. A quote that
    runs over a line break or respells one misheard word ("headsp space") is still that passage."""
    q = words_of(LINE_LABEL_RE.sub(" ", quote))

    def has(run):
        n = len(run)
        return any(hay[j:j + n] == run for j in range(len(hay) - n + 1))
    if not q:
        return False
    return has(q) or any(has(q[i:i + QUOTE_MIN_RUN]) for i in range(len(q) - QUOTE_MIN_RUN + 1))


def content_words(text):
    return {w for w in words_of(text) if len(w) >= 5 and w not in STOP}


def shingles(text, n=5):
    w = words_of(text)
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def first_sentence(text, max_words=18):
    toks = text.split()
    out = []
    for t in toks:
        out.append(t)
        if SENT_END.search(t) or len(out) >= max_words:
            break
    return " ".join(out)


def lines_in(lines, start, end):
    return [l for l in lines if l["end"] > start and l["start"] < end]


def embed(texts):
    global _embed_available
    if _embed_available is False:
        return None
    try:
        req = urllib.request.Request(f"{OLLAMA}/api/embed", data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _embed_available = True
        return data.get("embeddings")
    except Exception as e:  # noqa: BLE001
        if _embed_available is None:
            _log(f"intel: embeddings unavailable ({EMBED_MODEL}): {e}")
        _embed_available = False
        return None


def cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (da * db)


# ---------------------------------------------------------------------------
# Tier 1
# ---------------------------------------------------------------------------
def heat_peaks(heat, top=3):
    """heat: [[t_pct 0..100, value 0..100], ...] sampled left to right. Returns top-N local maxima [(t_pct, v)]."""
    if not heat or len(heat) < 3:
        return []
    peaks = []
    for i in range(1, len(heat) - 1):
        if heat[i][1] >= heat[i - 1][1] and heat[i][1] > heat[i + 1][1] and heat[i][1] > 0:
            peaks.append((heat[i][0], heat[i][1]))
    peaks.sort(key=lambda p: -p[1])
    return peaks[:top]


def segue_type(ad_text, before_text):
    fs = first_sentence(ad_text)
    opening = " ".join(ad_text.split()[:30])  # the first two sentences or so
    if FAKE_OUT.search(opening) or FAKE_OUT.search(before_text[-200:]):
        return "humor/fake-out", fs
    if HARD_CUT.search(opening):
        return "hard cut", fs
    shared = content_words(fs) & content_words(before_text)
    if len(shared) >= 2 or any(len(w) >= 7 for w in shared):
        return "topical bridge", f"{fs} (shares: {', '.join(sorted(shared)[:4])})"
    return "none", fs


def pace(ad_lines, other_lines):
    def wps(ls):
        vals = []
        for l in ls:
            d = l["end"] - l["start"]
            if d >= 1.0:
                vals.append(len(l["text"].split()) / d)
        return vals
    a, o = wps(ad_lines), wps(other_lines)
    if not a or not o:
        return None
    ad_rate = sum(a) / len(a)
    o.sort()
    med = o[len(o) // 2]
    return {"ad_wps": round(ad_rate, 2), "creator_median_wps": round(med, 2), "delta_pct": round(100 * (ad_rate - med) / med) if med else None}


def ctas_and_offers(ad_text, terms):
    low = ad_text.lower()
    cta = 0
    for t in terms:
        if len(t) >= 4:
            cta += low.count(t.lower())
    cta += len(re.findall(r"\b(?:use )?code\b|link (?:is )?in the description|link below|\.com\b|\.com/|sign up|go to|head to|check out", low))
    offers = []
    for pat, label in OFFER_PATTERNS:
        for m in re.finditer(pat, low):
            offers.append({"offer": label, "text": m.group(0)})
    seen, uniq = set(), []
    for o in offers:
        if o["text"] not in seen:
            seen.add(o["text"])
            uniq.append(o)
    return cta, uniq[:8]


def template_match(c, seg_id, brand, channel_id, sh):
    """Highest Jaccard of this ad's 5-word shingles against other ads of the same brand by other creators."""
    best, best_vid = 0.0, None
    for r in c.execute("SELECT segment_id, video_id, channel_id, shingles FROM ad_text WHERE brand = ? AND segment_id != ?", (brand, seg_id)):
        if r["channel_id"] == channel_id:
            continue
        other = set(json.loads(r["shingles"]))
        j = jaccard(sh, other)
        if j > best:
            best, best_vid = j, r["video_id"]
    return round(best, 2), best_vid


def chapter_of(chapters, t):
    """chapters: [[start_s, title], ...] sorted. Returns (title, chapter_start, chapter_end or None)."""
    cur = None
    for i, (cs, title) in enumerate(chapters):
        if t >= cs:
            ce = chapters[i + 1][0] if i + 1 < len(chapters) else None
            cur = (title, cs, ce)
    return cur


def compute_tier1(job, segments, seg_ids, terms):
    """Compute and store Tier 1 metrics for each sponsor segment. seg_ids: {(start, end): segment_id}."""
    lines = job["lines"]
    duration = job.get("duration") or (lines[-1]["end"] if lines else 0)
    chapters = job.get("chapter_times") or []
    heat = job.get("heat") or []
    peaks = heat_peaks(heat)
    sponsor = [s for s in segments if s.get("category") == "sponsor"]
    if not sponsor:
        return
    ad_ranges = [(s["start"], s["end"]) for s in sponsor]
    other_lines = [l for l in lines if not any(l["end"] > a and l["start"] < b for a, b in ad_ranges)]
    rest_text = " ".join(l["text"] for l in other_lines)
    ad_texts = {}
    with stats._lock, stats.connect() as c:
        for s in sponsor:
            sid = seg_ids.get((s["start"], s["end"]))
            if sid is None:
                continue
            ad_lines = lines_in(lines, s["start"], s["end"])
            ad_text = " ".join(l["text"] for l in ad_lines)
            ad_texts[sid] = ad_text
            before = " ".join(l["text"] for l in lines_in(lines, max(0, s["start"] - 30), s["start"]))
            m = {"position_pct": round(100 * s["start"] / duration, 1) if duration else None,
                 "after_hook_s": round(max(0.0, s["start"] - HOOK_S), 1), "in_hook": s["start"] < HOOK_S,
                 "length_s": round(s["end"] - s["start"], 1), "words": len(ad_text.split())}
            ch = chapter_of(chapters, s["start"]) if chapters else None
            if ch:
                title, cs, ce = ch
                m["chapter"] = title
                m["chapter_placement"] = "at chapter boundary" if abs(s["start"] - cs) <= CHAPTER_BOUNDARY_S or (ce and abs(s["end"] - ce) <= CHAPTER_BOUNDARY_S) else "mid-chapter"
            else:
                m["chapter"] = None
                m["chapter_placement"] = "no chapters" if not chapters else "before first chapter"
            if peaks and duration:
                nxt = [(p, (p[0] / 100.0) * duration) for p in peaks if (p[0] / 100.0) * duration >= s["end"]]
                nxt.sort(key=lambda x: x[1])
                if nxt:
                    dist = nxt[0][1] - s["end"]
                    m["next_peak_s"] = round(dist, 1)
                    m["before_top_peak"] = dist <= PEAK_WINDOW_S
                else:
                    m["next_peak_s"] = None
                    m["before_top_peak"] = False
                m["peaks_pct"] = [round(p[0], 1) for p in peaks]
            else:
                m["next_peak_s"] = None
                m["before_top_peak"] = None
            st, fs = segue_type(ad_text, before)
            m["segue"], m["first_sentence"] = st, fs
            m["pace"] = pace(ad_lines, other_lines)
            m["cta_count"], m["offers"] = ctas_and_offers(ad_text, terms)
            sh = shingles(ad_text)
            c.execute("INSERT OR REPLACE INTO ad_text(segment_id, brand, video_id, channel_id, text, shingles) VALUES (?,?,?,?,?,?)",
                      (sid, s.get("brand_canonical") or s.get("brand", ""), job["video_id"], job.get("channel_id", ""), ad_text, json.dumps(sorted(sh))))
            sim, other_vid = template_match(c, sid, s.get("brand_canonical") or s.get("brand", ""), job.get("channel_id", ""), sh)
            m["template_similarity"], m["template_match_video"] = sim, other_vid
            m["template_script"] = sim >= TEMPLATE_THRESHOLD
            m["topic_fit"] = None
            c.execute("INSERT OR REPLACE INTO ad_metrics(segment_id, computed_at, metrics) VALUES (?,?,?)",
                      (sid, stats.now(), json.dumps(m)))
        c.execute("INSERT OR REPLACE INTO video_context(video_id, heat, chapters, paid_promotion, disclosed) VALUES (?,?,?,?,?)",
                  (job["video_id"], json.dumps(heat[:200]), json.dumps(chapters),
                   None if job.get("paid_promotion") is None else int(bool(job["paid_promotion"])),
                   None if job.get("disclosed") is None else int(bool(job["disclosed"]))))
    # topic fit (optional embeddings), outside the lock: one call for all ads of this video
    if ad_texts and rest_text.strip():
        texts = [rest_text[:6000]] + [t[:4000] for t in ad_texts.values()]
        vecs = embed(texts)
        if vecs and len(vecs) == len(texts):
            with stats._lock, stats.connect() as c:
                for (sid, _), v in zip(ad_texts.items(), vecs[1:]):
                    fit = round(cosine(vecs[0], v), 3)
                    row = c.execute("SELECT metrics FROM ad_metrics WHERE segment_id = ?", (sid,)).fetchone()
                    if row:
                        m = json.loads(row["metrics"])
                        m["topic_fit"] = fit
                        c.execute("UPDATE ad_metrics SET metrics = ? WHERE segment_id = ?", (json.dumps(m), sid))
    _log(f"[{job['video_id']}] intel: tier-1 metrics for {len(ad_texts)} ad(s)"
         f"{' (topic fit via ' + EMBED_MODEL + ')' if _embed_available else ''}")


# ---------------------------------------------------------------------------
# Tier 2 (Qwen, idle-only)
# ---------------------------------------------------------------------------
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "persuasion": {"type": "array", "items": {"type": "object", "properties": {
            "tag": {"type": "string", "enum": ["testimony", "problem/fear framing", "anchoring", "risk reversal",
                                               "urgency/scarcity", "social proof", "authority", "reciprocity"]},
            "quote": {"type": "string"}}, "required": ["tag", "quote"]}},
        "beats": {"type": "array", "items": {"type": "object", "properties": {
            "beat": {"type": "string", "enum": ["hook", "problem", "product", "offer", "cta", "other"]},
            "line": {"type": "integer"}}, "required": ["beat", "line"]}},
        "better_placement": {"type": "object", "properties": {
            "time_s": {"type": "number"}, "reason": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["time_s", "reason", "quote"]},
    },
    "required": ["persuasion", "beats", "better_placement"],
}

ANALYSIS_PROMPT = """You analyze how a sponsor read is written and placed in a YouTube video. Be concrete and brief.

AD (brand {brand!r}, {start}-{end} in the video), transcript lines:
{ad_lines}

REST OF THE VIDEO (sampled lines with timestamps; "[gap]" marks omitted parts):
{context}

Return:
- persuasion: each technique actually used in the AD, with a SHORT verbatim quote from the AD lines (copy exactly).
  Tags: testimony, problem/fear framing, anchoring, risk reversal, urgency/scarcity, social proof, authority, reciprocity.
  Only tags with a real quote. Empty list if none.
- beats: the AD's structure in order as (beat, line number L from the AD lines): hook, problem, product, offer, cta, other.
- better_placement: the single best alternative timestamp (seconds) in the REST OF THE VIDEO where the topic naturally
  connects to this product, with a one-line reason, and a verbatim quote from the transcript at that place. If the
  current placement is already the best, return the ad's own start time and say so."""


def pending_analysis(limit=1):
    """Sponsor segments without an analysis at the current ANALYSIS_VERSION."""
    with stats._lock, stats.connect() as c:
        rows = c.execute("""SELECT s.id, s.video_id, s.seg_start, s.seg_end, s.brand FROM segments s
                            LEFT JOIN ad_analysis a ON a.segment_id = s.id AND a.version = ?
                            WHERE s.category = 'sponsor' AND a.segment_id IS NULL ORDER BY s.id DESC LIMIT ?""",
                         (ANALYSIS_VERSION, limit)).fetchall()
        return [dict(r) for r in rows]


def sample_context(lines, start, end, max_lines=90):
    """Lines outside the ad, thinned evenly to max_lines, with [gap] markers."""
    outside = [(i, l) for i, l in enumerate(lines) if not (l["end"] > start and l["start"] < end)]
    if len(outside) > max_lines:
        step = len(outside) / max_lines
        picked = [outside[int(k * step)] for k in range(max_lines)]
    else:
        picked = outside
    out, last_i = [], None
    for i, l in picked:
        if last_i is not None and i != last_i + 1:
            out.append("[gap]")
        out.append(f"[{stats.fmt_time(l['start'])}] {l['text']}")
        last_i = i
    return "\n".join(out)


def run_analysis(row, chat_fn, load_transcript):
    """One Qwen call for one segment. chat_fn(prompt, schema) -> dict; load_transcript(video_id) -> lines or None."""
    sid, vid = row["id"], row["video_id"]
    lines = load_transcript(vid)
    if not lines:
        with stats._lock, stats.connect() as c:
            c.execute("INSERT OR REPLACE INTO ad_analysis(segment_id, version, created_at, analysis, error) VALUES (?,?,?,?,?)",
                      (sid, ANALYSIS_VERSION, stats.now(), None, "transcript not cached"))
        return
    ad_lines = lines_in(lines, row["seg_start"], row["seg_end"])
    ad_block = "\n".join(f"L{i} {l['text']}" for i, l in enumerate(ad_lines))
    prompt = ANALYSIS_PROMPT.format(brand=row["brand"], start=stats.fmt_time(row["seg_start"]), end=stats.fmt_time(row["seg_end"]),
                                    ad_lines=ad_block, context=sample_context(lines, row["seg_start"], row["seg_end"]))
    t0 = time.time()
    _log(f"[{vid}] intel: tier-2 analysis started for {row['brand']!r} {stats.fmt_time(row['seg_start'])}-{stats.fmt_time(row['seg_end'])} (idle)")
    try:
        result = chat_fn(prompt, ANALYSIS_SCHEMA)
        # Keep only quotes that are really in the ad. Matched against the ad's words alone: with the prompt's "L3"
        # labels in the haystack, every quote that ran over a line break was dropped.
        ad_words = words_of(" ".join(l["text"] for l in ad_lines))
        returned = [p for p in (result.get("persuasion") or []) if isinstance(p, dict)]
        pers, dropped = [], []
        for p in returned:
            quote = str(p.get("quote", ""))
            if quote_in(quote, ad_words):
                pers.append({"tag": p.get("tag"), "quote": quote[:200]})
            else:
                dropped.append(f"{p.get('tag')}: {quote[:60]!r}")
        tag_note = "model returned none" if not returned else f"model returned {len(returned)}" + (
            f"; {len(dropped)} dropped, quote not in the ad: " + " | ".join(dropped) if dropped else "")
        bp = result.get("better_placement") or {}
        try:
            t = float(bp.get("time_s", row["seg_start"]))
        except (TypeError, ValueError):
            t = row["seg_start"]
        # Locate the quote in the transcript; its line's time beats the model's number when they disagree.
        qn = " ".join(words_of(str(bp.get("quote", ""))))
        quote_ok, quote_t = False, None
        if qn:
            for l in lines:
                if qn in " ".join(words_of(l["text"])):
                    quote_ok, quote_t = True, l["start"]
                    break
            if not quote_ok:  # quote may span two lines
                for i in range(len(lines) - 1):
                    if qn in " ".join(words_of(lines[i]["text"] + " " + lines[i + 1]["text"])):
                        quote_ok, quote_t = True, lines[i]["start"]
                        break
        if quote_t is not None and abs(quote_t - t) > 10:
            t = quote_t
        analysis = {"persuasion": pers, "beats": [{"beat": b.get("beat"), "line": b.get("line")} for b in (result.get("beats") or []) if isinstance(b, dict)][:12],
                    "better_placement": {"time_s": round(t, 1), "reason": str(bp.get("reason", ""))[:300],
                                         "quote": str(bp.get("quote", ""))[:200], "quote_verified": bool(quote_ok),
                                         "time_from_quote": quote_t is not None,
                                         "same_as_now": abs(t - row["seg_start"]) < 15},
                    "seconds": round(time.time() - t0, 1)}
        err = None
    except Exception as e:  # noqa: BLE001
        if getattr(e, "preempted", False):
            # A watched video needed the model. Nothing is recorded: the segment stays pending and is retried when idle.
            _log(f"[{vid}] intel: tier-2 analysis preempted after {time.time() - t0:.1f}s (a video is being watched); will retry when idle")
            return
        analysis, err = None, repr(e)[:200]
    with stats._lock, stats.connect() as c:
        c.execute("INSERT OR REPLACE INTO ad_analysis(segment_id, version, created_at, analysis, error) VALUES (?,?,?,?,?)",
                  (sid, ANALYSIS_VERSION, stats.now(), json.dumps(analysis) if analysis else None, err))
    _log(f"[{vid}] intel: tier-2 analysis for {row['brand']!r} {stats.fmt_time(row['seg_start'])}-{stats.fmt_time(row['seg_end'])}"
         f" {'failed: ' + err if err else 'in %.1fs, %d tag(s) (%s)' % (time.time() - t0, len(analysis['persuasion']), tag_note)}")


# ---------------------------------------------------------------------------
# Read side for the dashboard
# ---------------------------------------------------------------------------
def disclosure(paid, described):
    """Same rule as server.disclosure_state, on the stored 1/0/NULL columns."""
    if paid == 1 or described == 1:
        return "disclosed"
    if paid == 0 and described == 0:
        return "undisclosed"
    return "unknown"


def ads_json():
    with stats._lock, stats.connect() as c:
        rows = c.execute("""SELECT s.*, v.channel_name, v.title, v.duration_s, m.metrics, a.analysis, a.error AS analysis_error,
                                   x.heat, x.chapters, x.paid_promotion, x.disclosed
                            FROM segments s LEFT JOIN videos v ON v.video_id = s.video_id
                            LEFT JOIN ad_metrics m ON m.segment_id = s.id LEFT JOIN ad_analysis a ON a.segment_id = s.id AND a.version = ?
                            LEFT JOIN video_context x ON x.video_id = s.video_id
                            WHERE s.category = 'sponsor' ORDER BY s.created_at DESC, s.id DESC""", (ANALYSIS_VERSION,)).fetchall()
        ev = c.execute("SELECT video_id, seg_start, seg_end, kind, COUNT(*) AS n FROM events GROUP BY video_id, seg_start, seg_end, kind").fetchall()
    counts = {}
    for e in ev:
        counts.setdefault((e["video_id"], e["seg_start"], e["seg_end"]), {})[e["kind"]] = e["n"]
    ads = []
    for r in rows:
        ads.append({
            "id": r["id"], "video_id": r["video_id"], "title": r["title"], "creator": r["channel_name"], "duration_s": r["duration_s"],
            "start": r["seg_start"], "end": r["seg_end"], "brand": r["brand"], "brand_raw": r["brand_raw"], "style": r["style"],
            "action": r["action"], "confidence": r["confidence"], "evidence": r["evidence"],
            "metrics": json.loads(r["metrics"]) if r["metrics"] else None,
            "analysis": json.loads(r["analysis"]) if r["analysis"] else None, "analysis_error": r["analysis_error"],
            "heat": json.loads(r["heat"]) if r["heat"] else [], "chapters": json.loads(r["chapters"]) if r["chapters"] else [],
            "paid_promotion": None if r["paid_promotion"] is None else bool(r["paid_promotion"]),
            "disclosed_in_description": None if r["disclosed"] is None else bool(r["disclosed"]),
            "disclosure": disclosure(r["paid_promotion"], r["disclosed"]) if r["style"] == "read" else None,
            "undisclosed": r["style"] == "read" and disclosure(r["paid_promotion"], r["disclosed"]) == "undisclosed",
            "events": counts.get((r["video_id"], r["seg_start"], r["seg_end"]), {}),
        })
    # insights
    with_m = [a for a in ads if a["metrics"]]
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    segues, tags, placements, offers = {}, {}, {}, {}
    for a in with_m:
        m = a["metrics"]
        segues[m.get("segue")] = segues.get(m.get("segue"), 0) + 1
        placements[m.get("chapter_placement")] = placements.get(m.get("chapter_placement"), 0) + 1
        for o in m.get("offers", []):
            offers[o["offer"]] = offers.get(o["offer"], 0) + 1
        if a["analysis"]:
            for p in a["analysis"].get("persuasion", []):
                tags[p["tag"]] = tags.get(p["tag"], 0) + 1
    insights = {
        "ads": len(ads), "with_metrics": len(with_m), "analyzed": sum(1 for a in ads if a["analysis"]),
        "pending_analysis": sum(1 for a in ads if not a["analysis"] and not a["analysis_error"]),
        "avg_position_pct": avg([a["metrics"].get("position_pct") for a in with_m]),
        "in_hook_pct": stats.pct(sum(1 for a in with_m if a["metrics"].get("in_hook")), len(with_m)),
        "avg_pace_delta_pct": avg([(a["metrics"].get("pace") or {}).get("delta_pct") for a in with_m]),
        "avg_cta": avg([a["metrics"].get("cta_count") for a in with_m]),
        "template_pct": stats.pct(sum(1 for a in with_m if a["metrics"].get("template_script")), len(with_m)),
        "avg_topic_fit": avg([a["metrics"].get("topic_fit") for a in with_m]),
        "before_top_peak_pct": stats.pct(sum(1 for a in with_m if a["metrics"].get("before_top_peak")), sum(1 for a in with_m if a["metrics"].get("before_top_peak") is not None)),
        "segues": segues, "placements": placements, "offers": offers, "persuasion": tags,
        "better_placement_suggested": sum(1 for a in ads if a["analysis"] and not a["analysis"]["better_placement"].get("same_as_now")),
        "undisclosed": sum(1 for a in ads if a.get("undisclosed")),
        "disclosure_unknown": sum(1 for a in ads if a.get("disclosure") == "unknown"),
        "paid_promotion_pct": stats.pct(sum(1 for a in ads if a["paid_promotion"]), sum(1 for a in ads if a["paid_promotion"] is not None)),
    }
    return {"generated_at": stats.now(), "ads": ads, "insights": insights, "embed_model": EMBED_MODEL if _embed_available else None}
