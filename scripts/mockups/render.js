#!/usr/bin/env node
// README mockups: the REAL extension UI (content.js, popup.js, the dashboard) rendered with FICTIONAL data.
//
//   node scripts/mockups/render.js              # every image + docs/demo.gif
//   node scripts/mockups/render.js --no-gif     # PNGs only
//   node scripts/mockups/render.js --only=hero,popup   (hero, tooltip, popup, dashboard, adcard, gif)
//
// Needs: Google Chrome (CHROME=/path to override), Node >= 22 (fetch + WebSocket), python3 with Pillow, ffmpeg (GIF).
// What it does, all in a temp dir that is removed afterwards (never server/cache):
//   1. seed.py writes a fictional stats DB through the server's own code, and the fixture for the player/popup;
//   2. a second server instance (SR_PORT, default 8769) serves the dashboard from that DB;
//   3. a static server serves extension/ as is (only the content-script font URL and the popup's stub <script> are
//      rewritten) plus this folder's mockup pages;
//   4. headless Chrome, driven over the DevTools protocol, renders each shot at 2x;
//   5. optimize.py keeps each PNG under ~300 KB (palette quantization only when needed); ffmpeg makes the GIF.
'use strict';
const { spawn, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const EXT = path.join(ROOT, 'extension');
const HERE = __dirname;
const OUT = path.join(ROOT, 'docs', 'img');
const GIF = path.join(ROOT, 'docs', 'demo.gif');
const arg = (name) => { const a = process.argv.find((x) => x.startsWith(`--${name}`)); return a === undefined ? null : (a.split('=')[1] ?? true); };
const ONLY = arg('only') ? String(arg('only')).split(',') : ['hero', 'tooltip', 'popup', 'dashboard', 'adcard', 'gif'];
if (arg('no-gif')) ONLY.splice(ONLY.indexOf('gif') >>> 0, ONLY.includes('gif') ? 1 : 0);
const SR_PORT = Number(arg('port') || 8769);
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const say = (...a) => console.log('mockups:', ...a);

// ------------------------------------------------------------------ DevTools protocol, one page
class Page {
  static async open(port) {
    const t = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json();
    const p = new Page(t.webSocketDebuggerUrl);
    await p.connected;
    await p.send('Page.enable');
    await p.send('Runtime.enable');
    return p;
  }
  constructor(url) {
    this.ws = new WebSocket(url);
    this.seq = 0; this.pending = new Map(); this.waiters = [];
    this.connected = new Promise((res, rej) => { this.ws.onopen = res; this.ws.onerror = rej; });
    this.ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id && this.pending.has(m.id)) {
        const { res, rej } = this.pending.get(m.id);
        this.pending.delete(m.id);
        if (m.error) rej(new Error(`${m.error.message} ${m.error.data || ''}`)); else res(m.result);
        return;
      }
      if (m.method === 'Runtime.exceptionThrown') console.error('page error:', m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
      if (m.method === 'Runtime.consoleAPICalled' && (m.params.type === 'error' || arg('debug'))) console.error('page console:', m.params.args.map((a) => a.value || a.description).join(' '));
      const keep = [];
      for (const w of this.waiters) (w.method === m.method ? w.res(m.params) : keep.push(w));
      this.waiters = keep;
    };
  }
  send(method, params = {}, ms = 30000) {   // every call has a time limit: a stuck step fails with its name
    const id = ++this.seq;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => {
      const timer = setTimeout(() => { this.pending.delete(id); rej(new Error(`DevTools ${method} timed out`)); }, ms);
      this.pending.set(id, { res: (v) => { clearTimeout(timer); res(v); }, rej: (e) => { clearTimeout(timer); rej(e); } });
    });
  }
  once(method) { return new Promise((res) => this.waiters.push({ method, res })); }
  async goto(url, { width, height, scale = 2, scheme = 'light' }) {
    await this.size(width, height, scale);
    await this.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: scheme }] });
    say(`  page ${url.replace(/^http:\/\/127\.0\.0\.1:\d+/, '')}`);
    const loaded = this.once('Page.loadEventFired');
    const nav = await this.send('Page.navigate', { url });
    // a hash change on the same page (#stats -> #ads) is a same-document navigation: no loaderId, no load event
    if (nav.loaderId) await Promise.race([loaded, sleep(20000).then(() => { throw new Error(`no load event for ${url}`); })]);
    await this.run('document.fonts.ready.then(() => true)');
  }
  size(width, height, scale = 2) { return this.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: scale, mobile: false }); }
  // Runs this script's own JavaScript in the headless page (Runtime.evaluate); nothing from outside is evaluated.
  // Each call has a time limit and a name, so a step that never settles says which one it was.
  async run(expression, ms = 30000) {
    let timer;
    const limit = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`page step timed out: ${expression.slice(0, 80)}`)), ms); });
    const r = await Promise.race([this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }), limit]).finally(() => clearTimeout(timer));
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
  async shot(file, clip) {
    const r = await this.send('Page.captureScreenshot', { format: 'png', ...(clip ? { clip: { ...clip, scale: 1 }, captureBeyondViewport: true } : {}) });
    fs.writeFileSync(file, Buffer.from(r.data, 'base64'));
    return file;
  }
  close() { this.ws.close(); }
}

// ------------------------------------------------------------------ static files: extension/ as is, plus the mockup pages
const TYPES = { '.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.json': 'application/json',
                '.woff2': 'font/woff2', '.png': 'image/png', '.svg': 'image/svg+xml' };
function serveStatic(fixture) {
  const inside = (base, rel) => { const p = path.resolve(base, '.' + rel); return p.startsWith(base + path.sep) ? p : null; };
  const server = http.createServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    let file = null, body = null;
    if (u.pathname === '/watch') file = path.join(HERE, 'player.html');
    else if (u.pathname === '/mock/fixture.js') body = `window.__FIXTURE = ${JSON.stringify(fixture)};\n`;
    else if (u.pathname.startsWith('/mock/')) file = inside(HERE, u.pathname.slice(5));
    else if (u.pathname.startsWith('/ext/')) file = inside(EXT, u.pathname.slice(4));
    if (!body && (!file || !fs.existsSync(file))) { res.writeHead(404); res.end(); return; }
    if (!body) {
      body = fs.readFileSync(file);
      const rel = file.startsWith(EXT) ? path.relative(EXT, file) : '';
      // the content script's faces are chrome-extension:// URLs (Chrome fills in the id): serve them from here
      if (rel === path.join('ui', 'fonts-content.css')) body = String(body).replaceAll('chrome-extension://__MSG_@@extension_id__/', '/ext/');
      // the popup, with chrome.* answered by the stub (popup.js itself is untouched)
      if (rel === 'popup.html') {
        const tag = '<script src="popup.js"></script>';
        if (!String(body).includes(tag)) throw new Error('popup.html: script tag not found');
        body = String(body).replace(tag, `<script src="/mock/fixture.js"></script><script src="/mock/popup-stub.js"></script>${tag}`);
      }
    }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file || '.js')] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    res.end(body);
  });
  return new Promise((r) => server.listen(0, '127.0.0.1', () => r(server)));
}

// ------------------------------------------------------------------ helpers
async function waitFor(fn, what, ms = 30000) {
  const t0 = Date.now();
  for (;;) {
    try { const v = await fn(); if (v) return v; } catch { /* not yet */ }
    if (Date.now() - t0 > ms) throw new Error(`timed out waiting for ${what}`);
    await sleep(200);
  }
}
const api = (p) => fetch(`http://127.0.0.1:${SR_PORT}${p}`).then((r) => { if (!r.ok) throw new Error(`${p}: ${r.status}`); return r.json(); });

async function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sr-mockups-'));
  const data = path.join(tmp, 'data');
  fs.mkdirSync(data);
  fs.mkdirSync(OUT, { recursive: true });
  const kids = [];
  const cleanup = async () => {
    await Promise.all(kids.map((k) => new Promise((r) => { if (k.exitCode !== null || k.signalCode) return r(); k.once('exit', r); k.kill('SIGTERM'); setTimeout(r, 5000); })));
    if (!arg('keep')) fs.rmSync(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  };
  process.on('SIGINT', () => { cleanup().finally(() => process.exit(130)); });
  try {
    // 1. fictional data
    const env = { ...Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith('SR_') && !k.startsWith('SS_'))), SR_DATA_DIR: data };
    say(execFileSync('python3', [path.join(HERE, 'seed.py'), path.join(tmp, 'fixture.json')], { env, encoding: 'utf8' }).trim());
    const fixture = JSON.parse(fs.readFileSync(path.join(tmp, 'fixture.json'), 'utf8'));
    fixture.start = 338;
    fixture.manifestVersion = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8')).version;

    // 2. the dashboard's server, on its own port and data dir
    try { await api('/health'); throw new Error(`port ${SR_PORT} is already in use (pass --port=N)`); } catch (e) { if (String(e.message).includes('in use')) throw e; }
    const srv = spawn('python3', [path.join(ROOT, 'server', 'server.py')], { env: { ...env, SR_PORT: String(SR_PORT) },
      stdio: ['ignore', fs.openSync(path.join(tmp, 'server.log'), 'w'), 'inherit'] });
    kids.push(srv);
    await waitFor(() => api('/health').then((h) => h.ok), 'the mockup server');
    fixture.overall = (await api('/stats.json')).overall;
    fixture.codes = (await api('/codes.json')).codes;
    say(`server on ${SR_PORT}: ${fixture.overall.skips} skips, ${fixture.overall.saved_s} s saved, ${fixture.codes.length} codes`);

    // 3. pages
    const web = await serveStatic(fixture);
    const base = `http://127.0.0.1:${web.address().port}`;

    // 4. Chrome
    const profile = path.join(tmp, 'chrome');
    const chrome = spawn(CHROME, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run',
      '--no-default-browser-check', '--hide-scrollbars', '--mute-audio', '--force-color-profile=srgb', 'about:blank'], { stdio: 'ignore' });
    kids.push(chrome);
    const port = await waitFor(() => fs.readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0], 'Chrome');
    const page = await Page.open(port);
    const shots = [];
    const watch = `${base}/watch?v=${fixture.video.video_id}${arg("debug") ? "&debug=1" : ""}`;

    // player: the skip moment. Playback crosses the read's start; content.js seeks, settles the marker, shows the toast.
    if (ONLY.includes('hero') || ONLY.includes('popup')) {
      await page.goto(watch, { width: 1280, height: 720 });
      say('player markers:', await page.run('__mock.ready()'));
      await page.run('__mock.jump(366); __mock.play(372.4)');
      await page.run('__mock.advance(900)');   // the toast's enter animation, on the page's virtual clock
      await page.run('__mock.play(431.5)');
      if (arg('debug')) say('debug:', JSON.stringify(await page.run(`({ now: __mock.now(), toast: !!document.querySelector('.sr-toast'),
        markers: [...document.querySelectorAll('.sr-lane .sr-marker')].map((m) => m.className + ' ' + m.dataset.action) })`)));
      if (ONLY.includes('hero')) shots.push(await page.shot(path.join(OUT, 'player-hero.png')));
      fixture.tabState = await page.run('__mock.state()');   // what the popup is told about this tab
    }
    // player: the lane close-up, the read's tooltip before it plays
    if (ONLY.includes('tooltip')) {
      await page.goto(watch, { width: 1280, height: 720 });
      await page.run('__mock.ready()');
      await page.run('__mock.hover(0); __mock.advance(400)');
      const tip = await page.run('__mock.rect(".sr-tooltip")');
      const w = 760, x = Math.max(0, Math.min(1280 - w, tip.x + tip.width / 2 - w / 2)), y = Math.max(0, tip.y - 36);
      shots.push(await page.shot(path.join(OUT, 'player-tooltip.png'), { x, y, width: w, height: 720 - y }));
    }
    // popup: the real popup.html, 360 wide, as tall as its content
    if (ONLY.includes('popup')) {
      await page.goto(`${base}/ext/popup.html`, { width: 360, height: 600 });
      await waitFor(() => page.run(`document.getElementById('savedAll').textContent !== '0:00' && !document.getElementById('video').hidden && document.querySelectorAll('#videoDeal li').length > 0`), 'the popup to fill in');
      await page.run('new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))');
      const h = await page.run('Math.ceil(document.getElementById("app").getBoundingClientRect().height)');
      await page.size(360, h);
      shots.push(await page.shot(path.join(OUT, 'popup.png')));
    }
    // dashboard: the Stats tab (top of the page) and the first ad card
    if (ONLY.includes('dashboard') || ONLY.includes('adcard')) {
      const dash = `http://127.0.0.1:${SR_PORT}/dashboard`;
      if (ONLY.includes('dashboard')) {
        await page.goto(`${dash}#stats`, { width: 1280, height: 1300 });
        await waitFor(() => page.run(`document.querySelectorAll('.sr-table').length > 0`), 'the stats tab');
        await sleep(300);
        // down to the end of the category table: the brand and creator tables below run on for pages
        const h = await page.run(`(() => { const t = document.querySelectorAll('.sr-table')[0].closest('.sr-card').getBoundingClientRect(); return Math.ceil(t.bottom + scrollY + 20); })()`);
        shots.push(await page.shot(path.join(OUT, 'dashboard-stats.png'), { x: 0, y: 0, width: 1280, height: h }));
      }
      if (ONLY.includes('adcard')) {
        await page.goto(`${dash}#ads`, { width: 680, height: 1300 });   // one card per row: no neighbour in the crop
        await waitFor(() => page.run(`document.querySelectorAll('.sr-adcard').length > 0`), 'the ad cards');
        await sleep(300);
        // the hero video's skipped read, the one the player and popup shots show
        const r = await page.run(`(() => { const c = [...document.querySelectorAll('.sr-adcard')].find((e) => e.querySelector('.sr-adcard__title').textContent === ${JSON.stringify(fixture.segments[0].brand_canonical)} && e.textContent.includes(${JSON.stringify(fixture.video.title)}));
          for (const e of document.querySelectorAll('.sr-adcard')) if (e !== c) e.style.visibility = 'hidden';   // no neighbour edges in the crop
          const r = c.getBoundingClientRect(); return { x: r.left, y: r.top + scrollY, width: r.width, height: r.height }; })()`);
        const m = 16;
        shots.push(await page.shot(path.join(OUT, 'dashboard-adcard.png'), { x: r.x - m, y: r.y - m, width: r.width + 2 * m, height: r.height + 2 * m }));
      }
    }
    // the GIF: playback reaches the read, jumps over it, the toast comes in and the marker goes quiet
    if (ONLY.includes('gif')) {
      const frames = path.join(tmp, 'frames');
      fs.mkdirSync(frames);
      await page.goto(watch, { width: 1280, height: 720, scale: 1 });
      await page.run('__mock.ready()');
      await page.run('__mock.jump(362.5)');
      const FPS = 12, dt = 1000 / FPS;
      let n = 0, t = 362.5;
      const frame = async () => page.shot(path.join(frames, `f${String(n++).padStart(3, '0')}.png`));
      for (let i = 0; i < 12; i++) await frame();                        // 1 s still
      while (t < 372.4) {                                                  // ~3 s of playback at 3x
        t = Math.min(372.4, t + 0.25);
        await page.run(`__mock.play(${t}).then(() => __mock.advance(${dt}))`);
        await frame();
        if (await page.run('__mock.now()') > 400) break;                  // the skip happened
      }
      t = await page.run('__mock.now()');
      for (let i = 0; i < 46; i++) {                                       // ~4 s after the skip, playing at 1x
        t += 1 / FPS;
        await page.run(`__mock.play(${t.toFixed(3)}).then(() => __mock.advance(${dt}))`);
        await frame();
      }
      execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-framerate', String(FPS), '-i', path.join(frames, 'f%03d.png'),
        '-vf', 'split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle',
        '-loop', '0', GIF]);
      say(`demo.gif: ${n} frames, ${(fs.statSync(GIF).size / 1024).toFixed(0)} KB`);
    }
    page.close();
    web.close();

    // 5. sizes
    if (shots.length) say(execFileSync('python3', [path.join(HERE, 'optimize.py'), ...shots], { encoding: 'utf8' }).trim());
  } finally {
    await cleanup();
  }
}

main().catch((e) => { console.error('mockups: failed:', e.message); process.exit(1); });
