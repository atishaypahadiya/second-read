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
| dU7gCt-7kKk | fern | en | asr |  | SKIP 9:17-9:56 Udo (0.95) |  | BASELINE v1/1/1 qwen3:14b; 0 reused | 140s | 16 | 140s | 2026-10-13 06:10 |
| O7LvhSQ5Te4 | Johnny Harris | en | asr |  | SKIP 10:27-11:51 Incogni (0.95); SHOW 29:53-30:38 newpress.com (0.95) |  | BASELINE v1/1/1 qwen3:14b; 0 reused | 166s | 17 | 166s | 2026-10-13 06:13 |
| SOQynHuWPeI | fern | en | asr |  | SKIP 4:47-5:39 Cody (0.95) |  | BASELINE v1/1/1 qwen3:14b; 0 reused | 95s | 9 | 95s | 2026-10-13 06:14 |
| vGoRpjE-svI | Hindi vlog | hi | asr |  | none |  | BASELINE v1/1/1 qwen3:14b; 0 reused | 71s | 6 | - | 2026-10-13 06:15 |
| dU7gCt-7kKk | fern | en | asr |  | SKIP 9:17-9:56 Udo (0.95) |  | AFTER-8B v2/1/1 detect=qwen3:8b refine=qwen3:14b; windows 2/14; 0 reused | 54s | 4 | 54s | 2026-10-13 06:17 |
| SOQynHuWPeI | fern | en | asr |  | SKIP 4:44-5:39 Cody (0.95) |  | AFTER-8B v2/1/1 detect=qwen3:8b refine=qwen3:14b; windows 3/6; 0 reused | 123s | 6 | 109s | 2026-10-13 06:29 |
| vGoRpjE-svI | Hindi vlog | hi | asr |  | none |  | AFTER-8B v2/1/1 detect=qwen3:8b refine=qwen3:14b; windows 0/6; 0 reused | 0s | 0 | - | 2026-10-13 06:29 |
| SOQynHuWPeI | fern | en | asr |  | SKIP 4:44-5:39 Cody (0.95) |  | RAPID v2/1/1 detect=qwen3:8b refine=qwen3:14b; windows 3/6; 0 reused | 83s | 6 | 74s | 2026-10-13 06:30 |
| dU7gCt-7kKk | fern | en | asr |  | SKIP 9:17-9:56 Udo (0.95) |  | AFTER-14B v2/1/1 detect=qwen3:14b refine=qwen3:14b; windows 2/14; 0 reused | 62s | 4 | 62s | 2026-10-13 06:32 |
| O7LvhSQ5Te4 | Johnny Harris | en | asr |  | SKIP 10:27-11:51 Incogni (0.95); SHOW 29:53-30:38 newpress.com (0.95) |  | AFTER-14B v2/1/1 detect=qwen3:14b refine=qwen3:14b; windows 4/15; 0 reused | 90s | 6 | 47s | 2026-10-13 06:35 |
| SOQynHuWPeI | fern | en | asr |  | SKIP 4:47-5:39 Cody (0.95) |  | AFTER-14B v2/1/1 detect=qwen3:14b refine=qwen3:14b; windows 3/6; 0 reused | 67s | 6 | 60s | 2026-10-13 06:36 |
| vGoRpjE-svI | Hindi vlog | hi | asr |  | none |  | AFTER-14B v2/1/1 detect=qwen3:14b refine=qwen3:14b; windows 0/6; 0 reused | 0s | 0 | - | 2026-10-13 06:36 |
| SyNtH3t1c05 |  | en |  |  | SKIP 0:41-1:06 Coddy (0.90) |  | v2/1/1 detect=qwen3:14b refine=qwen3:14b; windows 1/1; 0 reused | 39s | 3 | 39s | 2026-10-13 06:41 |
