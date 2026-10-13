// Sponsor Skipper — background.js (service worker)
// The only part of the extension that talks to the local server. Content scripts
// can't reach localhost directly (cross-origin + private network access), so they
// send messages here and we relay them over fetch. host_permissions covers this.

const SERVER = 'http://127.0.0.1:8765';

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

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg.type !== 'string') return false;
  let p;
  switch (msg.type) {
    case 'analyze':
      p = call('/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ videoId: msg.videoId, lang: msg.lang, lines: msg.lines, title: msg.title || '',
                               kind: msg.kind || '', creator: msg.creator || '', description: msg.description || '',
                               position: msg.position || 0, analysisLines: msg.analysisLines || null,
                               analysisLang: msg.analysisLang || '' }),
      });
      break;
    case 'poll':
      p = call(`/analyze?v=${encodeURIComponent(msg.videoId)}`);
      break;
    case 'health':
      p = call('/health');
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
