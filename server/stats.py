"""Sponsor Skipper — local stats: SQLite store, brand normalization, metrics, dashboard page.

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

DB_PATH = os.path.join(os.environ.get("SS_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache"),
                       "stats.local.db")
_lock = threading.Lock()

EVENT_KINDS = ("skip", "undo", "seek_into", "false_skip", "missed_ad", "allowlisted")

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


def hint_candidates(hints):
    """Canonical-name candidates from description hints: brands, and domain labels/paths."""
    out = []
    for b in (hints or {}).get("brands", []):
        out.append(b)
    for d in (hints or {}).get("domains", []):
        host = d.split("/")[0]
        parts = host.split(".")
        label = parts[-2] if len(parts) >= 2 else host
        if len(label) >= 3:
            out.append(_display(label))
        if "/" in d:
            path = d.split("/", 1)[1].split("/")[0]
            if len(path) >= 3 and not path.isdigit():
                out.append(_display(path))
    seen, uniq = set(), []
    for x in out:
        if _norm(x) and _norm(x) not in seen:
            seen.add(_norm(x))
            uniq.append(x)
    return uniq


def canonical_brand(conn, raw, hints=None, evidence=""):
    """Resolve a model-emitted brand spelling to a canonical brand. Order:
    1. alias table (learned + builtin); 2. fuzzy match against description-hint candidates (the video's own
    sponsor) and record the alias; 3. exactly one hint candidate -> that; 4. fuzzy match against brands already
    in the DB (>= 0.8); 5. the raw spelling, trimmed."""
    raw = (raw or "").strip()
    key = _norm(raw)
    if not key:
        cands = hint_candidates(hints)
        return (cands[0], "hint-only") if len(cands) == 1 else ("", "none")
    row = conn.execute("SELECT brand FROM brand_aliases WHERE alias = ?", (key,)).fetchone()
    if row:
        return row["brand"], "alias"
    cands = hint_candidates(hints)
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
            conn.execute("INSERT OR IGNORE INTO brand_aliases(alias, brand, how) VALUES (?, ?, ?)",
                         (key, best, f"hint fuzzy {best_r:.2f}"))
        return best, f"hint fuzzy {best_r:.2f}"
    if len(cands) == 1:
        return cands[0], "single hint (this video only)"
    known = [r["brand"] for r in conn.execute("SELECT DISTINCT brand FROM segments WHERE brand != ''")]
    best, best_r = None, 0.0
    for b in known:
        r = difflib.SequenceMatcher(None, key, _norm(b)).ratio()
        if r > best_r:
            best, best_r = b, r
    if best and best_r >= 0.8:
        conn.execute("INSERT OR IGNORE INTO brand_aliases(alias, brand, how) VALUES (?, ?, ?)", (key, best, f"db fuzzy {best_r:.2f}"))
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
            brand, how = canonical_brand(c, s.get("brand", ""), hints, s.get("evidence", ""))
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
    hist = [0] * 10
    for s in sponsor:
        if s["duration_s"]:
            i = min(9, int(10 * s["seg_start"] / s["duration_s"]))
            hist[i] += 1
    overall = {
        "videos": len(videos), "videos_with_sponsor_pct": pct(len(with_sponsor), len(videos)),
        "ads_per_video": round(len(sponsor) / len(videos), 2) if videos else 0,
        "spots": len(sponsor), "brands": len(brand_rows), "creators": len(creator_rows),
        "skips": sum(1 for e in events if e["kind"] == "skip"), "saved_s": max(0, round(saved)),
        "undos": sum(1 for e in events if e["kind"] == "undo"), "wrong_skips": sum(1 for e in events if e["kind"] == "false_skip"),
        "missed_ads": sum(1 for e in events if e["kind"] == "missed_ad"),
        "position_hist": hist, "avg_ad_s": round(sum(s["seg_end"] - s["seg_start"] for s in sponsor) / len(sponsor), 1) if sponsor else 0,
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

    shame = sorted([r for r in brand_rows if r["skips"] >= 1],
                   key=lambda r: -(r["skip_rate"] * r["avg_len_s"]))[:5]
    fame = sorted([r for r in brand_rows if r["integration_pct"] >= 50 or (r["spots"] >= 2 and r["skips"] == 0) or r["watch_through_pct"] >= 50],
                  key=lambda r: (-r["integration_pct"], -r["watch_through_pct"], r["skips"]))[:5]
    return {"generated_at": now(), "overall": overall, "brands": brand_rows, "creators": creator_rows,
            "categories": category_rows, "hall_of_shame": shame, "hall_of_fame": fame, "aliases": aliases}


# ---------------------------------------------------------------------------
# Dashboard page (single HTML, inline CSS/JS, no external deps)
# ---------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Sponsor Skipper — stats</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fff;--fg:#1f1f1f;--muted:#6b6b6b;--line:#e6e6e6;--card:#f6f6f7;--red:#e53935;--green:#43a047;--amber:#f9a825}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#f1f1f1;--muted:#a0a0a0;--line:#333;--card:#242424}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 20px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 10px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.sub{color:var(--muted);font-size:12px}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:14px}
.tile{background:var(--card);border-radius:10px;padding:12px 14px}.tile .n{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}.tile .c{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}.verdict{color:var(--muted);font-size:12px}.raw{color:var(--muted);font-size:11px}
.halls{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:700px){.halls{grid-template-columns:1fr}}
.hall{background:var(--card);border-radius:10px;padding:12px 14px}.hall h3{margin:0 0 8px;font-size:15px}.hall li{margin:4px 0}.hall ol{padding-left:20px;margin:0}
.bars{display:flex;align-items:flex-end;gap:4px;height:90px;margin-top:8px}.bar{flex:1;background:var(--red);opacity:.85;border-radius:3px 3px 0 0;min-height:2px;position:relative}
.bar span{position:absolute;bottom:-18px;left:0;right:0;text-align:center;font-size:10px;color:var(--muted)}.axis{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:22px}
.empty{color:var(--muted);padding:30px;text-align:center;background:var(--card);border-radius:10px}
.pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;background:var(--line);margin:1px 2px 1px 0}
.tabs{display:flex;gap:4px;margin:14px 0 4px;border-bottom:1px solid var(--line)}.tabs a{padding:8px 12px;color:var(--muted);text-decoration:none;border-bottom:2px solid transparent;margin-bottom:-1px}.tabs a.on{color:var(--fg);border-color:var(--red)}
.card{background:var(--card);border-radius:12px;padding:14px 16px;margin:12px 0}.card h3{margin:0 0 2px;font-size:15px}.card .meta{font-size:12px;color:var(--muted);margin-bottom:8px}
.tl{position:relative;height:34px;margin:8px 0 4px}.tl .track{position:absolute;left:0;right:0;top:14px;height:6px;background:var(--line);border-radius:3px}
.tl .ad{position:absolute;top:12px;height:10px;background:var(--red);border-radius:3px;min-width:3px}.tl .ch{position:absolute;top:10px;width:1px;height:14px;background:var(--muted);opacity:.6}
.tl .pk{position:absolute;top:2px;width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-top:7px solid var(--amber)}
.tl .heat{position:absolute;left:0;right:0;top:22px;height:10px}.tl .heat i{position:absolute;bottom:0;width:1%;background:var(--amber);opacity:.5}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:4px 14px;font-size:12px}.kv b{font-weight:600}.kv span{color:var(--muted)}
.tags{margin-top:8px}.tag{display:inline-block;background:var(--line);border-radius:8px;padding:3px 8px;font-size:12px;margin:2px 4px 2px 0}.tag q{color:var(--muted);font-style:italic}
.sugg{margin-top:8px;padding:8px 10px;border-left:3px solid var(--green);background:var(--bg);border-radius:6px;font-size:12px}.sugg q{color:var(--muted)}
.flag{color:var(--amber);font-weight:600}.beats{font-size:12px;color:var(--muted);margin-top:6px}
.dist{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.dist .card{margin:0}.dist li{font-size:13px;margin:2px 0}.dist ul{margin:4px 0 0;padding-left:18px}
</style></head><body><main>
<h1>Sponsor Skipper</h1><div class="sub" id="sub">loading…</div>
<nav class="tabs"><a href="#stats" data-tab="stats">Stats</a><a href="#ads" data-tab="ads">Ad cards</a><a href="#insights" data-tab="insights">Insights</a></nav>
<div id="root"></div>
</main>
<script>
const $=(s)=>document.querySelector(s);
const esc=(s)=>String(s??'').replace(/[&<>"]/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dur=(s)=>{s=Math.round(s||0);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?`${h}h ${String(m).padStart(2,'0')}m`:m?`${m}m ${String(x).padStart(2,'0')}s`:`${x}s`};
function tiles(o){return `<div class="tiles">
<div class="tile"><div class="n">${dur(o.saved_s)}</div><div class="c">time saved</div></div>
<div class="tile"><div class="n">${o.skips}</div><div class="c">skips (${o.undos} undone)</div></div>
<div class="tile"><div class="n">${o.videos}</div><div class="c">videos analyzed</div></div>
<div class="tile"><div class="n">${o.videos_with_sponsor_pct}%</div><div class="c">videos with a sponsor</div></div>
<div class="tile"><div class="n">${o.ads_per_video}</div><div class="c">ads per video</div></div>
<div class="tile"><div class="n">${dur(o.avg_ad_s)}</div><div class="c">avg ad length</div></div>
<div class="tile"><div class="n">${o.brands}</div><div class="c">brands · ${o.creators} creators</div></div>
<div class="tile"><div class="n">${o.wrong_skips}</div><div class="c">wrong-skip flags · ${o.missed_ads} missed-ad flags</div></div>
</div>`}
function hist(h){const m=Math.max(1,...h);return `<div class="bars">${h.map((n,i)=>`<div class="bar" style="height:${Math.round(100*n/m)}%" title="${n} ad(s) start in ${i*10}–${i*10+10}% of the video"><span>${n||''}</span></div>`).join('')}</div><div class="axis"><span>start of video</span><span>end of video</span></div>`}
function brandsTable(rows){if(!rows.length)return '<div class="empty">No sponsor segments yet.</div>';return `<table><thead><tr><th>brand</th><th class="num">spots</th><th class="num">creators</th><th class="num">read / integr.</th><th class="num">skips</th><th class="num">skipped</th><th class="num">avg len</th><th class="num">watch-through</th><th class="num">wrong</th><th class="num">undisclosed?</th><th>top creators</th></tr></thead><tbody>${rows.map(r=>`<tr><td><b>${esc(r.brand)}</b>${r.raw_spellings.length&&r.raw_spellings.join('')!==r.brand?`<div class="raw">raw: ${esc(r.raw_spellings.join(', '))}</div>`:''}<div class="verdict">${esc(r.verdict)}</div></td><td class="num">${r.spots}</td><td class="num">${r.creators}</td><td class="num">${r.read_pct}% / ${r.integration_pct}%</td><td class="num">${r.skips}</td><td class="num">${dur(r.skipped_s)}</td><td class="num">${dur(r.avg_len_s)}</td><td class="num">${r.skips?r.watch_through_pct+'%':'–'}</td><td class="num">${r.wrong_skips||''}</td><td class="num">${r.undisclosed?`<span class="flag">${r.undisclosed}</span>`:''}</td><td>${r.top_creators.map(esc).join(', ')}</td></tr>`).join('')}</tbody></table>`}
function creatorsTable(rows){if(!rows.length)return '<div class="empty">No creators yet.</div>';return `<table><thead><tr><th>creator</th><th class="num">videos</th><th class="num">with ads</th><th class="num">spots</th><th class="num">ad density</th><th class="num">ad time</th><th class="num">integrations</th><th>brands</th></tr></thead><tbody>${rows.map(r=>`<tr><td><b>${esc(r.creator)}</b></td><td class="num">${r.videos}</td><td class="num">${r.videos_with_ads_pct}%</td><td class="num">${r.spots}</td><td class="num">${r.ad_density==null?'–':r.ad_density+' s/min'}</td><td class="num">${dur(r.ad_s)}</td><td class="num">${r.integration_pct}%</td><td>${r.brands.map(b=>`<span class="pill">${esc(b)}</span>`).join(' ')}</td></tr>`).join('')}</tbody></table>`}
const CAT_COLORS={'sponsor/read':'#e53935','sponsor/integration':'#43a047',affiliate:'#fb8c00',selfpromo:'#fdd835',membership:'#8e24aa',crosspromo:'#1e88e5',charity:'#ec407a',giveaway:'#00897b',interaction:'#9e9e9e',intro:'#4fc3f7',outro:'#4fc3f7',recap:'#b39ddb'};
function categoriesTable(rows){if(!rows||!rows.length)return '<div class="empty">No segments yet.</div>';return `<table><thead><tr><th>category</th><th class="num">segments</th><th class="num">videos</th><th class="num">avg len</th><th class="num">total</th><th class="num">skips</th><th class="num">skipped</th><th class="num">undo / seek-in</th></tr></thead><tbody>${rows.map(r=>`<tr><td><span class="sw" style="display:inline-block;width:10px;height:10px;border-radius:3px;background:${CAT_COLORS[r.category]||'#999'};margin-right:6px"></span><b>${esc(r.category)}</b></td><td class="num">${r.spots}</td><td class="num">${r.videos}</td><td class="num">${dur(r.avg_len_s)}</td><td class="num">${dur(r.total_s)}</td><td class="num">${r.skips}</td><td class="num">${dur(r.skipped_s)}</td><td class="num">${r.undos} / ${r.seek_into}</td></tr>`).join('')}</tbody></table>`}
function hall(title,rows,fmt){return `<div class="hall"><h3>${title}</h3>${rows.length?`<ol>${rows.map(r=>`<li>${fmt(r)}</li>`).join('')}</ol>`:'<div class="sub">nothing yet</div>'}</div>`}
function pctv(v){return v==null?'–':(v>=0?'+':'')+v+'%'}
function timeline(a){const D=a.duration_s||Math.max(a.end,1);const px=(t)=>Math.max(0,Math.min(100,100*t/D));
const heat=(a.heat||[]).length?`<div class="heat">${a.heat.map(p=>`<i style="left:${p[0]}%;height:${Math.max(1,p[1]/10)}px"></i>`).join('')}</div>`:'';
const chs=(a.chapters||[]).map(c=>`<div class="ch" style="left:${px(c[0])}%" title="${esc(c[1])} @ ${fmt(c[0])}"></div>`).join('');
const pks=((a.metrics||{}).peaks_pct||[]).map(p=>`<div class="pk" style="left:calc(${p}% - 4px)" title="most-replayed peak"></div>`).join('');
return `<div class="tl"><div class="track"></div>${heat}${chs}<div class="ad" style="left:${px(a.start)}%;width:${px(a.end)-px(a.start)}%" title="ad ${fmt(a.start)}–${fmt(a.end)}"></div>${pks}</div>`}
const fmt=(s)=>{s=Math.round(s||0);return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`};
function adCard(a){const m=a.metrics||{},an=a.analysis,ev=a.events||{};const pace=m.pace||{};
const kv=[['position',m.position_pct!=null?m.position_pct+'% of video':'–'],['after hook',m.in_hook?'inside the first 60s':(m.after_hook_s!=null?fmt(m.after_hook_s)+' after':'–')],
['chapter',m.chapter?`${esc(m.chapter)} · ${m.chapter_placement}`:(m.chapter_placement||'–')],['next replay peak',m.next_peak_s==null?'–':`${fmt(m.next_peak_s)} after ad${m.before_top_peak?' · <span class="flag">before a top-3 peak</span>':''}`],
['segue',m.segue||'–'],['pace',pace.delta_pct==null?'–':`${pctv(pace.delta_pct)} vs creator (${pace.ad_wps} vs ${pace.creator_median_wps} w/s)`],
['CTAs',m.cta_count!=null?m.cta_count:'–'],['offers',(m.offers||[]).map(o=>esc(o.text)).join(', ')||'none'],
['template',m.template_similarity!=null?`${Math.round(m.template_similarity*100)}% similar to another creator's ${esc(a.brand)} ad${m.template_script?' · <span class="flag">template script</span>':''}`:'–'],
['topic fit',m.topic_fit!=null?m.topic_fit:'– (embeddings off)'],['skips',`${ev.skip||0} skip · ${ev.undo||0} undo · ${ev.seek_into||0} seek-in${ev.false_skip?` · <span class="flag">${ev.false_skip} wrong-skip</span>`:''}`]];
const tags=an&&an.persuasion.length?`<div class="tags">${an.persuasion.map(p=>`<span class="tag">${esc(p.tag)} <q>“${esc(p.quote)}”</q></span>`).join('')}</div>`:(an?'<div class="tags"><span class="sub">no persuasion techniques quoted</span></div>':'');
const beats=an&&an.beats.length?`<div class="beats">structure: ${an.beats.map(b=>esc(b.beat)).join(' → ')}</div>`:'';
const bp=an&&an.better_placement;const sugg=bp?(bp.same_as_now?`<div class="sugg">placement: already the natural spot — ${esc(bp.reason)}</div>`:`<div class="sugg">better placement: <b>${fmt(bp.time_s)}</b> — ${esc(bp.reason)}${bp.quote?` <q>“${esc(bp.quote)}”</q>${bp.quote_verified?'':' <span class="flag">(quote not found verbatim)</span>'}`:''}</div>`):(a.analysis_error?`<div class="sub">analysis failed: ${esc(a.analysis_error)}</div>`:'<div class="sub">deeper analysis pending (runs when the server is idle)</div>');
return `<div class="card"><h3>${esc(a.brand||'?')} <span class="pill">${esc(a.style)}</span> <span class="pill">${esc(a.action)}</span>${a.undisclosed?' <span class="pill flag" title="YouTube paid-promotion flag is off and the description has no (ad)/sponsor line">undisclosed?</span>':''}${a.disclosure==='unknown'?' <span class="pill" title="the paid-promotion flag and/or the description were not received for this video">disclosure unknown</span>':''}${a.paid_promotion?' <span class="pill">paid promotion ✓</span>':''}</h3><div class="meta">${esc(a.creator||'')} · ${esc(a.title||a.video_id)} · ${fmt(a.start)}–${fmt(a.end)} (${fmt(a.end-a.start)}) · conf ${a.confidence}${a.brand_raw&&a.brand_raw!==a.brand?` · raw “${esc(a.brand_raw)}”`:''}</div>${timeline(a)}<div class="kv">${kv.map(([k,v])=>`<div><span>${k}</span><br><b>${v}</b></div>`).join('')}</div>${m.first_sentence?`<div class="beats">opens with: “${esc(m.first_sentence)}”</div>`:''}${beats}${tags}${sugg}</div>`}
function dist(title,obj){const e=Object.entries(obj||{}).sort((a,b)=>b[1]-a[1]);return `<div class="card"><h3>${title}</h3>${e.length?`<ul>${e.map(([k,v])=>`<li>${esc(k||'–')}: <b>${v}</b></li>`).join('')}</ul>`:'<div class="sub">nothing yet</div>'}</div>`}
function insights(i){return `<div class="tiles">
<div class="tile"><div class="n">${i.ads}</div><div class="c">ads · ${i.analyzed} deep-analyzed · ${i.pending_analysis} pending</div></div>
<div class="tile"><div class="n">${i.avg_position_pct==null?'–':i.avg_position_pct+'%'}</div><div class="c">avg position in video · ${i.in_hook_pct}% inside the hook</div></div>
<div class="tile"><div class="n">${i.before_top_peak_pct}%</div><div class="c">placed right before a most-replayed peak</div></div>
<div class="tile"><div class="n">${pctv(i.avg_pace_delta_pct)}</div><div class="c">reading pace vs creator</div></div>
<div class="tile"><div class="n">${i.avg_cta==null?'–':i.avg_cta}</div><div class="c">CTAs per ad</div></div>
<div class="tile"><div class="n">${i.template_pct}%</div><div class="c">template scripts (≥70% shared with another creator)</div></div>
<div class="tile"><div class="n">${i.avg_topic_fit==null?'–':i.avg_topic_fit}</div><div class="c">avg topic fit (cosine)</div></div>
<div class="tile"><div class="n">${i.better_placement_suggested}</div><div class="c">ads with a better placement suggested</div></div>
<div class="tile"><div class="n">${i.undisclosed}</div><div class="c">reads with no disclosure · ${i.disclosure_unknown} unknown · ${i.paid_promotion_pct}% carry YouTube's paid-promotion flag</div></div>
</div><h2>Distributions</h2><div class="dist">${dist('Segue type',i.segues)}${dist('Chapter placement',i.placements)}${dist('Offers',i.offers)}${dist('Persuasion techniques',i.persuasion)}</div>`}
let cache={};
async function show(tab){document.querySelectorAll('.tabs a').forEach(a=>a.classList.toggle('on',a.dataset.tab===tab));
if(tab==='stats'){const d=cache.stats||(cache.stats=await (await fetch('/stats.json')).json());$('#sub').textContent=`local only · generated ${d.generated_at}`;$('#root').innerHTML=renderStats(d);return}
const d=cache.ads||(cache.ads=await (await fetch('/ads.json')).json());$('#sub').textContent=`local only · generated ${d.generated_at}${d.embed_model?' · embeddings: '+d.embed_model:''}`;
$('#root').innerHTML=tab==='ads'?(d.ads.length?d.ads.map(adCard).join(''):'<div class="empty">No sponsor segments yet.</div>'):insights(d.insights)}
function renderStats(d){return tiles(d.overall)+`<h2>Where ads sit in a video</h2>`+hist(d.overall.position_hist)+`<h2>Halls</h2><div class="halls">`+
hall('💀 Hall of Shame',d.hall_of_shame,r=>`<b>${esc(r.brand)}</b> — skip rate ${Math.round(r.skip_rate*100)}%, avg read ${dur(r.avg_len_s)}, ${dur(r.skipped_s)} skipped`)+
hall('🏆 Hall of Fame',d.hall_of_fame,r=>`<b>${esc(r.brand)}</b> — ${r.integration_pct}% integrations, watch-through ${r.watch_through_pct}%, ${r.skips} skip${r.skips==1?'':'s'}`)+
`</div><h2>By category</h2>`+categoriesTable(d.categories)+`<h2>Brands</h2>`+brandsTable(d.brands)+`<h2>Creators</h2>`+creatorsTable(d.creators)+(d.aliases.length?`<h2>Brand aliases learned</h2><div class="sub">${d.aliases.map(a=>`${esc(a.alias)} → ${esc(a.brand)} (${esc(a.how)})`).join(' · ')}</div>`:'');}
function route(){const t=(location.hash||'#stats').slice(1);show(['stats','ads','insights'].includes(t)?t:'stats').catch(e=>{$('#sub').textContent='failed to load: '+e})}
window.addEventListener('hashchange',route);route();
</script></body></html>
"""
