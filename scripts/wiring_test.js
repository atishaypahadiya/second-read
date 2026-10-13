#!/usr/bin/env node
// Wiring test: does video metadata survive page.js -> content.js -> background.js -> POST /analyze, and do the
// skip / "Wrong skip" / "Missed an ad here" paths report the segment and the real playhead?
//
// Runs the real content.js and background.js in a sandbox with a stubbed page, chrome API and fetch, feeds
// content.js the reply page.js would post, and inspects the request body background.js sends to the server.
// Also checks statically that page.js replies with every field content.js reads, that the server reads nothing
// the extension does not send, and that the BUILD constants agree. No dependencies; run with: node scripts/wiring_test.js
// (scripts/regress.py runs it first.)

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.dirname(__dirname);
const read = (f) => fs.readFileSync(path.join(ROOT, f), 'utf8');
const src = { page: read('extension/page.js'), content: read('extension/content.js'), background: read('extension/background.js'), server: read('server/server.py') };

const failures = [];
const check = (ok, msg) => { if (!ok) failures.push(msg); };

// ---------------------------------------------------------------------------- fixture: what page.js would reply
const VIDEO_ID = 'SOQynHuWPeI';
const DESCRIPTION = 'Learn to code with Coddy (ad): https://fern.deals/coddy\n0:00 Intro\n4:47 Sponsor\n5:39 Onavo';
const build = (s, name) => { const m = s.match(/const BUILD = (\d+);/); check(!!m, `${name}: no BUILD constant`); return m ? Number(m[1]) : null; };
const builds = { page: build(src.page, 'page.js'), content: build(src.content, 'content.js'), background: build(src.background, 'background.js') };
const serverBuild = (src.server.match(/^CLIENT_BUILD = (\d+)/m) || [])[1];
check(builds.page === builds.content && builds.content === builds.background, `BUILD constants differ: ${JSON.stringify(builds)}`);
check(Number(serverBuild) === builds.content, `server CLIENT_BUILD ${serverBuild} != extension BUILD ${builds.content}`);

const pageReply = {
  source: 'sr-page', type: 'captions', videoId: VIDEO_ID, build: builds.page,
  lang: 'en', kind: 'asr', path: 'B',
  lines: [{ start: 0, end: 4, text: 'hello there', words: [[0, 2, 'hello'], [2, 4, 'there']], timed: true }],
  title: 'Facebook’s Most Dangerous App', creator: 'fern', channelId: 'UCODHrzPMGbNv67e84WDZhQQ',
  description: DESCRIPTION, heat: [[0, 10], [1, 20]], paidPromotion: true, analysisLines: null, analysisLang: 'en',
};

// The segment the fake server answers with: a sponsor read the extension will skip.
const SEG = { start: 100, end: 150, category: 'sponsor', style: 'read', brand: 'Cody', brand_canonical: 'Coddy', action: 'skip',
              confidence: 0.95, evidence: 'use code FERN20 for 20% off', reason: 'promo code and call to action', source: 'detect',
              line_start: 99.5, line_end: 151.2 };

// ---------------------------------------------------------------------------- sandbox
let fetched = null;
const posts = { '/feedback': [], '/event': [] };
const bgListeners = [];
// The service worker's own storage: a brand-new install (new extension ID after the folder was renamed), with one
// setting already changed locally before the server was reachable. The server holds the previous install's snapshot.
const bgStore = { settings: { enabled: true, devMode: true, actions: { sponsor_read: 'skip', selfpromo: 'label', outro: 'off' }, allowlist: { UCnew_channel_1: 'new channel' } },
                  stats: { today: { date: '2026-10-13', skips: 1, saved: 30 }, all: { skips: 1, saved: 30 } } };
const PREV_SNAPSHOT = { from: 'aaaaaaaaaaaaaaaa', extension: 'abcdefghijklmnopabcdefghijklmnop', saved_at: '2026-10-12T20:00:00',
  // plus things a tampered snapshot might carry: an unknown category, a value the popup cannot produce, a bad id,
  // a non-string name, an unknown field, a negative counter
  settings: { enabled: true, devMode: false, evil: 'x',
              actions: { sponsor_read: 'off', selfpromo: 'skip', outro: 'label', charity: 'off', intro: 'explode', madeup: 'skip' },
              allowlist: { UCold_channel_1: 'old channel', 'bad id!': 'x', UCold_channel_2: { not: 'a string' } } },
  stats: { today: { date: '2026-10-12', skips: 4, saved: 200 }, all: { skips: 40, saved: 2000 } } };
const bgStorageListeners = [];
const bgStorage = {
  get: async (keys) => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).filter((k) => k in bgStore).map((k) => [k, JSON.parse(JSON.stringify(bgStore[k]))])),
  set: async (obj) => { const changes = {}; for (const [k, v] of Object.entries(obj)) { changes[k] = { newValue: v }; bgStore[k] = JSON.parse(JSON.stringify(v)); }
                        for (const fn of bgStorageListeners) fn(changes, 'local'); },
  remove: async (key) => { delete bgStore[key]; for (const fn of bgStorageListeners) fn({ [key]: {} }, 'local'); },
};
const clientStateGets = [];
posts['/client-state'] = [];
const bgSandbox = {
  console: { log() {}, warn() {}, error() {} },
  chrome: { runtime: { onMessage: { addListener: (fn) => bgListeners.push(fn) } },
            storage: { local: bgStorage, onChanged: { addListener: (fn) => bgStorageListeners.push(fn) } } },
  fetch: async (url, init) => {
    const u = new URL(url); const route = u.pathname;
    if (route === '/analyze' && init && init.method === 'POST') fetched = { url, init };
    if (posts[route] && init && init.method === 'POST') posts[route].push(JSON.parse(init.body));
    // the server answers with one candidate per extension, newest first: a decoy from some other extension (empty,
    // and newer), then the snapshot of this extension's previous install
    if (route === '/client-state' && !(init && init.method === 'POST')) { clientStateGets.push(u.searchParams.get('not'));
      const decoy = { from: 'dddddddddddddddd', extension: 'ponmlkjihgfedcbaponmlkjihgfedcba', saved_at: '2026-10-13T08:00:00', settings: { actions: { selfpromo: 'label' } }, stats: null };
      return { ok: true, status: 200, json: async () => ({ candidates: [decoy, PREV_SNAPSHOT] }) }; }
    return { ok: true, status: 200, json: async () => (route === '/analyze' ? { status: 'done', segments: [SEG] } : { ok: true }) };
  },
  encodeURIComponent, JSON, URL, crypto: require('crypto').webcrypto, setTimeout, clearTimeout,
};
vm.runInNewContext(src.background, bgSandbox, { filename: 'background.js' });

const messageListeners = [];
const videoHandlers = {};
const fakeVideo = { currentTime: 12.5, duration: 1012.4, seeking: false,
                    addEventListener: (type, fn) => { videoHandlers[type] = fn; }, removeEventListener() {} };
// Minimal DOM: elements remember their children and click handlers so the test can press the toast's buttons.
const makeEl = () => ({ style: {}, dataset: {}, children: [], handlers: {}, attrs: {}, textContent: '', className: '',
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  append(...kids) { this.children.push(...kids); }, appendChild(k) { this.children.push(k); return k; },
  setAttribute(k, v) { this.attrs[k] = v; }, querySelectorAll: () => [], getBoundingClientRect: () => ({ left: 0, width: 0 }),
  addEventListener(type, fn) { this.handlers[type] = fn; }, remove() { this.removed = true; }, isConnected: true });
const hasClass = (e, c) => String(e.className || '').split(/\s+/).includes(c);
const findDeep = (e, pred) => { if (pred(e)) return e; for (const k of e.children || []) { const r = findDeep(k, pred); if (r) return r; } return null; };
const player = makeEl();
const contentRuntimeListeners = [];
const win = {
  addEventListener: (type, fn) => { if (type === 'message') messageListeners.push(fn); },
  removeEventListener: (type, fn) => { const i = messageListeners.indexOf(fn); if (i >= 0) messageListeners.splice(i, 1); },
  postMessage: (data) => {
    if (data && data.source === 'sr-content' && data.type === 'getCaptions') {
      setImmediate(() => { for (const fn of messageListeners.slice()) fn({ source: win, data: pageReply }); });
    }
  },
};
const sent = [];
const contentSandbox = {
  window: win,
  document: {
    addEventListener() {},
    getElementById: (id) => (id === 'movie_player' ? player : null),
    querySelector: (sel) => (String(sel).includes('video.html5-main-video') ? fakeVideo : null),
    createElement: makeEl,
  },
  location: { href: `https://www.youtube.com/watch?v=${VIDEO_ID}` },
  console: { log() {}, warn() {}, error() {} },
  URL, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  chrome: {
    storage: { local: { get: (k, cb) => cb({}), set() {} }, onChanged: { addListener() {} } },
    runtime: {
      lastError: undefined,
      getManifest: () => ({ version: 'test' }),
      onMessage: { addListener: (fn) => contentRuntimeListeners.push(fn) },
      sendMessage: (msg, cb) => {
        sent.push(JSON.parse(JSON.stringify(msg))); // structured-clone-ish, like the real channel
        let answered = false;
        for (const fn of bgListeners) fn(sent[sent.length - 1], {}, (res) => { answered = true; cb(res); });
        if (!answered) setImmediate(() => { if (!answered) cb(undefined); });
      },
    },
  },
};
vm.runInNewContext(src.content, contentSandbox, { filename: 'content.js' });

// ---------------------------------------------------------------------------- assertions (after the async hops)
setTimeout(() => {
  check(sent.some((m) => m.type === 'analyze'), 'content.js never sent an analyze message');
  check(!!fetched, 'background.js never called fetch for /analyze');
  let body = {};
  if (fetched) {
    check(/\/analyze$/.test(fetched.url), `unexpected URL ${fetched.url}`);
    body = JSON.parse(fetched.init.body);
  }
  const expect = {
    videoId: VIDEO_ID, lang: 'en', title: pageReply.title, creator: 'fern', channelId: pageReply.channelId, kind: 'asr',
    description: DESCRIPTION, paidPromotion: true, duration: 1012.4, position: 12.5, analysisLang: 'en',
  };
  for (const [k, v] of Object.entries(expect)) check(body[k] === v, `POST body.${k} = ${JSON.stringify(body[k])}, expected ${JSON.stringify(v)}`);
  check(Array.isArray(body.lines) && body.lines.length === 1 && Array.isArray(body.lines[0].words), 'POST body.lines lost or without words');
  check(Array.isArray(body.heat) && body.heat.length === 2, `POST body.heat = ${JSON.stringify(body.heat)}`);
  const c = body.client || {};
  check(c.page === builds.page && c.content === builds.content && c.background === builds.background,
        `POST body.client = ${JSON.stringify(c)}, expected all ${builds.content}`);

  // static: page.js must reply with every field content.js reads from the message (d.<field>)
  const replyBlock = (src.page.match(/reply\(\{ build: BUILD,([\s\S]*?)\}\);/) || [])[1] || '';
  const replied = new Set(['build', ...replyBlock.split(/[,\n]/).map((x) => x.trim().split(':')[0].trim()).filter(Boolean)]);
  const handler = (src.content.match(/function requestCaptions[\s\S]*?\n  \}\n/) || [''])[0];
  const readByContent = new Set([...handler.matchAll(/\bd\.([A-Za-z]+)\b/g)].map((m) => m[1]));
  for (const f of readByContent) {
    if (['source', 'type', 'videoId', 'error'].includes(f)) continue;
    check(replied.has(f), `content.js reads d.${f} but page.js does not reply with it`);
  }
  // static: every field the server's /analyze handler reads must be in the body the extension sends
  const srvBlock = (src.server.match(/vid = str\(data\.get\("videoId", ""\)\)[\s\S]*?job = start_or_get_job\(/) || [''])[0];
  check(srvBlock.length > 0, 'could not locate the /analyze handler in server.py');
  const readByServer = new Set([...srvBlock.matchAll(/data\.get\("([A-Za-z]+)"/g)].map((m) => m[1]));
  for (const f of readByServer) {
    if (f === 'tag') continue; // test/replay-only note
    check(Object.prototype.hasOwnProperty.call(body, f), `server reads "${f}" but the extension does not send it`);
  }

  // ---- playback: the skip fires, "Wrong skip" undoes it and reports the whole segment with the real playhead
  const PLAYHEAD = 120.25;
  fakeVideo.currentTime = PLAYHEAD;
  check(typeof videoHandlers.timeupdate === 'function', 'content.js did not bind timeupdate on the video');
  if (videoHandlers.timeupdate) videoHandlers.timeupdate();
  check(fakeVideo.currentTime === SEG.end, `skip did not fire: currentTime ${fakeVideo.currentTime}, expected ${SEG.end}`);
  const toast = player.children.find((c) => hasClass(c, 'sr-toast') && !c.removed);
  check(!!toast, 'no toast after the skip');
  // design-system markup (DESIGN.md "Toast"): glass surface, name + chip + "m:ss skipped", hairline, evidence, two actions
  if (toast) {
    check(hasClass(toast, 'sr-glass') && hasClass(toast, 'sr-ui'), `toast classes: ${toast.className}`);
    const text = (c) => (findDeep(toast, (e) => hasClass(e, c)) || {}).textContent;
    check(text('sr-toast__brand') === 'Coddy', `toast name = ${text('sr-toast__brand')}, expected the canonical brand`);
    check(text('sr-chip') === 'sponsor · read', `toast chip = ${text('sr-chip')}`);
    check(text('sr-toast__meta') === '0:29 skipped', `toast meta = ${text('sr-toast__meta')}`);
    check(text('sr-stream') === SEG.evidence, 'toast does not show the evidence quote');
    const span = findDeep(toast, (e) => hasClass(e, 'sr-hairline__span'));
    check(!!span && /%$/.test(span.style.left || '') && /%$/.test(span.style.width || ''), 'toast hairline span has no position');
    const undo = findDeep(toast, (e) => e.textContent === 'Undo' && hasClass(e, 'sr-button'));
    check(!!undo && typeof undo.handlers.click === 'function', 'toast has no Undo button');
  }
  const wrong = toast && findDeep(toast, (e) => e.textContent === 'Wrong skip');
  check(!!wrong && typeof wrong.handlers.click === 'function', 'toast has no "Wrong skip" button');
  if (wrong && wrong.handlers.click) wrong.handlers.click({ stopPropagation() {} });
  check(fakeVideo.currentTime === PLAYHEAD, `"Wrong skip" did not undo: currentTime ${fakeVideo.currentTime}, expected ${PLAYHEAD}`);

  // ---- popup "Missed an ad here" -> content.js -> POST /feedback
  fakeVideo.currentTime = 333.5;
  let missedReply = null;
  for (const fn of contentRuntimeListeners) fn({ type: 'missedAd' }, {}, (r) => { missedReply = r; });
  check(missedReply && missedReply.ok === true && missedReply.playhead === 333.5, `missedAd reply = ${JSON.stringify(missedReply)}`);

  setTimeout(() => {
    const fs_ = posts['/feedback'].find((b) => b.kind === 'false_skip');
    check(!!fs_, 'no false_skip POSTed to /feedback');
    if (fs_) {
      check(fs_.videoId === VIDEO_ID, `false_skip videoId = ${fs_.videoId}`);
      check(fs_.playhead === PLAYHEAD, `false_skip playhead = ${fs_.playhead}, expected the pre-skip playhead ${PLAYHEAD}`);
      const g = fs_.segment || {};
      for (const k of ['start', 'end', 'category', 'style', 'brand', 'brand_canonical', 'action', 'confidence', 'evidence', 'reason', 'source']) {
        check(g[k] === SEG[k], `false_skip segment.${k} = ${JSON.stringify(g[k])}, expected ${JSON.stringify(SEG[k])}`);
      }
      check(fs_.channelId === pageReply.channelId && fs_.channelName === 'fern', 'false_skip lost the channel');
    }
    const ma = posts['/feedback'].find((b) => b.kind === 'missed_ad');
    check(!!ma && ma.playhead === 333.5 && ma.videoId === VIDEO_ID, `missed_ad body = ${JSON.stringify(ma)}`);
    const kinds = posts['/event'].map((e) => e.kind);
    check(kinds.includes('skip') && kinds.includes('undo'), `stats events sent: ${JSON.stringify(kinds)}, expected skip and undo`);
    const sk = posts['/event'].find((e) => e.kind === 'skip');
    check(sk && sk.playhead === PLAYHEAD && Math.abs(sk.saved - (SEG.end - PLAYHEAD)) < 1e-9, `skip event = ${JSON.stringify(sk)}`);

    // ---- settings survive a new extension ID. Step 1: the worker found a previous install's snapshot and OFFERS
    // it. Nothing is adopted yet, the offer is already validated, and this install's own snapshot is uploaded.
    const id = bgStore.installId;
    check(/^[a-f0-9]{16}$/.test(id || ''), `installId = ${JSON.stringify(id)}`);
    check(clientStateGets.length === 1 && clientStateGets[0] === id, `GET /client-state?not= called with ${JSON.stringify(clientStateGets)}`);
    check(bgStore.restoreChecked === true, 'restoreChecked was not set');
    const offer = bgStore.restoreOffer;
    check(!!offer && offer.summary && offer.summary.channels === 1 && offer.summary.skips === 40, `restore offer = ${JSON.stringify(offer && offer.summary)}`);
    check(offer && Array.isArray(offer.all) && offer.all.length === 1, `an empty decoy snapshot was offered (or hid the real one): ${offer && offer.all && offer.all.length} offers`);
    check(offer && offer.snapshot.settings.actions.sponsor_read === undefined && !('madeup' in offer.snapshot.settings.actions) && !('evil' in offer.snapshot.settings),
          'the offer was not validated before being stored');
    check(Object.keys(bgStore.settings.allowlist).length === 1 && bgStore.stats.all.skips === 1, 'the snapshot was adopted without the user asking');
    const first = posts['/client-state'][0];
    check(!!first && first.installId === id && first.settings.allowlist.UCnew_channel_1 === 'new channel', `own snapshot upload = ${JSON.stringify(first).slice(0, 120)}`);

    // Step 2: the user presses Restore in the popup.
    let restoreReply = null;
    for (const fn of bgListeners) fn({ type: 'restore', accept: true, index: 0 }, {}, (r) => { restoreReply = r; });
    setTimeout(() => {
    check(restoreReply && restoreReply.ok && restoreReply.restored === true, `restore reply = ${JSON.stringify(restoreReply)}`);
    check(!('restoreOffer' in bgStore), 'the offer was not cleared after the decision');
    const ms = bgStore.settings || {}, mt = bgStore.stats || {};
    check(ms.allowlist && ms.allowlist.UCold_channel_1 === 'old channel' && ms.allowlist.UCnew_channel_1 === 'new channel', `allowlist not united: ${JSON.stringify(ms.allowlist)}`);
    check(Object.keys(ms.allowlist || {}).length === 2, `tampered allowlist entries were adopted: ${JSON.stringify(ms.allowlist)}`);
    check(ms.actions && ms.actions.sponsor_read === 'skip', `a value the popup cannot produce was adopted: sponsor_read = ${ms.actions && ms.actions.sponsor_read}`);
    check(ms.actions && !('madeup' in ms.actions) && ms.actions.intro !== 'explode' && !('evil' in ms), `unknown fields or values were adopted: ${JSON.stringify(ms)}`);
    check(ms.actions && ms.actions.selfpromo === 'skip', `previous non-default choice lost: selfpromo = ${ms.actions && ms.actions.selfpromo}`);
    check(ms.actions && ms.actions.outro === 'off', `local non-default choice lost: outro = ${ms.actions && ms.actions.outro}`);
    check(ms.actions && ms.actions.charity === 'off', `previous-only category lost: charity = ${ms.actions && ms.actions.charity}`);
    check(ms.devMode === true, 'local devMode lost');
    check(mt.all && mt.all.skips === 41 && mt.all.saved === 2030, `all-time stats not added up: ${JSON.stringify(mt.all)}`);
    check(mt.today && mt.today.date === '2026-10-13' && mt.today.skips === 1, `today's stats: ${JSON.stringify(mt.today)}`);
    const up = posts['/client-state'][posts['/client-state'].length - 1];
    check(!!up && up.installId === id && up.settings.allowlist.UCold_channel_1 === 'old channel' && up.stats.all.skips === 41,
          `snapshot after restore = ${JSON.stringify(up).slice(0, 160)}`);
    check(posts['/client-state'].every((b) => b.installId !== PREV_SNAPSHOT.from), 'the previous install\'s snapshot id was reused');

    if (failures.length) {
      console.log(`wiring: FAIL (${failures.length})`);
      for (const f of failures) console.log(`  - ${f}`);
      process.exit(1);
    }
    console.log(`wiring: PASS  metadata reaches POST /analyze (${Object.keys(body).length} fields, builds ${builds.content}/${serverBuild}); ` +
                `skip, Wrong skip and Missed-ad reports carry the segment and the real playhead; ` +
                `a previous install's settings are offered, validated, restored on request and backed up`);
    process.exit(0);
    }, 1800); // the backup after a storage change is debounced by 1.5s
  }, 50);
}, 50);
