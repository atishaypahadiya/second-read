// Sponsor Skipper — page.js (MAIN world, document_start)
// Stage 2: read the caption track for the current video from YouTube's own player
// and hand parsed lines to content.js via postMessage.
//
// content.js → window.postMessage({ source: 'ss-content', type: 'getCaptions', videoId })
// page.js    → window.postMessage({ source: 'ss-page', type: 'captions', videoId, lang, kind, path, lines })
//           or window.postMessage({ source: 'ss-page', type: 'captions', videoId, error: 'no captions' })
//
// Path A: fetch the track's baseUrl directly. Currently returns an empty body because
//         YouTube requires a proof-of-origin token that only the player attaches.
// Path B: capture the URL of the player's own /api/timedtext request (which carries the
//         token) the moment the request starts, then fetch that URL ourselves.
//         If CC is off we toggle it on just long enough to see the request start,
//         then toggle it back off before any caption renders.

(() => {
  if (window.__sponsorSkipperPageLoaded) return;
  window.__sponsorSkipperPageLoaded = true;

  // Bumped together in page.js, content.js, background.js and server.py (CLIENT_BUILD). The server logs all three
  // and warns when they differ, which is how a half-reloaded extension shows up.
  const BUILD = 14;
  const TAG = '[SponsorSkipper:page]';
  const dbg = (...a) => console.log(TAG, ...a);
  const MAX_LINE_SECONDS = 8;
  const PLAYER_WAIT_MS = 15000;
  const CLICK_CAPTURE_WAIT_MS = 3000;
  const CC_ON_CAPTURE_WAIT_MS = 8000;
  const SETTLE_MS = 1500;            // let YouTube apply its saved CC preference before we touch anything
  const EMPTY_RETRY_DELAY_MS = 2000; // retry once after a 0-byte response

  // ---------------------------------------------------------------------------
  // Path B infrastructure: capture /api/timedtext request URLs as they START.
  // Installed at document_start, before the player makes any request.
  // ---------------------------------------------------------------------------
  const captured = new Map(); // videoId -> { url, at }
  const waiters = new Map();  // videoId -> resolve(entry)

  function videoIdFromTimedtextUrl(url) {
    try {
      const u = new URL(url, location.href);
      if (!u.pathname.includes('/api/timedtext')) return null;
      return u.searchParams.get('v');
    } catch { return null; }
  }

  function captureUrl(url) {
    const videoId = videoIdFromTimedtextUrl(url);
    if (!videoId) return;
    const entry = { url: new URL(url, location.href).href, at: performance.now() };
    captured.set(videoId, entry);
    const resolve = waiters.get(videoId);
    if (resolve) { waiters.delete(videoId); resolve(entry); }
  }

  const origFetch = window.fetch;
  window.fetch = function (input) {
    try {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input && input.url;
      if (url) captureUrl(url);
    } catch { /* never break the page */ }
    return origFetch.apply(this, arguments);
  };

  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    try { captureUrl(typeof url === 'string' ? url : String(url)); } catch { /* ignore */ }
    return origOpen.apply(this, arguments);
  };

  function waitForCapture(videoId, timeoutMs) {
    const existing = captured.get(videoId);
    if (existing) return Promise.resolve(existing);
    return new Promise((resolve) => {
      const timer = setTimeout(() => { waiters.delete(videoId); resolve(null); }, timeoutMs);
      waiters.set(videoId, (entry) => { clearTimeout(timer); resolve(entry); });
    });
  }

  // ---------------------------------------------------------------------------
  // Player access
  // ---------------------------------------------------------------------------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const player = () => document.getElementById('movie_player');
  const videoEl = () => document.querySelector('#movie_player video.html5-main-video');
  const subtitlesButton = () => document.querySelector('#movie_player .ytp-subtitles-button');
  const subtitlesOn = () => { const b = subtitlesButton(); return !!b && b.getAttribute('aria-pressed') === 'true'; };
  const subtitlesButtonEnabled = () => {
    const b = subtitlesButton();
    return !!b && !b.disabled && b.getAttribute('aria-disabled') !== 'true' && b.style.display !== 'none';
  };

  // The user's selected caption track (may include translationLanguage when auto-translating).
  function getTrack() {
    try {
      const p = player();
      const t = p && typeof p.getOption === 'function' ? p.getOption('captions', 'track') : null;
      return t && typeof t === 'object' ? t : null;
    } catch { return null; }
  }
  function trackKey(t) {
    if (!t) return '(none)';
    const tl = t.translationLanguage && t.translationLanguage.languageCode;
    return `${t.languageCode || '?'}${t.kind === 'asr' ? '/asr' : ''}${tl ? `→${tl}` : ''}`;
  }
  function setTrack(t) {
    try {
      const p = player();
      if (p && typeof p.setOption === 'function') { p.setOption('captions', 'track', t); return true; }
    } catch (e) { dbg('setOption(track) threw', e); }
    return false;
  }

  async function waitForPlayerResponse(videoId) {
    const deadline = Date.now() + PLAYER_WAIT_MS;
    while (Date.now() < deadline) {
      const p = player();
      if (p && typeof p.getPlayerResponse === 'function') {
        try {
          const pr = p.getPlayerResponse();
          if (pr && pr.videoDetails && pr.videoDetails.videoId === videoId) return pr;
        } catch { /* player not ready */ }
      }
      await sleep(250);
    }
    return null;
  }

  // Ready = media metadata loaded and the CC button exists and is enabled.
  async function waitForPlayerReady() {
    const deadline = Date.now() + PLAYER_WAIT_MS;
    while (Date.now() < deadline) {
      const v = videoEl();
      if (v && v.readyState >= 1 && subtitlesButtonEnabled()) return true;
      await sleep(200);
    }
    return false;
  }

  function pickTrack(tracks) {
    const asr = tracks.find((t) => t.kind === 'asr');
    const spokenLang = asr && asr.languageCode;
    const manual = tracks.filter((t) => t.kind !== 'asr');
    return (
      (spokenLang && manual.find((t) => t.languageCode === spokenLang)) ||
      asr ||
      manual[0] ||
      tracks[0]
    );
  }

  // Build the URL we fetch: the captured (token-bearing) URL, retargeted at the picked
  // track so a user watching a translated or different track doesn't change what we analyze.
  function buildFetchUrl(capturedUrl, track) {
    const u = new URL(capturedUrl);
    const t = new URL(track.baseUrl, location.href);
    u.searchParams.set('fmt', 'json3');
    u.searchParams.delete('tlang');
    u.searchParams.set('lang', track.languageCode);
    for (const key of ['kind', 'name']) {
      if (t.searchParams.has(key)) u.searchParams.set(key, t.searchParams.get(key));
      else u.searchParams.delete(key);
    }
    return u.href;
  }

  // Minimal variant: captured URL as-is apart from fmt/tlang. Used if the retargeted fetch fails.
  function buildFallbackUrl(capturedUrl) {
    const u = new URL(capturedUrl);
    u.searchParams.set('fmt', 'json3');
    u.searchParams.delete('tlang');
    return u.href;
  }

  // URL params for the log with token-like values redacted to their length.
  function describeUrl(url) {
    const u = new URL(url);
    const parts = [];
    for (const [k, v] of u.searchParams) {
      parts.push(/pot|token|sig|signature|key/i.test(k) ? `${k}=<${v.length} chars>` : `${k}=${v}`);
    }
    return parts.join('&');
  }

  async function fetchOnce(url) {
    const res = await origFetch(url, { credentials: 'include' });
    const body = await res.text();
    dbg(`fetch ${res.status} ${res.type}${res.redirected ? ' (redirected)' : ''}, ${body.length} bytes`);
    if (body.length === 0 || !res.ok) {
      dbg(`  url params: ${describeUrl(url)}`);
      const headers = [];
      for (const [k, v] of res.headers) headers.push(`${k}: ${v}`);
      dbg(`  response headers: ${headers.join(' | ') || '(none visible)'}`);
    }
    return { ok: res.ok, body };
  }

  async function fetchJson3(url) {
    try {
      let r = await fetchOnce(url);
      if (r.ok && r.body.length === 0) {
        dbg(`empty body; retrying once in ${EMPTY_RETRY_DELAY_MS}ms`);
        await sleep(EMPTY_RETRY_DELAY_MS);
        r = await fetchOnce(url);
      }
      if (!r.ok) return null;
      return parseJson3(r.body);
    } catch (e) { dbg('fetch threw', e); return null; }
  }

  function parseJson3(body) {
    if (!body || !body.trim()) return null;
    try {
      const data = JSON.parse(body);
      return Array.isArray(data.events) ? data : null;
    } catch { return null; }
  }

  // Diagnostics: what shape did the json3 come in? (Some tracks have one seg per event and no
  // tOffsetMs, which means no word timing downstream.)
  function describeJson3(data) {
    const evs = data.events || [];
    let withSegs = 0, multiSeg = 0, withOffset = 0, appends = 0;
    for (const ev of evs) {
      if (!ev || !Array.isArray(ev.segs)) continue;
      withSegs++;
      if (ev.aAppend) appends++;
      if (ev.segs.length > 1) multiSeg++;
      if (ev.segs.some((sg) => sg && typeof sg.tOffsetMs === 'number')) withOffset++;
    }
    dbg(`json3: ${evs.length} events, ${withSegs} with segs, ${multiSeg} multi-seg, ${withOffset} with tOffsetMs, ${appends} aAppend`);
    const sample = evs.filter((ev) => ev && Array.isArray(ev.segs) && ev.segs.some((sg) => sg && sg.utf8 && sg.utf8.trim())).slice(0, 2);
    dbg(`json3 sample: ${JSON.stringify(sample).slice(0, 400)}`);
  }

  // ---------------------------------------------------------------------------
  // Parse: json3 events -> cues -> merged lines
  // ---------------------------------------------------------------------------
  // Each cue carries its words with timing. Auto tracks have one seg per word with tOffsetMs.
  // Manual tracks usually have one multi-word seg per cue with no word timing; we then spread
  // the words across the seg's span by character position (an estimate) and mark the line
  // timed=false so the server can log/treat it as approximate.
  function eventsToCues(events) {
    const cues = [];
    for (const ev of events) {
      if (!ev || !Array.isArray(ev.segs) || typeof ev.tStartMs !== 'number') continue;
      const start = ev.tStartMs / 1000;
      const end = Math.max(start, (ev.tStartMs + (ev.dDurationMs || 0)) / 1000);
      const segs = ev.segs
        .map((seg) => ({
          text: seg && typeof seg.utf8 === 'string' ? seg.utf8 : '',
          at: (ev.tStartMs + (seg && typeof seg.tOffsetMs === 'number' ? seg.tOffsetMs : 0)) / 1000,
          timed: !!(seg && typeof seg.tOffsetMs === 'number'),
        }))
        .filter((x) => x.text.trim());
      if (segs.length === 0) continue;
      const timed = segs.some((x) => x.timed);
      const words = [];
      for (let i = 0; i < segs.length; i++) {
        const segStart = segs[i].at;
        const segEnd = i + 1 < segs.length ? Math.max(segStart, segs[i + 1].at) : Math.max(segStart, end);
        const parts = segs[i].text.split(/\s+/).filter(Boolean);
        const totalChars = parts.reduce((n, w) => n + w.length, 0) + Math.max(0, parts.length - 1);
        let pos = 0;
        for (const w of parts) {
          const frac = totalChars > 0 ? pos / totalChars : 0;
          words.push({ start: segStart + (segEnd - segStart) * frac, text: w, cueEnd: end });
          pos += w.length + 1;
        }
      }
      cues.push({ start, end, words, timed, text: words.map((w) => w.text).join(' ') });
    }
    cues.sort((a, b) => a.start - b.start);
    // Auto captions often overlap the next cue; clamp so lines don't bleed into each other.
    for (let i = 0; i < cues.length - 1; i++) {
      if (cues[i].end > cues[i + 1].start) {
        cues[i].end = Math.max(cues[i].start, cues[i + 1].start);
        for (const w of cues[i].words) w.cueEnd = cues[i].end;
      }
    }
    return cues;
  }

  function mergeCues(cues) {
    const lines = [];
    let cur = null;
    const round = (n) => Math.round(n * 100) / 100;
    const flush = () => {
      if (cur) lines.push({ start: round(cur.start), end: round(cur.end), text: cur.text, words: cur.words, timed: cur.timed });
      cur = null;
    };
    for (const cue of cues) {
      if (cur && cue.end - cur.start > MAX_LINE_SECONDS) flush();
      if (!cur) cur = { start: cue.start, end: cue.end, text: cue.text, words: cue.words.slice(), timed: cue.timed };
      else { cur.text += ' ' + cue.text; cur.end = Math.max(cur.end, cue.end); cur.words.push(...cue.words); cur.timed = cur.timed && cue.timed; }
      if (/[.!?…][)"'”’]*$/.test(cur.text)) flush();
    }
    flush();
    // Word end = next word's start, capped at the word's own cue end so a pause between cues
    // shows up as a gap between words (the server snaps boundaries to such gaps). Emit compact triples.
    for (let i = 0; i < lines.length; i++) {
      const ws = lines[i].words;
      const nextStart = i + 1 < lines.length && lines[i + 1].words.length ? lines[i + 1].words[0].start : Infinity;
      lines[i].words = ws.map((w, j) => {
        const rawEnd = j + 1 < ws.length ? ws[j + 1].start : nextStart;
        const end = Math.max(w.start, Math.min(rawEnd, w.cueEnd, lines[i].end));
        return [round(w.start), round(end), w.text];
      });
    }
    return lines;
  }

  // ---------------------------------------------------------------------------
  // Path B: obtain a token-bearing timedtext URL for this video.
  // ---------------------------------------------------------------------------
  async function clickOnCaptureClickOff(videoId, label) {
    const reqId = activeRequest;
    const btn = subtitlesButton();
    if (!btn) { dbg(`${label}: no CC button`); return null; }
    const t0 = performance.now();
    btn.click();
    dbg(`${label}: CC click on done`);
    const entry = await waitForCapture(videoId, CLICK_CAPTURE_WAIT_MS);
    if (reqId !== activeRequest) dbg(`${label}: navigated away mid-capture (request ${reqId} superseded); restoring CC anyway`);
    // Turn it back off immediately, before the response arrives, so nothing renders.
    if (subtitlesOn()) { const b = subtitlesButton(); if (b) b.click(); dbg(`${label}: CC clicked back off`); }
    if (entry) dbg(`${label}: URL captured ${Math.round(entry.at - t0)}ms after click`);
    else dbg(`${label}: no timedtext request within ${CLICK_CAPTURE_WAIT_MS}ms`);
    return entry;
  }

  // YouTube applies the saved CC preference asynchronously after load. Give it SETTLE_MS; if the
  // player fires its own timedtext request meanwhile, use that and never click.
  async function settle(videoId) {
    const t0 = performance.now();
    let state = subtitlesOn();
    let flips = 0;
    while (performance.now() - t0 < SETTLE_MS) {
      const existing = captured.get(videoId);
      if (existing) return existing;
      const now = subtitlesOn();
      if (now !== state) { flips++; state = now; }
      await sleep(100);
    }
    if (flips) dbg(`CC state changed ${flips}x while settling`);
    return null;
  }

  // Make sure CC state and track are exactly what they were before we touched the player.
  async function verifyRestore(ccWasOn, trackBefore) {
    for (const delay of [300, 1200]) {
      await sleep(delay);
      const on = subtitlesOn();
      if (on !== ccWasOn) {
        const b = subtitlesButton();
        if (b) b.click();
        dbg(`CC state was ${on ? 'on' : 'off'} after ${delay}ms, expected ${ccWasOn ? 'on' : 'off'}; corrected`);
      }
    }
    const trackAfter = getTrack();
    if (trackBefore && trackAfter && trackKey(trackAfter) !== trackKey(trackBefore)) {
      const ok = setTrack(trackBefore);
      dbg(`track was ${trackKey(trackAfter)} after, expected ${trackKey(trackBefore)}; ${ok ? 'restored' : 'could not restore'}`);
    }
    dbg(`CC restored: state=${subtitlesOn() ? 'on' : 'off'} track=${trackKey(getTrack())}`);
  }

  async function obtainTimedtextUrl(videoId, stale) {
    const existing = captured.get(videoId);
    if (existing) { dbg('URL already captured from the player\'s own request'); return existing; }

    const ready = await waitForPlayerReady();
    if (stale()) return null;
    dbg(`player ready: ${ready} (readyState=${videoEl() && videoEl().readyState}, CC button enabled=${subtitlesButtonEnabled()})`);

    const settled = await settle(videoId);
    if (stale()) return null;
    if (settled) { dbg('URL captured from the player\'s own request while settling; not clicking'); return settled; }

    const ccWasOn = subtitlesOn();
    const trackBefore = getTrack();
    dbg(`CC state before: ${ccWasOn ? 'on' : 'off'}, track=${trackKey(trackBefore)}`);

    if (ccWasOn) {
      const entry = await waitForCapture(videoId, CC_ON_CAPTURE_WAIT_MS);
      dbg(entry ? 'URL captured from the player (CC was on)' : `no timedtext request within ${CC_ON_CAPTURE_WAIT_MS}ms with CC on`);
      return entry;
    }

    let entry = null;
    try {
      entry = await clickOnCaptureClickOff(videoId, 'click 1');
      if (stale()) return null;
      if (!entry) entry = await clickOnCaptureClickOff(videoId, 'click 2');
      if (stale()) return null;

      if (!entry) {
        const p = player();
        if (p && typeof p.loadModule === 'function') {
          dbg('fallback: loadModule(captions)');
          try { p.loadModule('captions'); } catch (e) { dbg('loadModule threw', e); }
          entry = await waitForCapture(videoId, CLICK_CAPTURE_WAIT_MS);
          try { p.unloadModule('captions'); dbg('unloadModule(captions) done'); } catch (e) { dbg('unloadModule threw', e); }
          dbg(entry ? 'URL captured via loadModule' : 'loadModule produced no timedtext request');
        } else {
          dbg('fallback unavailable: player.loadModule missing');
        }
      }
    } finally {
      verifyRestore(ccWasOn, trackBefore); // runs in the background; don't delay the fetch
    }
    return entry;
  }

  // ---------------------------------------------------------------------------
  // Main request handler
  // ---------------------------------------------------------------------------
  let activeRequest = 0;

  async function getCaptions(videoId) {
    const reqId = ++activeRequest;
    const stale = () => reqId !== activeRequest;
    const reply = (msg) => { if (!stale()) window.postMessage({ source: 'ss-page', type: 'captions', videoId, ...msg }, '*'); };

    const pr = await waitForPlayerResponse(videoId);
    if (stale()) return;
    if (!pr) return reply({ error: 'player not ready' });

    const tracks = pr.captions && pr.captions.playerCaptionsTracklistRenderer &&
                   pr.captions.playerCaptionsTracklistRenderer.captionTracks;
    if (!Array.isArray(tracks) || tracks.length === 0) return reply({ error: 'no captions' });

    const track = pickTrack(tracks);
    const lang = track.languageCode || 'und';
    const kind = track.kind === 'asr' ? 'asr' : 'manual';
    dbg(`${tracks.length} track(s); picked lang=${lang} kind=${kind}`);

    // Path A: direct fetch of the track with fmt=json3. Cheap; works if YouTube stops requiring the token.
    let path = 'A';
    let sourceUrl = null; // the URL that actually produced caption data (used again for the translation)
    const aUrl = new URL(track.baseUrl, location.href); aUrl.searchParams.set('fmt', 'json3');
    dbg('path A: fetching track baseUrl');
    let data = await fetchJson3(aUrl.href);
    if (data) sourceUrl = aUrl.href;
    if (stale()) return;

    // Path B: capture the player's token-bearing URL, fetch it ourselves retargeted at our track.
    if (!data) {
      path = 'B';
      dbg('path A empty; starting path B');
      const entry = await obtainTimedtextUrl(videoId, stale);
      if (stale()) return;
      if (entry) {
        const cu = new URL(entry.url);
        if (cu.searchParams.has('tlang')) {
          dbg(`path B: player's request was for a TRANSLATED track (lang=${cu.searchParams.get('lang')} tlang=${cu.searchParams.get('tlang')})`);
        }
        dbg(`path B: captured params: ${describeUrl(entry.url)}`);
        dbg('path B: fetching captured URL retargeted at picked track');
        const retargeted = buildFetchUrl(entry.url, track);
        data = await fetchJson3(retargeted);
        if (data) sourceUrl = retargeted;
        if (stale()) return;
        if (!data) {
          dbg('path B: retargeted fetch empty; fetching captured URL as-is');
          const asis = buildFallbackUrl(entry.url);
          data = await fetchJson3(asis);
          if (data) sourceUrl = asis;
          if (stale()) return;
        }
      }
    }

    if (!data) return reply({ error: 'caption fetch failed' });

    describeJson3(data);
    const lines = mergeCues(eventsToCues(data.events));
    const timed = lines.filter((l) => l.timed).length;
    dbg(`path ${path}: ${lines.length} lines, ${timed} with word timing`);
    if (lines.length === 0) return reply({ error: 'no captions' });
    const vd = pr.videoDetails || {};
    const title = typeof vd.title === 'string' ? vd.title : '';
    const creator = typeof vd.author === 'string' ? vd.author : '';
    const channelId = typeof vd.channelId === 'string' ? vd.channelId : '';
    const description = typeof vd.shortDescription === 'string' ? vd.shortDescription.slice(0, 5000) : '';

    // Non-English video: also fetch the English auto-translation of the same track. The server analyzes
    // the English text and cuts on the original track's word timing. English videos: single track.
    let analysisLines = null;
    if (!/^en\b/i.test(lang) && sourceUrl) {
      const tr = await fetchJson3(withTranslation(sourceUrl, 'en'));
      if (stale()) return;
      if (tr) {
        analysisLines = mergeCues(eventsToCues(tr.events));
        dbg(`analysis track: en translation, ${analysisLines.length} lines`);
      } else {
        dbg('analysis track: en translation unavailable; analyzing the original language');
      }
    }
    // YouTube's "Includes paid promotion" disclosure lives in the player response when the creator set it.
    const paidPromotion = !!(pr.paidContentOverlay && pr.paidContentOverlay.paidContentOverlayRenderer);
    const heat = readHeatmap();
    if (heat.length) dbg(`heatmap: ${heat.length} samples from the player's most-replayed graph`);
    dbg(`metadata (lengths): title=${title.length} creator=${creator.length} channelId=${channelId.length} ` +
        `description=${description.length} heat=${heat.length} paidPromotion=${paidPromotion} kind=${kind} ` +
        `videoDetails=${pr.videoDetails ? 'present' : 'MISSING'} build=${BUILD}`);
    reply({ build: BUILD, lang, kind, path, lines, title, creator, channelId, description, heat, paidPromotion,
            analysisLines, analysisLang: analysisLines ? 'en' : lang });
  }

  // "Most replayed" graph: the player draws it as an SVG path (x 0..1000, y 0..100, y grows downward).
  // Sample it into [[percent, intensity 0..100], ...] with 100 columns.
  function readHeatmap() {
    try {
      const path = document.querySelector('#movie_player .ytp-heat-map-path');
      const d = path && path.getAttribute('d');
      if (!d) return [];
      const nums = d.match(/-?\d+(?:\.\d+)?/g);
      if (!nums || nums.length < 6) return [];
      const pts = [];
      for (let i = 0; i + 1 < nums.length; i += 2) pts.push([parseFloat(nums[i]), parseFloat(nums[i + 1])]);
      const cols = new Array(100).fill(null);
      for (const [x, y] of pts) {
        if (!isFinite(x) || !isFinite(y)) continue;
        const c = Math.max(0, Math.min(99, Math.floor(x / 10)));
        const v = Math.max(0, Math.min(100, 100 - y));
        cols[c] = cols[c] === null ? v : Math.max(cols[c], v);
      }
      let last = 0;
      return cols.map((v, i) => { if (v !== null) last = v; return [i, Math.round(last * 10) / 10]; });
    } catch { return []; }
  }

  function withTranslation(url, tlang) {
    const u = new URL(url);
    u.searchParams.set('tlang', tlang);
    u.searchParams.set('fmt', 'json3');
    return u.href;
  }

  window.addEventListener('message', (ev) => {
    if (ev.source !== window) return;
    const d = ev.data;
    if (!d || d.source !== 'ss-content' || d.type !== 'getCaptions' || typeof d.videoId !== 'string') return;
    getCaptions(d.videoId).catch((e) => {
      console.warn(TAG, 'getCaptions failed', e);
      window.postMessage({ source: 'ss-page', type: 'captions', videoId: d.videoId, error: 'caption fetch failed' }, '*');
    });
  });
})();
