// Second Read — background.js (service worker)
// The only part of the extension that talks to the local server. Content scripts
// can't reach localhost directly (cross-origin + private network access), so they
// send messages here and we relay them over fetch. host_permissions covers this.

const SERVER = 'http://127.0.0.1:8765';
const BUILD = 17; // keep equal to page.js, content.js and server.py CLIENT_BUILD

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

// Leftovers of the settings backup that was tried and removed: its storage keys, and the toolbar badge it may
// have left showing. Harmless to run every time the worker starts.
chrome.storage.local.remove(['installId', 'restoreChecked', 'restoreOffer']);
if (chrome.action && chrome.action.setBadgeText) chrome.action.setBadgeText({ text: '' });

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
