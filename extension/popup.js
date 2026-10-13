// Sponsor Skipper — popup. Settings live in chrome.storage.local and are applied by content.js instantly.
const CATS = [
  ['sponsor_read', 'Sponsor reads', '#e53935', ['skip', 'label']],
  ['affiliate', 'Affiliate links', '#fb8c00', ['skip', 'label', 'off']],
  ['selfpromo', 'Self-promo', '#fdd835', ['skip', 'label', 'off']],
  ['membership', 'Membership / Patreon', '#8e24aa', ['skip', 'label', 'off']],
  ['crosspromo', 'Cross-promo', '#1e88e5', ['skip', 'label', 'off']],
  ['charity', 'Charity appeals', '#ec407a', ['skip', 'label', 'off']],
  ['giveaway', 'Giveaways', '#00897b', ['skip', 'label', 'off']],
  ['interaction', 'Like & subscribe', '#9e9e9e', ['skip', 'label', 'off']],
  ['intro', 'Intro', '#4fc3f7', ['skip', 'label', 'off']],
  ['outro', 'Outro', '#4fc3f7', ['skip', 'label', 'off']],
  ['recap', 'Recap / preview', '#b39ddb', ['skip', 'label', 'off']],
];
const DEFAULT_SETTINGS = {
  enabled: true,
  actions: Object.fromEntries(CATS.map(([k]) => [k, k === 'sponsor_read' ? 'skip' : 'label'])),
  devMode: false,
  allowlist: {},
};
const $ = (id) => document.getElementById(id);

let settings = null;
let tabState = null; // what content.js reports for the active tab

function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
}

function buildCategoryRows() {
  const box = $('catRows');
  box.textContent = '';
  for (const [key, label, color, opts] of CATS) {
    const row = document.createElement('div'); row.className = 'cat';
    const sw = document.createElement('span'); sw.className = 'sw'; sw.style.background = color;
    const lb = document.createElement('span'); lb.className = 'label'; lb.textContent = label;
    const seg = document.createElement('div'); seg.className = 'seg'; seg.dataset.key = key;
    for (const o of opts) { const b = document.createElement('button'); b.dataset.value = o; b.textContent = o[0].toUpperCase() + o.slice(1); seg.appendChild(b); }
    seg.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      settings.actions[key] = b.dataset.value;
      render(); save();
    });
    row.append(sw, lb, seg);
    box.appendChild(row);
  }
}

function render() {
  $('enabled').checked = settings.enabled;
  document.body.classList.toggle('disabled', !settings.enabled);
  for (const seg of document.querySelectorAll('.seg')) {
    const key = seg.dataset.key;
    for (const b of seg.querySelectorAll('button')) b.classList.toggle('on', b.dataset.value === settings.actions[key]);
  }
  $('devMode').checked = settings.devMode;
  renderChannels();
}

function renderChannels() {
  const ch = tabState && tabState.channelId ? tabState : null;
  const listed = ch && settings.allowlist[ch.channelId];
  $('channelName').textContent = ch ? (ch.channelName || ch.channelId) : 'Open a YouTube video to manage its channel.';
  $('channelName').classList.toggle('placeholder', !ch);
  $('allowBtn').disabled = !ch;
  $('allowBtn').textContent = listed ? 'Skip on this channel again' : "Don't skip on this channel";
  $('allowBtn').classList.toggle('danger', !!listed);
  const ul = $('allowlist');
  ul.textContent = '';
  const ids = Object.keys(settings.allowlist);
  for (const id of ids) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.textContent = settings.allowlist[id] || id;
    name.title = id;
    const rm = document.createElement('button');
    rm.className = 'remove'; rm.textContent = '✕'; rm.title = 'Remove from allowlist';
    rm.addEventListener('click', () => { delete settings.allowlist[id]; render(); save(); });
    li.append(name, rm);
    ul.appendChild(li);
  }
  $('allowHint').textContent = ids.length ? 'On allowlisted channels markers still show but nothing is skipped.'
                                          : 'No allowlisted channels yet.';
}

function save() {
  chrome.storage.local.set({ settings });
}

async function loadStats() {
  const { stats } = await chrome.storage.local.get('stats');
  const today = new Date().toISOString().slice(0, 10);
  const t = stats && stats.today && stats.today.date === today ? stats.today : { skips: 0, saved: 0 };
  const a = (stats && stats.all) || { skips: 0, saved: 0 };
  $('skipsToday').textContent = t.skips;
  $('savedToday').textContent = fmtDur(t.saved);
  $('skipsAll').textContent = a.skips;
  $('savedAll').textContent = fmtDur(a.saved);
}

function setStatus(kind, text) {
  $('statusDot').className = `dot ${kind}`;
  $('statusText').textContent = text;
}

function checkServer() {
  chrome.runtime.sendMessage({ type: 'health' }, (res) => {
    if (!res || !res.ok) return setStatus('red', 'server offline — run scripts/install.sh or python3 server/server.py');
    if (!res.ollama) return setStatus('amber', `server up · Ollama unreachable or ${res.model} not pulled`);
    if (res.loaded === false) return setStatus('amber', `server up · ${res.model} not loaded yet (first call will be slow)`);
    setStatus('green', `server up · ${res.model} loaded`);
  });
}

function askCurrentTab() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs && tabs[0];
    if (!tab) return;
    chrome.tabs.sendMessage(tab.id, { type: 'getState' }, (st) => {
      if (chrome.runtime.lastError || !st || !st.videoId) return;
      tabState = st;
      renderChannels();
      const skips = st.segments.filter((s) => s.effective === 'skip').length;
      const parts = [];
      if (st.status === 'done') parts.push(skips ? `${skips} to skip` : 'nothing to skip');
      else if (st.status) parts.push(st.status === 'error' ? 'analysis error' : 'analyzing…');
      else parts.push(st.captions || 'reading captions…');
      if (st.channelName) parts.push(st.channelName);
      $('videoText').textContent = `This video: ${parts.join(' · ')}`;
      $('video').classList.remove('hidden');
    });
  });
}

// --- Codes tab
let codeTimer = null;
function fmtT(s) { s = Math.round(s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }
function loadCodes(q) {
  chrome.runtime.sendMessage({ type: 'codes', q: q || '' }, (res) => {
    const ul = $('codeList');
    ul.textContent = '';
    if (!res || !res.ok) { $('codeHint').textContent = 'Server offline.'; return; }
    const rows = res.codes || [];
    $('codeHint').textContent = rows.length ? `${rows.length} item${rows.length === 1 ? '' : 's'}` : 'Nothing in the vault yet.';
    for (const r of rows) {
      const li = document.createElement('li');
      const left = document.createElement('div');
      const kind = document.createElement('span'); kind.className = 'kind'; kind.textContent = r.kind;
      const val = document.createElement('span'); val.className = 'val'; val.textContent = r.value;
      left.append(kind, val);
      const copy = document.createElement('button'); copy.className = 'btn'; copy.textContent = 'Copy';
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(r.value); copy.textContent = 'Copied'; setTimeout(() => { copy.textContent = 'Copy'; }, 1200); }
        catch { copy.textContent = 'Failed'; }
      });
      const ctx = document.createElement('div'); ctx.className = 'ctx';
      const link = document.createElement('a');
      link.href = `https://www.youtube.com/watch?v=${encodeURIComponent(r.video_id)}&t=${Math.floor(r.time_s || 0)}s`;
      link.target = '_blank'; link.rel = 'noopener'; link.textContent = `${r.channel_name || r.video_id} @ ${fmtT(r.time_s)}`;
      ctx.append(`${r.brand || '?'} · `, link, r.detail && r.kind === 'offer' ? ` · ${r.detail}` : '');
      li.append(left, copy, ctx);
      ul.appendChild(li);
    }
  });
}
function showPane(name) {
  for (const b of document.querySelectorAll('.ptab')) b.classList.toggle('on', b.dataset.pane === name);
  $('pane-main').classList.toggle('hidden', name !== 'main');
  $('pane-codes').classList.toggle('hidden', name !== 'codes');
  if (name === 'codes') loadCodes($('codeSearch').value);
}

async function init() {
  for (const b of document.querySelectorAll('.ptab')) b.addEventListener('click', () => showPane(b.dataset.pane));
  $('codeSearch').addEventListener('input', () => { clearTimeout(codeTimer); codeTimer = setTimeout(() => loadCodes($('codeSearch').value), 150); });
  const stored = await chrome.storage.local.get('settings');
  settings = { ...DEFAULT_SETTINGS, ...(stored.settings || {}) };
  settings.actions = { ...DEFAULT_SETTINGS.actions, ...(settings.actions || {}) };
  for (const k of Object.keys(settings.actions)) if (settings.actions[k] === 'show') settings.actions[k] = 'label';
  buildCategoryRows();
  render();
  loadStats();
  checkServer();
  askCurrentTab();
  $('version').textContent = `v${chrome.runtime.getManifest().version}`;

  $('enabled').addEventListener('change', (e) => { settings.enabled = e.target.checked; render(); save(); });
  $('devMode').addEventListener('change', (e) => { settings.devMode = e.target.checked; save(); });
  $('missedBtn').addEventListener('click', () => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs && tabs[0];
      if (!tab) return;
      chrome.tabs.sendMessage(tab.id, { type: 'missedAd' }, (r) => {
        const b = $('missedBtn');
        if (chrome.runtime.lastError || !r || !r.ok) { b.textContent = 'Could not report'; return; }
        b.textContent = `Reported at ${fmtDur(r.playhead)}`;
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
}

init();
