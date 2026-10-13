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

// ---------------------------------------------------------------------------- page.js caption parsing (the real functions)
// An auto track without word timing: one multi-word seg per cue, and each cue stays on screen while the next one is
// spoken (dDurationMs overlaps the next tStartMs). Words must stay inside [cue start, next cue start).
{
  const fn = (name) => (src.page.match(new RegExp(`  function ${name}\\([\\s\\S]*?\\n  \\}\\n`)) || [''])[0];
  const maxLine = (src.page.match(/const MAX_LINE_SECONDS = (\d+);/) || [])[1];
  check(!!fn('eventsToCues') && !!fn('mergeCues') && !!maxLine, 'could not locate eventsToCues / mergeCues / MAX_LINE_SECONDS in page.js');
  const parse = vm.runInNewContext(`const MAX_LINE_SECONDS = ${maxLine || 8};\n${fn('eventsToCues')}\n${fn('mergeCues')}\n` +
                                   '(events) => mergeCues(eventsToCues(events));');
  const untimed = parse([
    { tStartMs: 447600, dDurationMs: 7520, segs: [{ utf8: "been moving between two different continents. So, it's a happy coincidence" }] },
    { tStartMs: 451120, dDurationMs: 7200, segs: [{ utf8: "that the sponsor for this week's episode is Headspace, the mental health" }] },
    { tStartMs: 455120, dDurationMs: 5300, segs: [{ utf8: 'platform. Did you try it?' }] },
    { tStartMs: 456800, dDurationMs: 3600, segs: [{ utf8: "Yeah. Yeah. They sent me a free subscription and I've been taking their" }] },
  ]);
  const flat = untimed.flatMap((l) => l.words.map((w) => ({ w, l })));
  check(flat.length === 41, `untimed fixture: ${flat.length} words, expected 41`);
  const outside = flat.filter(({ w, l }) => !(l.start <= w[0] && w[0] <= w[1] && w[1] <= l.end));
  check(outside.length === 0, `untimed track: ${outside.length} word(s) outside their line, e.g. ${JSON.stringify(outside[0] && [outside[0].w, outside[0].l.start, outside[0].l.end])}`);
  const backwards = flat.filter((x, i) => i > 0 && x.w[0] < flat[i - 1].w[0]);
  check(backwards.length === 0, `untimed track: ${backwards.length} word(s) start before the word spoken before them, e.g. ${JSON.stringify(backwards[0] && backwards[0].w)}`);
  check(untimed.every((l) => l.timed === false), 'untimed track: lines must be flagged timed=false');
  const timed = parse([
    { tStartMs: 1000, dDurationMs: 6000, segs: [{ utf8: 'hello', tOffsetMs: 0 }, { utf8: ' there', tOffsetMs: 600 }, { utf8: ' world.', tOffsetMs: 1500 }] },
    { tStartMs: 4000, dDurationMs: 3000, segs: [{ utf8: 'Next', tOffsetMs: 0 }, { utf8: ' line.', tOffsetMs: 500 }] },
  ]);
  // short-read guard: a transcript ending more than 15% before the video's end is read once more
  const num = (name) => (src.page.match(new RegExp(`const ${name} = ([\\d.]+);`)) || [])[1];
  check(!!fn('endsShort') && !!fn('betterRead') && !!num('SHORT_READ_FRACTION') && !!num('SHORT_READ_SAME_S'), 'could not locate the short-read guard in page.js');
  const guard = vm.runInNewContext(`const SHORT_READ_FRACTION = ${num('SHORT_READ_FRACTION')}; const SHORT_READ_SAME_S = ${num('SHORT_READ_SAME_S')};\n` +
                                   `${fn('endsShort')}\n${fn('betterRead')}\n({ endsShort, betterRead });`);
  const upTo = (t) => [{ start: 0, end: 10 }, { start: t - 5, end: t }];
  check(guard.endsShort(upTo(800), 1000) === true && guard.endsShort(upTo(900), 1000) === false && guard.endsShort(upTo(800), 0) === false,
        'short-read guard: 800 of 1000 s is short, 900 of 1000 s is not, an unknown video length never is');
  const full = upTo(995), cut = upTo(800);
  check(guard.betterRead(cut, full).lines === full && guard.betterRead(cut, full).retry === 'longer', 'short-read guard: a longer second read must replace the first');
  check(guard.betterRead(cut, upTo(801)).lines === cut && /no speech/.test(guard.betterRead(cut, upTo(801)).retry), 'short-read guard: two reads ending together mean a real silent tail');
  check(guard.betterRead(cut, []).lines === cut, 'short-read guard: a failed second read keeps the first');
  check(JSON.stringify(timed.map((l) => l.words.map((w) => w[0]))) === '[[1,1.6,2.5],[4,4.5]]' && timed.every((l) => l.timed),
        `timed track: word starts must stay exactly as given, got ${JSON.stringify(timed.map((l) => l.words))}`);
}

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
  capture: { path: 'B', via: 'retargeted', how: 'cc click', retry: '', tracks: ['en:asr'], picked: 'en:asr',
             request: { params: { lang: 'en', kind: 'asr', fmt: 'json3' }, others: ['pot'] }, events: 1, withSegs: 1, withOffset: 1,
             multiSeg: 1, lines: 1, timed: 1, lastEnd: 4, videoSeconds: 1012 },
};

// The segment the fake server answers with: a sponsor read the extension will skip.
const SEG = { start: 100, end: 150, category: 'sponsor', style: 'read', brand: 'Cody', brand_canonical: 'Coddy', action: 'skip',
              confidence: 0.95, evidence: 'use code FERN20 for 20% off', reason: 'promo code and call to action', source: 'detect',
              line_start: 99.5, line_end: 151.2 };
// two sponsor integrations (server action 'keep'): one is played through, the viewer seeks out of the other
const KEEP = { start: 400, end: 420, category: 'sponsor', style: 'integration', brand: 'Notion', action: 'keep', confidence: 0.9, evidence: 'sponsored by Notion' };
const KEEP2 = { ...KEEP, start: 500, end: 520 };

// ---------------------------------------------------------------------------- sandbox
let fetched = null;
let storedUrl = null;   // GET /stored: the instant revisit asks by video id before the captions are read
const posts = { '/feedback': [], '/event': [] };
const bgListeners = [];
// background.js clears a few leftover storage keys and the toolbar badge when it starts
const bgRemoved = [];
const fetchedRoutes = new Set();
const bgSandbox = {
  console: { log() {}, warn() {}, error() {} },
  chrome: { runtime: { onMessage: { addListener: (fn) => bgListeners.push(fn) } },
            storage: { local: { remove: (keys) => { bgRemoved.push(...[].concat(keys)); } } },
            action: { setBadgeText: (o) => { bgSandbox.badge = o.text; } } },
  fetch: async (url, init) => {
    const route = new URL(url).pathname;
    fetchedRoutes.add(route);
    if (route === '/analyze' && init && init.method === 'POST') fetched = { url, init };
    if (posts[route] && init && init.method === 'POST') posts[route].push(JSON.parse(init.body));
    if (route === '/stored') storedUrl = url;
    return { ok: true, status: 200, json: async () => (route === '/analyze' ? { status: 'done', segments: [SEG, KEEP, KEEP2] }
      : route === '/stored' ? { status: 'done', segments: [SEG], cached: true, instant: true } : { ok: true }) };
  },
  encodeURIComponent, JSON, URL,
};
vm.runInNewContext(src.background, bgSandbox, { filename: 'background.js' });

const messageListeners = [];
const videoHandlers = {};
const fakeVideo = { currentTime: 12.5, duration: 1012.4, seeking: false,
                    addEventListener: (type, fn) => { videoHandlers[type] = fn; }, removeEventListener() {} };
// Minimal DOM: elements remember their children and click handlers so the test can press the toast's buttons.
const makeEl = () => {
  const e = { style: {}, dataset: {}, children: [], handlers: {}, attrs: {}, textContent: '', className: '',
  append(...kids) { this.children.push(...kids); }, appendChild(k) { this.children.push(k); return k; },
  setAttribute(k, v) { this.attrs[k] = v; }, querySelectorAll: () => [], getBoundingClientRect: () => ({ left: 0, width: 0 }),
  addEventListener(type, fn) { this.handlers[type] = fn; }, remove() { this.removed = true; }, isConnected: true };
  const names = () => String(e.className || '').split(/\s+/).filter(Boolean);
  e.classList = {
    contains: (c) => names().includes(c),
    add: (...cs) => { e.className = [...new Set([...names(), ...cs])].join(' '); },
    remove: (...cs) => { e.className = names().filter((n) => !cs.includes(n)).join(' '); },
    toggle: (c, on) => { const want = on === undefined ? !names().includes(c) : !!on; e.classList[want ? 'add' : 'remove'](c); return want; },
  };
  return e;
};
const hasClass = (e, c) => String(e.className || '').split(/\s+/).includes(c);
const findDeep = (e, pred) => { if (pred(e)) return e; for (const k of e.children || []) { const r = findDeep(k, pred); if (r) return r; } return null; };
const player = makeEl();
const progressBar = makeEl();   // '#movie_player .ytp-progress-bar(-container)': where content.js hangs its marker lane
const liveLane = () => progressBar.children.filter((c) => hasClass(c, 'sr-lane') && !c.removed).pop();
const markerOf = (seg) => (liveLane() ? liveLane().children.find((m) => hasClass(m, 'sr-marker') && m.style.left === `${(seg.start / fakeVideo.duration) * 100}%`) : null);
const tooltipTime = (seg) => {
  const m = markerOf(seg);
  if (!m || !m.handlers.mouseenter) return null;
  m.handlers.mouseenter();
  const tip = liveLane().children.filter((c) => hasClass(c, 'sr-tooltip') && !c.removed).pop();
  return tip ? (findDeep(tip, (e) => hasClass(e, 'sr-tooltip__time')) || {}).textContent : null;
};
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
const stored = {};   // chrome.storage.local
const tabStateNow = () => { let st = null; for (const fn of contentRuntimeListeners) fn({ type: 'getState' }, {}, (r) => { st = r; }); return st; };
const contentSandbox = {
  window: win,
  document: {
    addEventListener() {},
    getElementById: (id) => (id === 'movie_player' ? player : null),
    querySelector: (sel) => (String(sel).includes('video.html5-main-video') ? fakeVideo
      : String(sel).includes('ytp-progress-bar') ? progressBar : null),
    createElement: makeEl,
  },
  location: { href: `https://www.youtube.com/watch?v=${VIDEO_ID}` },
  console: { log() {}, warn() {}, error() {} },
  URL, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  chrome: {
    storage: { local: { get: (k, cb) => cb(JSON.parse(JSON.stringify(stored))), set: (o) => { Object.assign(stored, JSON.parse(JSON.stringify(o))); } },
               onChanged: { addListener() {} } },
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
  // instant revisit: the stored result is asked for by id first, with the build, before the caption read is answered
  const order = sent.map((m) => m.type);
  check(order.indexOf('stored') >= 0 && order.indexOf('stored') < order.indexOf('analyze'), `message order: ${order.slice(0, 4)}`);
  check(!!storedUrl && new RegExp(`[?&]v=${VIDEO_ID}(&|$)`).test(storedUrl) && new RegExp(`[?&]build=${builds.background}(&|$)`).test(storedUrl),
        `GET /stored url: ${storedUrl}`);
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
  check(body.capture && body.capture.path === 'B' && body.capture.via === 'retargeted', `POST body.capture = ${JSON.stringify(body.capture)}`);
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
  // marker lane: the skip marker is accent ("will skip") until the segment has been dealt with, then it goes quiet
  const before = markerOf(SEG);
  check(!!before && before.dataset.action === 'skip' && !hasClass(before, 'is-done'), `marker before the skip: ${before && before.className} / ${before && before.dataset.action}`);
  check(/· will skip$/.test(tooltipTime(SEG) || ''), `tooltip before the skip: ${tooltipTime(SEG)}`);
  if (videoHandlers.timeupdate) videoHandlers.timeupdate();
  // what the popup shows: counters in chrome.storage, and this video's segments with their outcome
  const all1 = (stored.stats || {}).all || {};
  check(all1.skips === 1 && Math.abs(all1.saved - (SEG.end - PLAYHEAD)) < 1e-9 && !all1.undos, `stats after the skip: ${JSON.stringify(stored.stats)}`);
  const st1 = tabStateNow() || {};
  const seg1 = (st1.segments || []).find((x) => x.start === SEG.start) || {};
  check(st1.duration === fakeVideo.duration && seg1.effective === 'skip' && seg1.done === 'skipped',
        `getState after the skip: duration ${st1.duration}, segment ${JSON.stringify({ effective: seg1.effective, done: seg1.done })}`);
  const after = markerOf(SEG);
  check(!!after && hasClass(after, 'is-done') && after.dataset.action === 'skip', `marker after the skip is not marked done: ${after && after.className}`);
  check(/· skipped$/.test(tooltipTime(SEG) || ''), `tooltip after the skip: ${tooltipTime(SEG)}`);
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
  const all2 = (stored.stats || {}).all || {};
  check(all2.skips === 1 && all2.saved === 0 && all2.undos === 1, `stats after the undo (the skip still counts, its seconds do not): ${JSON.stringify(stored.stats)}`);
  check((((tabStateNow() || {}).segments || []).find((x) => x.start === SEG.start) || {}).done === 'plays', 'getState after the undo: the segment must read as playing');
  const undone = markerOf(SEG);
  check(!!undone && hasClass(undone, 'is-done'), 'marker went back to accent after the undo (the segment will not skip again, so it must stay quiet)');
  check(/· plays$/.test(tooltipTime(SEG) || ''), `tooltip after the undo: ${tooltipTime(SEG)}`);

  // ---- a sponsor integration: reported once playback runs through its end, not when the viewer seeks out of it
  for (const t of [KEEP.start + 5, KEEP.end - 0.2, KEEP.end + 0.3]) { fakeVideo.currentTime = t; videoHandlers.timeupdate(); }
  check((((stored.stats || {}).all || {}).kept) === 1, `integrations left playing after one was played through: ${JSON.stringify((stored.stats || {}).all)}`);
  fakeVideo.currentTime = KEEP2.start + 5; videoHandlers.timeupdate();
  fakeVideo.currentTime = KEEP2.end + 60; if (videoHandlers.seeking) videoHandlers.seeking();   // the viewer seeks out of it
  videoHandlers.timeupdate();
  fakeVideo.currentTime = KEEP2.end + 0.3; if (videoHandlers.seeking) videoHandlers.seeking(); videoHandlers.timeupdate();
  check((((stored.stats || {}).all || {}).kept) === 1, `an integration the viewer seeked out of was counted: ${JSON.stringify((stored.stats || {}).all)}`);

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
    const played = posts['/event'].filter((e) => e.kind === 'integration_played');
    check(played.length === 1 && played[0].segment && played[0].segment.start === KEEP.start && Math.abs(played[0].playhead - (KEEP.end + 0.3)) < 1e-9,
          `integration_played events: ${JSON.stringify(played)}`);
    const kinds = posts['/event'].map((e) => e.kind);
    check(kinds.includes('skip') && kinds.includes('undo'), `stats events sent: ${JSON.stringify(kinds)}, expected skip and undo`);
    const sk = posts['/event'].find((e) => e.kind === 'skip');
    check(sk && sk.playhead === PLAYHEAD && Math.abs(sk.saved - (SEG.end - PLAYHEAD)) < 1e-9, `skip event = ${JSON.stringify(sk)}`);

    // ---- the removed settings backup leaves nothing behind: no request to the server, its storage keys and badge cleared
    check(!fetchedRoutes.has('/client-state'), 'background.js still talks to /client-state');
    check(['installId', 'restoreChecked', 'restoreOffer'].every((k) => bgRemoved.includes(k)), `leftover storage keys not cleared: ${JSON.stringify(bgRemoved)}`);
    check(bgSandbox.badge === '', 'the toolbar badge was not cleared');
    check([...fetchedRoutes].every((r) => ['/analyze', '/event', '/feedback', '/stored'].includes(r)), `unexpected server routes: ${[...fetchedRoutes]}`);

    if (failures.length) {
      console.log(`wiring: FAIL (${failures.length})`);
      for (const f of failures) console.log(`  - ${f}`);
      process.exit(1);
    }
    console.log(`wiring: PASS  metadata reaches POST /analyze (${Object.keys(body).length} fields, builds ${builds.content}/${serverBuild}); ` +
                `skip, Wrong skip and Missed-ad reports carry the segment and the real playhead; ` +
                `nothing else is sent to the server`);
    process.exit(0);
  }, 50);
}, 50);
