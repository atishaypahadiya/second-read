// Second Read — content.js (isolated world)
// Stage 1: detect the current video and react to YouTube's in-app navigation.
// Stage 2: ask page.js (MAIN world) for the caption lines and log what came back.
// Stage 3: send the lines to the local server via background.js and poll for segments.
// Stage 6: marker lane, live skipping with Undo toast, status badge. All visuals come from ui/components.css
// (DESIGN.md); this file only builds the markup those classes expect.

(() => {
  const BUILD = 17; // keep equal to page.js, background.js and server.py CLIENT_BUILD
  const TAG = '[SecondRead]';
  // Dev mode gates ordinary logs; errors always print.
  const log = (...args) => { if (settings.devMode) console.log(TAG, ...args); };
  const logErr = (...args) => console.warn(TAG, ...args);

  // ---------------------------------------------------------------------------
  // Settings (chrome.storage.local, edited by the popup; applied here instantly)
  // ---------------------------------------------------------------------------
  // Categories the server emits. Only sponsor reads skip by default; everything else is label-only. The user
  // sets each to skip / label / off in the popup. Charity never auto-skips unless set explicitly.
  const CATEGORIES = ['affiliate', 'selfpromo', 'membership', 'crosspromo', 'charity', 'giveaway', 'interaction', 'intro', 'outro', 'recap'];
  const DEFAULT_SETTINGS = {
    enabled: true,
    actions: { sponsor_read: 'skip', affiliate: 'label', selfpromo: 'label', membership: 'label', crosspromo: 'label',
               charity: 'label', giveaway: 'label', interaction: 'label', intro: 'label', outro: 'label', recap: 'label' },
    devMode: false,
    allowlist: {}, // channelId -> channel name; markers show, nothing skips
  };
  let settings = { ...DEFAULT_SETTINGS };

  function mergeSettings(stored) {
    const s = { ...DEFAULT_SETTINGS, ...(stored || {}) };
    s.actions = { ...DEFAULT_SETTINGS.actions, ...((stored && stored.actions) || {}) };
    for (const k of Object.keys(s.actions)) if (s.actions[k] === 'show') s.actions[k] = 'label'; // old value name
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
      a = settings.actions[seg.category] || 'label';
    }
    if (a === 'label') a = 'show';
    if (a === 'skip' && current && current.channelId && settings.allowlist[current.channelId]) a = 'show';
    return a;
  }

  // Run once per page. If Chrome injects this script a second time (e.g. after an
  // extension reload with the tab still open), the second copy must not add its own
  // listeners or it would announce every video twice.
  if (window.__secondReadLoaded) return;
  window.__secondReadLoaded = true;

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
    current = { videoId, cleanups: [], segments: [], handled: new Set(), outcome: new Map(), markerEls: new Map() };
    log('video', videoId);
    if (!settings.enabled) { log('disabled in popup; not analyzing'); return; }
    setBadge('reading captions', 'reading');
    attachPlayback();
    requestCaptions(videoId);
  }

  // Stage 2: captions. page.js runs in the MAIN world and can reach the player;
  // we talk to it over window.postMessage and only accept replies for our video.
  function requestCaptions(videoId) {
    const onMessage = (ev) => {
      if (ev.source !== window) return;
      const d = ev.data;
      if (!d || d.source !== 'sr-page' || d.type !== 'captions' || d.videoId !== videoId) return;
      window.removeEventListener('message', onMessage);
      if (d.error) {
        log('captions:', String(d.error).slice(0, 80));
        current.captionsNote = d.error === 'no captions' ? 'no captions' : 'captions unavailable';
        setBadge(current.captionsNote, 'nocaptions');
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
      // null = page.js did not say (older page.js, or no player response); never coerce "unknown" to false
      current.paidPromotion = typeof d.paidPromotion === 'boolean' ? d.paidPromotion : null;
      current.heat = Array.isArray(d.heat) && d.heat.length <= 200 && d.heat.every((p) => Array.isArray(p) && p.length === 2 && Number.isFinite(p[0]) && Number.isFinite(p[1])) ? d.heat : [];
      current.analysisLines = validLines(d.analysisLines) ? d.analysisLines : null;
      current.analysisLang = typeof d.analysisLang === 'string' ? d.analysisLang.slice(0, 12) : d.lang;
      current.pageBuild = Number.isInteger(d.build) ? d.build : null;
      if (current.analysisLines) log(`analysis track: ${current.analysisLang}, ${current.analysisLines.length} lines`);
      log(`metadata from page.js (lengths): title=${current.title.length} creator=${current.creator.length} ` +
          `channelId=${current.channelId.length} description=${current.description.length} heat=${current.heat.length} ` +
          `paidPromotion=${current.paidPromotion} kind=${current.kind} pageBuild=${current.pageBuild} contentBuild=${BUILD}`);
      const missing = [];
      if (!current.title) missing.push('title');
      if (!current.creator) missing.push('creator');
      if (!current.channelId) missing.push('channelId');
      if (current.paidPromotion === null) missing.push('paidPromotion');
      if (current.pageBuild !== BUILD) missing.push(`page.js build ${current.pageBuild} != ${BUILD}`);
      if (missing.length) logErr(`metadata missing from page.js: ${missing.join(', ')} (reload the extension, then refresh this tab)`);
      analyze(videoId);
    };
    window.addEventListener('message', onMessage);
    current.cleanups.push(() => window.removeEventListener('message', onMessage));
    window.postMessage({ source: 'sr-content', type: 'getCaptions', videoId }, '*');
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

  // Everything the server's POST /analyze reads. This object is the request body: background.js forwards it
  // as is (it used to re-list the fields and silently dropped channelId and duration).
  function analyzePayload(state) {
    const v = videoEl();
    return {
      videoId: state.videoId, lang: state.lang, lines: state.lines,
      title: state.title, creator: state.creator, channelId: state.channelId, kind: state.kind,
      description: state.description, paidPromotion: state.paidPromotion, heat: state.heat || [],
      duration: v && isFinite(v.duration) && v.duration > 0 ? v.duration : null,
      position: v && isFinite(v.currentTime) ? v.currentTime : 0,
      analysisLines: state.analysisLines, analysisLang: state.analysisLang,
      client: { page: state.pageBuild, content: BUILD },
    };
  }

  async function analyze(videoId) {
    const state = current;
    const alive = () => current === state && state.videoId === videoId;
    setBadge('analyzing', 'busy');
    const res = await send({ type: 'analyze', payload: analyzePayload(state) });
    if (!alive()) return;
    if (!res.ok) { logErr('server:', res.error); serverErrorBadge(res.error); return; }
    handleJob(videoId, res);
    if (res.status === 'done' || res.status === 'error') return;

    let timer = null;
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    state.cleanups.push(stop);
    timer = setInterval(async () => {
      const r = await send({ type: 'poll', videoId });
      if (!alive()) { stop(); return; }
      if (!r.ok) { logErr('server:', r.error); serverErrorBadge(r.error); stop(); return; }
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
      // Only something that will be skipped gets the accent "ready" dot; everything else is the quiet state.
      if (skips) setBadge('ready', 'ready', `${skips} to skip`);
      else setBadge(state.segments.length ? 'nothing to skip' : 'no ads', 'none');
      collapseBadgeSoon();
    } else if (job.status === 'error') {
      setBadge('analysis failed', 'error', job.error || '');
    } else {
      setBadge(job.paused ? 'paused' : 'analyzing', 'busy', [job.progress || '', skips ? `${skips} to skip` : ''].filter(Boolean).join(' · '));
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

  // --- status badge (dev mode). Markup per DESIGN.md: .sr-badge.sr-glass[data-kind] > .sr-dot + text + .sr-badge__count
  // kinds: reading | busy | ready | none | nocaptions | off | error
  const BADGE_COLLAPSE_MS = 3000;
  function setBadge(text, kind, count) {
    const state = current;
    if (!state) return;
    // remembered so the tick can (re)place it
    state.badgeText = text; state.badgeKind = kind; state.badgeCount = count || ''; state.badgeCollapsed = false;
    if (state.badgeTimer) { clearTimeout(state.badgeTimer); state.badgeTimer = null; }
    renderBadge();
  }
  function renderBadge() {
    const state = current;
    if (!state || !settings.devMode || !state.badgeText) return;
    const p = playerEl();
    if (!p) return;
    if (!state.badge || !state.badge.isConnected) {
      if (state.badge) state.badge.remove();
      const el = document.createElement('div');
      el.className = 'sr-badge sr-glass sr-ui';
      const dot = document.createElement('span'); dot.className = 'sr-dot';
      const label = document.createElement('span');
      const cnt = document.createElement('span'); cnt.className = 'sr-badge__count';
      el.append(dot, label, cnt);
      el.srLabel = label; el.srCount = cnt;
      p.appendChild(el);
      state.badge = el;
      state.cleanups.push(() => { if (state.badge) state.badge.remove(); state.badge = null; });
    }
    state.badge.srLabel.textContent = state.badgeText;
    state.badge.srCount.textContent = state.badgeCount;
    state.badge.srCount.style.display = state.badgeCount ? '' : 'none';
    state.badge.dataset.kind = state.badgeKind;
    state.badge.classList.toggle('is-collapsed', !!state.badgeCollapsed);
  }
  function collapseBadgeSoon() {   // after 3s only the dot is left
    const state = current;
    if (state.badgeTimer) clearTimeout(state.badgeTimer);
    state.badgeTimer = setTimeout(() => { state.badgeTimer = null; state.badgeCollapsed = true; if (current === state) renderBadge(); }, BADGE_COLLAPSE_MS);
    state.cleanups.push(() => { if (state.badgeTimer) clearTimeout(state.badgeTimer); });
  }
  function serverErrorBadge(error) {
    if (error === 'server offline') setBadge('server offline', 'off');
    else setBadge('analysis failed', 'error', String(error || ''));
  }

  // --- labels shared by the tooltip and the toast (chips are lowercase; DESIGN.md "Writing")
  const CATEGORY_LABEL = { affiliate: 'affiliate', selfpromo: 'self-promo', membership: 'membership', crosspromo: 'cross-promo',
                           charity: 'charity', giveaway: 'giveaway', interaction: 'like & subscribe', intro: 'intro', outro: 'outro', recap: 'recap' };
  const categoryLabel = (s) => (s.category === 'sponsor' ? (s.style === 'integration' ? 'integration' : 'sponsor · read') : (CATEGORY_LABEL[s.category] || s.category));
  const sentence = (t) => t.charAt(0).toUpperCase() + t.slice(1);
  const brandOf = (s) => s.brand_canonical || s.brand || '';
  // Title: the brand when there is one, otherwise the category in sentence case ("Outro", "Like & subscribe").
  const displayName = (s) => brandOf(s) || (s.category === 'sponsor' ? 'Sponsor' : sentence(CATEGORY_LABEL[s.category] || s.category));
  // Chip: the category, unless the title already says it (a brand-less outro is not "Outro [outro]").
  const chipLabel = (s) => (brandOf(s) || s.category === 'sponsor' ? categoryLabel(s) : '');
  // Same thresholds as the server (SKIP_THRESHOLD 0.85; label-only segments under 0.8). A low-confidence segment is
  // hatched so it never looks like a confident one. Rule and chapter segments are exact, never low.
  const SKIP_CONF = 0.85, LABEL_LOW_CONF = 0.8;
  function isLow(s) {
    if (s.source === 'rule' || s.source === 'chapter') return false;
    const c = Number(s.confidence);
    if (!Number.isFinite(c)) return false;
    return s.category === 'sponsor' && s.style !== 'integration' ? c < SKIP_CONF : c < LABEL_LOW_CONF;
  }
  function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // --- marker lane: its own 4px strip above YouTube's bar (.sr-lane), one .sr-marker per segment.
  // Meaning is shape + colour: skip = filled accent, keep (integration) = outline, show (label) = dim, low = hatched.
  // A skip segment that has been dealt with (skipped, undone, or seeked into by hand) will not skip again in this
  // page view. Its marker goes quiet, so the accent only ever means "this will skip" (DESIGN.md, Marker lane).
  // how: 'skipped' | 'plays'
  function settleMarker(state, s, how) {
    state.outcome.set(segKey(s), how);
    const m = state.markerEls.get(segKey(s));
    if (m) { m.classList.add('is-done'); m.dataset.done = how; }
  }

  function drawMarkers() {
    const state = current;
    if (!state) return;
    const bar = document.querySelector('#movie_player .ytp-progress-bar');
    const video = videoEl();
    if (!bar || !video || !isFinite(video.duration) || video.duration <= 0) return;
    const host = document.querySelector('#movie_player .ytp-progress-bar-container') || bar;
    if (state.markers) state.markers.remove();
    host.querySelectorAll('.sr-lane').forEach((e) => e.remove());
    const lane = el('div', 'sr-lane sr-ui');
    const dur = video.duration;
    state.markerEls = new Map();
    for (const s of state.segments) {
      const eff = effectiveAction(s);
      if (eff === 'off') continue;
      const m = el('div', `sr-marker${isLow(s) ? ' is-low' : ''}`);
      m.dataset.action = eff;
      m.dataset.category = s.category === 'sponsor' ? (s.style === 'integration' ? 'integration' : 'sponsor') : s.category;
      const done = eff === 'skip' ? state.outcome.get(segKey(s)) : '';
      if (done) { m.classList.add('is-done'); m.dataset.done = done; }
      state.markerEls.set(segKey(s), m);
      const left = (Math.max(0, s.start) / dur) * 100;
      const width = (Math.max(0, Math.min(dur, s.end) - Math.max(0, s.start)) / dur) * 100;
      m.style.left = `${left}%`;
      m.style.width = `${width}%`;
      m.addEventListener('mouseenter', () => showTooltip(lane, s, eff, left + width / 2));
      m.addEventListener('mouseleave', () => hideTooltip(lane));
      m.addEventListener('click', (ev) => {
        ev.stopPropagation(); ev.preventDefault();
        const r = bar.getBoundingClientRect();
        const frac = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
        const v = videoEl();
        if (v) v.currentTime = frac * v.duration;
      });
      lane.appendChild(m);
    }
    host.appendChild(lane);
    state.markers = lane;
  }

  // --- marker tooltip: .sr-tooltip.sr-glass.sr-glass--solid, a child of the lane centred on the marker
  function showTooltip(lane, s, eff, centerPct) {
    hideTooltip(lane);
    const low = isLow(s);
    const tip = el('div', 'sr-tooltip sr-glass sr-glass--solid');
    const head = el('div', 'sr-tooltip__head');
    head.append(el('span', 'sr-tooltip__brand', displayName(s)));
    const chip = [chipLabel(s), low ? 'low' : ''].filter(Boolean).join(' · ');
    if (chip) head.append(el('span', 'sr-chip', chip));
    const c = Number(s.confidence);
    if (Number.isFinite(c)) head.append(el('span', 'sr-tooltip__conf', c.toFixed(2)));
    const done = eff === 'skip' && current ? current.outcome.get(segKey(s)) : '';
    const what = eff !== 'skip' ? 'plays' : done === 'skipped' ? 'skipped' : done ? 'plays' : 'will skip';
    const why = eff !== 'skip' && s.category === 'sponsor' && s.style !== 'integration' && low ? ` · below ${SKIP_CONF}` : '';
    tip.append(head, el('span', 'sr-tooltip__time', `${fmtTime(s.start)} – ${fmtTime(s.end)} · ${what}${why}`));
    const quote = s.evidence ? `“${s.evidence}”` : (s.reason || '');
    if (quote) tip.append(el('span', 'sr-tooltip__quote', quote.length > 220 ? `${quote.slice(0, 217)}…` : quote));
    // keep the 320px card inside the player: clamp its centre to the lane
    const w = lane.getBoundingClientRect().width || 0;
    const half = Math.min(160, Math.max(0, w / 2 - 8));
    const x = w ? Math.min(w - half, Math.max(half, (centerPct / 100) * w)) : 0;
    tip.style.left = w ? `${x}px` : `${centerPct}%`;
    lane.appendChild(tip);
    lane.srTip = tip;
  }
  function hideTooltip(lane) {
    if (lane.srTip) { lane.srTip.remove(); lane.srTip = null; }
  }

  // --- stats events (skip / undo / seek_into / allowlisted) -> background -> POST /event. Fire and forget.
  function sendEvent(kind, seg, extra) {
    const state = current;
    if (!state) return;
    send({ type: 'event', payload: { kind, videoId: state.videoId, channelId: state.channelId || '',
      channelName: state.creator || '', title: state.title || '',
      segment: seg ? { start: seg.start, end: seg.end, brand: seg.brand, category: seg.category, style: seg.style } : null,
      ...(extra || {}) } });
  }

  // --- feedback: wrong skip (toast) and missed ad (popup) -> background -> POST /feedback
  function sendFeedback(fb) {
    const state = current;
    if (!state) return;
    const payload = {
      videoId: state.videoId, channelId: state.channelId || '', channelName: state.creator || '',
      title: state.title || '', kind: fb.kind, playhead: Math.round((fb.playhead || 0) * 100) / 100,
      // the whole segment as the server reported it, so the report stands on its own
      segment: fb.segment ? { start: fb.segment.start, end: fb.segment.end, category: fb.segment.category,
                              style: fb.segment.style, brand: fb.segment.brand, brand_canonical: fb.segment.brand_canonical || '',
                              action: fb.segment.action, confidence: fb.segment.confidence,
                              evidence: fb.segment.evidence || '', reason: fb.segment.reason || '', source: fb.segment.source || '',
                              line_start: fb.segment.line_start, line_end: fb.segment.line_end } : null,
    };
    send({ type: 'feedback', payload }).then((r) => {
      if (r && r.ok) log(`feedback sent: ${fb.kind}`); else logErr('feedback failed:', r && r.error);
    });
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

  // --- toast after a skip: .sr-toast.sr-glass (DESIGN.md): head (dot, name, chip, "0:52 skipped"), hairline with the
  // skipped span, the evidence quote (two lines), Undo + Wrong skip. Enters with sr-enter, leaves with .is-leaving.
  const TOAST_EXIT_MS = 180;
  function showToast(info, onUndo, onWrong) {
    const state = current;
    const p = playerEl();
    if (!state || !p) return;
    hideToast(true);
    const s = info.segment;
    const v = videoEl();
    const dur = v && isFinite(v.duration) && v.duration > 0 ? v.duration : 0;
    const toast = el('div', 'sr-toast sr-glass sr-ui');
    toast.setAttribute('role', 'status');
    const head = el('div', 'sr-toast__head');
    head.append(el('span', 'sr-dot sr-dot--lg'), el('span', 'sr-toast__brand', displayName(s)));
    if (chipLabel(s)) head.append(el('span', 'sr-chip', chipLabel(s)));
    head.append(el('span', 'sr-toast__meta', `${fmtTime(info.saved)} skipped`));
    const hair = el('div', 'sr-hairline');
    const track = el('div', 'sr-hairline__track');
    const span = el('div', 'sr-hairline__span');
    if (dur) {
      span.style.left = `${Math.min(100, (Math.max(0, s.start) / dur) * 100)}%`;
      span.style.width = `${Math.max(0.8, ((Math.min(dur, s.end) - Math.max(0, s.start)) / dur) * 100)}%`;
    } else { span.style.left = '0'; span.style.width = '100%'; }
    track.append(span);
    const ends = el('div', 'sr-hairline__ends');
    ends.append(el('span', '', `${fmtTime(s.start)} → ${fmtTime(s.end)}`), el('span', '', dur ? fmtTime(dur) : ''));
    hair.append(track, ends);
    toast.append(head, hair);
    if (s.evidence) toast.append(el('div', 'sr-stream sr-stream--2', s.evidence));
    const actions = el('div', 'sr-toast__actions');
    const undo = el('button', 'sr-button sr-button--sm', 'Undo');
    undo.addEventListener('click', (ev) => { ev.stopPropagation(); onUndo(); hideToast(); });
    actions.append(undo);
    if (onWrong) {
      const wrong = el('button', 'sr-button--text', 'Wrong skip');
      wrong.title = 'Undo and report this as a wrong skip';
      wrong.addEventListener('click', (ev) => { ev.stopPropagation(); onUndo(); onWrong(); hideToast(); });
      actions.append(wrong);
    }
    toast.append(actions);
    p.appendChild(toast);
    state.toast = toast;
    state.toastTimer = setTimeout(hideToast, TOAST_MS);
  }
  function hideToast(immediate) {
    const state = current;
    if (!state) return;
    if (state.toastTimer) { clearTimeout(state.toastTimer); state.toastTimer = null; }
    const toast = state.toast;
    if (!toast) return;
    state.toast = null;
    if (immediate === true) { toast.remove(); return; }
    toast.classList.add('is-leaving');
    setTimeout(() => toast.remove(), TOAST_EXIT_MS);
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
        if (effectiveAction(s) !== 'skip') {
          if (s.action === 'skip' && state.channelId && settings.allowlist[state.channelId] &&
              t >= s.start && t < s.end && !state.handled.has('allow:' + segKey(s))) {
            state.handled.add('allow:' + segKey(s));
            sendEvent('allowlisted', s, { playhead: t });
          }
          continue;
        }
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
          sendEvent('skip', s, { playhead: from, saved });
          settleMarker(state, s, 'skipped');
          const undo = () => {
            const v = videoEl();
            if (!v) return;
            ourSeek = true;
            v.currentTime = from;
            recordSkip(-saved);
            sendEvent('undo', s, { playhead: from, saved });
            settleMarker(state, s, 'plays');
            log(`undo: back to ${fmtTime(from)}; segment stays handled`);
          };
          showToast({ segment: s, saved }, undo, () => {
            sendFeedback({ kind: 'false_skip', segment: s, playhead: from });
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
          sendEvent('seek_into', s, { playhead: t });
          settleMarker(state, s, 'plays');
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
      if (settings.devMode && (!state.badge || !state.badge.isConnected) && state.badgeText) renderBadge();
    }, 1000);

    bind(videoEl());
    state.cleanups.push(() => {
      clearInterval(tick);
      unbind();
      hideToast(true);
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
    if (settings.devMode && current.badgeText) renderBadge();
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
    if (msg && msg.type === 'missedAd') {
      const v = videoEl();
      if (!current || !v || !Number.isFinite(v.currentTime)) { sendResponse({ ok: false, error: 'no video' }); return false; }
      sendFeedback({ kind: 'missed_ad', playhead: v.currentTime });
      sendResponse({ ok: true, playhead: v.currentTime });
      return false;
    }
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
