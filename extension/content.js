// Sponsor Skipper — content.js (isolated world)
// Stage 1: detect the current video and react to YouTube's in-app navigation.
// Stage 2: ask page.js (MAIN world) for the caption lines and log what came back.
// Stage 3: send the lines to the local server via background.js and poll for segments.

(() => {
  const TAG = '[SponsorSkipper]';
  const log = (...args) => console.log(TAG, ...args);

  // Run once per page. If Chrome injects this script a second time (e.g. after an
  // extension reload with the tab still open), the second copy must not add its own
  // listeners or it would announce every video twice.
  if (window.__sponsorSkipperLoaded) return;
  window.__sponsorSkipperLoaded = true;

  // Everything tied to the current video lives here so we can tear it down cleanly.
  let current = null; // { videoId, cleanups: Function[] }

  function getVideoIdFromUrl() {
    const url = new URL(location.href);
    if (url.pathname !== '/watch') return null; // ignores Shorts, home, channel pages, etc.
    const id = url.searchParams.get('v');
    return id && /^[A-Za-z0-9_-]{11}$/.test(id) ? id : null;
  }

  function teardown() {
    if (!current) return;
    for (const fn of current.cleanups.splice(0)) {
      try { fn(); } catch (e) { console.warn(TAG, 'cleanup failed', e); }
    }
    current = null;
  }

  function setup(videoId) {
    current = { videoId, cleanups: [] };
    log('video', videoId);
    requestCaptions(videoId);
  }

  // Stage 2: captions. page.js runs in the MAIN world and can reach the player;
  // we talk to it over window.postMessage and only accept replies for our video.
  function requestCaptions(videoId) {
    const onMessage = (ev) => {
      if (ev.source !== window) return;
      const d = ev.data;
      if (!d || d.source !== 'ss-page' || d.type !== 'captions' || d.videoId !== videoId) return;
      window.removeEventListener('message', onMessage);
      if (d.error) { log('captions:', String(d.error).slice(0, 80)); return; }
      // Any page script can post a message shaped like this one, so validate before trusting it.
      if (!validLines(d.lines) || typeof d.lang !== 'string' || typeof d.kind !== 'string' || typeof d.path !== 'string') {
        log('captions: ignored malformed reply');
        return;
      }
      const timed = d.lines.filter((l) => l.timed).length;
      log(`captions: ${d.lines.length} lines (${timed} with word timing), lang=${d.lang}, kind=${d.kind}, path=${d.path}`);
      for (const l of d.lines.slice(0, 5)) log(`  [${fmtTime(l.start)}-${fmtTime(l.end)}] ${l.text}`);
      current.lines = d.lines;
      current.lang = d.lang;
      analyze(videoId, d.lang, d.lines);
    };
    window.addEventListener('message', onMessage);
    current.cleanups.push(() => window.removeEventListener('message', onMessage));
    window.postMessage({ source: 'ss-content', type: 'getCaptions', videoId }, '*');
  }

  // Stage 3: server relay. POST once, then poll every 2s until done/error.
  const POLL_MS = 2000;

  function send(msg) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (res) => {
          if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
          else resolve(res || { ok: false, error: 'no response' });
        });
      } catch (e) { resolve({ ok: false, error: String(e) }); }
    });
  }

  async function analyze(videoId, lang, lines) {
    const state = current;
    const alive = () => current === state && state.videoId === videoId;
    const res = await send({ type: 'analyze', videoId, lang, lines });
    if (!alive()) return;
    if (!res.ok) { log('server:', res.error); return; }
    handleJob(videoId, res);
    if (res.status === 'done' || res.status === 'error') return;

    let timer = null;
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    state.cleanups.push(stop);
    timer = setInterval(async () => {
      const r = await send({ type: 'poll', videoId });
      if (!alive()) { stop(); return; }
      if (!r.ok) { log('server:', r.error); stop(); return; }
      handleJob(videoId, r);
      if (r.status === 'done' || r.status === 'error') stop();
    }, POLL_MS);
  }

  function handleJob(videoId, job) {
    const state = current;
    const prev = state.lastStatus;
    state.lastStatus = job.status;
    state.segments = job.segments || [];
    if (job.status !== prev) log(`analysis ${job.status} (${state.segments.length} segment(s))`);
    if (job.status === 'done') {
      for (const s of state.segments) {
        log(`  ${s.action.toUpperCase()} [${fmtTime(s.start)}-${fmtTime(s.end)}] ${s.category}/${s.style} ${s.brand} conf=${s.confidence} — ${s.reason}`);
      }
    }
  }

  const MAX_LINES = 20000;
  function validLines(lines) {
    if (!Array.isArray(lines) || lines.length === 0 || lines.length > MAX_LINES) return false;
    return lines.every((l) => l && typeof l === 'object' &&
      Number.isFinite(l.start) && Number.isFinite(l.end) && l.start >= 0 && l.end >= l.start &&
      typeof l.text === 'string' && l.text.length > 0 && l.text.length <= 2000 &&
      (l.words === undefined || validWords(l.words)) && (l.timed === undefined || typeof l.timed === 'boolean'));
  }
  function validWords(words) {
    return Array.isArray(words) && words.length <= 400 && words.every((w) =>
      Array.isArray(w) && w.length === 3 && Number.isFinite(w[0]) && Number.isFinite(w[1]) &&
      w[1] >= w[0] && typeof w[2] === 'string' && w[2].length <= 200);
  }

  function fmtTime(s) {
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60);
    return `${m}:${String(sec).padStart(2, '0')}`;
  }

  function onNavigate() {
    const videoId = getVideoIdFromUrl();
    // Same video as the one already announced: ignore. This is what dedupes the
    // initial run against the first yt-navigate-finish, and repeat events after that.
    if (current && current.videoId === videoId) return;
    teardown();
    if (videoId) setup(videoId);
  }

  document.addEventListener('yt-navigate-finish', onNavigate);
  window.addEventListener('popstate', onNavigate);
  onNavigate(); // initial page load
})();
