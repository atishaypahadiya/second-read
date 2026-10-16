#!/usr/bin/env python3
"""Seed a FICTIONAL stats database for the README mockups, through the server's own code.

    SR_DATA_DIR=/tmp/x python3 scripts/mockups/seed.py /tmp/x/fixture.json

Every brand, creator, title, code and number here is invented. The videos, segments, codes and tier-1 ad metrics
are written by server.sync_stats (the same call a finished job makes), events by stats.record_event, and then
back-dated so "today" is one afternoon's worth. Tier-2 analyses are written directly (no model call), so the
server instance that serves the dashboard never has anything to analyze. SR_DATA_DIR must be a fresh temp dir:
this refuses to run against server/cache.

The fixture file holds the one video the player and popup shots show, with its segments as the extension would
receive them, so all shots agree.
"""
import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.environ.get("SR_DATA_DIR", "")
if not DATA or os.path.realpath(DATA).startswith(os.path.realpath(os.path.join(ROOT, "server"))):
    sys.exit("seed.py: set SR_DATA_DIR to a fresh temp dir (never server/cache)")
sys.path.insert(0, os.path.join(ROOT, "server"))
import server  # noqa: E402  (reads SR_DATA_DIR at import)
import stats  # noqa: E402
import intel  # noqa: E402

server.log = stats_log = (lambda *a: None)
stats.set_logger(stats_log)
intel.set_logger(stats_log)
intel._embed_available = False   # topic fit needs nomic-embed-text; the mockups do without
stats.init()
intel.init()
rng = random.Random(20261014)

# --------------------------------------------------------------------------- the invented world
BRANDS = {
    "Northwind VPN": {"domain": "northwindvpn.com", "pitch": [
        "this video is sponsored by Northwind VPN.",
        "I'm on hotel and café wifi most of the time, and Northwind keeps all of that private.",
        "It runs quietly in the background on my phone and laptop, and I never think about it.",
        "Right now you get 3 months free with code {CODE} at northwindvpn.com/{slug}.",
        "There's a 30-day money-back guarantee, so you can try it with no risk.",
        "The link is in the description. Thanks to Northwind for supporting the channel."]},
    "Lumen Notes": {"domain": "lumennotes.app", "pitch": [
        "today's video is brought to you by Lumen Notes.",
        "It's the notes app I plan every video in, with outlines, sketches and voice memos in one place.",
        "Everything syncs between my phone and laptop, even offline.",
        "Try it free for 30 days at lumennotes.app/{slug}, and use code {CODE} for 20% off a year."]},
    "Pinecrest Coffee": {"domain": "pinecrestcoffee.com", "pitch": [
        "quick thank you to Pinecrest Coffee for sponsoring this one.",
        "They roast small batches and ship them within two days of roasting.",
        "My current bag is the dark Highland blend, and it has carried every early morning of this trip.",
        "Your first order comes with a sample bag of the new roast at pinecrestcoffee.com/{slug}."]},
    "Halcyon Sleep": {"domain": "halcyonsleep.com", "pitch": [
        "this part of the video is sponsored by Halcyon Sleep.",
        "Their mattress comes rolled in a box and you get a hundred nights to try it at home.",
        "I've slept on mine for a year and my back has never been happier.",
        "Go to halcyonsleep.com/{slug} and use code {CODE} for $150 off."]},
    "Tidepool Learning": {"domain": "tidepool.io", "pitch": [
        "this video is supported by Tidepool Learning.",
        "It's a site full of short interactive courses in maths, science and programming.",
        "I've been working through the statistics course in the evenings and it's genuinely fun.",
        "The first 200 people to sign up at tidepool.io/{slug} get 20% off a yearly plan."]},
    "Orbitmail": {"domain": "orbitmail.com", "pitch": [
        "a word from today's sponsor, Orbitmail.",
        "It's an encrypted inbox that strips trackers out of every newsletter you get.",
        "Setting it up took me five minutes, and my old address forwards to it.",
        "Use code {CODE} at orbitmail.com/{slug} for 50% off your first year."]},
    "Sprigly": {"domain": "sprigly.com", "pitch": [
        "this video is sponsored by Sprigly.",
        "They ship potted plants that are already settled in, with a care card for each one.",
        "The fern on my desk came from them and it's somehow still alive.",
        "Get free shipping on your first order with code {CODE} at sprigly.com/{slug}."]},
    "Frameline": {"domain": "frameline.app", "pitch": [   # an editor creators use on screen: integrations
        "I'm cutting this one in Frameline, and they're sponsoring this part of the video.",
        "I drop the clips in, it lays out a rough cut from the transcript, and I trim from there.",
        "Then I add captions with one click and fix the two words it got wrong.",
        "If you want to try it, there's a free plan at frameline.app/{slug}."]},
}
# The full names, through the server's own normalization: the alias table is consulted first, and rows stored as
# 'builtin' survive the startup re-derivation (without them "Northwind VPN" resolves to the written "Northwind").
with stats._lock, stats.connect() as c:
    for name in BRANDS:
        for key in {stats._norm(name), stats._norm(name.split()[0])}:
            c.execute("INSERT OR REPLACE INTO brand_aliases(alias, brand, how) VALUES (?, ?, 'builtin')", (key, name))
READ_BRANDS = ["Northwind VPN", "Lumen Notes", "Pinecrest Coffee", "Halcyon Sleep", "Tidepool Learning", "Orbitmail", "Sprigly"]
READ_WEIGHTS = [26, 16, 12, 12, 14, 10, 10]
CREATOR_A = ["Slow", "Quiet", "Copper", "Tallgrass", "Lowtide", "Westwind", "Harbor", "Lantern", "Paper", "Cinder", "Moss",
             "Field", "North", "Juniper", "Kettle", "Ember", "Saltmarsh", "Birch", "Driftwood", "Fernhill"]
CREATOR_B = ["Trails", "Kitchen Lab", "Desk", "Builds", "Coding", "Garage", "Light Studio", "Science", "Moon Games",
             "& Sage", "Diaries", "Notes", "Audio", "Workshop", "& Kit", "Bikes", "Sketchbook", "Cartography", "Rides", "Radio"]
TOPICS = {
    "travel": ["A Quiet Week in the Fjords", "Night Train Across the Alps", "Three Days on a Lighthouse Island", "Walking the Old Salt Road"],
    "food": ["Sourdough From Scratch, Honestly", "One Pan, Five Dinners", "The Slowest Tomato Sauce", "Baking With a Wood Stove"],
    "tech": ["My Desk Setup, Five Years In", "Building a Tiny Home Server", "Rewiring an Old Radio", "A Keyboard From Spare Parts"],
    "science": ["Why Tides Have Two Peaks", "How Lichens Survive Anywhere", "The Physics of a Perfect Skim", "Mapping a Cave by Sound"],
    "making": ["Restoring a Rusted Hand Plane", "A Bench From One Board", "Turning a Bowl From Driftwood", "Fixing a 40-Year-Old Bike"],
}
FILLER = {
    "travel": ["The ferry leaves at eight, so we have the morning to ourselves.", "The light up here changes every few minutes.",
               "We walked along the water until the path ran out.", "This little café has been here since the fifties.",
               "The wind picked up the moment we reached the ridge.", "Tomorrow we take the slow train north."],
    "food": ["The dough needs another hour, so let's talk about flour.", "Low heat is the whole trick here.",
             "Taste it now, and then again in ten minutes.", "I always toast the spices first.",
             "This is the part everyone rushes.", "A pinch more salt than you think."],
    "tech": ["The cable management took longer than the build.", "This drive has been running for six years.",
             "I want it silent more than I want it fast.", "Here's the part that surprised me.",
             "Let's open it up and see what's inside.", "Nothing here is new, and that's the point."],
    "science": ["The moon pulls on the near side and the far side differently.", "Here's the experiment, step by step.",
                "That number turns out to matter a lot.", "Let's draw it out on the board.",
                "It took a while for anyone to believe this.", "Now watch what happens when we add water."],
    "making": ["The rust came off easier than I expected.", "Measure twice, then once more.",
               "This joint has to be tight or nothing works.", "I'm leaving the tool marks in on purpose.",
               "Sharpening first, always.", "Here's how it looked before I started."],
}


def creator_name(i):
    return f"{CREATOR_A[i % len(CREATOR_A)]} {CREATOR_B[(i * 7 + 3 + i // len(CREATOR_A)) % len(CREATOR_B)]}"   # 40 distinct


def vid_id(n):
    return f"mock{n:07d}"   # 11 characters, like a YouTube id; obviously not one


def slug_of(creator):
    """The channel's distinctive word: 'Slow Atlas Travel' -> atlas, 'Copper Desk' -> desk."""
    words = [w for w in creator.lower().split() if w.isalpha()]
    return words[1] if len(words) > 1 else words[0]


def code_of(creator, brand):
    return (slug_of(creator).upper() + ("" if brand in ("Northwind VPN", "Orbitmail") else "20"))[:12]


def ad_lines(brand, creator, start, end):
    """The brand's pitch, spread over [start, end) in caption lines."""
    text = [t.replace("{CODE}", code_of(creator, brand)).replace("{slug}", slug_of(creator)) for t in BRANDS[brand]["pitch"]]
    if brand != "Frameline":
        text[0] = "Before we keep going, " + text[0]
    step = (end - start) / len(text)
    return [{"start": round(start + i * step, 2), "end": round(start + (i + 1) * step - 0.3, 2), "text": t} for i, t in enumerate(text)]


def transcript(topic, duration, ads):
    lines, t = [], 0.6
    while t < duration - 4:
        inside = next((a for a in ads if a[0] - 0.5 <= t < a[1]), None)
        if inside:
            lines += ad_lines(inside[2], inside[3], inside[0], inside[1])
            t = inside[1] + 0.4
            continue
        nxt = min([a[0] for a in ads if a[0] > t] + [duration])
        if t + 5.2 > nxt - 0.4:   # no room for a whole line before the next ad: start the ad
            t = nxt if nxt < duration else duration
            continue
        lines.append({"start": round(t, 2), "end": round(t + 5.2, 2), "text": rng.choice(FILLER[topic])})
        t += 6.0
    return lines


def description(creator, brands):
    out = [f"New video from {creator}. Thanks for watching!", ""]
    for b in brands:
        if "{CODE}" not in " ".join(BRANDS[b]["pitch"]):   # no code in the pitch: a link-only sponsor line
            out.append(f"{b} (sponsor): https://{BRANDS[b]['domain']}/{slug_of(creator)}")
        else:
            out.append(f"{b} (ad): use code {code_of(creator, b)} at https://{BRANDS[b]['domain']}/{slug_of(creator)}")
    return "\n".join(out)


def heat_curve(duration, peaks):
    cols = []
    for i in range(100):
        v = 0.25 + 0.1 * rng.random() + sum(0.7 * max(0.0, 1 - abs(i - p) / 6) for p in peaks)
        cols.append([i, round(min(1.0, v), 3)])
    return cols


def analysis_for(seg, lines, duration):
    text = [l["text"] for l in lines if l["end"] > seg["start"] and l["start"] < seg["end"]]
    said = text[2] if len(text) > 2 else text[-1]
    tags = [{"tag": "testimony", "quote": said.split(", ")[0].rstrip(".") if len(said) > 60 else said.rstrip(".")}]
    if any("guarantee" in t for t in text):
        tags.append({"tag": "risk reversal", "quote": "30-day money-back guarantee"})
    if any("first 200" in t for t in text):
        tags.append({"tag": "urgency/scarcity", "quote": "The first 200 people"})
    later = [l for l in lines if l["start"] > seg["end"] + 60] or lines
    spot = later[len(later) // 3]
    return {"persuasion": tags,
            "beats": [{"beat": "hook", "line": 0}, {"beat": "product", "line": 1}, {"beat": "offer", "line": len(text) - 2},
                      {"beat": "cta", "line": len(text) - 1}],
            "better_placement": {"time_s": round(spot["start"], 1), "reason": "A natural pause in the story where the product fits the topic.",
                                 "quote": spot["text"], "quote_verified": True, "time_from_quote": True, "same_as_now": False},
            "seconds": round(6 + 6 * rng.random(), 1)}


def sync(job_id, creator, cid, title, topic, duration, segs, ads, chapters, heat, paid=True):
    lines = transcript(topic, duration, ads)
    desc = description(creator, [a[2] for a in ads])
    hints = server.extract_hints(desc)
    job = {"video_id": job_id, "creator": creator, "channel_id": cid, "title": title, "lang": "en", "duration": duration,
           "lines": lines, "alines": lines, "hints": hints, "terms": server.hint_terms(hints, creator), "heat": heat,
           "chapter_times": chapters, "paid_promotion": paid, "disclosed": True, "description": desc}
    for s in segs:
        if s["category"] == "sponsor" and not s["evidence"]:
            s["evidence"] = next((l["text"] for l in lines if l["start"] >= s["start"] - 0.5), "")[:200]
    server.sync_stats(job, segs)
    LINES[job_id] = lines
    return job


LINES = {}   # video id -> the transcript that was stored for it


def seg(start, end, brand, style="read", category="sponsor", action=None, conf=0.95, source="detect", evidence="", **kw):
    return {"start": round(start, 2), "end": round(end, 2), "category": category, "style": style, "brand": brand,
            "action": action or ("keep" if style == "integration" else "skip" if category == "sponsor" else "show"),
            "confidence": conf, "evidence": evidence, "reason": f"illustrative {category} segment", "source": source, **kw}


# --------------------------------------------------------------------------- the hero video (player + popup shots)
HERO = {"video_id": "atlasFjord1", "creator": "Slow Atlas Travel", "channel_id": "UCmockAtlasTravel000000",
        "title": "A Quiet Week in the Fjords", "duration": 1104.0}
HERO_EVIDENCE = ("Before we keep going, this video is sponsored by Northwind VPN. I'm on hotel and café wifi most of the time, "
                 "and Northwind keeps all of that private.")
hero_segs = [
    seg(372.0, 430.0, "Northwind VPN", lead_in_s=4.0, conf=0.96, source="hint", evidence=HERO_EVIDENCE),
    seg(570.0, 665.0, "Frameline", style="integration", conf=0.9, source="integration"),
    seg(920.0, 965.0, "Pinecrest Coffee", conf=0.93),
    seg(1030.0, 1052.0, "Atlas Prints", category="selfpromo", conf=0.88),
]

# --------------------------------------------------------------------------- the other videos
events = []   # (days_ago, kind, video, channel, seg, playhead, saved)
videos = []
n = 0
for ci in range(40):
    creator = creator_name(ci) if ci else HERO["creator"]
    cid = f"UCmock{ci:02d}{slug_of(creator)[:6]:<6}".replace(" ", "x")[:24]
    topic = list(TOPICS)[ci % len(TOPICS)]
    for k in range(rng.randint(5, 8)):
        n += 1
        duration = float(rng.randint(540, 2100))
        ads, segs = [], []
        if rng.random() < 0.76:   # about a quarter of the videos carry no sponsor at all
            brand = rng.choices(READ_BRANDS, READ_WEIGHTS)[0]
            a0 = round(rng.uniform(0.12, 0.4) * duration, 1)
            a1 = round(a0 + rng.uniform(38, 95), 1)
            ads.append((a0, a1, brand, creator))
            segs.append(seg(a0, a1, brand, conf=round(rng.uniform(0.86, 0.98), 2)))
            if rng.random() < 0.22:   # a second read later on (60-75%, after any integration)
                b2 = rng.choice([b for b in READ_BRANDS if b != brand])
                s0 = round(rng.uniform(0.66, 0.75) * duration, 1)
                ads.append((s0, s0 + 50, b2, creator))
                segs.append(seg(s0, s0 + 50, b2, conf=0.9))
        if rng.random() < 0.15:   # an integration, between the first read and any second one
            i0 = round(0.48 * duration, 1)
            ads.append((i0, i0 + 80, "Frameline", creator))
            segs.append(seg(i0, i0 + 80, "Frameline", style="integration", conf=0.9, source="integration"))
        if rng.random() < 0.12:
            segs.append(seg(duration - 70, duration - 50, "", category="selfpromo", conf=0.86))
        if rng.random() < 0.3:
            segs.append(seg(duration - 40, duration - 30, "", category="interaction", conf=1.0, source="rule"))
        segs.sort(key=lambda s: s["start"])
        title = rng.choice(TOPICS[topic]) + ("" if k == 0 else f" (Part {k + 1})")
        videos.append((vid_id(n), creator, cid, title, topic, duration, segs, sorted(ads), [], heat_curve(duration, [30, 70])))

# --------------------------------------------------------------------------- events: one viewer's history
reads = [(v, s) for v in videos for s in v[6] if s["category"] == "sponsor" and s["style"] == "read"]
integ = [(v, s) for v in videos for s in v[6] if s["style"] == "integration"]
rng.shuffle(reads)
TARGET_SKIPS, TARGET_UNDOS, TARGET_KEPT, TARGET_SAVED_S = 213, 3, 17, 11560 - 58   # the hero's own skip makes 214 / 11560
TODAY_SKIPS = 6
if len(reads) < TARGET_SKIPS + 6 or len(integ) < TARGET_KEPT:
    sys.exit(f"seed.py: not enough segments ({len(reads)} reads, {len(integ)} integrations)")
skipped = reads[:TARGET_SKIPS]
undone = skipped[10:10 + TARGET_UNDOS]
saved = [s["end"] - s["start"] for _, s in skipped]
over = sum(saved) - sum(saved[10:10 + TARGET_UNDOS]) - TARGET_SAVED_S
if over < 0:
    sys.exit("seed.py: reads too short for the target total")
for i in range(len(saved)):   # some skips started a few seconds in (the viewer was already past the start)
    if over <= 0:
        break
    if 10 <= i < 10 + TARGET_UNDOS:
        continue
    cut = min(over, saved[i] * 0.4)
    saved[i] -= cut
    over -= cut
for i, ((v, s), sv) in enumerate(zip(skipped, saved)):
    days = 0 if i < TODAY_SKIPS else 1 + (i * 97) % 120
    events.append((days, "skip", v, s, s["end"] - sv, sv))
    if 10 <= i < 10 + TARGET_UNDOS:
        events.append((days, "undo", v, s, s["end"] - sv, sv))
for v, s in reads[TARGET_SKIPS:TARGET_SKIPS + 5]:
    events.append((2 + rng.randint(0, 60), "seek_into", v, s, s["start"] + 3, None))
for i, (v, s) in enumerate(integ[:TARGET_KEPT]):
    events.append((0 if i < 1 else 1 + (i * 13) % 100, "integration_played", v, s, s["end"], None))

# --------------------------------------------------------------------------- write: other videos first, hero last
base = time.time()
for idx, (vid, creator, cid, title, topic, duration, segs, ads, chapters, heat) in enumerate(videos):
    sync(vid, creator, cid, title, topic, duration, segs, ads, chapters, heat)
hero_ads = [(372.0, 430.0, "Northwind VPN", HERO["creator"]), (570.0, 665.0, "Frameline", HERO["creator"]),
            (920.0, 965.0, "Pinecrest Coffee", HERO["creator"])]
hero_chapters = [(0.0, "Arrival"), (300.0, "The ferry north"), (540.0, "Editing on the boat"), (820.0, "Cabin mornings"),
                 (1000.0, "Last light")]
hero_job = sync(HERO["video_id"], HERO["creator"], HERO["channel_id"], HERO["title"], "travel", HERO["duration"],
                hero_segs, hero_ads, hero_chapters, heat_curve(HERO["duration"], [44, 63, 81]))
events.append((0, "skip", (HERO["video_id"], HERO["creator"], HERO["channel_id"], HERO["title"]), hero_segs[0], 372.0, 58.0))

for days, kind, v, s, playhead, sv in events:
    vid, creator, cid, title = v[0], v[1], v[2], v[3]
    stats.record_event({"kind": kind, "videoId": vid, "channelId": cid, "channelName": creator, "title": title,
                        "segment": {"start": s["start"], "end": s["end"], "brand": s["brand"], "category": s["category"], "style": s["style"]},
                        "playhead": playhead, "saved": sv})
# back-date: record_event stamps "now"; spread the history over the past months, today's at this afternoon's hours
with stats._lock, stats.connect() as c:
    rows = c.execute("SELECT id FROM events ORDER BY id").fetchall()
    for (days, kind, *_), r in zip(events, rows):
        hour = 9 + (r["id"] * 5) % 12
        t = base - days * 86400 if days else base - 3600 * (len(rows) - r["id"]) / max(1, len(rows)) - 60
        stamp = time.strftime("%Y-%m-%dT", time.localtime(t)) + (time.strftime("%H:%M:%S", time.localtime(t)) if not days else f"{hour:02d}:{(r['id'] * 7) % 60:02d}:00")
        c.execute("UPDATE events SET ts = ? WHERE id = ?", (stamp, r["id"]))
    # tier-2: written directly so the dashboard's server has nothing to analyze (no model call)
    segs_rows = c.execute("SELECT s.id, s.video_id, s.seg_start, s.seg_end FROM segments s WHERE s.category = 'sponsor'").fetchall()
lines_of = LINES
dur_of = {v[0]: v[5] for v in videos}
dur_of[HERO["video_id"]] = HERO["duration"]
with stats._lock, stats.connect() as c:
    for r in segs_rows:
        a = analysis_for({"start": r["seg_start"], "end": r["seg_end"]}, lines_of[r["video_id"]], dur_of[r["video_id"]])
        c.execute("INSERT OR REPLACE INTO ad_analysis(segment_id, version, created_at, analysis, error) VALUES (?,?,?,?,?)",
                  (r["id"], intel.ANALYSIS_VERSION, stats.now(), json.dumps(a), None))

# --------------------------------------------------------------------------- the fixture for the player and popup pages
pub = []
for s in hero_segs:
    p = {k: s[k] for k in ("start", "end", "category", "style", "brand", "action", "confidence", "evidence", "reason", "source")}
    p["brand_canonical"] = s.get("brand_canonical") or s["brand"]
    if s.get("lead_in_s"):
        p["lead_in_s"] = s["lead_in_s"]
        p["lead_in"] = {"from": s["start"] + s["lead_in_s"], "sentences": ["Before we keep going,"]}
    pub.append(p)
fixture = {"video": HERO, "segments": pub, "lines": hero_job["lines"][:40], "description": hero_job["description"]}
json.dump(fixture, open(sys.argv[1], "w"), indent=1)
stats.renormalize_brands()   # what the server does at startup, done here so it has nothing left to change
o = stats.compute()["overall"]
print(f"seed: {len(videos) + 1} videos, {len(reads) + 2} reads, {len(integ) + 1} integrations; "
      f"skips {o['skips']}, undos {o['undos']}, saved {o['saved_s']} s, kept {o['integrations_played']}, today {o['today']}")
