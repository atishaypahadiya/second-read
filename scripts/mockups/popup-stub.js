// chrome.* for the real popup.js, answered from the mockup fixture (render.js fills it in):
//   stats  -> the mock server's /stats.json "overall" (the same totals the dashboard shot shows)
//   codes  -> the mock server's /codes.json
//   health -> a ready server on the extension's build
//   tabs.sendMessage(getState) -> what content.js reported on the player mockup after its skip
(() => {
  const F = window.__FIXTURE;
  const store = {};
  const listeners = [];
  const later = (fn) => Promise.resolve().then(fn);
  const reply = (msg) => {
    switch (msg && msg.type) {
      case 'stats': return { ok: true, overall: F.overall };
      case 'codes': return { ok: true, codes: F.codes };
      case 'health': return { ok: true, build: 21, extensionBuild: 21, ollama: true, loaded: true, model: 'qwen3:14b' };
      default: return { ok: true };
    }
  };
  window.chrome = {
    runtime: {
      lastError: undefined,
      sendMessage(msg, cb) { const r = reply(msg); later(() => cb && cb(r)); },
      getManifest: () => ({ version: F.manifestVersion }),
    },
    tabs: {
      query(q, cb) { later(() => cb([{ id: 1 }])); },
      sendMessage(id, msg, cb) { later(() => cb(msg && msg.type === 'getState' ? F.tabState : { ok: false })); },
    },
    storage: {
      local: {
        get(key, cb) { const r = { [key]: store[key] }; if (cb) later(() => cb(r)); return Promise.resolve(r); },
        set(obj, cb) {
          const changes = {};
          for (const [k, v] of Object.entries(obj)) { changes[k] = { oldValue: store[k], newValue: v }; store[k] = v; }
          later(() => { for (const fn of listeners) fn(changes, 'local'); if (cb) cb(); });
          return Promise.resolve();
        },
      },
      onChanged: { addListener(fn) { listeners.push(fn); } },
    },
  };
})();
