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
| QVZHQ8Cmr70 | Sidemen | en | asr | (unverified) | outro 19:38–19:47 (rule), selfpromo 19:47–20:25 | | |

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
