// Second Read — background.js (service worker)
// The only part of the extension that talks to the local server. Content scripts
// can't reach localhost directly (cross-origin + private network access), so they
// send messages here and we relay them over fetch. host_permissions covers this.

const SERVER = 'http://127.0.0.1:8765';
const BUILD = 15; // keep equal to page.js, content.js and server.py CLIENT_BUILD

async function call(path, init) {
  let res;
  try {
    res = await fetch(SERVER + path, init);
  } catch (e) {
    return { ok: false, error: 'server offline' };
  }
  let body = null;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) return { ok: false, status: res.status, error: (body && body.error) || `http ${res.status}` };
  return { ok: true, ...body };
}

// ---------------------------------------------------------------------------------------------------------
// Settings backup. chrome.storage belongs to the extension ID, and an unpacked extension's ID is derived from
// its folder path: renaming or moving the folder (or loading it on another profile) gives a new ID whose storage
// is empty. So the local server keeps a snapshot of `settings` (categories, don't-skip list) and `stats` per
// install, and a new install adopts the most recent snapshot of a previous one, once.
// Rules: never upload before looking for a previous snapshot; never overwrite another install's snapshot.
// ---------------------------------------------------------------------------------------------------------
const BACKUP_KEYS = ['settings', 'stats'];
const DEFAULT_ACTION = (key) => (key === 'sponsor_read' ? 'skip' : 'label');

function newInstallId() {
  const a = new Uint8Array(8);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
}

// What this install has (possibly nothing, possibly a few changes made before the server was reachable) combined
// with the previous install's snapshot. Local, non-default choices win; lists are united; counters add up.
function mergeState(local, prev) {
  const out = {};
  const ls = local.settings, ps = prev.settings;
  if (ls || ps) {
    const s = { ...(ps || {}), ...(ls || {}) };
    s.actions = { ...((ps && ps.actions) || {}) };
    for (const [k, v] of Object.entries((ls && ls.actions) || {})) {
      if (!(k in s.actions) || v !== DEFAULT_ACTION(k)) s.actions[k] = v;
    }
    s.allowlist = { ...((ps && ps.allowlist) || {}), ...((ls && ls.allowlist) || {}) };
    out.settings = s;
  }
  const lt = local.stats, pt = prev.stats;
  if (lt || pt) {
    const num = (x) => (Number.isFinite(x) ? x : 0);
    const add = (a, b) => ({ skips: num(a && a.skips) + num(b && b.skips), saved: num(a && a.saved) + num(b && b.saved) });
    const st = { all: add(lt && lt.all, pt && pt.all) };
    const ld = lt && lt.today, pd = pt && pt.today;
    if (ld && pd && ld.date === pd.date) st.today = { date: ld.date, ...add(ld, pd) };
    else st.today = [ld, pd].filter(Boolean).sort((a, b) => String(b.date).localeCompare(String(a.date)))[0] || undefined;
    if (!st.today) delete st.today;
    out.stats = st;
  }
  return out;
}

async function syncBackup() {
  const st = await chrome.storage.local.get([...BACKUP_KEYS, 'installId', 'restoreChecked']);
  let { installId } = st;
  if (!installId) { installId = newInstallId(); await chrome.storage.local.set({ installId }); }
  if (!st.restoreChecked) {
    const prev = await call(`/client-state?not=${installId}`);
    if (!prev.ok) return; // server not reachable: look again next time, and do not upload before having looked
    const update = { restoreChecked: true };
    if (prev.settings || prev.stats) {
      const merged = mergeState({ settings: st.settings, stats: st.stats }, prev);
      if (merged.settings) update.settings = merged.settings;
      if (merged.stats) update.stats = merged.stats;
      console.log(`[SecondRead:bg] restored settings and stats from a previous install (${prev.from}, saved ${prev.saved_at})`);
    }
    await chrome.storage.local.set(update);
    Object.assign(st, update);
  }
  if (!st.settings && !st.stats) return;
  await call('/client-state', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                               body: JSON.stringify({ installId, settings: st.settings || null, stats: st.stats || null }) });
}

let syncChain = Promise.resolve();
let syncTimer = null;
const queueSync = () => { syncChain = syncChain.then(syncBackup, syncBackup).catch(() => {}); return syncChain; };
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local' || !BACKUP_KEYS.some((k) => changes[k])) return;
  clearTimeout(syncTimer);
  syncTimer = setTimeout(queueSync, 1500);
});
queueSync(); // every time the service worker starts

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg.type !== 'string') return false;
  let p;
  switch (msg.type) {
    case 'analyze': {
      // Forward content.js's payload untouched; only stamp this worker's build. (An older content.js sent the
      // fields at the top level of the message, so fall back to that.)
      const base = msg.payload || msg;
      const payload = { ...base, client: { ...(base.client || {}), background: BUILD } };
      delete payload.type;
      const len = (v) => (typeof v === 'string' || Array.isArray(v) ? v.length : v === null || v === undefined ? 'MISSING' : v);
      console.log('[SecondRead:bg] analyze', payload.videoId, 'lengths:', JSON.stringify({
        lines: len(payload.lines), title: len(payload.title), creator: len(payload.creator), channelId: len(payload.channelId),
        kind: payload.kind, description: len(payload.description), heat: len(payload.heat), duration: len(payload.duration),
        paidPromotion: len(payload.paidPromotion), client: payload.client }));
      p = call('/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      break;
    }
    case 'poll':
      p = call(`/analyze?v=${encodeURIComponent(msg.videoId)}`);
      break;
    case 'codes':
      p = call(`/codes.json?q=${encodeURIComponent(msg.q || '')}`);
      break;
    case 'health':
      p = call('/health');
      break;
    case 'event':
      p = call('/event', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(msg.payload || {}) });
      break;
    case 'feedback':
      p = call('/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(msg.payload || {}),
      });
      break;
    default:
      return false;
  }
  p.then(sendResponse, (e) => sendResponse({ ok: false, error: String(e) }));
  return true; // keep the message channel open for the async response
});
