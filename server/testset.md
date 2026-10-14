# Second Read — test set

Hand-curated. Expectations come from watching the video; **verdict** is filled in by Atishay after each pipeline
change (`ok` / `false skip` / `missed` / `boundary off` / `wrong marker`). Per-run timings and the full history of
automatic rows live in `server/cache/results.local.md` (gitignored: it records which videos were watched).

| video | creator | lang | kind | expected | latest result | verdict | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dU7gCt-7kKk | fern | en | asr | SKIP Odoo read ~9:17–9:55 (asr mishears brand as Udo/Udu) | SKIP 9:17–9:56 Udo (0.95) | ok | start varied 9:03/9:17 run to run (candidate range differs) |
| O7LvhSQ5Te4 | Johnny Harris | en | asr | SKIP Incogni read ~10:27–11:52 with promo code; newpress.com selfpromo = SHOW | SKIP 10:27–11:51 Incogni (0.95); SHOW 29:53–30:38 newpress.com | ok | |
| SOQynHuWPeI | fern | en | asr | SKIP Coddy read ~4:47–5:39 (code FERN20); NO marker for the VPN disclaimer 1:11 or Onavo 11:57 | SKIP 4:47–5:39 Cody (0.95); disclaimer + Onavo dropped by filter | ok | start snaps back to "You" |
| vGoRpjE-svI | (Hindi vlog) | hi | asr | NOTHING to skip; NO segment of any kind at 0:45–0:57 ("shooting for Fossil": his own brand work, told as story) | none (prefilter: 0 windows sent on the Hindi track; on the English track detect + refine label Fossil sponsor/integration and the ad-marker filter drops it) | ok | was a false skip (Adoni, 0.75) before the evidence gate + 0.85 threshold; was a stored Fossil integration (keep, 0.80) before post v5 |
| QVZHQ8Cmr70 | Sidemen | en | asr | (unverified; snapshot row in testset.json) | outro 19:38–19:47 (rule), selfpromo 19:47–20:25 | | |
| uI8oJYmCaz0 | fern | en | asr | SKIP the Saylite read from its lead-in ("Look at Lens, for example, one of our 3D animators. Hey, I'm currently in China.") | SKIP 2:01–2:49 (was 2:06: published at 2:06, lead-in added) | | lead-in check: both sentences lead-in |
| tdIUMkXxtHg | Howtown | en | asr | SKIP the Headspace read from its lead-in ("I've been moving between two different continents. So, it's a happy coincidence that...") | SKIP 7:27–8:37 (was 7:31) | | cached transcript still has pre-build-17 word times; "I'm like distracted by the news." stays |
| 5ZqolNRkxvM | fern | en | asr | SKIP the Displate read from the segue question ("Do you actually know about today's sponsor already?") | SKIP 4:47–5:56 (was 4:49) | | refine's start kept over the word pass; the banter before it stays |
| HauQtcj7UTM | fern | en | asr | SKIP the Brilliant read from "A much better deal..."; NOTHING cut before it | SKIP 9:40–10:32, start unchanged | ok | "By late 2023, BMW had dropped the idea..." is content |
| it5G_rIan6g | Daniel Steiner | en | asr | SKIP the Ground News read from "This has gone back and forth in the courts..."; NOTHING cut before it | SKIP 8:01–9:29, start unchanged | | the sentence before is content (and longer than the 6 s cap) |

### Snapshot rows (pipeline output, not verified)

Added 2026-10-13 from videos watched during development. Each expects what the pipeline produced at the time
(`"verified": false, "source": "pipeline-snapshot"`); regress.py reports them apart from the verified rows. A
verdict here turns a row into a verified one.

| video | creator | lang | kind | snapshot | verdict | notes |
| --- | --- | --- | --- | --- | --- | --- |
| kS-CGkiPetQ | Veritasium | en | manual | SKIP Boot.dev 8:40–9:41 | | manual captions, estimated word times |
| RDoVLHaYfgM | Cleo Abram | en | manual | SKIP Ground News 7:13–8:12 | | |
| iX6w1P60m8M | Cleo Abram | en | asr | SKIP Rhinoshield 7:11–8:28 | | |
| bCmwCkNY85g | Cleo Abram | en | manual | SKIP Shopify 1:28–1:30 | | 2 s: probably not the whole read |
| E1BeAUHnLIg | Mrwhosetheboss | en | asr | SKIP Eight Sleep 11:17–11:59 | | |
| vpn3VmGvIXA | TBJZL | en | asr | nothing | | only the creator's own links |
| g5BMdEpLLZE | Untriggered | ar (really en) | asr | nothing | | podcast; track labeled `ar` by an as-is read |
| yTwtg9cwr90 | MoreSidemen | en | asr | selfpromo label 24:24–24:39 | | Sidemen's own brands |
| sTOEvwyRYxM | Ramasamy Ramanathan | en | asr | nothing | | no description |
| _phJIm8mUT8 | Dhruv Rathee | hi | manual | nothing (Hindi track only) | | the English translation the live run used is not cached |
| yTV22sMVHPQ | Dhruv Rathee | hi | asr | nothing (Hindi track only) | | same |

### Transcript-labeled (labeled from the transcript, not checked by a human)

`"verified": false, "source": "transcript-labeled"`: the labels come from reading the transcript, independently of
what the pipeline produced; nobody has checked them against the audio. Reported apart from verified rows.

| video | creator | label | notes |
| --- | --- | --- | --- |
| 4zxG_oUxBg4 | fern | SKIP DeleteMe read 5:28.8–6:16.9 (from the segue "But there's your way out…" to "…link in the description."); SKIP reminder 24:40.2–24:49.0 | the skip may start ≤ 4 s late / end ≤ 4.5 s early |
| yS0ML6cwEYQ | The Rest Is Science | SKIP Cancer Research UK read 1:37.0–2:51.2 | a charity paying for the spot; was missed before post v8 |
| FBeMrUWX73s | RAINBOLT | NOTHING: no ad; 61:42–62:02 ("got all kinds of uh sponsors") is an interview about a community project | was kept as a "Sponsors" integration before post v9 |

### Needs a label (replayed, not checked)

| video | creator | question |
| --- | --- | --- |
| lyMTD1eAZAw | Dhruv Rathee | Times of the NordVPN read (live: skip 9:35–10:10 named "Na VPN"). The description line "Get an exclusive NordVPN deal here" was not read as a sponsor line; "Viewer" from the sponsor disclaimer line became the hint brand. |

Rule-based categories currently expected by `testset.json` (label-only, guard against regressions):
fern dU7gCt-7kKk outro 46:29–46:35 ("byebye"); Hindi vlog intro 0:00–0:15 (no speech); Sidemen outro + selfpromo above.

## Speed (replayed transcripts, qwen3:14b, 24 GB M4 Pro)

Columns: total job time / time-to-first-skip / Ollama calls. Replays carry no description, so the hint fast path
did not fire on the real videos; on a synthetic transcript with a "(ad)" description line it reached the skip in
11s with a single call.

| video | baseline | prefilter + scheduler | + merged refine/anchors (AFTER2) |
| --- | --- | --- | --- |
| dU7gCt-7kKk | 140s / 140s / 16 | 62s / 62s / 4 | 59s / 59s / 4 (anchor rejected, word pass ran) |
| O7LvhSQ5Te4 | 166s / 166s / 17 | 90s / 47s / 6 | 89s / 44s / 6 (anchors used) |
| SOQynHuWPeI | 95s / 95s / 9 | 67s / 60s / 6 | 46s / 39s / 5 (anchor rejected, word pass ran) |
| vGoRpjE-svI | 71s / – / 6 | 0s / – / 0 | 0s / – / 0 |

Per-call latency on this machine varies ~2x between runs (detect of the same window: 9s to 24s), so differences
under ~10s are noise. The ≤ 25s time-to-first-skip target is only reached via the hint fast path so far.
