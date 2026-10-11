// Sponsor Skipper — content.js (isolated world)
// Stage 1: detect the current video and react to YouTube's in-app navigation.

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
    // Later stages register their timers, listeners and DOM nodes via current.cleanups.
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
