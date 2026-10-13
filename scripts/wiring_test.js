#!/usr/bin/env node
// Wiring test: does video metadata survive page.js -> content.js -> background.js -> POST /analyze ?
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
  source: 'ss-page', type: 'captions', videoId: VIDEO_ID, build: builds.page,
  lang: 'en', kind: 'asr', path: 'B',
  lines: [{ start: 0, end: 4, text: 'hello there', words: [[0, 2, 'hello'], [2, 4, 'there']], timed: true }],
  title: 'Facebook’s Most Dangerous App', creator: 'fern', channelId: 'UCODHrzPMGbNv67e84WDZhQQ',
  description: DESCRIPTION, heat: [[0, 10], [1, 20]], paidPromotion: true, analysisLines: null, analysisLang: 'en',
};

// ---------------------------------------------------------------------------- sandbox
let fetched = null;
const bgListeners = [];
const bgSandbox = {
  console: { log() {}, warn() {}, error() {} },
  chrome: { runtime: { onMessage: { addListener: (fn) => bgListeners.push(fn) } } },
  fetch: async (url, init) => { fetched = { url, init }; return { ok: true, status: 200, json: async () => ({ status: 'done', segments: [] }) }; },
  encodeURIComponent, JSON,
};
vm.runInNewContext(src.background, bgSandbox, { filename: 'background.js' });

const messageListeners = [];
const fakeVideo = { currentTime: 12.5, duration: 1012.4, seeking: false, addEventListener() {}, removeEventListener() {} };
const win = {
  addEventListener: (type, fn) => { if (type === 'message') messageListeners.push(fn); },
  removeEventListener: (type, fn) => { const i = messageListeners.indexOf(fn); if (i >= 0) messageListeners.splice(i, 1); },
  postMessage: (data) => {
    if (data && data.source === 'ss-content' && data.type === 'getCaptions') {
      setImmediate(() => { for (const fn of messageListeners.slice()) fn({ source: win, data: pageReply }); });
    }
  },
};
const sent = [];
const contentSandbox = {
  window: win,
  document: {
    addEventListener() {},
    getElementById: () => null,
    querySelector: (sel) => (String(sel).includes('video.html5-main-video') ? fakeVideo : null),
    createElement: () => ({ style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} }, append() {}, appendChild() {}, addEventListener() {}, remove() {} }),
  },
  location: { href: `https://www.youtube.com/watch?v=${VIDEO_ID}` },
  console: { log() {}, warn() {}, error() {} },
  URL, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  chrome: {
    storage: { local: { get: (k, cb) => cb({}), set() {} }, onChanged: { addListener() {} } },
    runtime: {
      lastError: undefined,
      getManifest: () => ({ version: 'test' }),
      onMessage: { addListener() {} },
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

  if (failures.length) {
    console.log(`wiring: FAIL (${failures.length})`);
    for (const f of failures) console.log(`  - ${f}`);
    process.exit(1);
  }
  console.log(`wiring: PASS  metadata survives page.js -> content.js -> background.js -> POST /analyze ` +
              `(${Object.keys(body).length} fields, builds ${builds.content}/${serverBuild})`);
}, 50);
