# Sponsor Skipper — results log

One row per analyzed video. The server appends a row automatically when a job finishes (verdict blank);
fill in **verdict** by watching the video: `ok` / `false skip` / `missed` / `boundary off` / `wrong marker`.
Rows above the line are hand-entered expectations for the initial test set.

Columns: video ID · creator · language · caption kind · expected · actual segments (action m:ss-m:ss brand (conf)) ·
verdict · notes · total job time · Ollama calls · time-to-first-skip · when.

| video | creator | lang | kind | expected | actual | verdict | notes | job time | calls | first skip | when |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dU7gCt-7kKk | fern | en | asr | SKIP Odoo read ~9:17-9:55 (asr mishears brand as Udo/Udu) | SKIP 9:17-9:56 Udo (0.90) | ok | v8: end from word timing; start varies 9:03/9:17 run to run | | | | 2026-10-13 |
| O7LvhSQ5Te4 | Johnny Harris | en | asr | SKIP Incogni read ~10:27-11:52 with promo code; newpress.com selfpromo = SHOW | SKIP 10:27-11:51 Incogni (0.95); SHOW 29:53-30:38 newpress.com | ok | v8 | | | | 2026-10-13 |
| SOQynHuWPeI | fern | en | asr | SKIP Coddy read ~4:47-5:39 (code FERN20); NO marker for VPN disclaimer 1:11 or Onavo 11:57 | SKIP 4:49-5:39 Cody (0.90) | ok | v8: disclaimer + Onavo dropped by filter; start snapped to 'Almost' | | | | 2026-10-13 |
| vGoRpjE-svI | (Hindi vlog) | hi | asr | NOTHING to skip | SHOW 9:10-9:22 ? (0.60) -> dropped by 0.7 show threshold | ok | was a false skip (Adoni, 0.75) before evidence gate + 0.85 threshold | | | | 2026-10-13 |
| QVZHQ8Cmr70 | Sidemen | en | asr | (unverified) | SHOW 19:46-20:24 ? | | | | | | 2026-10-13 |

<!-- auto-appended rows below (newest last) -->
