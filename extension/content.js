// Sponsor Skipper — content.js (isolated world)
// Stage 1: detect the current video and react to YouTube's in-app navigation.
// Stage 2: ask page.js (MAIN world) for the caption lines and log what came back.

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
      if (d.error) { log('captions:', d.error); return; }
      log(`captions: ${d.lines.length} lines, lang=${d.lang}, kind=${d.kind}, path=${d.path}`);
      for (const l of d.lines.slice(0, 5)) log(`  [${fmtTime(l.start)}-${fmtTime(l.end)}] ${l.text}`);
      current.lines = d.lines;
      current.lang = d.lang;
    };
    window.addEventListener('message', onMessage);
    current.cleanups.push(() => window.removeEventListener('message', onMessage));
    window.postMessage({ source: 'ss-content', type: 'getCaptions', videoId }, '*');
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
