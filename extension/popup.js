// Second Read — popup. Settings live in chrome.storage.local and are applied by content.js
// instantly. Markup and classes follow DESIGN.md ("Popup"); nothing here decides how things look.

// [storage key, label, options, shown by default]. The four most-used categories are always visible; the rest sit
// behind the "more categories" disclosure. Storage values (skip / label / off) are unchanged.
const CATS = [
  ['sponsor_read', 'sponsor read', ['skip', 'label'], true],
  ['integration', 'sponsor integration', null, true],          // not a setting: integrations always play
  ['selfpromo', 'self-promo', ['skip', 'label', 'off'], true],
  ['interaction', 'like & subscribe', ['skip', 'label', 'off'], true],
  ['affiliate', 'affiliate links', ['skip', 'label', 'off'], false],
  ['membership', 'membership / patreon', ['skip', 'label', 'off'], false],
  ['crosspromo', 'cross-promo', ['skip', 'label', 'off'], false],
  ['charity', 'charity appeals', ['skip', 'label', 'off'], false],
  ['giveaway', 'giveaways', ['skip', 'label', 'off'], false],
  ['intro', 'intro', ['skip', 'label', 'off'], false],
  ['outro', 'outro', ['skip', 'label', 'off'], false],
  ['recap', 'recap / preview', ['skip', 'label', 'off'], false],
];
const SETTING_KEYS = CATS.filter(([, , opts]) => opts).map(([k]) => k);
const DEFAULT_SETTINGS = {
  enabled: true,
  actions: Object.fromEntries(SETTING_KEYS.map((k) => [k, k === 'sponsor_read' ? 'skip' : 'label'])),
  devMode: false,
  allowlist: {},
};
const $ = (id) => document.getElementById(id);
const el = (tag, className, text) => {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
};

let settings = null;
let tabState = null; // what content.js reports for the active tab
let serverHint = ''; // what to do when the server or the model is not available (shown in the channel row)
let serverStatus = ['sr-dot--faint', 'checking', '']; // [dot class, text, hint] from the last health check
let dealFor = '';    // video id whose code / link is shown in the "this video" block

function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
}

// ---------------------------------------------------------------- categories
function buildCategoryRows() {
  $('mainRows').textContent = '';
  $('moreRows').textContent = '';
  for (const [key, label, opts, primary] of CATS) {
    const row = el('div', 'sr-row');
    row.append(el('span', 'sr-row__label', label));
    if (!opts) {
      const chip = el('span', 'sr-chip', 'always plays');
      chip.title = 'The creator is using the product as part of the video. Integrations are never skipped.';
      row.append(chip);
    } else {
      const seg = el('div', 'sr-seg');
      seg.dataset.key = key;
      seg.setAttribute('role', 'group');
      seg.setAttribute('aria-label', label);
      for (const o of opts) {
        const b = el('button', 'sr-seg__btn', o);
        b.dataset.value = o;
        seg.appendChild(b);
      }
      seg.addEventListener('click', (e) => {
        const b = e.target.closest('button');
        if (!b) return;
        settings.actions[key] = b.dataset.value;
        render(); save();
      });
      row.append(seg);
    }
    (primary ? $('mainRows') : $('moreRows')).appendChild(row);
  }
}

function render() {
  $('enabled').setAttribute('aria-pressed', String(!!settings.enabled));
  $('enabledLabel').textContent = settings.enabled ? 'Skipping on' : 'Paused';
  $('app').classList.toggle('is-paused', !settings.enabled);
  for (const seg of document.querySelectorAll('.sr-seg')) {
    const key = seg.dataset.key;
    for (const b of seg.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.value === settings.actions[key]));
  }
  // how many hidden categories are not on their default, so a setting is never out of sight and forgotten
  const changed = CATS.filter(([k, , opts, primary]) => opts && !primary && settings.actions[k] !== DEFAULT_SETTINGS.actions[k]).length;
  $('moreCount').hidden = !changed;
  $('moreCount').textContent = `${changed} changed`;
  $('devMode').checked = settings.devMode;
  paintStatus();
  renderChannel();
  renderVideo();
  renderAllowlist();
}

function setMoreOpen(open) {
  $('moreRows').hidden = !open;
  $('moreBtn').setAttribute('aria-expanded', String(open));
  $('moreText').textContent = open ? 'fewer categories' : 'more categories';
}

// ---------------------------------------------------------------- current channel
function renderChannel() {
  const st = tabState && tabState.videoId ? tabState : null;
  const ch = st && st.channelId ? st : null;
  $('channelThumb').classList.toggle('is-empty', !st);
  $('missedBtn').disabled = !st || $('missedBtn').dataset.reported === '1';
  if (!st) {
    $('channelName').textContent = 'no video';
    $('channelSub').textContent = serverHint || 'open a youtube video';
    $('allowBtn').hidden = true;
    return;
  }
  $('channelName').textContent = st.channelName || st.channelId || 'this video';
  const toSkip = st.segments.filter((s) => s.effective === 'skip' && !s.done).length;
  const skipped = st.segments.filter((s) => s.effective === 'skip' && s.done === 'skipped').length;
  const ads = (k) => `${k} ad${k === 1 ? '' : 's'}`;
  let sub;
  if (st.status === 'done') sub = toSkip ? `${ads(toSkip)} to skip` : skipped ? `${ads(skipped)} skipped` : 'nothing to skip';
  else if (st.status === 'error') sub = 'analysis failed';
  else if (st.status) sub = 'analyzing…';
  else sub = st.captions || 'reading captions…';
  // A tab whose content script predates this popup (the extension's files changed, the tab was not refreshed)
  // answers without the newer fields; say so instead of showing half the picture.
  if (st.duration === undefined) sub = 'refresh this tab to finish updating';
  $('channelSub').textContent = serverHint || sub;   // a server problem explains why nothing is happening
  const btn = $('allowBtn');
  btn.hidden = !ch;
  if (ch) {
    const listed = !!settings.allowlist[ch.channelId];
    const name = ch.channelName && ch.channelName.length <= 12 ? ch.channelName : 'this channel';
    btn.textContent = listed ? `Skip on ${name} again` : `Don’t skip ${name}`;
    btn.title = listed ? 'This channel is on your don’t-skip list. Markers still show; nothing is skipped.'
                       : 'Never skip on this channel. Markers still show.';
  }
}

// ---------------------------------------------------------------- this video: what was found, what happened to it
function renderVideo() {
  const st = tabState && tabState.videoId ? tabState : null;
  const ads = st ? st.segments.filter((s) => s.category === 'sponsor' && s.effective !== 'off') : [];
  $('video').hidden = !ads.length;
  if (!ads.length) return;
  // Without the video's real length there is no honest scale for a timeline: show the rows alone. (Guessing the
  // length from the last segment drew a read in the middle of a video as if it ran to the end.)
  const dur = st.duration > 0 ? st.duration : 0;
  $('videoLen').textContent = dur ? fmtDur(dur) : '';
  const track = $('videoTrack'), rows = $('videoRows');
  track.parentElement.hidden = !dur;
  track.textContent = '';
  rows.textContent = '';
  for (const s of dur ? ads : []) {
    // same meaning as the lane on the player: accent = this will skip; anything else is quiet
    const pending = s.effective === 'skip' && !s.done;
    const span = el('span', `sr-hairline__span${pending ? ' sr-hairline__span--accent' : s.done === 'skipped' ? '' : ' is-quiet'}`);
    span.style.left = `${Math.min(99, (s.start / dur) * 100)}%`;
    span.style.width = `${Math.max(0, ((s.end - s.start) / dur) * 100)}%`;
    track.appendChild(span);
  }
  for (const s of ads.slice(0, 3)) {
    const row = el('div', 'sr-row');
    const name = el('span', 'sr-row__label sr-video__name', s.brand_canonical || s.brand || 'sponsor');
    let what;
    if (s.effective !== 'skip') what = s.style === 'integration' ? 'integration · plays' : 'plays';
    else if (s.done === 'skipped') what = `${fmtDur(s.end - s.start)} skipped`;
    else if (s.done) what = 'plays';
    else what = `skips at ${fmtDur(s.start)}`;
    row.append(name, el('span', 'sr-hint', what));
    rows.appendChild(row);
  }
  if (dealFor !== st.videoId) { dealFor = st.videoId; loadDeal(st.videoId); }
}

// The code or link from this video's sponsor read, with its offer: you skip the ad and keep the deal.
function loadDeal(videoId) {
  $('videoDeal').textContent = '';
  chrome.runtime.sendMessage({ type: 'codes', q: '' }, (res) => {
    if (!res || !res.ok || dealFor !== videoId) return;
    const mine = (res.codes || []).filter((r) => r.video_id === videoId);
    const best = mine.find((r) => r.kind === 'code') || mine.find((r) => r.kind === 'link');
    if (!best) return;
    const offers = [...new Set(mine.filter((r) => r.kind === 'offer').map((r) => r.value))].slice(0, 2);
    const also = best.kind === 'code' ? mine.find((r) => r.kind === 'link') : null;
    const note = [...offers, also ? also.value : ''].filter(Boolean).join(' · ');
    $('videoDeal').textContent = '';
    $('videoDeal').appendChild(codeItem(best, note));
  });
}

function renderAllowlist() {
  const ul = $('allowlist');
  ul.textContent = '';
  const ids = Object.keys(settings.allowlist);
  for (const id of ids) {
    const li = el('li');
    const name = el('span', 'sr-list__name', settings.allowlist[id] || id);
    name.title = id;
    const rm = el('button', 'sr-button--text', 'Remove');
    rm.addEventListener('click', () => { delete settings.allowlist[id]; render(); save(); });
    li.append(name, rm);
    ul.appendChild(li);
  }
  $('allowHint').textContent = ids.length ? 'Markers still show on these channels; nothing is skipped.'
                                          : 'None yet. Use “Don’t skip …” next to a channel on the main view.';
}

function save() {
  chrome.storage.local.set({ settings });
}

// ---------------------------------------------------------------- stats (hero card)
// One set of numbers, the server's: it is what the dashboard shows, and it survives what resets the extension's
// own storage (a new extension ID after the folder moves, a reinstall). chrome.storage is the copy shown while
// the server cannot be asked, and it is brought in line every time the popup opens.
const todayKey = () => new Date().toISOString().slice(0, 10);

function syncStats() {
  chrome.runtime.sendMessage({ type: 'stats' }, async (res) => {
    const o = res && res.ok && res.overall;
    if (!o) return;   // server offline: keep showing the local copy
    const { stats } = await chrome.storage.local.get('stats');
    const local = stats || {};
    const lt = local.today && local.today.date === todayKey() ? local.today : {};
    const st = o.today || { skips: lt.skips || 0, undos: lt.undos || 0, saved_s: lt.saved || 0 };   // an older server has no "today"
    const next = {
      // integrations left playing: the server's count too (an older server does not have it: keep the local one)
      today: { date: todayKey(), skips: st.skips, undos: st.undos, saved: st.saved_s,
               kept: st.integrations_played !== undefined ? st.integrations_played : (lt.kept || 0) },
      all: { skips: o.skips, undos: o.undos, saved: o.saved_s,
             kept: o.integrations_played !== undefined ? o.integrations_played : ((local.all && local.all.kept) || 0) },
    };
    if (JSON.stringify(next) !== JSON.stringify({ today: local.today, all: local.all })) chrome.storage.local.set({ stats: next });   // repaints through onChanged
  });
}

async function loadStats() {
  const { stats } = await chrome.storage.local.get('stats');
  const today = todayKey();
  const t = stats && stats.today && stats.today.date === today ? stats.today : { skips: 0, saved: 0 };
  const a = (stats && stats.all) || { skips: 0, saved: 0 };
  const n = (count, one, many) => `${count} ${count === 1 ? one : many}`;
  $('savedAll').textContent = fmtDur(a.saved);
  $('savedToday').textContent = `today ${fmtDur(t.saved)}`;
  $('savedToday').title = `${n(t.skips || 0, 'skip', 'skips')} today`;
  const kept = a.kept || 0;
  $('trust').textContent = [n(a.skips || 0, 'skip', 'skips'), `${a.undos || 0} undone`,
                            kept ? `${n(kept, 'integration', 'integrations')} left playing` : ''].filter(Boolean).join(' · ');
  $('trust').title = 'All-time, the same totals as the dashboard. An undone skip still counts as a skip; its seconds are taken back out.';
}

// ---------------------------------------------------------------- server status (head chip)
// dot: ink = ready, pulsing = warming up, ring = something is off (DESIGN.md "Dot")
function setStatus(dot, text, hint) {
  serverStatus = [dot, text, hint || ''];
  serverHint = dot === 'sr-dot--ring' ? (hint || '') : '';
  $('channelSub').title = serverHint;
  paintStatus();
  if (settings) renderChannel();
}

// The off switch is at the end of the pane, so the head chip says "paused" whenever skipping is off.
function paintStatus() {
  const paused = settings && !settings.enabled;
  const [dot, text, hint] = paused ? ['sr-dot--ring', 'paused', 'Skipping is off. The switch is at the bottom.'] : serverStatus;
  $('statusDot').className = `sr-dot ${dot}`;
  $('statusText').textContent = text;
  $('status').title = hint;
}

function checkServer() {
  chrome.runtime.sendMessage({ type: 'health' }, (res) => {
    if (!res || !res.ok) return setStatus('sr-dot--ring', 'server offline', 'start it: python3 server/server.py');
    if (!res.ollama) return setStatus('sr-dot--ring', 'model offline', `start Ollama, or pull ${res.model}`);
    if (res.loaded === false) return setStatus('sr-dot--ink is-live', 'loading model', `${res.model} is not loaded yet; the first video will be slow`);
    setStatus('sr-dot--ink', 'server ready', `${res.model} loaded`);
  });
}

function askCurrentTab() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs && tabs[0];
    if (!tab) return;
    chrome.tabs.sendMessage(tab.id, { type: 'getState' }, (st) => {
      if (chrome.runtime.lastError || !st || !st.videoId) return;
      tabState = st;
      renderChannel();
      renderVideo();
    });
  });
}

// ---------------------------------------------------------------- codes vault
let codeTimer = null;
function fmtT(s) { s = Math.round(s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }
function loadCodes(q) {
  chrome.runtime.sendMessage({ type: 'codes', q: q || '' }, (res) => {
    const ul = $('codeList');
    ul.textContent = '';
    if (!res || !res.ok) { $('codeHint').textContent = 'Server offline.'; return; }
    const rows = res.codes || [];
    $('codeHint').textContent = rows.length ? `${rows.length} item${rows.length === 1 ? '' : 's'}` : 'Nothing in the vault yet.';
    for (const r of rows) ul.appendChild(codeItem(r));
  });
}

// One vault row: kind chip, the value, a context line, Copy. With `note` the context line is that text (the
// "this video" block); without it, the brand and a link to the moment in the video (the vault).
function codeItem(r, note) {
  const li = el('li');
  const main = el('div', 'sr-list__main');
  const line = el('div', 'sr-list__line');
  line.append(el('span', 'sr-chip', r.kind), el('span', 'sr-list__name sr-list__value', r.value));
  const ctx = el('div', 'sr-hint');
  if (note !== undefined) ctx.textContent = note;
  else {
    const link = el('a', '', `${r.channel_name || r.video_id} @ ${fmtT(r.time_s)}`);
    link.href = `https://www.youtube.com/watch?v=${encodeURIComponent(r.video_id)}&t=${Math.floor(r.time_s || 0)}s`;
    link.target = '_blank'; link.rel = 'noopener';
    ctx.append(`${r.brand || '?'} · `, link);
  }
  main.append(line);
  if (ctx.textContent) main.append(ctx);
  const copy = el('button', 'sr-button--text', 'Copy');
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(r.value); copy.textContent = 'Copied'; setTimeout(() => { copy.textContent = 'Copy'; }, 1200); }
    catch { copy.textContent = 'Failed'; }
  });
  li.append(main, copy);
  return li;
}

function showPane(name) {
  $('pane-main').hidden = name !== 'main';
  $('pane-more').hidden = name !== 'more';
  $('moreNav').textContent = name === 'more' ? 'Back' : 'Codes & more';
  if (name === 'more') loadCodes($('codeSearch').value);
}

async function init() {
  $('moreNav').addEventListener('click', () => showPane($('pane-more').hidden ? 'more' : 'main'));
  $('moreBtn').addEventListener('click', () => setMoreOpen($('moreRows').hidden));
  $('codeSearch').addEventListener('input', () => { clearTimeout(codeTimer); codeTimer = setTimeout(() => loadCodes($('codeSearch').value), 150); });
  const stored = await chrome.storage.local.get('settings');
  settings = { ...DEFAULT_SETTINGS, ...(stored.settings || {}) };
  settings.actions = { ...DEFAULT_SETTINGS.actions, ...(settings.actions || {}) };
  for (const k of Object.keys(settings.actions)) if (settings.actions[k] === 'show') settings.actions[k] = 'label';
  buildCategoryRows();
  render();
  loadStats();
  syncStats();
  checkServer();
  askCurrentTab();
  $('version').textContent = `v${chrome.runtime.getManifest().version}`;

  $('enabled').addEventListener('click', () => { settings.enabled = !settings.enabled; render(); save(); });
  $('devMode').addEventListener('change', (e) => { settings.devMode = e.target.checked; save(); });
  $('missedBtn').addEventListener('click', () => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs && tabs[0];
      if (!tab) return;
      chrome.tabs.sendMessage(tab.id, { type: 'missedAd' }, (r) => {
        const b = $('missedBtn');
        if (chrome.runtime.lastError || !r || !r.ok) { b.textContent = 'Could not report'; return; }
        b.textContent = `Reported at ${fmtDur(r.playhead)}`;
        b.dataset.reported = '1';
        b.disabled = true;
      });
    });
  });
  $('allowBtn').addEventListener('click', () => {
    if (!tabState || !tabState.channelId) return;
    if (settings.allowlist[tabState.channelId]) delete settings.allowlist[tabState.channelId];
    else settings.allowlist[tabState.channelId] = tabState.channelName || tabState.channelId;
    render(); save();
  });
  chrome.storage.onChanged.addListener((changes) => { if (changes.stats) loadStats(); });
  // while the popup is open, keep "this video" current (analysis finishing, a skip happening)
  setInterval(askCurrentTab, 1500);
}

init();
