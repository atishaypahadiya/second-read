// Sponsor Skipper — popup. Settings live in chrome.storage.local and are applied by content.js instantly.
const DEFAULT_SETTINGS = {
  enabled: true,
  actions: { sponsor_read: 'skip', selfpromo: 'show', interaction: 'show' },
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
  $('channelName').textContent = ch ? (ch.channelName || ch.channelId) : 'open a YouTube video';
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

async function init() {
  const stored = await chrome.storage.local.get('settings');
  settings = { ...DEFAULT_SETTINGS, ...(stored.settings || {}) };
  settings.actions = { ...DEFAULT_SETTINGS.actions, ...(settings.actions || {}) };
  render();
  loadStats();
  checkServer();
  askCurrentTab();
  $('version').textContent = `v${chrome.runtime.getManifest().version}`;

  $('enabled').addEventListener('change', (e) => { settings.enabled = e.target.checked; render(); save(); });
  $('devMode').addEventListener('change', (e) => { settings.devMode = e.target.checked; save(); });
  for (const seg of document.querySelectorAll('.seg')) {
    seg.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      settings.actions[seg.dataset.key] = b.dataset.value;
      render(); save();
    });
  }
  $('allowBtn').addEventListener('click', () => {
    if (!tabState || !tabState.channelId) return;
    if (settings.allowlist[tabState.channelId]) delete settings.allowlist[tabState.channelId];
    else settings.allowlist[tabState.channelId] = tabState.channelName || tabState.channelId;
    render(); save();
  });
  chrome.storage.onChanged.addListener((changes) => { if (changes.stats) loadStats(); });
}

init();
