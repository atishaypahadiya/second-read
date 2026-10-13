# Sponsor Skipper — test set

Hand-curated. Expectations come from watching the video; **verdict** is filled in by Atishay after each pipeline
change (`ok` / `false skip` / `missed` / `boundary off` / `wrong marker`). Per-run timings and the full history of
automatic rows live in `server/cache/results.local.md` (gitignored: it records which videos were watched).

| video | creator | lang | kind | expected | latest result | verdict | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dU7gCt-7kKk | fern | en | asr | SKIP Odoo read ~9:17–9:55 (asr mishears brand as Udo/Udu) | SKIP 9:17–9:56 Udo (0.95) | ok | start varied 9:03/9:17 run to run (candidate range differs) |
| O7LvhSQ5Te4 | Johnny Harris | en | asr | SKIP Incogni read ~10:27–11:52 with promo code; newpress.com selfpromo = SHOW | SKIP 10:27–11:51 Incogni (0.95); SHOW 29:53–30:38 newpress.com | ok | |
| SOQynHuWPeI | fern | en | asr | SKIP Coddy read ~4:47–5:39 (code FERN20); NO marker for the VPN disclaimer 1:11 or Onavo 11:57 | SKIP 4:47–5:39 Cody (0.95); disclaimer + Onavo dropped by filter | ok | start snaps back to "You" |
| vGoRpjE-svI | (Hindi vlog) | hi | asr | NOTHING to skip | none (prefilter: 0 windows sent) | ok | was a false skip (Adoni, 0.75) before the evidence gate + 0.85 threshold |
| QVZHQ8Cmr70 | Sidemen | en | asr | (unverified) | SHOW 19:46–20:24 | | |

## Speed (replayed transcripts, qwen3:14b, 24 GB M4 Pro)

| video | baseline job / first skip / calls | after prefilter+scheduler job / first skip / calls |
| --- | --- | --- |
| dU7gCt-7kKk | 140s / 140s / 16 | 62s / 62s / 4 |
| O7LvhSQ5Te4 | 166s / 166s / 17 | 90s / 47s / 6 |
| SOQynHuWPeI | 95s / 95s / 9 | 67s / 60s / 6 |
| vGoRpjE-svI | 71s / – / 6 | 0s / – / 0 |
