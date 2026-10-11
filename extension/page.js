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

  const TAG = '[SponsorSkipper:page]';
  const dbg = (...a) => console.log(TAG, ...a);
  const MAX_LINE_SECONDS = 8;
  const PLAYER_WAIT_MS = 15000;
  const CLICK_CAPTURE_WAIT_MS = 3000;
  const CC_ON_CAPTURE_WAIT_MS = 8000;

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

  async function fetchJson3(url) {
    try {
      const res = await origFetch(url, { credentials: 'include' });
      const body = await res.text();
      dbg(`fetch ${res.status}, ${body.length} bytes`);
      if (!res.ok) return null;
      return parseJson3(body);
    } catch (e) { dbg('fetch threw', e); return null; }
  }

  function parseJson3(body) {
    if (!body || !body.trim()) return null;
    try {
      const data = JSON.parse(body);
      return Array.isArray(data.events) ? data : null;
    } catch { return null; }
  }

  // ---------------------------------------------------------------------------
  // Parse: json3 events -> cues -> merged lines
  // ---------------------------------------------------------------------------
  function eventsToCues(events) {
    const cues = [];
    for (const ev of events) {
      if (!ev || !Array.isArray(ev.segs) || typeof ev.tStartMs !== 'number') continue;
      const text = ev.segs.map((s) => (s && s.utf8) || '').join('').replace(/\s+/g, ' ').trim();
      if (!text) continue;
      const start = ev.tStartMs / 1000;
      const end = (ev.tStartMs + (ev.dDurationMs || 0)) / 1000;
      cues.push({ start, end: Math.max(end, start), text });
    }
    cues.sort((a, b) => a.start - b.start);
    // Auto captions often overlap the next cue; clamp so lines don't bleed into each other.
    for (let i = 0; i < cues.length - 1; i++) {
      if (cues[i].end > cues[i + 1].start) cues[i].end = cues[i + 1].start;
    }
    return cues;
  }

  function mergeCues(cues) {
    const lines = [];
    let cur = null;
    const round = (n) => Math.round(n * 100) / 100;
    const flush = () => {
      if (cur) lines.push({ start: round(cur.start), end: round(cur.end), text: cur.text });
      cur = null;
    };
    for (const cue of cues) {
      if (cur && cue.end - cur.start > MAX_LINE_SECONDS) flush();
      if (!cur) cur = { start: cue.start, end: cue.end, text: cue.text };
      else { cur.text += ' ' + cue.text; cur.end = Math.max(cur.end, cue.end); }
      if (/[.!?…][)"'”’]*$/.test(cur.text)) flush();
    }
    flush();
    return lines;
  }

  // ---------------------------------------------------------------------------
  // Path B: obtain a token-bearing timedtext URL for this video.
  // ---------------------------------------------------------------------------
  async function clickOnCaptureClickOff(videoId, label) {
    const btn = subtitlesButton();
    if (!btn) { dbg(`${label}: no CC button`); return null; }
    const t0 = performance.now();
    btn.click();
    dbg(`${label}: CC click on done`);
    const entry = await waitForCapture(videoId, CLICK_CAPTURE_WAIT_MS);
    // Turn it back off immediately, before the response arrives, so nothing renders.
    if (subtitlesOn()) { const b = subtitlesButton(); if (b) b.click(); dbg(`${label}: CC restored off`); }
    if (entry) dbg(`${label}: URL captured ${Math.round(entry.at - t0)}ms after click`);
    else dbg(`${label}: no timedtext request within ${CLICK_CAPTURE_WAIT_MS}ms`);
    return entry;
  }

  async function obtainTimedtextUrl(videoId, stale) {
    // The player may already have requested captions for this video (CC on, or user toggled).
    const existing = captured.get(videoId);
    if (existing) { dbg('URL already captured from the player\'s own request'); return existing; }

    const ready = await waitForPlayerReady();
    if (stale()) return null;
    dbg(`player ready: ${ready} (readyState=${videoEl() && videoEl().readyState}, CC button enabled=${subtitlesButtonEnabled()})`);

    const ccWasOn = subtitlesOn();
    dbg(`CC state before: ${ccWasOn ? 'on' : 'off'}`);

    if (ccWasOn) {
      // Don't touch anything; the player will request captions on its own.
      const entry = await waitForCapture(videoId, CC_ON_CAPTURE_WAIT_MS);
      dbg(entry ? 'URL captured from the player (CC was on)' : `no timedtext request within ${CC_ON_CAPTURE_WAIT_MS}ms with CC on`);
      return entry;
    }

    let entry = await clickOnCaptureClickOff(videoId, 'click 1');
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
        if (subtitlesOn()) { const b = subtitlesButton(); if (b) b.click(); dbg('CC restored off after module fallback'); }
        dbg(entry ? 'URL captured via loadModule' : 'loadModule produced no timedtext request');
      } else {
        dbg('fallback unavailable: player.loadModule missing');
      }
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
    const aUrl = new URL(track.baseUrl, location.href); aUrl.searchParams.set('fmt', 'json3');
    dbg('path A: fetching track baseUrl');
    let data = await fetchJson3(aUrl.href);
    if (stale()) return;

    // Path B: capture the player's token-bearing URL, fetch it ourselves retargeted at our track.
    if (!data) {
      path = 'B';
      dbg('path A empty; starting path B');
      const entry = await obtainTimedtextUrl(videoId, stale);
      if (stale()) return;
      if (entry) {
        dbg('path B: fetching captured URL retargeted at picked track');
        data = await fetchJson3(buildFetchUrl(entry.url, track));
        if (stale()) return;
        if (!data) {
          dbg('path B: retargeted fetch empty; fetching captured URL as-is');
          data = await fetchJson3(buildFallbackUrl(entry.url));
          if (stale()) return;
        }
      }
    }

    if (!data) return reply({ error: 'caption fetch failed' });

    const lines = mergeCues(eventsToCues(data.events));
    dbg(`path ${path}: ${lines.length} lines`);
    if (lines.length === 0) return reply({ error: 'no captions' });
    reply({ lang, kind, path, lines });
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
