"""Sponsor Skipper — local stats: SQLite store, brand normalization, metrics, dashboard page.

Stdlib only. Imported by server.py. Everything here is local and gitignored (server/cache/stats.local.db).
The schema is user-agnostic (no user identity) so rows could later be aggregated across many users.
"""

import difflib
import json
import os
import re
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "stats.local.db")
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
        conn.execute("INSERT OR REPLACE INTO brand_aliases(alias, brand, how) VALUES (?, ?, ?)",
                     (key, best, f"hint fuzzy {best_r:.2f}"))
        return best, f"hint fuzzy {best_r:.2f}"
    if len(cands) == 1 and (not evidence or _norm(cands[0]) in _norm(evidence) or True):
        conn.execute("INSERT OR REPLACE INTO brand_aliases(alias, brand, how) VALUES (?, ?, 'single hint')", (key, cands[0]))
        return cands[0], "single hint"
    known = [r["brand"] for r in conn.execute("SELECT DISTINCT brand FROM segments WHERE brand != ''")]
    best, best_r = None, 0.0
    for b in known:
        r = difflib.SequenceMatcher(None, key, _norm(b)).ratio()
        if r > best_r:
            best, best_r = b, r
    if best and best_r >= 0.8:
        conn.execute("INSERT OR REPLACE INTO brand_aliases(alias, brand, how) VALUES (?, ?, ?)", (key, best, f"db fuzzy {best_r:.2f}"))
        return best, f"db fuzzy {best_r:.2f}"
    return _display(raw[:80]), "raw"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def record_video(job, segments, versions):
    """Called at job end: upsert the video and all final segments with canonical brands."""
    hints = job.get("hints") or {}
    with _lock, connect() as c:
        c.execute("""INSERT INTO videos(video_id, channel_id, channel_name, title, lang, duration_s, analyzed_at, versions, hints)
                     VALUES (?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(video_id) DO UPDATE SET channel_id=excluded.channel_id, channel_name=excluded.channel_name,
                       title=excluded.title, lang=excluded.lang, duration_s=COALESCE(excluded.duration_s, videos.duration_s),
                       analyzed_at=excluded.analyzed_at, versions=excluded.versions, hints=excluded.hints""",
                  (job["video_id"], job.get("channel_id", ""), job.get("creator", ""), job.get("title", ""), job.get("lang", ""),
                   job.get("duration") or None, now(), json.dumps(versions), json.dumps(hints)))
        c.execute("DELETE FROM segments WHERE video_id = ?", (job["video_id"],))
        for s in segments:
            brand, how = canonical_brand(c, s.get("brand", ""), hints, s.get("evidence", ""))
            c.execute("""INSERT OR REPLACE INTO segments(video_id, seg_start, seg_end, category, style, action, brand_raw, brand,
                         confidence, evidence, reason, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (job["video_id"], s["start"], s["end"], s.get("category"), s.get("style"), s.get("action"),
                       s.get("brand", ""), brand, s.get("confidence"), s.get("evidence", ""), s.get("reason", ""),
                       s.get("source", ""), now()))


def record_event(data):
    """Client event -> events row. Returns (ok, error)."""
    kind = str(data.get("kind") or "")
    if kind not in EVENT_KINDS:
        return False, "bad kind"
    seg = data.get("segment") if isinstance(data.get("segment"), dict) else {}

    def num(x):
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None
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
        segs = [dict(r) for r in c.execute("SELECT s.*, v.channel_id, v.channel_name, v.duration_s FROM segments s "
                                           "LEFT JOIN videos v ON v.video_id = s.video_id")]
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
            "skips": 0, "skipped_s": 0.0, "undos": 0, "seek_into": 0, "wrong_skips": 0, "raw": set(), "creator_counts": {}})
        b["spots"] += 1
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
            "wrong_skips": b["wrong_skips"], "skip_rate": round(b["skips"] / b["spots"], 2),
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

    shame = sorted([r for r in brand_rows if r["skips"] >= 1],
                   key=lambda r: -(r["skip_rate"] * r["avg_len_s"]))[:5]
    fame = sorted([r for r in brand_rows if r["integration_pct"] >= 50 or (r["spots"] >= 2 and r["skips"] == 0) or r["watch_through_pct"] >= 50],
                  key=lambda r: (-r["integration_pct"], -r["watch_through_pct"], r["skips"]))[:5]
    return {"generated_at": now(), "overall": overall, "brands": brand_rows, "creators": creator_rows,
            "hall_of_shame": shame, "hall_of_fame": fame, "aliases": aliases}


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
.pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;background:var(--line)}
</style></head><body><main>
<h1>Sponsor Skipper — stats</h1><div class="sub" id="sub">loading…</div>
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
function brandsTable(rows){if(!rows.length)return '<div class="empty">No sponsor segments yet.</div>';return `<table><thead><tr><th>brand</th><th class="num">spots</th><th class="num">creators</th><th class="num">read / integr.</th><th class="num">skips</th><th class="num">skipped</th><th class="num">avg len</th><th class="num">watch-through</th><th class="num">wrong</th><th>top creators</th></tr></thead><tbody>${rows.map(r=>`<tr><td><b>${esc(r.brand)}</b>${r.raw_spellings.length&&r.raw_spellings.join('')!==r.brand?`<div class="raw">raw: ${esc(r.raw_spellings.join(', '))}</div>`:''}<div class="verdict">${esc(r.verdict)}</div></td><td class="num">${r.spots}</td><td class="num">${r.creators}</td><td class="num">${r.read_pct}% / ${r.integration_pct}%</td><td class="num">${r.skips}</td><td class="num">${dur(r.skipped_s)}</td><td class="num">${dur(r.avg_len_s)}</td><td class="num">${r.skips?r.watch_through_pct+'%':'–'}</td><td class="num">${r.wrong_skips||''}</td><td>${r.top_creators.map(esc).join(', ')}</td></tr>`).join('')}</tbody></table>`}
function creatorsTable(rows){if(!rows.length)return '<div class="empty">No creators yet.</div>';return `<table><thead><tr><th>creator</th><th class="num">videos</th><th class="num">with ads</th><th class="num">spots</th><th class="num">ad density</th><th class="num">ad time</th><th class="num">integrations</th><th>brands</th></tr></thead><tbody>${rows.map(r=>`<tr><td><b>${esc(r.creator)}</b></td><td class="num">${r.videos}</td><td class="num">${r.videos_with_ads_pct}%</td><td class="num">${r.spots}</td><td class="num">${r.ad_density==null?'–':r.ad_density+' s/min'}</td><td class="num">${dur(r.ad_s)}</td><td class="num">${r.integration_pct}%</td><td>${r.brands.map(b=>`<span class="pill">${esc(b)}</span>`).join(' ')}</td></tr>`).join('')}</tbody></table>`}
function hall(title,rows,fmt){return `<div class="hall"><h3>${title}</h3>${rows.length?`<ol>${rows.map(r=>`<li>${fmt(r)}</li>`).join('')}</ol>`:'<div class="sub">nothing yet</div>'}</div>`}
async function load(){const d=await (await fetch('/stats.json')).json();$('#sub').textContent=`local only · generated ${d.generated_at}`;
$('#root').innerHTML=tiles(d.overall)+`<h2>Where ads sit in a video</h2>`+hist(d.overall.position_hist)+`<h2>Halls</h2><div class="halls">`+
hall('💀 Hall of Shame',d.hall_of_shame,r=>`<b>${esc(r.brand)}</b> — skip rate ${Math.round(r.skip_rate*100)}%, avg read ${dur(r.avg_len_s)}, ${dur(r.skipped_s)} skipped`)+
hall('🏆 Hall of Fame',d.hall_of_fame,r=>`<b>${esc(r.brand)}</b> — ${r.integration_pct}% integrations, watch-through ${r.watch_through_pct}%, ${r.skips} skip${r.skips==1?'':'s'}`)+
`</div><h2>Brands</h2>`+brandsTable(d.brands)+`<h2>Creators</h2>`+creatorsTable(d.creators)+(d.aliases.length?`<h2>Brand aliases learned</h2><div class="sub">${d.aliases.map(a=>`${esc(a.alias)} → ${esc(a.brand)} (${esc(a.how)})`).join(' · ')}</div>`:'');}
load().catch(e=>{$('#sub').textContent='failed to load /stats.json: '+e});
</script></body></html>
"""
