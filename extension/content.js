// Sponsor Skipper — content.js (isolated world)
// Stage 1: detect the current video and react to YouTube's in-app navigation.
// Stage 2: ask page.js (MAIN world) for the caption lines and log what came back.
// Stage 3: send the lines to the local server via background.js and poll for segments.
// Stage 6: progress-bar markers, live skipping with Undo toast, status badge.

(() => {
  const TAG = '[SponsorSkipper]';
  // Dev mode gates ordinary logs; errors always print.
  const log = (...args) => { if (settings.devMode) console.log(TAG, ...args); };
  const logErr = (...args) => console.warn(TAG, ...args);

  // ---------------------------------------------------------------------------
  // Settings (chrome.storage.local, edited by the popup; applied here instantly)
  // ---------------------------------------------------------------------------
  const DEFAULT_SETTINGS = {
    enabled: true,
    actions: { sponsor_read: 'skip', selfpromo: 'show', interaction: 'show' },
    devMode: false,
    allowlist: {}, // channelId -> channel name; markers show, nothing skips
  };
  let settings = { ...DEFAULT_SETTINGS };

  function mergeSettings(stored) {
    const s = { ...DEFAULT_SETTINGS, ...(stored || {}) };
    s.actions = { ...DEFAULT_SETTINGS.actions, ...((stored && stored.actions) || {}) };
    s.allowlist = (stored && stored.allowlist) || {};
    return s;
  }

  // What this segment does for this user right now: 'skip' | 'keep' | 'show' | 'off'.
  function effectiveAction(seg) {
    if (!settings.enabled) return 'off';
    let a;
    if (seg.category === 'sponsor') {
      a = seg.style === 'integration' ? 'keep' : (seg.action === 'skip' ? settings.actions.sponsor_read : seg.action);
    } else {
      a = settings.actions[seg.category] || 'show';
    }
    if (a === 'skip' && current && current.channelId && settings.allowlist[current.channelId]) a = 'show';
    return a;
  }

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
    current = { videoId, cleanups: [], segments: [], handled: new Set() };
    log('video', videoId);
    if (!settings.enabled) { log('disabled in popup; not analyzing'); return; }
    setBadge('reading captions', 'busy');
    attachPlayback();
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
      if (d.error) {
        log('captions:', String(d.error).slice(0, 80));
        current.captionsNote = d.error === 'no captions' ? 'no captions' : 'captions unavailable';
        setBadge(current.captionsNote, 'off');
        return;
      }
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
      current.title = typeof d.title === 'string' ? d.title.slice(0, 300) : '';
      current.creator = typeof d.creator === 'string' ? d.creator.slice(0, 100) : '';
      current.channelId = typeof d.channelId === 'string' ? d.channelId.slice(0, 40) : '';
      current.kind = d.kind;
      current.description = typeof d.description === 'string' ? d.description.slice(0, 5000) : '';
      current.analysisLines = validLines(d.analysisLines) ? d.analysisLines : null;
      current.analysisLang = typeof d.analysisLang === 'string' ? d.analysisLang.slice(0, 12) : d.lang;
      if (current.analysisLines) log(`analysis track: ${current.analysisLang}, ${current.analysisLines.length} lines`);
      analyze(videoId, d.lang, d.lines, current.title);
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

  async function analyze(videoId, lang, lines, title) {
    const state = current;
    const alive = () => current === state && state.videoId === videoId;
    setBadge('analyzing', 'busy');
    const v = videoEl();
    const position = v && isFinite(v.currentTime) ? v.currentTime : 0;
    const res = await send({ type: 'analyze', videoId, lang, lines, title, kind: state.kind, creator: state.creator,
                             description: state.description, position,
                             analysisLines: state.analysisLines, analysisLang: state.analysisLang });
    if (!alive()) return;
    if (!res.ok) { logErr('server:', res.error); setBadge(res.error === 'server offline' ? 'server offline' : `server: ${res.error}`, 'error'); return; }
    handleJob(videoId, res);
    if (res.status === 'done' || res.status === 'error') return;

    let timer = null;
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    state.cleanups.push(stop);
    timer = setInterval(async () => {
      const r = await send({ type: 'poll', videoId });
      if (!alive()) { stop(); return; }
      if (!r.ok) { logErr('server:', r.error); setBadge(r.error === 'server offline' ? 'server offline' : `server: ${r.error}`, 'error'); stop(); return; }
      handleJob(videoId, r);
      if (r.status === 'done' || r.status === 'error') stop();
    }, POLL_MS);
  }

  function handleJob(videoId, job) {
    const state = current;
    const prev = state.lastStatus;
    state.lastStatus = job.status;
    state.segments = job.segments || [];
    if (job.status !== prev) log(`analysis ${job.status} (${state.segments.length} segment(s))${job.cached ? ' [cached]' : ''}`);
    const skips = state.segments.filter((s) => s.action === 'skip').length;
    if (job.status === 'done') {
      for (const s of state.segments) {
        log(`  ${s.action.toUpperCase()} [${fmtTime(s.start)}-${fmtTime(s.end)}] ${s.category}/${s.style} ${s.brand} conf=${s.confidence} — ${s.reason}`);
      }
      setBadge(`ready · ${skips} to skip`, 'ready');
      dimBadgeSoon();
    } else if (job.status === 'error') {
      setBadge(`analysis error${job.error ? `: ${job.error}` : ''}`, 'error');
    } else {
      const prog = job.progress ? ` ${job.progress}` : '';
      setBadge(`${job.paused ? 'paused' : 'analyzing'}${prog}${skips ? ` · ${skips} to skip so far` : ''}`, 'busy');
    }
    drawMarkers();
  }

  // ---------------------------------------------------------------------------
  // Stage 6: playback UI
  // ---------------------------------------------------------------------------
  const SKIP_GUARD_S = 0.5;   // don't trigger inside the last 0.5s of a segment
  const TOAST_MS = 6000;

  const playerEl = () => document.getElementById('movie_player');
  const videoEl = () => document.querySelector('#movie_player video.html5-main-video');
  const adShowing = () => { const p = playerEl(); return !!p && p.classList.contains('ad-showing'); };
  const segKey = (s) => `${s.start}-${s.end}`;

  // --- status badge
  function setBadge(text, kind) {
    const state = current;
    if (!state) return;
    state.badgeText = text; state.badgeKind = kind; // remembered so the tick can (re)place it
    if (!settings.devMode) return;
    const p = playerEl();
    if (!p) return;
    if (!state.badge || !state.badge.isConnected) {
      if (state.badge) state.badge.remove();
      state.badge = document.createElement('div');
      state.badge.className = 'ss-badge';
      p.appendChild(state.badge);
      state.cleanups.push(() => { if (state.badge) state.badge.remove(); state.badge = null; });
    }
    state.badge.textContent = text;
    state.badge.dataset.kind = kind;
    state.badge.classList.remove('ss-dim');
  }
  function dimBadgeSoon() {
    const state = current;
    const t = setTimeout(() => { if (state.badge) state.badge.classList.add('ss-dim'); }, 4000);
    state.cleanups.push(() => clearTimeout(t));
  }

  // --- markers
  function drawMarkers() {
    const state = current;
    if (!state) return;
    const bar = document.querySelector('#movie_player .ytp-progress-bar');
    const video = videoEl();
    if (!bar || !video || !isFinite(video.duration) || video.duration <= 0) return;
    if (state.markers) state.markers.remove();
    bar.querySelectorAll('.ss-markers').forEach((e) => e.remove());
    const box = document.createElement('div');
    box.className = 'ss-markers';
    const dur = video.duration;
    for (const s of state.segments) {
      const eff = effectiveAction(s);
      if (eff === 'off') continue;
      const el = document.createElement('div');
      el.className = 'ss-marker';
      el.dataset.action = eff;
      el.style.left = `${(Math.max(0, s.start) / dur) * 100}%`;
      el.style.width = `${(Math.max(0, Math.min(dur, s.end) - Math.max(0, s.start)) / dur) * 100}%`;
      el.title = [
        `${eff.toUpperCase()}${eff !== s.action ? ` (server: ${s.action})` : ''} ${fmtTime(s.start)}–${fmtTime(s.end)}`,
        `${s.category}/${s.style}${s.brand ? ` · ${s.brand}` : ''} · conf ${Number(s.confidence).toFixed(2)}`,
        s.evidence ? `evidence: ${s.evidence}` : '',
        s.reason || '',
      ].filter(Boolean).join('\n');
      el.addEventListener('click', (ev) => {
        ev.stopPropagation(); ev.preventDefault();
        const r = bar.getBoundingClientRect();
        const frac = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
        const v = videoEl();
        if (v) v.currentTime = frac * v.duration;
      });
      box.appendChild(el);
    }
    bar.appendChild(box);
    state.markers = box;
  }

  // --- stats (today + all-time), read by the popup
  function recordSkip(savedSeconds) {
    chrome.storage.local.get('stats', (res) => {
      const today = new Date().toISOString().slice(0, 10);
      const st = res.stats || {};
      const t = st.today && st.today.date === today ? st.today : { date: today, skips: 0, saved: 0 };
      const a = st.all || { skips: 0, saved: 0 };
      const d = savedSeconds >= 0 ? 1 : -1;
      t.skips = Math.max(0, t.skips + d); t.saved = Math.max(0, t.saved + savedSeconds);
      a.skips = Math.max(0, a.skips + d); a.saved = Math.max(0, a.saved + savedSeconds);
      chrome.storage.local.set({ stats: { today: t, all: a } });
    });
  }

  // --- toast
  function showToast(text, onUndo) {
    const state = current;
    const p = playerEl();
    if (!state || !p) return;
    hideToast();
    const el = document.createElement('div');
    el.className = 'ss-toast';
    const span = document.createElement('span');
    span.textContent = text;
    const btn = document.createElement('button');
    btn.textContent = 'Undo';
    btn.addEventListener('click', (ev) => { ev.stopPropagation(); onUndo(); hideToast(); });
    el.append(span, btn);
    p.appendChild(el);
    state.toast = el;
    state.toastTimer = setTimeout(hideToast, TOAST_MS);
  }
  function hideToast() {
    const state = current;
    if (!state) return;
    if (state.toastTimer) { clearTimeout(state.toastTimer); state.toastTimer = null; }
    if (state.toast) { state.toast.remove(); state.toast = null; }
  }

  // --- skipping
  function attachPlayback() {
    const state = current;
    let video = null;
    let ourSeek = false;

    const onTimeUpdate = () => {
      if (!video || adShowing() || video.seeking) return;
      const t = video.currentTime;
      for (const s of state.segments) {
        if (effectiveAction(s) !== 'skip') continue;
        const key = segKey(s);
        if (state.handled.has(key)) continue;
        if (t >= s.start && t < s.end - SKIP_GUARD_S) {
          state.handled.add(key);
          const from = t;
          const saved = s.end - from;
          ourSeek = true;
          video.currentTime = s.end;
          log(`skipped ${s.brand || 'sponsor'} ${s.category === 'sponsor' ? 'read' : s.category} ${fmtTime(s.start)}-${fmtTime(s.end)} (from ${fmtTime(from)})`);
          recordSkip(saved);
          const what = s.category === 'sponsor' ? `${s.brand ? `${s.brand} ` : ''}read` : (s.category === 'selfpromo' ? 'self-promo' : 'like & subscribe');
          showToast(`Skipped ${what} (${fmtTime(saved)})`, () => {
            const v = videoEl();
            if (!v) return;
            ourSeek = true;
            v.currentTime = from;
            recordSkip(-saved);
            log(`undo: back to ${fmtTime(from)}; segment stays handled`);
          });
          return;
        }
      }
    };

    const onSeeking = () => {
      if (!video) return;
      if (ourSeek) { ourSeek = false; return; }
      // The user seeked. If they landed inside a skip segment, they want to watch it.
      const t = video.currentTime;
      for (const s of state.segments) {
        if (effectiveAction(s) === 'skip' && t >= s.start && t < s.end && !state.handled.has(segKey(s))) {
          state.handled.add(segKey(s));
          log(`manual seek into ${fmtTime(s.start)}-${fmtTime(s.end)}; letting it play`);
        }
      }
    };

    const onLoadedMetadata = () => drawMarkers();

    const bind = (v) => {
      if (video === v) return;
      unbind();
      video = v;
      if (!video) return;
      video.addEventListener('timeupdate', onTimeUpdate);
      video.addEventListener('seeking', onSeeking);
      video.addEventListener('loadedmetadata', onLoadedMetadata);
    };
    const unbind = () => {
      if (!video) return;
      video.removeEventListener('timeupdate', onTimeUpdate);
      video.removeEventListener('seeking', onSeeking);
      video.removeEventListener('loadedmetadata', onLoadedMetadata);
      video = null;
    };

    // YouTube can swap the <video> and rebuild the progress bar; re-bind and redraw as needed.
    const tick = setInterval(() => {
      const v = videoEl();
      if (v !== video) bind(v);
      if (state.segments.length && (!state.markers || !state.markers.isConnected)) drawMarkers();
      if (settings.devMode && (!state.badge || !state.badge.isConnected) && state.badgeText) setBadge(state.badgeText, state.badgeKind);
    }, 1000);

    bind(videoEl());
    state.cleanups.push(() => {
      clearInterval(tick);
      unbind();
      hideToast();
      if (state.markers) { state.markers.remove(); state.markers = null; }
    });
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

  function applySettingsChange(prev) {
    if (!current) return;
    if (prev.enabled !== settings.enabled) {
      // Turning off tears everything down; turning on re-runs the current video.
      const id = current.videoId;
      teardown();
      if (settings.enabled && id) setup(id);
      return;
    }
    if (!settings.devMode && current.badge) { current.badge.remove(); current.badge = null; }
    if (settings.devMode && current.badgeText) setBadge(current.badgeText, current.badgeKind);
    drawMarkers();
  }

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local' || !changes.settings) return;
    const prev = settings;
    settings = mergeSettings(changes.settings.newValue);
    applySettingsChange(prev);
  });

  // Popup asks what is going on in this tab.
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== 'getState') return false;
    const v = videoEl();
    sendResponse(current ? {
      videoId: current.videoId, channelId: current.channelId || '', channelName: current.creator || '',
      title: current.title || '', position: v ? v.currentTime : 0, status: current.lastStatus || '',
      captions: current.captionsNote || '',
      segments: (current.segments || []).map((s) => ({ ...s, effective: effectiveAction(s) })),
    } : {});
    return false;
  });

  chrome.storage.local.get('settings', (stored) => {
    settings = mergeSettings(stored && stored.settings);
    document.addEventListener('yt-navigate-finish', onNavigate);
    window.addEventListener('popstate', onNavigate);
    onNavigate(); // initial page load
  });
})();
