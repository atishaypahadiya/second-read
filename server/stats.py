"""Second Read — local stats: SQLite store, brand normalization, metrics, dashboard page.

Stdlib only. Imported by server.py. Everything here is local and gitignored (server/cache/stats.local.db).
The schema is user-agnostic (no user identity) so rows could later be aggregated across many users.
"""

import difflib
import json
import math
import os
import re
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.environ.get("SR_DATA_DIR") or os.environ.get("SS_DATA_DIR")
                       or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache"),
                       "stats.local.db")
_lock = threading.Lock()
_log = print


def set_logger(fn):
    global _log
    _log = fn


EVENT_KINDS = ("skip", "undo", "seek_into", "false_skip", "missed_ad", "allowlisted")
HALL_MIN_SPOTS = 3  # spots a brand needs before it can enter the Hall of Shame or the Hall of Fame
RULE_CATEGORIES = ("interaction", "intro", "outro", "recap")  # same as server.RULE_CATEGORIES: never carry a brand
BRAND_RULES_VERSION = 1  # bump when canonical_brand's rules change: stored brands are re-derived once at startup

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT PRIMARY KEY, channel_id TEXT, channel_name TEXT, title TEXT, lang TEXT,
  duration_s REAL, analyzed_at TEXT, versions TEXT, hints TEXT
);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL, seg_start REAL NOT NULL, seg_end REAL NOT NULL,
  category TEXT, style TEXT, action TEXT, brand_raw TEXT, brand TEXT, confidence REAL,
  evidence TEXT, reason TEXT, source TEXT, created_at TEXT,
  UNIQUE(video_id, seg_start, seg_end)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL, video_id TEXT, channel_id TEXT,
  seg_start REAL, seg_end REAL, playhead REAL, saved_s REAL, brand TEXT, extra TEXT
);
CREATE TABLE IF NOT EXISTS brand_aliases (
  alias TEXT PRIMARY KEY, brand TEXT NOT NULL, how TEXT
);
CREATE TABLE IF NOT EXISTS codes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brand TEXT, video_id TEXT, channel_name TEXT, title TEXT, time_s REAL, kind TEXT, value TEXT, detail TEXT, created_at TEXT,
  UNIQUE(video_id, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_codes_brand ON codes(brand);
CREATE INDEX IF NOT EXISTS idx_segments_brand ON segments(brand);
CREATE INDEX IF NOT EXISTS idx_segments_video ON segments(video_id);
CREATE INDEX IF NOT EXISTS idx_events_video ON events(video_id);
"""

BUILTIN_ALIASES = {
    "nord": "NordVPN", "nordvpn": "NordVPN", "express vpn": "ExpressVPN", "expressvpn": "ExpressVPN",
    "surfshark vpn": "Surfshark", "brilliant.org": "Brilliant", "raid shadow legends": "RAID: Shadow Legends",
    "skill share": "Skillshare", "square space": "Squarespace", "hello fresh": "HelloFresh",
    "better help": "BetterHelp", "ground news": "Ground News", "incogni.com": "Incogni",
}


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with _lock, connect() as c:
        c.executescript(SCHEMA)
        for alias, brand in BUILTIN_ALIASES.items():
            c.execute("INSERT OR IGNORE INTO brand_aliases(alias, brand, how) VALUES (?, ?, 'builtin')", (alias, brand))


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def fmt_time(s):
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}"


def fmt_dur(s):
    s = int(round(s or 0))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}h {m:02d}m" if h else (f"{m}m {sec:02d}s" if m else f"{sec}s")


# ---------------------------------------------------------------------------
# Brand normalization
# ---------------------------------------------------------------------------
def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _display(name):
    """Capitalize a bare domain label ('odoo' -> 'Odoo'); keep names that already have case."""
    if not name:
        return name
    return name if any(ch.isupper() for ch in name) else name[:1].upper() + name[1:]


# Parts of a sponsor link's host that are not the brand: TLD-like endings, and the marketing affixes around the
# name ("headspace-web.app", "get-foo.com", "foo-app.io").
DOMAIN_ENDINGS = {"com", "net", "org", "io", "co", "uk", "us", "de", "deals", "gg", "ai", "app", "tv", "me", "ly", "fm",
                  "shop", "store", "xyz", "dev", "to", "link", "page", "site", "info"}
DOMAIN_AFFIXES = {"web", "app", "get", "try", "go", "join", "use", "shop", "store", "hq", "official", "online"}


# Words that are never a brand on their own. They reach us as capitalized words on a sponsor line ("Coddy Pro Plan")
# or as the model echoing such a hint; "Epidemic Sound" is fine, "Sound" alone is not.
GENERIC_WORDS = {
    "tech", "pro", "plan", "plans", "sound", "premium", "plus", "basic", "standard", "free", "trial", "app", "apps",
    "web", "website", "site", "online", "shop", "store", "official", "video", "videos", "channel", "music", "news",
    "game", "games", "deal", "deals", "offer", "offers", "sale", "code", "codes", "link", "links", "sponsor",
    "sponsors", "sponsored", "course", "courses", "team", "company", "product", "products", "service", "services",
    "software", "the", "and", "for", "with", "from", "this", "that", "your", "our", "get", "try", "use", "new",
    "best", "now", "today", "here", "more"}
ALIAS_MIN_CHARS = 4  # a shorter spelling ("Udo") is resolved from each video's own hints, never remembered


def is_generic(name):
    words = _norm(name).split()
    return all(w in GENERIC_WORDS or w.isdigit() for w in words)


def learn_alias(conn, key, brand, how, hints, evidence):
    """Remember spelling -> brand for every later video. Only for a real name the video itself backs up: neither
    side generic, at least ALIAS_MIN_CHARS characters, and the brand is written on a sponsor line of the description
    or said in the ad's evidence quote. Every alias learned is logged."""
    target = _norm(brand)
    backed = target in {_norm(b) for b in hint_brands(hints)} or f" {target} " in f" {_norm(evidence)} "
    if is_generic(key) or is_generic(brand) or len(key.replace(" ", "")) < ALIAS_MIN_CHARS or key == target or not backed:
        return False
    if conn.execute("INSERT OR IGNORE INTO brand_aliases(alias, brand, how) VALUES (?, ?, ?)", (key, brand, how)).rowcount:
        _log(f"stats: alias learned: {key!r} -> {brand!r} ({how})")
    return True


def _uniq(names):
    seen, out = set(), []
    for x in names:
        if _norm(x) and _norm(x) not in seen:
            seen.add(_norm(x))
            out.append(x)
    return out


def domain_name(domain):
    """Brand-like name from a sponsor link's host: 'headspace-web.app' -> 'Headspace', 'www.odoo.com/r' -> 'Odoo'.
    '' when nothing name-like is left."""
    parts = [p for p in domain.split("/")[0].lower().split(".") if p and p != "www"]
    while len(parts) > 1 and parts[-1] in DOMAIN_ENDINGS:
        parts.pop()
    bits = [b for b in (parts[-1] if parts else "").split("-") if b]
    while len(bits) > 1 and bits[-1] in DOMAIN_AFFIXES:
        bits.pop()
    while len(bits) > 1 and bits[0] in DOMAIN_AFFIXES:
        bits.pop(0)
    label = "-".join(bits)
    return "" if len(label) < 3 or label in DOMAIN_ENDINGS | DOMAIN_AFFIXES else _display(label)


def hint_brands(hints):
    """Names the creator wrote on a sponsor line of the description (generic words are not names)."""
    return _uniq(b for b in (hints or {}).get("brands", []) if not is_generic(b))


def hint_link_names(hints):
    """Names derived from the sponsor links: the host's name and the first path segment ('fern.deals/coddy' ->
    Fern, Coddy). A guess at best, so they are used only when the ad has no spoken name and no written one."""
    out = []
    for d in (hints or {}).get("domains", []):
        out.append(domain_name(d))
        if "/" in d:
            path = d.split("/", 1)[1].split("/")[0]
            if len(path) >= 3 and not path.isdigit():
                out.append(_display(path))
    return _uniq(out)


def canonical_brand(conn, raw, hints=None, evidence="", category="sponsor", source=""):
    """Resolve a model-emitted brand spelling (the name as spoken in the transcript) to a canonical brand. Order:
    1. alias table (learned + builtin); 2. fuzzy match against the brands written in the description's sponsor
    lines (the video's own sponsor) and record the alias; 3. exactly one written brand -> that (sponsor segments);
    4. fuzzy match against brands already in the DB (>= 0.8); 5. the raw spelling, trimmed.
    The spoken name always beats a name derived from a sponsor link: a link names the brand only when the model
    gave none and the description wrote none ("headspace-web.app" -> Headspace).
    A rule segment (recap, outro, intro, interaction) never gets a brand, and a segment the model gave no brand
    borrows the video's single hint only if it is a sponsor segment."""
    raw = (raw or "").strip()
    key = _norm(raw)
    if source == "rule" or category in RULE_CATEGORIES:
        return "", "rule segment"
    if is_generic(raw):  # no name, or a word that is not one ("Tech"): nothing was spoken that names the brand
        cands = (hint_brands(hints) or hint_link_names(hints)) if category == "sponsor" else []
        if len(cands) != 1:
            return _display(raw[:80]), ("raw (generic word)" if key else "none")
        row = conn.execute("SELECT brand FROM brand_aliases WHERE alias = ?", (_norm(cands[0]),)).fetchone()
        return (row["brand"] if row else cands[0]), "hint-only"
    row = conn.execute("SELECT brand FROM brand_aliases WHERE alias = ?", (key,)).fetchone()
    if row:
        return row["brand"], "alias"
    cands = hint_brands(hints)
    best, best_r = None, 0.0
    for c in cands:
        r = difflib.SequenceMatcher(None, key, _norm(c)).ratio()
        if _norm(c) in key or key in _norm(c):
            r = max(r, 0.9)
        if r > best_r:
            best, best_r = c, r
    if best and best_r >= 0.55:
        # Hints come from the page (the creator's description), so only confident matches become global aliases;
        # weaker ones resolve for this video only and are re-derived from its own hints next time.
        if best_r >= 0.8:
            learn_alias(conn, key, best, f"hint fuzzy {best_r:.2f}", hints, evidence)
        return best, f"hint fuzzy {best_r:.2f}"
    if len(cands) == 1 and category == "sponsor":
        return cands[0], "single hint (this video only)"
    known = [r["brand"] for r in conn.execute("SELECT DISTINCT brand FROM segments WHERE brand != ''")]
    best, best_r = None, 0.0
    for b in known:
        r = difflib.SequenceMatcher(None, key, _norm(b)).ratio()
        if r > best_r:
            best, best_r = b, r
    if best and best_r >= 0.8:
        learn_alias(conn, key, best, f"db fuzzy {best_r:.2f}", hints, evidence)
        return best, f"db fuzzy {best_r:.2f}"
    return _display(raw[:80]), "raw"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def record_video(job, segments, versions):
    """Upsert the video and its final segments with canonical brands. Segment ids are stable across re-records
    (keyed by video + start + end), so ad metrics, ad analysis and events stay attached. Sets s['brand_canonical'].
    Returns {(start, end): segment_id}."""
    hints = job.get("hints") or {}
    lines = job.get("lines") or []
    # The player's duration when the client sent it; otherwise the end of the transcript (close enough for
    # position statistics, and never NULL).
    duration = job.get("duration") or (lines[-1]["end"] if lines else None)
    with _lock, connect() as c:
        c.execute("""INSERT INTO videos(video_id, channel_id, channel_name, title, lang, duration_s, analyzed_at, versions, hints)
                     VALUES (?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(video_id) DO UPDATE SET
                       channel_id=CASE WHEN excluded.channel_id != '' THEN excluded.channel_id ELSE videos.channel_id END,
                       channel_name=CASE WHEN excluded.channel_name != '' THEN excluded.channel_name ELSE videos.channel_name END,
                       title=CASE WHEN excluded.title != '' THEN excluded.title ELSE videos.title END,
                       lang=excluded.lang, duration_s=COALESCE(excluded.duration_s, videos.duration_s),
                       analyzed_at=excluded.analyzed_at, versions=excluded.versions, hints=excluded.hints""",
                  (job["video_id"], job.get("channel_id", ""), job.get("creator", ""), job.get("title", ""), job.get("lang", ""),
                   duration, now(), json.dumps(versions), json.dumps(hints)))
        ids, keep = {}, []
        for s in segments:
            brand, how = canonical_brand(c, s.get("brand", ""), hints, s.get("evidence", ""),
                                         s.get("category") or "", s.get("source") or "")
            s["brand_canonical"] = brand
            c.execute("""INSERT INTO segments(video_id, seg_start, seg_end, category, style, action, brand_raw, brand,
                         confidence, evidence, reason, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(video_id, seg_start, seg_end) DO UPDATE SET category=excluded.category, style=excluded.style,
                           action=excluded.action, brand_raw=excluded.brand_raw, brand=excluded.brand,
                           confidence=excluded.confidence, evidence=excluded.evidence, reason=excluded.reason,
                           source=excluded.source""",
                      (job["video_id"], s["start"], s["end"], s.get("category"), s.get("style"), s.get("action"),
                       s.get("brand", ""), brand, s.get("confidence"), s.get("evidence", ""), s.get("reason", ""),
                       s.get("source", ""), now()))
            sid = c.execute("SELECT id FROM segments WHERE video_id=? AND seg_start=? AND seg_end=?",
                            (job["video_id"], s["start"], s["end"])).fetchone()["id"]
            ids[(s["start"], s["end"])] = sid
            keep.append(sid)
        gone = [r["id"] for r in c.execute("SELECT id FROM segments WHERE video_id = ?", (job["video_id"],)) if r["id"] not in keep]
        for sid in gone:
            c.execute("DELETE FROM segments WHERE id = ?", (sid,))
            for table in ("ad_metrics", "ad_text", "ad_analysis"):
                try:
                    c.execute(f"DELETE FROM {table} WHERE segment_id = ?", (sid,))
                except sqlite3.OperationalError:
                    pass  # intel tables not created yet
        return ids


CODE_IN_TEXT_RE = re.compile(r"\b(?:promo |discount |coupon )?code[:\s]+([A-Z0-9]{3,20})\b", re.I)
# linear-time form (no nested quantifier): label(.label)*.tld(/path)?
DOMAIN_IN_TEXT_RE = re.compile(r"\b([a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){0,4}\.(?:com|net|org|io|co|deals|gg|ai|app|tv|me|ly|fm|shop|store|xyz|dev|to)(?:/[\w.-]{1,80})?)\b", re.I)
MAX_SCAN_CHARS = 6000
SPOKEN_DOMAIN_RE = re.compile(r"\b([a-z0-9-]{3,})\s+dot\s+(com|net|org|io|co|deals|gg|ai|app|tv)(?:\s+slash\s+([a-z0-9-]+))?\b", re.I)
OFFER_IN_TEXT = [(r"(\d{1,3})\s*(?:%|percent) off", "percent off"), (r"free (?:trial|month|week|year)", "free trial"),
                 (r"(\d+)[- ]day(?:s)? (?:free|trial)", "free trial"), (r"money[- ]back guarantee", "guarantee"),
                 (r"free shipping", "free shipping"), (r"(\d+)\s*(?:extra )?months? free", "months free"),
                 (r"first (\d+) (?:people|months|users|customers)", "limited offer")]
SOCIAL = ("youtube.", "youtu.be", "instagram.", "twitter.", "x.com", "tiktok.", "facebook.", "patreon.", "discord.", "twitch.",
          "reddit.", "spotify.", "apple.", "bit.ly", "linktr.ee", "google.", "wikipedia.", "amazon.com/shop")


def record_codes(job, segments):
    """7d: every promo code, link and offer from the description hints and the ad text, per brand, with the ad's time."""
    hints = job.get("hints") or {}
    lines = job.get("lines") or []
    sponsor = [s for s in segments if s.get("category") == "sponsor"]
    first_t = sponsor[0]["start"] if sponsor else 0.0
    found = []  # (brand, time, kind, value, detail)

    def brand_at(t):
        for s in sponsor:
            if s["start"] - 5 <= t <= s["end"] + 5:
                return s.get("brand_canonical") or s.get("brand") or ""
        return (sponsor[0].get("brand_canonical") or sponsor[0].get("brand")) if len(sponsor) == 1 else ""
    for c in hints.get("codes", []):
        found.append((brand_at(first_t), first_t, "code", c.upper(), "description"))
    for d in hints.get("domains", []):
        if not d.lower().startswith(SOCIAL):
            found.append((brand_at(first_t), first_t, "link", d, "description"))
    for s in sponsor:
        text = " ".join(l["text"] for l in lines if l["end"] > s["start"] and l["start"] < s["end"])[:MAX_SCAN_CHARS]
        b = s.get("brand_canonical") or s.get("brand") or ""
        for m in CODE_IN_TEXT_RE.finditer(text):
            found.append((b, s["start"], "code", m.group(1).upper(), "spoken"))
        for m in DOMAIN_IN_TEXT_RE.finditer(text):
            if not m.group(1).lower().startswith(SOCIAL):
                found.append((b, s["start"], "link", m.group(1).lower(), "spoken"))
        for m in SPOKEN_DOMAIN_RE.finditer(text):
            dom = f"{m.group(1)}.{m.group(2)}".lower() + (f"/{m.group(3)}" if m.group(3) else "")
            found.append((b, s["start"], "link", dom, "spoken"))
        for pat, label in OFFER_IN_TEXT:
            for m in re.finditer(pat, text, re.I):
                found.append((b, s["start"], "offer", m.group(0).lower(), label))
    if not found:
        return 0
    n = 0
    with _lock, connect() as c:
        for brand, t, kind, value, detail in found:
            cur = c.execute("""INSERT OR IGNORE INTO codes(brand, video_id, channel_name, title, time_s, kind, value, detail, created_at)
                               VALUES (?,?,?,?,?,?,?,?,?)""",
                            (brand, job["video_id"], job.get("creator", ""), job.get("title", ""), round(t, 1), kind, value[:120], detail, now()))
            n += cur.rowcount
    return n


def codes_json(query=""):
    q = f"%{query.strip().lower()}%" if query.strip() else None
    with _lock, connect() as c:
        if q:
            rows = c.execute("SELECT * FROM codes WHERE lower(brand) LIKE ? OR lower(value) LIKE ? OR lower(channel_name) LIKE ? "
                             "OR lower(title) LIKE ? ORDER BY created_at DESC, id DESC LIMIT 500", (q, q, q, q)).fetchall()
        else:
            rows = c.execute("SELECT * FROM codes ORDER BY created_at DESC, id DESC LIMIT 500").fetchall()
    return {"count": len(rows), "codes": [dict(r) for r in rows]}


def videos_without_duration():
    with _lock, connect() as c:
        return [r["video_id"] for r in c.execute("SELECT video_id FROM videos WHERE duration_s IS NULL OR duration_s <= 0")]


def set_duration(video_id, seconds):
    with _lock, connect() as c:
        c.execute("UPDATE videos SET duration_s = ? WHERE video_id = ? AND (duration_s IS NULL OR duration_s <= 0)", (seconds, video_id))


def repair_legacy_rows(current_post_version):
    """One-time, idempotent repairs for rows written by older builds:
    - a video with no channel gets it from its own client events (which always carried channelId/channelName);
    - disclosure columns recorded as 0 when the data was simply missing (before the tri-state rule, post < 3)
      are reset to NULL = unknown.
    Returns (channels_fixed, disclosure_reset)."""
    fixed = reset = 0
    with _lock, connect() as c:
        for v in c.execute("SELECT video_id, channel_id, channel_name FROM videos WHERE channel_name = '' OR channel_id = ''").fetchall():
            ev = c.execute("SELECT channel_id, extra FROM events WHERE video_id = ? AND channel_id != '' ORDER BY id DESC LIMIT 1",
                           (v["video_id"],)).fetchone()
            if not ev:
                continue
            try:
                name = (json.loads(ev["extra"] or "{}") or {}).get("channelName") or ""
            except ValueError:
                name = ""
            c.execute("UPDATE videos SET channel_id = CASE WHEN channel_id = '' THEN ? ELSE channel_id END, "
                      "channel_name = CASE WHEN channel_name = '' THEN ? ELSE channel_name END WHERE video_id = ?",
                      (ev["channel_id"], str(name)[:100], v["video_id"]))
            fixed += 1
        try:
            for v in c.execute("SELECT video_id, versions FROM videos").fetchall():
                try:
                    post = (json.loads(v["versions"] or "{}") or {}).get("post") or 0
                except ValueError:
                    post = 0
                if post < 3:
                    cur = c.execute("UPDATE video_context SET paid_promotion = NULL, disclosed = NULL "
                                    "WHERE video_id = ? AND (paid_promotion IS NOT NULL OR disclosed IS NOT NULL)", (v["video_id"],))
                    reset += cur.rowcount
        except sqlite3.OperationalError:
            pass  # video_context is created by intel.init()
    return fixed, reset


def renormalize_brands():
    """Once per BRAND_RULES_VERSION (kept in PRAGMA user_version): re-derive every stored canonical brand under the
    current rules. Learned aliases are derived data too, so they are dropped and re-learned from the segments.
    Events and promo-code rows follow their segment's brand. Returns None when already done, else
    (segments_changed, aliases_dropped)."""
    with _lock, connect() as c:
        if c.execute("PRAGMA user_version").fetchone()[0] >= BRAND_RULES_VERSION:
            return None
        dropped = c.execute("DELETE FROM brand_aliases WHERE how != 'builtin'").rowcount
        rows = c.execute("SELECT s.*, v.hints FROM segments s LEFT JOIN videos v ON v.video_id = s.video_id ORDER BY s.id").fetchall()
        c.execute("UPDATE segments SET brand = ''")  # the fuzzy match against known brands must not see an old value
        changed = 0
        for r in rows:
            try:
                hints = json.loads(r["hints"] or "{}") or {}
            except ValueError:
                hints = {}
            brand, how = canonical_brand(c, r["brand_raw"], hints, r["evidence"] or "", r["category"] or "", r["source"] or "")
            c.execute("UPDATE segments SET brand = ? WHERE id = ?", (brand, r["id"]))
            c.execute("UPDATE events SET brand = ? WHERE video_id = ? AND seg_start <= ? AND seg_end >= ?",
                      (brand, r["video_id"], r["seg_end"], r["seg_start"]))
            if r["category"] == "sponsor":
                c.execute("UPDATE codes SET brand = ? WHERE video_id = ? AND brand != '' AND time_s BETWEEN ? AND ?",
                          (brand, r["video_id"], r["seg_start"] - 5, r["seg_end"] + 5))
            if brand != (r["brand"] or ""):
                changed += 1
                _log(f"stats: [{r['video_id']}] {r['category']} {fmt_time(r['seg_start'])}-{fmt_time(r['seg_end'])} "
                     f"brand {r['brand']!r} -> {brand!r} ({how}; spoken {r['brand_raw']!r})")
        c.execute(f"PRAGMA user_version = {BRAND_RULES_VERSION}")
    return changed, dropped


def record_event(data):
    """Client event -> events row. Returns (ok, error)."""
    kind = str(data.get("kind") or "")
    if kind not in EVENT_KINDS:
        return False, "bad kind"
    seg = data.get("segment") if isinstance(data.get("segment"), dict) else {}

    def num(x):
        try:
            f = float(x) if x is not None else None
        except (TypeError, ValueError):
            return None
        return f if f is None or math.isfinite(f) else None
    vid = str(data.get("videoId") or "")[:11]
    with _lock, connect() as c:
        brand = ""
        if seg.get("start") is not None:
            row = c.execute("SELECT brand FROM segments WHERE video_id=? AND seg_start<=? AND seg_end>=?",
                            (vid, num(seg.get("end")) or 0, num(seg.get("start")) or 0)).fetchone()
            brand = row["brand"] if row else str(seg.get("brand") or "")[:80]
        c.execute("""INSERT INTO events(ts, kind, video_id, channel_id, seg_start, seg_end, playhead, saved_s, brand, extra)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (now(), kind, vid, str(data.get("channelId") or "")[:40], num(seg.get("start")), num(seg.get("end")),
                   num(data.get("playhead")), num(data.get("saved")), brand,
                   json.dumps({k: v for k, v in data.items() if k in ("channelName", "title")})[:500]))
    return True, None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def pct(a, b):
    return round(100.0 * a / b) if b else 0


def verdict_for(b):
    """Templated one-liner from a brand's numbers."""
    name, spots = b["brand"], b["spots"]
    if b["integration_pct"] >= 60:
        tail = f"watched through {b['watch_through']}×" if b["watch_through"] else "never skipped" if not b["skips"] else ""
        return f"{name} — {b['integration_pct']}% integrations{', ' + tail if tail else ''}. This is how it's done."
    if b["skips"] and b["read_pct"] >= 80:
        return (f"{name} — {spots} spot{'s' if spots != 1 else ''}, {b['read_pct']}% scripted reads, "
                f"{fmt_dur(b['skipped_s'])} skipped. Let creators actually use the product.")
    if b["wrong_skips"]:
        return f"{name} — {b['wrong_skips']} wrong-skip flag{'s' if b['wrong_skips'] != 1 else ''}; the model is over-eager here."
    if b["skips"] == 0 and spots:
        return f"{name} — {spots} spot{'s' if spots != 1 else ''}, nothing skipped yet."
    return (f"{name} — {spots} spot{'s' if spots != 1 else ''}, {b['read_pct']}% reads, "
            f"{b['skips']} skip{'s' if b['skips'] != 1 else ''}, avg {fmt_dur(b['avg_len_s'])}.")


def compute():
    """All dashboard numbers in one dict."""
    with _lock, connect() as c:
        videos = [dict(r) for r in c.execute("SELECT * FROM videos")]
        segs = [dict(r) for r in c.execute("SELECT s.*, v.channel_id, v.channel_name, v.duration_s, x.paid_promotion, x.disclosed "
                                           "FROM segments s LEFT JOIN videos v ON v.video_id = s.video_id "
                                           "LEFT JOIN video_context x ON x.video_id = s.video_id")]
        events = [dict(r) for r in c.execute("SELECT * FROM events")]
        aliases = [dict(r) for r in c.execute("SELECT * FROM brand_aliases WHERE how != 'builtin'")]

    sponsor = [s for s in segs if s["category"] == "sponsor"]
    by_seg_key = {}
    for s in segs:
        by_seg_key[(s["video_id"], s["seg_start"], s["seg_end"])] = s

    def seg_of(e):
        if e["seg_start"] is None:
            return None
        for s in segs:
            if s["video_id"] == e["video_id"] and s["seg_start"] <= (e["seg_end"] or e["seg_start"]) and s["seg_end"] >= e["seg_start"]:
                return s
        return None

    # per brand
    brands = {}
    for s in sponsor:
        b = brands.setdefault(s["brand"] or "(unknown)", {
            "brand": s["brand"] or "(unknown)", "spots": 0, "reads": 0, "integrations": 0, "creators": set(), "len_s": 0.0,
            "skips": 0, "skipped_s": 0.0, "undos": 0, "seek_into": 0, "wrong_skips": 0, "raw": set(), "creator_counts": {},
            "undisclosed": 0})
        b["spots"] += 1
        if s["style"] == "read" and s.get("paid_promotion") == 0 and s.get("disclosed") == 0:
            b["undisclosed"] += 1  # only when both signals are known and negative
        b["reads" if s["style"] == "read" else "integrations"] += 1
        b["creators"].add(s["channel_name"] or s["channel_id"] or "?")
        b["creator_counts"][s["channel_name"] or "?"] = b["creator_counts"].get(s["channel_name"] or "?", 0) + 1
        b["len_s"] += s["seg_end"] - s["seg_start"]
        if s["brand_raw"]:
            b["raw"].add(s["brand_raw"])
    for e in events:
        s = seg_of(e)
        name = (s["brand"] if s else e["brand"]) or "(unknown)"
        if name not in brands:
            continue
        b = brands[name]
        if e["kind"] == "skip":
            b["skips"] += 1
            b["skipped_s"] += e["saved_s"] or 0
        elif e["kind"] == "undo":
            b["undos"] += 1
            b["skipped_s"] -= e["saved_s"] or 0
        elif e["kind"] == "seek_into":
            b["seek_into"] += 1
        elif e["kind"] == "false_skip":
            b["wrong_skips"] += 1
    brand_rows = []
    for b in brands.values():
        wt = b["undos"] + b["seek_into"]
        row = {
            "brand": b["brand"], "spots": b["spots"], "creators": len(b["creators"]),
            "read_pct": pct(b["reads"], b["spots"]), "integration_pct": pct(b["integrations"], b["spots"]),
            "skips": b["skips"], "skipped_s": max(0.0, round(b["skipped_s"], 1)), "avg_len_s": round(b["len_s"] / b["spots"], 1),
            "watch_through": wt, "watch_through_pct": min(100, pct(wt, b["skips"])) if b["skips"] else 0,
            "wrong_skips": b["wrong_skips"], "skip_rate": round(b["skips"] / b["spots"], 2), "undisclosed": b["undisclosed"],
            "top_creators": [k for k, _ in sorted(b["creator_counts"].items(), key=lambda x: -x[1])[:3]],
            "raw_spellings": sorted(b["raw"]),
        }
        row["verdict"] = verdict_for(row)
        brand_rows.append(row)
    brand_rows.sort(key=lambda r: (-r["spots"], -r["skipped_s"]))

    # per creator
    creators = {}
    for v in videos:
        cr = creators.setdefault(v["channel_name"] or v["channel_id"] or "?", {
            "creator": v["channel_name"] or v["channel_id"] or "?", "videos": 0, "video_minutes": 0.0, "ad_s": 0.0,
            "spots": 0, "integrations": 0, "brands": set(), "videos_with_ads": 0})
        cr["videos"] += 1
        cr["video_minutes"] += (v["duration_s"] or 0) / 60.0
        vs = [s for s in sponsor if s["video_id"] == v["video_id"]]
        if vs:
            cr["videos_with_ads"] += 1
        for s in vs:
            cr["spots"] += 1
            cr["ad_s"] += s["seg_end"] - s["seg_start"]
            cr["integrations"] += 1 if s["style"] == "integration" else 0
            if s["brand"]:
                cr["brands"].add(s["brand"])
    creator_rows = []
    for cr in creators.values():
        creator_rows.append({
            "creator": cr["creator"], "videos": cr["videos"], "spots": cr["spots"],
            "ad_density": round(cr["ad_s"] / cr["video_minutes"], 2) if cr["video_minutes"] else None,
            "ad_s": round(cr["ad_s"]), "integration_pct": pct(cr["integrations"], cr["spots"]),
            "brands": sorted(cr["brands"]), "videos_with_ads_pct": pct(cr["videos_with_ads"], cr["videos"]),
        })
    creator_rows.sort(key=lambda r: -(r["ad_density"] or 0))

    # overall
    saved = sum((e["saved_s"] or 0) for e in events if e["kind"] == "skip") - sum((e["saved_s"] or 0) for e in events if e["kind"] == "undo")
    with_sponsor = {s["video_id"] for s in sponsor}
    hist, unplaced = [0] * 10, 0
    for s in sponsor:
        if s["duration_s"]:
            i = max(0, min(9, int(10 * s["seg_start"] / s["duration_s"])))
            hist[i] += 1
        else:
            unplaced += 1  # video length unknown: cannot say where in the video the ad sits
    overall = {
        "videos": len(videos), "videos_with_sponsor_pct": pct(len(with_sponsor), len(videos)),
        "ads_per_video": round(len(sponsor) / len(videos), 2) if videos else 0,
        "spots": len(sponsor), "brands": len(brand_rows), "creators": len(creator_rows),
        "skips": sum(1 for e in events if e["kind"] == "skip"), "saved_s": max(0, round(saved)),
        "undos": sum(1 for e in events if e["kind"] == "undo"), "wrong_skips": sum(1 for e in events if e["kind"] == "false_skip"),
        "missed_ads": sum(1 for e in events if e["kind"] == "missed_ad"),
        "position_hist": hist, "position_unplaced": unplaced, "avg_ad_s": round(sum(s["seg_end"] - s["seg_start"] for s in sponsor) / len(sponsor), 1) if sponsor else 0,
    }

    # per category
    cats = {}
    for s in segs:
        key = s["category"] + ("/" + s["style"] if s["category"] == "sponsor" else "")
        c = cats.setdefault(key, {"category": key, "spots": 0, "videos": set(), "len_s": 0.0, "skips": 0, "skipped_s": 0.0, "undos": 0, "seek_into": 0})
        c["spots"] += 1
        c["videos"].add(s["video_id"])
        c["len_s"] += s["seg_end"] - s["seg_start"]
    for e in events:
        sg = seg_of(e)
        if not sg:
            continue
        key = sg["category"] + ("/" + sg["style"] if sg["category"] == "sponsor" else "")
        c = cats.get(key)
        if not c:
            continue
        if e["kind"] == "skip":
            c["skips"] += 1
            c["skipped_s"] += e["saved_s"] or 0
        elif e["kind"] == "undo":
            c["undos"] += 1
            c["skipped_s"] -= e["saved_s"] or 0
        elif e["kind"] == "seek_into":
            c["seek_into"] += 1
    category_rows = sorted([{"category": c["category"], "spots": c["spots"], "videos": len(c["videos"]),
                             "avg_len_s": round(c["len_s"] / c["spots"], 1), "total_s": round(c["len_s"]),
                             "skips": c["skips"], "skipped_s": max(0.0, round(c["skipped_s"], 1)), "undos": c["undos"], "seek_into": c["seek_into"]}
                            for c in cats.values()], key=lambda r: -r["spots"])

    # A brand is ranked only once it has HALL_MIN_SPOTS spots: one skipped ad is an anecdote, not a pattern.
    ranked = [r for r in brand_rows if r["spots"] >= HALL_MIN_SPOTS]
    shame = sorted([r for r in ranked if r["skips"] >= 1],
                   key=lambda r: -(r["skip_rate"] * r["avg_len_s"]))[:5]
    fame = sorted([r for r in ranked if r["integration_pct"] >= 50 or r["skips"] == 0 or r["watch_through_pct"] >= 50],
                  key=lambda r: (-r["integration_pct"], -r["watch_through_pct"], r["skips"]))[:5]
    return {"generated_at": now(), "overall": overall, "brands": brand_rows, "creators": creator_rows,
            "categories": category_rows, "hall_of_shame": shame, "hall_of_fame": fame, "hall_min_spots": HALL_MIN_SPOTS,
            "aliases": aliases}


# ---------------------------------------------------------------------------
# Dashboard page (single HTML, inline CSS/JS, no external deps)
# ---------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>second read · stats</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/ui/tokens.css"><link rel="stylesheet" href="/ui/fonts.css"><link rel="stylesheet" href="/ui/components.css">
<style>html,body{margin:0}body{min-height:100vh;border-radius:0}</style>
</head><body class="sr-ui sr-sheet">
<div class="sr-dash">
  <div class="sr-dash__head">
    <div class="sr-dash__nav">
      <h1 class="sr-dash__title" id="title">second read · stats</h1>
      <div class="sr-seg" role="navigation" aria-label="Sections">
        <a class="sr-seg__btn" href="#stats" data-tab="stats">stats</a><a class="sr-seg__btn" href="#ads" data-tab="ads">ad cards</a><a class="sr-seg__btn" href="#insights" data-tab="insights">insights</a>
      </div>
    </div>
    <span class="sr-dash__sub" id="sub">loading</span>
  </div>
  <div id="root" style="display:flex;flex-direction:column;gap:var(--sr-s7)"></div>
</div>
<script>
// Markup follows DESIGN.md ("Dashboard"): deep tiles for numbers, the histogram as hairline-weight bars with the peak
// in the accent, halls as lists, tables inside a light card. Colour is state only; a warning is a chip with a ring dot.
// ?theme=light|dark forces a theme (the default follows the system)
{const t=new URLSearchParams(location.search).get('theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}
const $=(s)=>document.querySelector(s);
const esc=(s)=>String(s??'').replace(/[&<>"]/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const clock=(s)=>{s=Math.round(s||0);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?`${h}:${String(m).padStart(2,'0')}:${String(x).padStart(2,'0')}`:`${m}:${String(x).padStart(2,'0')}`};
const plural=(n,w)=>`${n} ${w}${n==1?'':'s'}`;
const tile=(n,c)=>`<div class="sr-card sr-tile"><span class="sr-stat__num">${n}</span><span class="sr-stat__cap">${c}</span></div>`;
const section=(title,body)=>`<div><h2>${title}</h2>${body}</div>`;
const empty=(text)=>`<div class="sr-card sr-card--light sr-empty">${text}</div>`;
const flag=(text,title)=>`<span class="sr-chip sr-chip--flag"${title?` title="${esc(title)}"`:''}><span class="sr-dot"></span>${text}</span>`;
const chips=(list)=>list.length?`<div class="sr-chips">${list.map(x=>`<span class="sr-chip">${esc(x)}</span>`).join('')}</div>`:'';
function tiles(o){return `<div class="sr-tiles">`+
  tile(clock(o.saved_s),'time saved')+tile(o.skips,`skips · ${o.undos} undone`)+tile(o.videos,'videos analyzed')+
  tile(o.videos_with_sponsor_pct+'%','videos with a sponsor')+tile(o.ads_per_video,'ads per video')+tile(clock(o.avg_ad_s),'avg ad length')+
  tile(o.brands,`brands · ${plural(o.creators,'creator')}`)+tile(o.wrong_skips,`wrong-skip flags · ${o.missed_ads} missed-ad flags`)+`</div>`}
function hist(h,unplaced){const total=h.reduce((a,b)=>a+b,0),m=Math.max(1,...h),peak=h.indexOf(Math.max(...h));
  const note=unplaced?`<div class="sr-note">${plural(unplaced,'ad')} not placed: video length unknown. Open the video once more to fix.</div>`:'';
  if(!total)return empty('No ads with a known position yet. Watch a sponsored video with the extension on.')+note;
  return `<div class="sr-card" style="padding:var(--sr-s7) var(--sr-s6) var(--sr-s5)"><div class="sr-bars">${h.map((n,i)=>`<div class="sr-bar${n&&i===peak?' is-peak':''}" style="height:${n?Math.max(4,Math.round(100*n/m)):0}%" title="${plural(n,'ad')} start in ${i*10}–${i*10+10}% of the video">${n?`<span>${n}</span>`:''}</div>`).join('')}</div><div class="sr-axis"><span>start of video</span><span>${plural(total,'ad')}</span><span>end of video</span></div></div>${note}`}
function hall(title,rows,name,why,min){return `<div class="sr-card sr-hall"><div class="sr-title">${title}</div>${rows.length?`<ol>${rows.map((r,i)=>`<li><span class="sr-rank">${i+1}</span><span>${esc(name(r))}</span><span class="sr-why">${why(r)}</span></li>`).join('')}</ol>`:`<div class="sr-hall__empty">Nothing yet.${min?` A brand is ranked after ${min} spots.`:''}</div>`}</div>`}
function table(head,rows){return `<div class="sr-card sr-card--light" style="padding:var(--sr-s4) var(--sr-s2);overflow-x:auto"><table class="sr-table"><thead><tr>${head.map(([t,num])=>`<th${num?' class="sr-num"':''}>${t}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`}
const num=(v)=>`<td class="sr-num">${v}</td>`;
function categoriesTable(rows){if(!rows||!rows.length)return empty('No segments yet.');
  return table([['category'],['segments',1],['videos',1],['avg len',1],['total',1],['skips',1],['skipped',1],['undo / seek-in',1]],
    rows.map(r=>`<tr><td>${esc(r.category.replace('/',' · '))}</td>${num(r.spots)}${num(r.videos)}${num(clock(r.avg_len_s))}${num(clock(r.total_s))}${num(r.skips)}${num(clock(r.skipped_s))}${num(r.undos+' / '+r.seek_into)}</tr>`))}
function brandsTable(rows){if(!rows.length)return empty('No sponsor segments yet. Watch a sponsored video with the extension on.');
  return table([['brand'],['spots',1],['creators',1],['read / integr.',1],['skips',1],['skipped',1],['avg len',1],['watch-through',1],['wrong',1],['undisclosed?',1],['top creators']],
    rows.map(r=>`<tr><td><div>${esc(r.brand)}</div>${r.raw_spellings.length&&r.raw_spellings.join('')!==r.brand?`<div class="sr-raw">raw: ${esc(r.raw_spellings.join(', '))}</div>`:''}<div class="sr-verdict">${esc(r.verdict)}</div></td>${num(r.spots)}${num(r.creators)}${num(r.read_pct+'% / '+r.integration_pct+'%')}${num(r.skips)}${num(r.skips?clock(r.skipped_s):'–')}${num(clock(r.avg_len_s))}${num(r.skips?r.watch_through_pct+'%':'–')}${num(r.wrong_skips||'–')}${num(r.undisclosed||'–')}<td>${chips(r.top_creators)}</td></tr>`))}
function creatorsTable(rows){if(!rows.length)return empty('No creators yet.');
  return table([['creator'],['videos',1],['with ads',1],['spots',1],['ad density',1],['ad time',1],['integrations',1],['brands']],
    rows.map(r=>`<tr><td>${esc(r.creator)}</td>${num(r.videos)}${num(r.videos_with_ads_pct+'%')}${num(r.spots)}${num(r.ad_density==null?'–':r.ad_density+' s/min')}${num(clock(r.ad_s))}${num(r.integration_pct+'%')}<td>${chips(r.brands)}</td></tr>`))}
function renderStats(d){return tiles(d.overall)+section('where ads sit in a video',hist(d.overall.position_hist,d.overall.position_unplaced))+
  `<div class="sr-halls">`+
  hall('Hall of shame',d.hall_of_shame,r=>r.brand,r=>`skipped ${r.skips}× over ${plural(r.spots,'spot')} · ${clock(r.avg_len_s)} reads`,d.hall_min_spots)+
  hall('Hall of fame',d.hall_of_fame,r=>r.brand,r=>r.integration_pct?`${r.integration_pct}% integrations`:(r.skips?`watched through ${r.watch_through_pct}%`:'never skipped'),d.hall_min_spots)+`</div>`+
  section('by category',categoriesTable(d.categories))+section('brands',brandsTable(d.brands))+section('creators',creatorsTable(d.creators))+
  (d.aliases.length?`<div class="sr-note">brand aliases learned: ${d.aliases.map(a=>`${esc(a.alias)} → ${esc(a.brand)} (${esc(a.how)})`).join(' · ')}</div>`:'')}

// ---- ad cards
const pctv=(v)=>v==null?'–':(v>=0?'+':'')+v+'%';
function timeline(a){const D=a.duration_s||Math.max(a.end,1);const px=(t)=>Math.max(0,Math.min(100,100*t/D));
  const peaks=new Set(((a.metrics||{}).peaks_pct||[]).map(p=>Math.round(p)));
  const heat=(a.heat||[]).length?`<div class="sr-timeline__heat" title="most replayed">${a.heat.map(p=>`<i${peaks.has(Math.round(p[0]))?' class="is-peak"':''} style="height:${Math.max(1,Math.round(p[1]*0.18))}px"></i>`).join('')}</div>`:'';
  const ticks=(a.chapters||[]).map(c=>`<div class="sr-timeline__tick" style="left:${px(c[0])}%" title="${esc(c[1])} @ ${clock(c[0])}"></div>`).join('');
  return `<div class="sr-timeline">${heat}<div class="sr-hairline"><div class="sr-hairline__track">${ticks}<div class="sr-hairline__span${a.action==='skip'?' sr-hairline__span--accent':''}" style="left:${px(a.start)}%;width:${Math.max(0.6,px(a.end)-px(a.start))}%"></div></div><div class="sr-hairline__ends"><span>${clock(a.start)} → ${clock(a.end)}</span><span>${a.duration_s?clock(a.duration_s):''}</span></div></div></div>`}
function adCard(a){const m=a.metrics||{},an=a.analysis,ev=a.events||{},pace=m.pace||{};
  const head=[`<span class="sr-chip">${a.style==='integration'?'integration':'sponsor · read'}</span>`,
    `<span class="sr-chip">${a.action==='skip'?'skips':a.action==='keep'?'plays':'label only'}</span>`,
    a.undisclosed?flag('undisclosed?','YouTube’s paid-promotion flag is off and the description has no (ad)/sponsor line'):'',
    a.disclosure==='unknown'?`<span class="sr-chip" title="The paid-promotion flag and/or the description were not received for this video">disclosure unknown</span>`:'',
    a.paid_promotion?`<span class="sr-chip">paid promotion</span>`:'',
    m.template_script?flag('template script','At least 70% of the wording is shared with another creator’s ad for this brand'):'',
    ev.false_skip?flag(plural(ev.false_skip,'wrong-skip flag')):''].join('');
  const kv=[['position',m.position_pct!=null?m.position_pct+'% into the video':'–'],
    ['after the hook',m.in_hook?'inside the first 60s':(m.after_hook_s!=null?clock(m.after_hook_s)+' after it':'–')],
    ['chapter',m.chapter?`${esc(m.chapter)} · ${esc(m.chapter_placement)}`:esc(m.chapter_placement||'–')],
    ['next replay peak',m.next_peak_s==null?'–':`${clock(m.next_peak_s)} after the ad${m.before_top_peak?' · right before a top-3 peak':''}`],
    ['segue',esc(m.segue||'–')],
    ['pace',pace.delta_pct==null?'–':`${pctv(pace.delta_pct)} vs the creator (${pace.ad_wps} vs ${pace.creator_median_wps} words/s)`],
    ['calls to action',m.cta_count!=null?m.cta_count:'–'],
    ['offers',(m.offers||[]).map(o=>esc(o.text)).join(', ')||'none'],
    ['template',m.template_similarity!=null?`${Math.round(m.template_similarity*100)}% shared with another creator’s ad`:'–'],
    ['topic fit',m.topic_fit!=null?m.topic_fit:'– (embeddings off)'],
    ['viewers',`${plural(ev.skip||0,'skip')} · ${ev.undo||0} undone · ${ev.seek_into||0} sought back`]];
  const opens=m.first_sentence?`<div class="sr-quote sr-quote--dim">opens with “${esc(m.first_sentence)}”</div>`:'';
  const beats=an&&an.beats.length?`<div class="sr-quote sr-quote--dim">${an.beats.map(b=>esc(b.beat)).join(' → ')}</div>`:'';
  const tags=an&&an.persuasion.length?`<div class="sr-tags">${an.persuasion.map(p=>`<div class="sr-tag"><span class="sr-chip">${esc(p.tag)}</span><span class="sr-quote">“${esc(p.quote)}”</span></div>`).join('')}</div>`:'';
  const bp=an&&an.better_placement;
  const sugg=bp?(bp.same_as_now?`<div class="sr-inset"><span class="sr-inset__label">placement</span><span>Already the natural spot. ${esc(bp.reason)}</span></div>`
      :`<div class="sr-inset"><span class="sr-inset__label">better placement · ${clock(bp.time_s)}</span><span>${esc(bp.reason)}</span>${bp.quote?`<span class="sr-quote sr-quote--dim">“${esc(bp.quote)}”${bp.quote_verified?'':' (quote not found verbatim)'}</span>`:''}</div>`)
    :`<div class="sr-inset"><span class="sr-inset__label">deeper analysis</span><span>${a.analysis_error?'Failed: '+esc(a.analysis_error):'Pending. It runs while the server is idle.'}</span></div>`;
  return `<div class="sr-card sr-adcard"><div class="sr-adcard__head"><span class="sr-adcard__title">${esc(a.brand||'Unknown brand')}</span>${head}</div>`+
    `<div class="sr-adcard__meta">${esc(a.creator||'unknown creator')} · ${esc(a.title||a.video_id)} · ${clock(a.end-a.start)} long · confidence ${Number(a.confidence).toFixed(2)}${a.brand_raw&&a.brand_raw!==a.brand?` · heard as “${esc(a.brand_raw)}”`:''}</div>`+
    timeline(a)+`<dl class="sr-kv">${kv.map(([k,v])=>`<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl>${opens}${beats}${tags}${sugg}</div>`}
function dist(title,obj){const e=Object.entries(obj||{}).sort((a,b)=>b[1]-a[1]);
  return `<div class="sr-card sr-hall"><div class="sr-title">${title}</div>${e.length?`<ol>${e.map(([k,v])=>`<li><span>${esc(k||'–')}</span><span class="sr-why">${v}</span></li>`).join('')}</ol>`:'<div class="sr-hall__empty">Nothing yet.</div>'}</div>`}
function insights(i){return `<div class="sr-tiles">`+
  tile(i.ads,`ads · ${i.analyzed} analyzed in depth · ${i.pending_analysis} pending`)+
  tile(i.avg_position_pct==null?'–':Math.round(i.avg_position_pct)+'%',`avg position in the video · ${i.in_hook_pct}% inside the hook`)+
  tile(i.before_top_peak_pct+'%','placed right before a most-replayed peak')+tile(pctv(i.avg_pace_delta_pct),'reading pace vs the creator')+
  tile(i.avg_cta==null?'–':i.avg_cta,'calls to action per ad')+tile(i.template_pct+'%','template scripts (≥ 70% shared with another creator)')+
  tile(i.avg_topic_fit==null?'–':i.avg_topic_fit,'avg topic fit (cosine)')+tile(i.better_placement_suggested,'ads with a better placement suggested')+
  tile(i.undisclosed,`reads with no disclosure · ${i.disclosure_unknown} unknown · ${i.paid_promotion_pct}% carry YouTube’s flag`)+`</div>`+
  section('distributions',`<div class="sr-halls">${dist('Segue type',i.segues)}${dist('Chapter placement',i.placements)}${dist('Offers',i.offers)}${dist('Persuasion techniques',i.persuasion)}</div>`)}

let cache={};
const NAMES={stats:'stats',ads:'ad cards',insights:'insights'};
async function show(tab){
  document.querySelectorAll('.sr-seg a').forEach(a=>{if(a.dataset.tab===tab)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')});
  $('#title').textContent=`second read · ${NAMES[tab]}`;
  if(tab==='stats'){const d=cache.stats||(cache.stats=await (await fetch('/stats.json')).json());
    const o=d.overall;$('#sub').textContent=`${plural(o.videos,'video')} · ${plural(o.creators,'creator')} · ${plural(o.brands,'brand')} · local only · ${d.generated_at.replace('T',' ')}`;
    $('#root').innerHTML=renderStats(d);return}
  const d=cache.ads||(cache.ads=await (await fetch('/ads.json')).json());
  $('#sub').textContent=`${plural(d.ads.length,'ad')} · local only · ${d.generated_at.replace('T',' ')}${d.embed_model?' · embeddings '+d.embed_model:''}`;
  $('#root').innerHTML=tab==='ads'?(d.ads.length?`<div class="sr-ads">${d.ads.map(adCard).join('')}</div>`:empty('No sponsor segments yet. Watch a sponsored video with the extension on.')):insights(d.insights)}
function route(){const t=(location.hash||'#stats').slice(1);show(NAMES[t]?t:'stats').catch(e=>{$('#sub').textContent='Could not load the data. Is the server still running? ('+e+')'})}
window.addEventListener('hashchange',route);route();
</script></body></html>
"""
