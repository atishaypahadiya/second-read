#!/usr/bin/env python3
"""Regression harness: replay the test set from cached transcripts and cached model outputs.

    python3 scripts/regress.py            # all videos in server/testset.json
    python3 scripts/regress.py SOQynHuWPeI dU7gCt-7kKk
    python3 scripts/regress.py --allow-model   # permit Ollama calls when a cached output is missing/outdated

Runs the server pipeline in-process (no HTTP). With matching DETECT/REFINE versions every model call is served
from stored model outputs, so a run takes a second or two and makes zero Ollama calls; a prompt change needs the
model once (--allow-model).

Isolation: SR_DATA_DIR points at a fresh temp dir before the server is imported, so its result cache, stats DB,
results log and feedback file all live there. The test-set videos' cache files are COPIED in from server/cache;
nothing is ever written back to server/cache. Model outputs the harness itself paid for (--allow-model) are kept
in server/testset.calls.local.json (gitignored) and merged into the copies on the next run.
Exit code 1 on any failure.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_CACHE = os.path.join(ROOT, "server", "cache")  # read-only for the harness
CALLS_STORE = os.path.join(ROOT, "server", "testset.calls.local.json")
DATA_DIR = tempfile.mkdtemp(prefix="sr-regress-")
os.environ["SR_DATA_DIR"] = DATA_DIR  # before the imports below: the server resolves every local path from it
os.environ.pop("SS_DATA_DIR", None)
sys.path.insert(0, os.path.join(ROOT, "server"))

import server  # noqa: E402
import stats  # noqa: E402
import intel  # noqa: E402

DEFAULT_TOL = 1.5


def load_testset():
    with open(os.path.join(ROOT, "server", "testset.json"), "r", encoding="utf-8") as f:
        return json.load(f)["videos"]


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def seed_data_dir(video_ids):
    """Copy the videos' cache files into the temp data dir, with the harness's own stored model outputs merged in
    (a stored output replaces the copy's only when it was made under a newer or equal version)."""
    store = read_json(CALLS_STORE) or {}
    for vid in video_ids:
        data = read_json(os.path.join(REAL_CACHE, f"{vid}.json"))
        if not data:
            continue
        calls = data.setdefault("calls", {})
        for key, out in (store.get(vid) or {}).items():
            if key not in calls or (out.get("v") or 0) >= (calls[key].get("v") or 0):
                calls[key] = out
        server.save_cache(vid, data)  # CACHE_DIR is the temp dir (checked in main)
    return store


def keep_model_outputs(video_ids, store):
    """After --allow-model: remember every model output of the test-set videos, so the next run needs no model."""
    for vid in video_ids:
        calls = (read_json(os.path.join(DATA_DIR, f"{vid}.json")) or {}).get("calls")
        if calls:
            store[vid] = calls
    tmp = CALLS_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CALLS_STORE)


def run_video(entry, allow_model):
    vid = entry["video"]
    cached = server.load_cache(vid)
    if not cached or not cached.get("transcript"):
        return None, "no cached transcript (watch it once with the extension)"
    lines = server.clean_lines(cached["transcript"])  # exactly what POST /analyze does with the client's lines
    if not allow_model:
        # Refuse to touch Ollama: any call not served from the cache raises and the video is reported as not run.
        def no_model(*a, **k):
            raise RuntimeError("model call needed but --allow-model not given")
        server.ollama_chat = no_model
    server.cache_valid = lambda *a, **k: False  # always re-process (reusing cached calls)
    server.jobs.clear()  # entries may share a video (with and without metadata)
    m = entry.get("meta") or {}
    meta = {"creator": m.get("creator", entry.get("creator", "")), "kind": m.get("kind", ""), "tag": "REGRESS",
            "channel_id": m.get("channelId", ""), "duration": m.get("duration"),
            "paid_promotion": m.get("paidPromotion") if isinstance(m.get("paidPromotion"), bool) else None,
            "heat": [], "client": None}
    # A non-English video is analyzed on its English auto-translation, which the cache does not keep: an entry can
    # carry a stand-in for it (meta.analysisLines, same line timings).
    alines = server.clean_lines(m["analysisLines"]) if m.get("analysisLines") else None
    job = server.start_or_get_job(vid, cached.get("lang", "en"), lines, m.get("title", cached.get("title", "")),
                                  meta, alines, m.get("analysisLang", "en") if alines else None, 0.0, m.get("description", ""))
    t0 = time.time()
    while job["status"] not in ("done", "error"):
        server.touch(job)
        try:
            server.worker_step()
        except server.JobError as je:
            server.fail_job(je.job, je.cause)
        if time.time() - t0 > 900:
            return None, "timeout"
    if job["status"] == "error":
        if not allow_model:
            return None, "a model output is missing or outdated in the cache; run the video through the server once, or pass --allow-model"
        return None, f"job error: {job.get('error')}"
    return job, None


def check_meta(entry, job):
    """expect_meta: what the server must have derived from the request metadata (hints, canonical brand, stats row)."""
    out = []
    em = entry.get("expect_meta") or {}
    if not em:
        return out
    hints = job.get("hints") or {}
    if "hints" in em:
        got = bool(hints.get("brands") or hints.get("codes") or hints.get("domains"))
        out.append((got == em["hints"], f"hints {'found' if got else 'none'}: brands={hints.get('brands')} domains={hints.get('domains')}"
                    + ("" if got == em["hints"] else f" (expected {'some' if em['hints'] else 'none'})")))
    skips = [s for s in job["segments"] if s["action"] == "skip"]
    if "brand" in em:
        got = skips[0].get("brand_canonical") if skips else None
        out.append((got == em["brand"], f"brand {skips[0].get('brand')!r} -> {got!r}" if skips else "brand: no skip segment"
                    if got == em["brand"] else f"brand {skips[0].get('brand') if skips else None!r} -> {got!r}, expected {em['brand']!r}"))
    c = sqlite3.connect(stats.DB_PATH)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM videos WHERE video_id = ?", (job["video_id"],)).fetchone()
    c.close()
    if "creator" in em:
        got = row["channel_name"] if row else None
        out.append((got == em["creator"], f"stats creator {got!r}" + ("" if got == em["creator"] else f", expected {em['creator']!r}")))
    if em.get("duration"):
        got = row["duration_s"] if row else None
        out.append((bool(got), f"stats duration {got}"))
    if "disclosure" in em:
        got = sorted({s.get("disclosure") for s in job["segments"] if s.get("category") == "sponsor" and s.get("style") == "read"})
        out.append((got == [em["disclosure"]], f"disclosure {got}" + ("" if got == [em["disclosure"]] else f", expected {em['disclosure']!r}")))
    return out


def check(entry, segments):
    """Return list of (ok, message)."""
    out = []
    skips = [s for s in segments if s["action"] == "skip"]
    if entry.get("must_not_skip"):
        out.append((not skips, "no skip" if not skips else f"UNEXPECTED skip(s): {[(server.fmt_time(s['start']), server.fmt_time(s['end']), s['brand']) for s in skips]}"))
    matched = set()
    for e in entry.get("expect", []):
        tol = e.get("tol", DEFAULT_TOL)
        hit = None
        for i, s in enumerate(segments):
            # start may be later than expected by start_late_ok (a later start is the safe direction), never earlier
            start_ok = -tol <= s["start"] - e["start"] <= tol + e.get("start_late_ok", 0)
            if s["action"] == e["action"] and start_ok and abs(s["end"] - e["end"]) <= tol \
                    and (not e.get("category") or s.get("category") == e["category"]):
                hit = i
                break
        if hit is not None:
            matched.add(hit)
            out.append((True, f"{e['action']}{' ' + e['category'] if e.get('category') else ''} {server.fmt_time(e['start'])}-{server.fmt_time(e['end'])} ok"))
        else:
            near = [s for s in segments if s["action"] == e["action"] and s["start"] <= e["end"] and s["end"] >= e["start"]]
            detail = f"got {server.fmt_time(near[0]['start'])}-{server.fmt_time(near[0]['end'])} (off by {near[0]['start'] - e['start']:+.1f}s / {near[0]['end'] - e['end']:+.1f}s)" if near else "nothing there"
            out.append((False, f"{e['action']}{' ' + e['category'] if e.get('category') else ''} {server.fmt_time(e['start'])}-{server.fmt_time(e['end'])} MISSING: {detail}"))
    for i, s in enumerate(skips):
        idx = segments.index(s)
        if idx not in matched and not entry.get("must_not_skip"):
            out.append((False, f"UNEXPECTED skip {server.fmt_time(s['start'])}-{server.fmt_time(s['end'])} {s['brand']!r}"))
    for fr in entry.get("forbid", []):
        bad = [s for s in segments if s["start"] < fr["end"] and s["end"] > fr["start"]]
        out.append((not bad, f"nothing at {server.fmt_time(fr['start'])}-{server.fmt_time(fr['end'])}" if not bad
                    else f"FORBIDDEN segment at {server.fmt_time(fr['start'])}-{server.fmt_time(fr['end'])}: {[(s['action'], s['category'], s['brand']) for s in bad]}"))
    return out


def unit_checks():
    """Pure-function guards for rules that have regressed before. Returns (ok, message) pairs."""
    out = []

    def eq(name, got, want):
        out.append((got == want, f"{name}" if got == want else f"{name}: got {got!r}, expected {want!r}"))
    # transcript cleaning is idempotent, including consecutive speaker markers
    for t in (">> >> So it needed insights.", "expensive, >> especially >> in the US", "[Music] >> [Applause] >> hello"):
        once = server.strip_noise(t)
        eq(f"strip_noise({t!r}) has no '>>' and is stable", (">>" in once, server.strip_noise(once) == once), (False, True))
    # end snap: never past the picked last ad word into the next sentence (tdIUMkXxtHg: an auto track without word
    # timing, word starts estimated past their line's end; the snap took the capped ends for pauses and went +4 words)
    def wl(start, end, words):
        return {"start": start, "end": end, "text": " ".join(w[2] for w in words), "words": words, "timed": False}
    est = server.clean_lines([
        wl(513.2, 517.84, [[513.2, 513.61, "with"], [513.61, 514.02, "this"], [514.02, 514.51, "link,"], [514.51, 514.84, "you"],
                           [514.84, 515.17, "get"], [515.17, 515.33, "a"], [515.33, 515.75, "full"], [515.75, 515.99, "60"],
                           [515.99, 516.4, "days"], [516.4, 517.31, "completely"], [517.31, 517.8, "free."], [517.8, 518.37, "Before"],
                           [518.37, 518.62, "we"], [518.62, 518.95, "get"], [518.95, 519.19, "to"], [519.19, 519.6, "Tik"]]),
        wl(517.84, 522.0, [[517.84, 518.34, "Tok,"], [518.34, 519.14, "there's"], [519.14, 519.34, "a"], [519.34, 520.03, "bigger"]])])
    eq("words: never start after their line's end, and cleaning is stable",
       (all(w[0] <= w[1] <= l["end"] for l in est for w in l["words"]), server.clean_lines(est) == est), (True, True))
    eq("end snap: no pause at the pick -> stays (was 517.84, +4 words)", server.snap_end(est, 0, 0, 517.8)[0], None)
    # the same lines with other word timings are another input: a cached result made from older estimates is redone
    moved = [dict(l, words=[[w[0], round(w[1] - 0.01, 2), w[2]] for w in l["words"]]) for l in est]
    eq("inputs digest: covers word timings, not only line starts and text",
       (server.transcript_hash(est) == server.transcript_hash(moved),
        server.inputs_digest("h", "t", "d", None, None, est) == server.inputs_digest("h", "t", "d", None, None, moved),
        server.inputs_digest("h", "t", "d", None, None, est) == server.inputs_digest("h", "t", "d", None, None, list(est))),
       (True, False, True))

    def gapped(after_free, after_before):
        t = 517.8 + after_free
        return [wl(513.2, 530.0, [[516.4, 517.31, "completely"], [517.31, 517.8, "free."], [t, t + 0.3, "Before"],
                                  [t + 0.3 + after_before, t + 0.9 + after_before, "we"]])]
    eq("end snap: a pause right after the pick ends there", server.snap_end(gapped(0.6, 0.0), 0, 0, 517.8)[0], 517.8)
    eq("end snap: a pause one word into the next sentence is not used", server.snap_end(gapped(0.0, 0.9), 0, 0, 517.8)[0], None)
    eq("end snap: a pick mid-sentence still extends to its own sentence end", server.snap_end(gapped(0.6, 0.0), 0, 0, 517.31)[0], 517.8)
    # the creator's own brand work told as story (vGoRpjE-svI: "shooting for Fossil", labeled sponsor/integration
    # 0.80 by detect and refine) is content: a sponsor label that is not a skip needs an ad marker
    story = [{"start": 42.72, "end": 48.64, "text": "did nothing at all and it was so much fun but yes now we are back and"},
             {"start": 48.64, "end": 54.64, "text": "we are going for a shoot shooting for fossil for this new watch come on it is 11 in the morning"},
             {"start": 54.64, "end": 61.44, "text": "we will go shoot come back home and have coffee I will talk to you later"},
             {"start": 400.0, "end": 406.0, "text": "then we went for ice cream at the market with everyone"}]

    def labeled(lines, terms=(), paid=None, **over):
        seg = {"start": 48.64, "end": 54.64, "line_start": 48.64, "line_end": 54.64, "start_line": 1, "end_line": 1,
               "category": "sponsor", "style": "integration", "action": "keep", "brand": "fossil", "confidence": 0.8,
               "evidence": "shooting for fossil for this new watch", "source": "detect",
               "reason": "Creator is shooting for Fossil for a new watch, indicating a collaboration with the brand."}
        seg.update(over)
        return [s["action"] for s in server.post_filter([seg], "Back in Delhi and somehow won an award??", server.clean_lines(lines),
                                                        False, terms, paid)]

    def with_line(i, text):
        return [dict(l, text=text) if k == i else l for k, l in enumerate(story)]
    eq("filter: brand work told as story, no ad marker -> no segment (keep, or a downgraded read)",
       (labeled(story), labeled(story, style="read", action="show")), ([], []))
    eq("filter: the same mention with an ad's markers stays (offer + link, sponsorship language, description hint, pitched elsewhere)",
       [labeled(with_line(2, "use my link in the description for 20% off the new watch")),
        labeled(with_line(0, "this part of the vlog is sponsored by fossil and")),
        labeled(story, terms=["Fossil", "fossil"]),
        labeled(with_line(3, "thanks to fossil for sponsoring, use code CHIRAG for 10% off"))], [["keep"]] * 4)
    eq("filter: YouTube's paid-promotion flag is an ad marker only when received and true",
       [labeled(story, paid=True), labeled(story, paid=False), labeled(story, paid=None)], [["keep"], [], []])
    eq("filter: skips and other categories are not touched by it",
       (labeled(story, style="read", action="skip", confidence=0.95), labeled(story, category="selfpromo", action="show")), (["skip"], ["show"]))
    # description hints: only sponsor lines count; the creator's own links, socials and name do not
    h = server.extract_hints("Learn to code with Coddy (ad): https://fern.deals/coddy")
    eq("hints: (ad) line -> brand + link", (h["brands"], h["domains"]), (["Coddy"], ["fern.deals/coddy"]))
    eq("hint terms drop the creator's own name", server.hint_terms(h, "fern"), ["Coddy", "coddy"])
    h = server.extract_hints("Get the Coddy Pro Plan for Tech lovers (ad): https://fern.deals/coddy")
    eq("hints: generic capitalized words on a sponsor line are not brands", h["brands"], ["Coddy"])
    eq("hint terms: no generic word becomes a sponsor cue", server.hint_terms(h, "fern"), ["Coddy", "coddy"])
    h = server.extract_hints("Order food at: https://www.eatsides.com/\nMerch: https://www.sidemenclothing.com\nhttps://www.instagram.com/Sidemen")
    eq("hints: own shop and social links are not sponsor hints", (h["brands"], h["domains"]), ([], []))
    h = server.extract_hints("Get 60% off Incogni with code HARRIS: https://incogni.com/harris")
    eq("hint terms: creator's code/name excluded, brand kept", server.hint_terms(h, "Johnny Harris"), ["incogni.com/harris", "incogni"])
    h = server.extract_hints("This video is sponsored by Odoo.\nhttps://www.odoo.com/r/fern")
    eq("hints: link on the line after a sponsor line", (h["brands"], h["domains"]), (["Odoo"], ["odoo.com/r"]))
    eq("ad lines: a denial is not a sponsorship marker",
       [server.is_ad_line(l) for l in ("Coddy (ad): https://fern.deals/coddy", "This video is sponsored by Odoo.",
                                       "This video is not sponsored.", "No sponsor today.")], [True, True, False, False])
    # halls: a brand is ranked only from HALL_MIN_SPOTS spots
    def spot(i, brand, style="read", action="skip"):
        vid = f"hall{brand[:3]}{i:04d}"[:11].ljust(11, "0")
        stats.record_video({"video_id": vid, "channel_id": "UCh", "creator": "hall test", "title": "", "lang": "en", "duration": 600.0,
                            "hints": {"brands": [brand]}, "lines": []},
                           [{"start": 100.0, "end": 160.0, "category": "sponsor", "style": style, "action": action, "brand": brand,
                             "confidence": 0.95, "evidence": "", "reason": "", "source": "detect"}], {})
        if action == "skip":
            stats.record_event({"kind": "skip", "videoId": vid, "segment": {"start": 100.0, "end": 160.0}, "playhead": 100.0, "saved": 60.0})
    for i in range(3):
        spot(i, "Thricebrand")
        spot(i, "Integrated", "integration", "keep")
    for i in range(2):
        spot(i, "Twicebrand")
    d = stats.compute()
    o = d["overall"]
    eq("totals: today's skips and seconds are the same definition as the all-time ones (every test event is from today)",
       (o["today"], o["skips"] > 0), ({"skips": o["skips"], "undos": o["undos"], "saved_s": o["saved_s"]}, True))
    eq(f"halls: ranked only from {stats.HALL_MIN_SPOTS} spots",
       ([b["brand"] for b in d["hall_of_shame"]], [b["brand"] for b in d["hall_of_fame"]]), (["Thricebrand"], ["Integrated"]))
    # brands: a rule segment (recap/outro/intro/interaction) never gets one, not even the video's only hint
    one_hint = {"brands": ["Odoo"], "domains": []}

    def canon(raw, hints=None, evidence="", category="sponsor", source="detect"):
        with stats.connect() as c:
            return stats.canonical_brand(c, raw, hints, evidence, category, source)[0]
    eq("brands: rule segments stay unbranded",
       [canon("", one_hint, "Thanks for watching", cat, "rule") for cat in ("recap", "outro", "intro", "interaction")], [""] * 4)
    eq("brands: no spoken name -> only a sponsor segment takes the single hint",
       (canon("", one_hint), canon("", one_hint, category="selfpromo")), ("Odoo", ""))
    # brands: the spoken name beats a domain label; a link only names the brand when nothing was spoken
    link_only = {"brands": [], "domains": ["headspace-web.app"]}
    eq("brands: name from a sponsor link drops affixes and TLDs",
       [stats.domain_name(d) for d in ("headspace-web.app", "headspace-web.app.link/e", "www.odoo.com/r", "go.nordvpn.net/x", "app.link")],
       ["Headspace", "Headspace", "Odoo", "Nordvpn", ""])
    eq("brands: spoken name kept over the domain label", canon("Headspace", link_only), "Headspace")
    eq("brands: no spoken name -> the name from the link", canon("", link_only), "Headspace")
    eq("brands: a written hint brand still corrects a misheard one", canon("Udo", {"brands": ["Odoo"], "domains": ["odoo.com/r"]}), "Odoo")
    # brands: generic words are never a brand or an alias; an alias is learned only for a name the video backs up
    learned = []
    stats.set_logger(learned.append)

    def alias(key):
        with stats.connect() as c:
            row = c.execute("SELECT brand FROM brand_aliases WHERE alias = ?", (key,)).fetchone()
            return row["brand"] if row else None
    junk = {"brands": ["Coddy", "Tech", "Pro", "Plan", "Sound"], "domains": ["fern.deals/coddy", "fern.deals/ES"]}
    eq("brands: a generic word is not a brand", (canon("Tech", junk, "code fern20 for 20% off"), alias("tech")), ("Coddy", None))
    eq("aliases: learned when the written brand backs it up, and logged",
       (canon("Cody", {"brands": ["Coddy"]}), alias("cody"), [m for m in learned if "'cody' -> 'Coddy'" in m] != []), ("Coddy", "Coddy", True))
    eq("aliases: under 4 characters is resolved per video, never learned", (canon("Udo", {"brands": ["Odoo"]}), alias("udo")), ("Odoo", None))
    eq("aliases: a look-alike in the DB is not learned unless the ad's evidence names the brand",
       (canon("Thricebrnd", {}, "use my link"), alias("thricebrnd"),
        canon("Thricebrnd", {}, "thanks to Thricebrand for sponsoring"), alias("thricebrnd")), ("Thricebrand", None, "Thricebrand", "Thricebrand"))
    eq("aliases: an established brand name is never redirected by one video's description",
       (canon("Thricebrand", {"brands": ["Thricebrant"]}), alias("thricebrand")), ("Thricebrant", None))
    stats.set_logger(server.log)
    # brands: a sponsor link corrects a misheard name only when the two are close (by letters or by sound)
    odoo = {"brands": [], "domains": ["www.odoo.com/r"]}
    eq("brands: close to the sponsor link -> the link's spelling",
       [canon("Udo", odoo), canon("Udu", odoo), canon("Udu", {"brands": [], "domains": ["fern.deals/odoo"]}),
        canon("headsp space", link_only), canon("Nord VPM", {"brands": [], "domains": ["go.nordvpn.net/x"]})],
       ["Odoo", "Odoo", "Odoo", "Headspace", "NordVPN"])
    eq("brands: not close to the sponsor link -> the spoken name stays (and nothing is learned)",
       [canon("Squarespace", odoo), canon("Audible", odoo), canon("Nord", link_only), canon("Udo", odoo, category="selfpromo"), alias("udu")],
       ["Squarespace", "Audible", "NordVPN", "Udo", None])
    # existing rows written under the old rules are re-derived once (and the alias that came from a domain label goes)
    with stats.connect() as c:
        c.execute("INSERT INTO videos(video_id, hints) VALUES ('renormvideo', ?)", (json.dumps(link_only),))
        c.execute("INSERT INTO segments(video_id, seg_start, seg_end, category, style, action, brand_raw, brand, evidence, source) VALUES "
                  "('renormvideo', 451.64, 517.84, 'sponsor', 'read', 'skip', 'Headspace', 'Headspace-web', 'the sponsor is Headspace', 'detect'), "
                  "('renormvideo', 1399.6, 1404.48, 'recap', 'read', 'show', '', 'Headspace-web', 'Last time I', 'rule')")
        c.execute("INSERT INTO events(ts, kind, video_id, seg_start, seg_end, brand) VALUES ('t', 'skip', 'renormvideo', 451.64, 517.84, 'Headspace-web')")
        c.execute("INSERT INTO codes(brand, video_id, time_s, kind, value) VALUES ('Headspace-web', 'renormvideo', 451.6, 'link', 'headspace-web.app')")
        c.execute("INSERT OR REPLACE INTO brand_aliases(alias, brand, how) VALUES ('headspace', 'Headspace-web', 'hint fuzzy 0.90')")
        c.execute("PRAGMA user_version = 0")
    stats.renormalize_brands()
    with stats.connect() as c:
        got = ([r["brand"] for r in c.execute("SELECT brand FROM segments WHERE video_id = 'renormvideo' ORDER BY seg_start")],
               [r["brand"] for r in c.execute("SELECT brand FROM events WHERE video_id = 'renormvideo'")],
               [r["brand"] for r in c.execute("SELECT brand FROM codes WHERE video_id = 'renormvideo'")],
               [r["brand"] for r in c.execute("SELECT brand FROM brand_aliases WHERE alias = 'headspace'")])
    eq("brands: old rows re-normalized (segments, events, codes, aliases)", got, (["Headspace", ""], ["Headspace"], ["Headspace"], []))
    eq("brands: re-normalization runs once per rules version", stats.renormalize_brands(), None)
    # promo codes: only tokens that look like codes (a digit, or ALL CAPS where caps mean something), never a common word
    eq("codes: spoken text",
       [stats.find_codes(t) for t in ("or scanning this QR code that's on screen", "use code fern20 for 20% off",
                                      "Use code HARRIS at checkout", "use my promo code SAVE today", "my area code 415 is",
                                      "USE CODE BELOW FOR 20% OFF", "USE CODE FERN NOW", "USE CODE FERN20 NOW")],
       [[], ["FERN20"], ["HARRIS"], [], [], [], [], ["FERN20"]])
    eq("codes: description hints take the same rule",
       [server.extract_hints(d)["codes"] for d in ("Get 20% off with code FERN20: https://fern.deals/coddy",
                                                   "Use code BELOW for 20% off: https://fern.deals/coddy")], [["FERN20"], []])
    with stats.connect() as c:
        c.execute("INSERT INTO codes(brand, video_id, kind, value, detail) VALUES ('Headspace', 'codesvideo0', 'code', 'THAT', 'spoken'), "
                  "('Coddy', 'codesvideo0', 'code', 'FERN20', 'spoken'), ('Coddy', 'codesvideo0', 'offer', 'free trial', 'free trial')")
    eq("codes: junk rows leave the vault, the rest stays",
       (stats.purge_bad_codes(), [r["value"] for r in stats.connect().execute("SELECT value FROM codes WHERE video_id = 'codesvideo0' ORDER BY id")]),
       (1, ["FERN20", "free trial"]))
    # tier-2: a persuasion quote is matched like evidence (whole quote or a 6-word run), across line breaks
    ad = [{"start": 456.8 + 4 * i, "end": 460.8 + 4 * i, "text": t} for i, t in enumerate((
        "Yeah. Yeah. They sent me a free subscription and I've been taking their",
        "finding focus course, which is basically 10-minute sessions of guided",
        "sustain attention. In one study, people who meditated with headsp space for a",
        "month were less distractable than a control group who did these standard brain training exercises"))]
    hay = intel.words_of(" ".join(l["text"] for l in ad))
    eq("tier-2 quotes: across lines / 6-word run despite a respelled word / invented / short and present",
       [intel.quote_in(q, hay) for q in (
           "They sent me a free subscription and I've been taking their finding focus course",
           "In one study, people who meditated with headspace for a month were less distractable",
           "Headspace changed my life and it will change yours", "a free subscription")], [True, True, False, True])
    with stats.connect() as c:
        c.execute("INSERT INTO segments(video_id, seg_start, seg_end, category, style, action, brand_raw, brand, source) "
                  "VALUES ('tier2video0', 456.8, 472.8, 'sponsor', 'read', 'skip', 'Headspace', 'Headspace', 'detect')")
        row = dict(c.execute("SELECT id, video_id, seg_start, seg_end, brand FROM segments WHERE video_id = 'tier2video0'").fetchone())
    said = []
    intel.set_logger(said.append)
    intel.run_analysis(row, lambda prompt, schema: {"persuasion": [
        {"tag": "testimony", "quote": "They sent me a free subscription and I've been taking their finding focus course"},
        {"tag": "authority", "quote": "Doctors everywhere recommend it"}], "beats": [], "better_placement": {}}, lambda vid: ad)
    intel.set_logger(server.log)
    with stats.connect() as c:
        kept = json.loads(c.execute("SELECT analysis FROM ad_analysis WHERE segment_id = ?", (row["id"],)).fetchone()["analysis"])["persuasion"]
    eq("tier-2 tags: the real quote is kept, the invented one dropped, and the log says which",
       ([p["tag"] for p in kept], [("model returned 2" in m, "Doctors everywhere" in m) for m in said if "tag(s)" in m]), (["testimony"], [(True, True)]))
    # disclosure is 'undisclosed' only when both signals are known and negative
    table = {(p, d): server.disclosure_state(p, d) for p in (True, False, None) for d in (True, False, None)}
    eq("disclosure: only (False, False) is undisclosed", sorted(k for k, v in table.items() if v == "undisclosed"), [(False, False)])
    eq("disclosure: any missing signal without a positive one is unknown",
       [table[(None, None)], table[(False, None)], table[(None, False)]], ["unknown"] * 3)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    allow_model = "--allow-model" in sys.argv
    # Nothing the harness does may land in the user's local data. Refuse to run if any path escaped the temp dir.
    paths = (server.CACHE_DIR, stats.DB_PATH, server.RESULTS_MD, server.FEEDBACK_JSONL)
    if server.CACHE_DIR != DATA_DIR or not all(os.path.realpath(p).startswith(os.path.realpath(DATA_DIR)) for p in paths):
        sys.exit(f"regress: not isolated, refusing to run: {paths}")
    intel._embed_available = False  # no embedding calls during regression
    server.log = lambda *a: None  # quiet; set SR_REGRESS_VERBOSE=1 to see the pipeline log
    if os.environ.get("SR_REGRESS_VERBOSE") or os.environ.get("SS_REGRESS_VERBOSE"):
        server.log = lambda *a: print("   ", *a)
    stats.init()
    intel.init()
    intel.set_logger(server.log)
    stats.set_logger(server.log)
    entries = load_testset()
    if args:
        entries = [e for e in entries if e["video"] in args or e.get("id") in args]
    video_ids = sorted({e["video"] for e in entries})
    store = seed_data_dir(video_ids)
    failures = 0
    rows = []
    if not args:
        # Extension wiring first: metadata must survive page.js -> content.js -> background.js -> POST /analyze.
        t0 = time.time()
        node = shutil.which("node")
        if node:
            r = subprocess.run([node, os.path.join(ROOT, "scripts", "wiring_test.js")], capture_output=True, text=True)
            out = [l.strip() for l in (r.stdout + r.stderr).strip().splitlines() if l.strip()] or ["(no output)"]
            failures += 0 if r.returncode == 0 else 1
            rows.append(("wiring", "extension", "PASS" if r.returncode == 0 else "FAIL", out, time.time() - t0))
        else:
            rows.append(("wiring", "extension", "SKIP", ["not run: node not found"], 0.0))
    if not args:
        t0 = time.time()
        results = unit_checks()
        bad = [m for ok, m in results if not ok]
        failures += 1 if bad else 0
        rows.append(("rules", "server", "FAIL" if bad else "PASS", bad or [f"{len(results)} rule checks ok (cleaning, end snap, ad markers, hints, halls, brands, codes, tier-2 quotes, disclosure)"], time.time() - t0))
    for e in entries:
        t0 = time.time()
        label = e.get("id", e["video"])
        job, err = run_video(e, allow_model)
        if err:
            rows.append((label, e.get("creator", ""), "SKIP", [f"not run: {err}"], time.time() - t0))
            continue
        results = check(e, job["segments"]) + check_meta(e, job)
        ok = all(r[0] for r in results)
        failures += 0 if ok else 1
        rows.append((label, e.get("creator", ""), "PASS" if ok else "FAIL", [m for _, m in results], time.time() - t0))
    if allow_model:
        keep_model_outputs(video_ids, store)
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    print(f"{'video':22} {'creator':14} {'result':6} {'time':>6}  details")
    print("-" * 110)
    for vid, cr, res, details, dt in rows:
        print(f"{vid:22} {cr[:14]:14} {res:6} {dt:5.1f}s  {details[0]}")
        for d in details[1:]:
            print(f"{'':22} {'':14} {'':6} {'':>6}  {d}")
    skipped = sum(1 for r in rows if r[2] == "SKIP")
    print("-" * 110)
    print(f"{len(rows) - failures - skipped} passed, {failures} failed, {skipped} not run"
          f"  (versions detect={server.DETECT_VERSION} refine={server.REFINE_VERSION} post={server.POST_VERSION})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
