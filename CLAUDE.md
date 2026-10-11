# Sponsor Skipper — CLAUDE.md

Chrome extension that skips **scripted YouTube sponsor reads** and lets **integrated sponsors**
(the creator actually using the product inside the content) play. Local-first prototype:
everything runs on Atishay's M4 Pro MacBook.

## Hard constraints (do not change without asking)

- **Text only.** The transcript comes from YouTube's own caption track, read inside the page.
  No audio download, no Whisper, no yt-dlp.
- **No external datasets.** No SponsorBlock, no third-party APIs.
- **Local LLM only.** Qwen 14B (`qwen3:14b`) via Ollama at `http://localhost:11434`. No API keys.
- **Server is stdlib Python.** No pip dependencies unless asked.
- **Extension is plain Manifest V3 JavaScript.** No bundler, no TypeScript, no npm for now.
- **Bias toward playing.** A wrong skip (skipping real content) is the worst failure. When unsure, don't skip.

## How we work

- Build **one build stage at a time** (list below). Don't start the next stage until the
  current one's "done when" check passes on a real video.
- Keep each stage's change small and reviewable. Explain what you changed and how to test it.
- After a stage passes: tick it in the checklist at the bottom and commit
  (`stage N: <summary>`).
- If a decision in "Open decisions" comes up, ask; don't pick silently.

## Repo layout

```
sponsor-skipper/
├── CLAUDE.md
├── README.md
├── extension/
│   ├── manifest.json    MV3 manifest: two content scripts + background worker
│   ├── page.js          MAIN world, document_start: reads captions from YouTube's player
│   ├── content.js       isolated world: video detection, server calls, markers, skipping, toast
│   ├── content.css      markers, toast, status badge
│   └── background.js    service worker: the only thing that talks to localhost
└── server/
    ├── server.py        localhost:8765, runs Qwen on the transcript, returns segments
    └── cache/           <videoId>.json results (gitignored)
```

## The pipeline

```
[1] content.js   video opens / YouTube in-app navigation → video ID
[2] page.js      player.getPlayerResponse() → caption tracks → caption text
[3] page.js      json3 cues → merged lines {start, end, text}
[4] content.js → background.js → POST localhost:8765/analyze
[5] server.py    transcript hash → cache hit? return it
[6] server.py    Qwen 14B over windows (first 4 min first) → line-range segments
[7] server.py    lines → seconds, skip/keep/show, merge overlaps, cache
[8] content.js   polls GET /analyze every 2s → markers on the progress bar
[9] content.js   playback enters a skip segment → seek to its end → toast with Undo
```

### [1] Video detection (content.js)
- YouTube is a SPA: listen for `yt-navigate-finish` on `document` (plus `popstate`, plus an initial run).
- Video ID = `v` URL param on `/watch`. Ignore Shorts and non-watch pages.
- On every new video: tear down the previous state (listeners, timers, markers, toast). Nothing leaks across videos.

### [2] Caption fetch (page.js, MAIN world)
- Must run in the MAIN world (`"world": "MAIN"` in the manifest) at `document_start`.
- Get the player response from `document.getElementById('movie_player').getPlayerResponse()`.
  **Not** `window.ytInitialPlayerResponse`, which is stale after in-app navigation.
  Wait until `videoDetails.videoId` matches the requested ID.
- Tracks: `captions.playerCaptionsTracklistRenderer.captionTracks`.
  Pick: manual track in the spoken language (= the `asr` track's languageCode) > `asr` (auto) > any manual > first.
- **Path A:** fetch `baseUrl` with `fmt=json3`, `credentials: 'include'`.
- **Path B (fallback when A returns an empty body):** YouTube may require a proof-of-origin token that only the
  player attaches. Patch `fetch` and `XMLHttpRequest` at document_start to capture responses to `/api/timedtext`;
  click `.ytp-subtitles-button` on, wait (≤8s) for the captured response for this video ID, click it back off if it was off.
  If the captured body isn't json3, refetch the captured URL with `fmt=json3`.
- No tracks → report `no captions`; the extension does nothing for that video.
- Talks to content.js via `window.postMessage` (`source: 'ss-content'` → `source: 'ss-page'`).

### [3] Parse (page.js)
- json3 `events[]`: `tStartMs`, `dDurationMs`, `segs[].utf8`. Skip events without `segs` or with whitespace-only text.
- Merge cues into lines: close a line at sentence-ending punctuation or when it would exceed ~8s.
- Output: `[{start, end, text}]` in seconds, 2 decimals.

### [4] Handoff
- content.js → `chrome.runtime.sendMessage` → background.js → `fetch('http://127.0.0.1:8765/...')`.
- Reason: content scripts can't call localhost (cross-origin + private network access). `host_permissions` covers the worker.

### [5]–[7] Server (server.py)
API:
| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| POST | `/analyze` | `{videoId, lang, lines}` | job `{status, segments}`; starts it if new |
| GET | `/analyze?v=ID` | — | job `{status, segments}` or 404 `unknown` |
| GET | `/health` | — | `{ok, model}` |

Status values: `queued → classifying → classifying_rest → done` (or `error`).

- Validate video ID (`^[A-Za-z0-9_-]{11}$`), clean lines, sort by start.
- Cache key: videoId + short SHA-1 of `start|text` lines. Cache file `cache/<videoId>.json` holds hash, lang,
  segments, transcript. A changed caption track = new hash = re-run.
- Run in a background thread. One LLM call at a time (lock).
- **Windows:** 240s with 30s overlap. Classify 0–240s first, then the rest.
- **Prompt input:** `Transcript language: xx` then lines formatted `L{i} [m:ss] text` (global line indices).
- **Ollama call:** `/api/chat`, `stream: false`, `format: <JSON schema>`, `temperature: 0`, `num_ctx: 8192`,
  `think: false` for qwen3.
- **Output schema:** `{"segments": [{start_line, end_line, category, style, brand, confidence, reason}]}`
  - category: `sponsor | selfpromo | interaction`
  - style: `read | integration`
- **Rubric (system prompt):**
  - read = "sponsored by", promo codes, "link in the description", generic benefit claims, topic switch away and back.
  - integration = product used to do the video's actual task, advances the content, no hard topic break.
  - mixed (integration that ends in a promo-code pitch) → two segments; only the pitch is a read.
  - start at the transition line ("but first, a word from…"); end at the last promo line, not where content resumes.
  - transcripts may be auto-generated (no punctuation, misheard brands) and in any language; reasons in English.
- **Post-processing:**
  - Drop any segment whose lines fall outside the window it was given (hallucinated indices).
  - start = start of `start_line`; end = start of the line after `end_line` (or `end_line`'s end if last).
  - Action: sponsor+read with confidence ≥ `SKIP_THRESHOLD` (0.75) → `skip`; sponsor+integration → `keep`;
    low-confidence reads, selfpromo, interaction → `show`.
  - Merge overlapping segments with the same action.
- Log every final segment to the terminal: action, time range, category/style, brand, confidence, reason.

### [8]–[9] Playback (content.js + content.css)
- After POST, poll GET every 2s until `done`/`error`. Redraw markers on each update.
- Video element: `video.html5-main-video`. Progress bar: `.ytp-progress-bar`.
- Markers: absolutely positioned divs, left/width as % of duration; red = skip, green = keep, yellow = show.
  Tooltip shows category/style/brand/confidence/reason. Redraw on `loadedmetadata`.
- On `timeupdate`: if inside a `skip` segment (before `end - 0.5`) not already handled → mark handled,
  set `video.currentTime = end`, show toast "Skipped <brand> read (m:ss) · Undo" for 6s.
- Undo restores the pre-skip time; the segment stays handled.
- Manual seek into a skip segment (`seeking` not caused by us) → mark it handled; respect the user.
- Do nothing while `#movie_player` has class `ad-showing` (YouTube's own ads share the video element).
- Small status badge bottom-right: reading captions / analyzing / ready · N to skip / no captions / server offline.

## Build stages

Each stage ends with a "done when" check on a real video.

1. **Extension skeleton + video detection** — manifest, content.js logs `[SponsorSkipper] video <id>` on load and on
   every in-app navigation, tears down cleanly.
   *Done when:* clicking through 3 videos logs 3 correct IDs, no duplicates.
2. **Caption fetch + parse** — page.js (paths A and B), content.js requests captions and logs line count, language,
   track kind, path used, and the first 5 lines.
   *Done when:* works on a manual-caption video, an auto-caption video, after in-app navigation, and reports
   `no captions` on a video without any.
3. **Server skeleton + relay** — server.py with the API above and a **stub classifier** (fake segment), background.js
   relay, content.js POST + polling.
   *Done when:* opening a video shows the stub segment in the console and in `cache/<id>.json`.
4. **Qwen classification** — real Ollama call, prompt, schema, windows, first-window-first.
   *Done when:* a known sponsored video returns a correct sponsor read with sane boundaries in the server log.
5. **Post-processing + cache** — hallucination filter, boundary snapping, actions, merge, transcript-hash cache.
   *Done when:* revisiting a video is instant and segment times line up with the actual read (±3s).
6. **Playback UI** — markers, skip, toast with Undo, manual-seek respect, ad-showing guard, status badge.
   *Done when:* the read is skipped live, Undo works, seeking back into it lets it play, integrations stay green.
7. **Tuning pass** — collect ~20 real videos (reads, integrations, mixed, non-English, no sponsor), log results,
   tune prompt and threshold until wrong skips are ~zero.

## Open decisions (ask Atishay)

- Skip threshold: starting at 0.75.
- Self-promo and like/subscribe: `show` (current) or `skip`?
- Line merge size: ~8s. Window size: 240s.
- Videos without captions: do nothing (current) vs. a later fallback.
- Qwen model tag: `qwen3:14b` vs `qwen2.5:14b-instruct` if qwen3 misbehaves with JSON.

## Running locally

```bash
ollama serve &                 # or the Ollama app
ollama pull qwen3:14b
python3 server/server.py       # http://localhost:8765
```
Chrome → `chrome://extensions` → Developer mode → Load unpacked → `extension/`.
After editing extension files: hit reload on the extension card, then refresh the YouTube tab.
Debug: DevTools console filtered by `SponsorSkipper`; server terminal for segments; `curl localhost:8765/health`.

## Progress

- [x] 1. Extension skeleton + video detection
- [ ] 2. Caption fetch + parse
- [ ] 3. Server skeleton + relay
- [ ] 4. Qwen classification
- [ ] 5. Post-processing + cache
- [ ] 6. Playback UI
- [ ] 7. Tuning pass
