import { test, expect, type Page, type Locator } from '@playwright/test';
import { execFile, execFileSync, spawn, type ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as net from 'node:net';
import * as os from 'node:os';
import * as path from 'node:path';
import { promisify } from 'node:util';

/**
 * Records every browser scene of docs/VIDEO-SCRIPT.md (1920x1080, video on, visible callouts).
 *
 * Fully local and throwaway: it starts its own PostgreSQL (UTF8, 127.0.0.1 only, no unix socket, database name ending in
 * `test`) and CityPulse (role `all`, city `paris`) on free ports taken from CITYPULSE_PORT_RANGE (default 41500-41599), in a
 * temp directory (under E2E_SCRATCH when set) that is deleted at the end. A fresh database for every take.
 * It never reads a .env or key file and never touches a running stack.
 *
 *   cd docs/video && npm install && python3 build.py            (the normal way: narration, recording and mux in one command)
 *   RECORD_NO_VIDEO=1 RECORD_WARMUP_S=45 RECORD_PACE=0.3 npx playwright test    (quick rehearsal without narration)
 *
 * Narration cut (driven by build.py): SCENE_INFO=<durations.json> makes every scene follow the measured audio (each action starts when the
 * narration sentence it illustrates begins), SCENE_DURATIONS=<json> gives the minimum length of each scene (audio + 0.7 s), TIMINGS_OUT=<file>
 * receives the scene start times (wall clock), the clock calibration for the video and the checks made on the live burst.
 *
 * Other knobs: RECORD_WARMUP_S (default 300, seconds the fleet reports before filming starts), RECORD_PACE (default 1; multiplies pauses),
 * RECORD_PG_BIN, RECORD_NO_VIDEO=1, RECORD_CITYPULSE_BIN (default ../../.venv/bin/citypulse), RECORD_SIM_SPEED (default 1), HEADED=1.
 *
 * Terminal scenes (2, 5, 8, 10) are drawn as an on-page terminal card that runs the REAL commands (curl against the local app).
 * Scene 2 is recorded first (the Overview needs a signed-in session) and scene 1 second; build.py puts scene 1 at the front.
 */

const exec = promisify(execFile);
const PACE = Number(process.env.RECORD_PACE ?? '1');
const WARMUP_S = Number(process.env.RECORD_WARMUP_S ?? '300');
const DEMO_PASSWORD = 'citypulse-demo-2026';
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const pause = (ms: number) => sleep(Math.round(ms * PACE));

// ---------------------------------------------------------------- narration timing (only active when build.py sets SCENE_INFO)

const readJson = (f?: string) => (f ? JSON.parse(fs.readFileSync(f, 'utf8')) : null);
const INFO: Record<string, { audio: number; offsets: number[] }> | null = readJson(process.env.SCENE_INFO);
const MIN: Record<string, number> | null = readJson(process.env.SCENE_DURATIONS);
const LEAD = 0.25; // an on-screen change a little before the sentence it illustrates
const marks: Record<number, number> = {};
const checks: string[] = [];
let curScene = 0;
let curT0 = 0;

async function scene(n: number) {
  await endScene();
  curScene = n;
  curT0 = Date.now();
  marks[n] = curT0;
}
async function endScene() {
  if (!curScene || !MIN) return;
  const need = MIN[String(curScene)] * 1000 - (Date.now() - curT0);
  if (need > 0) await sleep(need);
  else console.log(`scene ${curScene}: actions overran the minimum by ${(-need / 1000).toFixed(1)} s`);
}
const cueMs = (k: number) => {
  const off = INFO![String(curScene)].offsets[k];
  if (off === undefined) throw new Error(`scene ${curScene} has no narration sentence ${k}`);
  return curT0 + (off - LEAD) * 1000;
};
/** Wait until narration sentence k of the current scene begins (rehearsal mode: a short pause). */
async function at(k: number, fallbackMs = 1000) {
  if (!INFO) { await pause(fallbackMs); return; }
  const wait = cueMs(k) - Date.now();
  if (wait > 0) await sleep(wait);
  else if (wait < -1000) console.log(`scene ${curScene}: sentence ${k} reached ${(-wait / 1000).toFixed(1)} s late`);
}
const until = (k: number, fallbackMs = 2500) => () => at(k, fallbackMs);
/** Hold until the narration of the current scene is nearly finished. */
const holdEnd = (fallbackMs = 2500) => async () => {
  if (!INFO) { await pause(fallbackMs); return; }
  const wait = curT0 + INFO[String(curScene)].audio * 1000 - 400 - Date.now();
  if (wait > 0) await sleep(wait);
};
const early = (k: number) => !INFO || Date.now() < cueMs(k);

// ---------------------------------------------------------------- local stack ----------------------------------------

const used = new Set<number>();
function tryPort(port: number): Promise<number | undefined> {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.unref();
    s.on('error', () => resolve(undefined));
    s.listen(port, '127.0.0.1', () => {
      const got = (s.address() as net.AddressInfo).port;
      s.close(() => resolve(got));
    });
  });
}
async function freePort(): Promise<number> {
  const m = /^(\d+)-(\d+)$/.exec(process.env.CITYPULSE_PORT_RANGE ?? '41500-41599');
  if (!m) throw new Error('CITYPULSE_PORT_RANGE must look like 41500-41599');
  const lo = Number(m[1]);
  const hi = Number(m[2]);
  for (let i = 0; i < 300; i++) {
    const port = lo + Math.floor(Math.random() * (hi - lo + 1));
    if (used.has(port)) continue;
    if (await tryPort(port)) { used.add(port); return port; }
  }
  throw new Error(`no free port in ${lo}-${hi}`);
}

function findPgBin(): string {
  if (process.env.RECORD_PG_BIN) return process.env.RECORD_PG_BIN;
  try {
    return path.dirname(execFileSync('/usr/bin/which', ['pg_ctl'], { encoding: 'utf8' }).trim());
  } catch {
    throw new Error('PostgreSQL binaries not found: set RECORD_PG_BIN to the folder that holds initdb and postgres');
  }
}

interface Stack {
  url: string;
  logs(): string;
  stop(): Promise<void>;
}

async function startStack(): Promise<Stack> {
  const pgBin = findPgBin();
  const dir = fs.mkdtempSync(path.join(process.env.E2E_SCRATCH || os.tmpdir(), 'citypulse-video-'));
  const data = path.join(dir, 'pgdata');
  const pgPort = await freePort();
  const pgEnv = { PATH: `${pgBin}:/usr/bin:/bin`, LANG: 'C', LC_ALL: 'C' };
  execFileSync(path.join(pgBin, 'initdb'), ['-D', data, '-E', 'UTF8', '--locale=C', '-A', 'trust', '-U', 'postgres'], { env: pgEnv, stdio: 'pipe' });
  const pgLog = fs.openSync(path.join(dir, 'postgres.log'), 'a');
  const pg: ChildProcess = spawn(
    path.join(pgBin, 'postgres'),
    ['-D', data, '-c', 'listen_addresses=127.0.0.1', '-c', `port=${pgPort}`, '-c', 'unix_socket_directories=', '-c', 'fsync=off', '-c', 'max_connections=200'],
    { env: pgEnv, stdio: ['ignore', pgLog, pgLog] },
  );
  const psql = (db: string, sql: string) =>
    execFileSync(path.join(pgBin, 'psql'), ['-h', '127.0.0.1', '-p', String(pgPort), '-U', 'postgres', '-d', db, '-v', 'ON_ERROR_STOP=1', '-c', sql], { env: pgEnv, stdio: 'pipe' });
  for (let i = 0; ; i++) {
    try {
      execFileSync(path.join(pgBin, 'pg_isready'), ['-h', '127.0.0.1', '-p', String(pgPort)], { env: pgEnv, stdio: 'pipe' });
      break;
    } catch {
      if (i > 120) throw new Error('postgres did not start');
      await sleep(250);
    }
  }
  psql('postgres', "CREATE DATABASE citypulse_recording_test ENCODING 'UTF8' TEMPLATE template0");

  const venvBin = path.resolve(__dirname, '..', '..', '.venv', 'bin', 'citypulse');
  const bin = process.env.RECORD_CITYPULSE_BIN || (fs.existsSync(venvBin) ? venvBin : 'citypulse');
  const port = await freePort();
  const env: Record<string, string> = {
    PATH: '/usr/bin:/bin',
    HOME: dir,
    TMPDIR: dir,
    LANG: 'en_US.UTF-8',
    PYTHONUNBUFFERED: '1',
    PYTHONDONTWRITEBYTECODE: '1',
    NO_COLOR: '1',
    HOST: '127.0.0.1',
    PORT: String(port),
    DATABASE_URL: `postgresql://postgres@127.0.0.1:${pgPort}/citypulse_recording_test`,
    CITYPULSE_ROLE: 'all',
    CITYPULSE_CITY: 'paris',
    SIM_SENSORS: '3000',
    SIM_SPEED: process.env.RECORD_SIM_SPEED ?? '1',
  };
  let buf = '';
  const app = spawn(bin, ['start'], { env, cwd: dir, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
  app.stdout!.on('data', (d) => (buf += d));
  app.stderr!.on('data', (d) => (buf += d));
  const url = `http://127.0.0.1:${port}`;
  for (let i = 0; ; i++) {
    try {
      if ((await fetch(url + '/readyz')).ok) break;
    } catch {
      /* not up yet */
    }
    if (i > 240) throw new Error('citypulse did not become ready\n' + buf.slice(-2000));
    await sleep(500);
  }
  return {
    url,
    logs: () => buf,
    async stop() {
      try { process.kill(-app.pid!, 'SIGTERM'); } catch { /* already gone */ }
      await sleep(1500);
      try { process.kill(-app.pid!, 'SIGKILL'); } catch { /* already gone */ }
      try { pg.kill('SIGINT'); } catch { /* already gone */ }
      await sleep(1000);
      try { pg.kill('SIGKILL'); } catch { /* already gone */ }
      fs.rmSync(dir, { recursive: true, force: true });
    },
  };
}

// ---------------------------------------------------------------- on-screen helpers ----------------------------------------
// The app's CSP forbids inline style attributes in markup, but styles set through the CSSOM (element.style.x) are allowed,
// and page.evaluate bypasses the page CSP. Everything below is injected, never part of the product.

const INIT = () => {
  const css = (el: HTMLElement, o: Record<string, string>) => Object.assign(el.style, o);
  const layer = () => {
    let l = document.getElementById('rec-layer');
    if (!l) {
      l = document.createElement('div');
      l.id = 'rec-layer';
      css(l, { position: 'fixed', inset: '0', zIndex: '2147483000', pointerEvents: 'none' });
      document.documentElement.append(l);
    }
    return l;
  };
  addEventListener('DOMContentLoaded', () => {
    const dot = document.createElement('div');
    dot.id = 'rec-cursor';
    css(dot, { position: 'fixed', width: '26px', height: '26px', borderRadius: '50%', border: '3px solid #fff', background: 'rgba(37,99,235,.55)',
      boxShadow: '0 0 0 2px #1d4ed8, 0 2px 8px rgba(0,0,0,.4)', transform: 'translate(-50%,-50%)', left: '-50px', top: '-50px', transition: 'left .05s linear, top .05s linear' });
    layer().append(dot);
    addEventListener('mousemove', (e) => css(dot, { left: `${e.clientX}px`, top: `${e.clientY}px` }), true);
    addEventListener('mousedown', () => css(dot, { background: 'rgba(220,38,38,.7)' }), true);
    addEventListener('mouseup', () => css(dot, { background: 'rgba(37,99,235,.55)' }), true);
    // Tokens (cp_...) are never legible in the video: blur them the moment they are rendered.
    new MutationObserver(() => {
      document.querySelectorAll('.codewrap code').forEach((n) => {
        if (/^\s*cp_/.test(n.textContent || '')) (n as HTMLElement).style.filter = 'blur(9px)';
      });
    }).observe(document, { childList: true, subtree: true, characterData: true });
  });
};

let mx = 960;
let my: number = 540;
/** A visible, continuous cursor movement lasting about `sec` seconds. */
async function glide(page: Page, x: number, y: number, sec: number) {
  const n = Math.max(2, Math.round(sec * 20));
  const x0 = mx;
  const y0 = my;
  for (let i = 1; i <= n; i++) {
    const t = i / n;
    const e = t * t * (3 - 2 * t);
    mx = x0 + (x - x0) * e;
    my = y0 + (y - y0) * e;
    await page.mouse.move(mx, my);
    await sleep(Math.round(sec * 1000 / n));
  }
}

/** The Overview redraws every 5 seconds, so an element can be replaced between lookup and use: retry on a fresh lookup. */
async function box(loc: Locator) {
  let last: unknown;
  for (let i = 0; i < 6; i++) {
    try {
      await loc.scrollIntoViewIfNeeded();
      const b = await loc.boundingBox();
      if (b) return b;
    } catch (e) {
      last = e;
    }
    await sleep(400);
  }
  throw new Error(`element has no box: ${String(last)}`);
}

async function moveTo(page: Page, loc: Locator, dx = 0, dy = 0, sec = 0.7) {
  const b = await box(loc);
  await glide(page, b.x + b.width / 2 + dx, b.y + b.height / 2 + dy, sec);
}

async function click(page: Page, loc: Locator) {
  await moveTo(page, loc);
  await pause(300);
  await page.mouse.down();
  await sleep(90);
  await page.mouse.up();
}

async function type(page: Page, loc: Locator, text: string, delay = 90) {
  await click(page, loc);
  await page.keyboard.type(text, { delay: delay * Math.max(PACE, 0.2) });
}

/** Keep the cursor moving over the given targets until narration sentence k begins (a few moves in rehearsal mode). */
async function wander(page: Page, targets: Locator[], k: number) {
  let i = 0;
  while (early(k) && (INFO || i < 3)) {
    const b = await box(targets[i % targets.length]);
    const rx = b.x + b.width * (0.2 + 0.6 * ((i * 0.37) % 1));
    const ry = b.y + b.height * (0.3 + 0.4 * ((i * 0.61) % 1));
    await glide(page, rx, ry, 1.6);
    i++;
  }
  if (INFO) await at(k);
}

/** A rectangle with a caption around an element (or only a lower-third caption when no element is given); held until `hold` returns. */
async function callout(page: Page, text: string, target?: Locator, hold: number | (() => Promise<void>) = 3200) {
  const b = target ? await box(target) : null;
  await page.evaluate(({ text, b }) => {
    const css = (el: HTMLElement, o: Record<string, string>) => Object.assign(el.style, o);
    const layer = document.getElementById('rec-layer')!;
    const wrap = document.createElement('div');
    wrap.className = 'rec-callout';
    if (b) {
      const ring = document.createElement('div');
      css(ring, { position: 'fixed', left: `${b.x - 8}px`, top: `${b.y - 8}px`, width: `${b.width + 16}px`, height: `${b.height + 16}px`,
        border: '4px solid #f59e0b', borderRadius: '10px', boxShadow: '0 0 0 9999px rgba(15,23,42,.18)' });
      wrap.append(ring);
    }
    const chip = document.createElement('div');
    chip.textContent = text;
    const below = b && b.y + b.height + 70 < innerHeight;
    css(chip, { position: 'fixed', background: '#0f172a', color: '#fff', font: '600 26px system-ui, sans-serif', padding: '12px 20px', borderRadius: '10px',
      boxShadow: '0 4px 18px rgba(0,0,0,.35)', maxWidth: '900px',
      ...(b ? { left: `${Math.max(16, Math.min(b.x, innerWidth - 920))}px`, top: below ? `${b.y + b.height + 16}px` : `${Math.max(16, b.y - 64)}px` }
        : { left: '48px', bottom: '48px' }) });
    wrap.append(chip);
    layer.append(wrap);
  }, { text, b });
  if (typeof hold === 'function') await hold(); else await pause(hold);
  await page.evaluate(() => document.querySelectorAll('.rec-callout').forEach((n) => n.remove()));
}

async function showCard(page: Page, title: string, lines: string[], dark = true) {
  await page.evaluate(({ title, lines, dark }) => {
    document.getElementById('rec-card')?.remove();
    const css = (el: HTMLElement, o: Record<string, string>) => Object.assign(el.style, o);
    const el = document.createElement('div');
    el.id = 'rec-card';
    css(el, { position: 'fixed', inset: '0', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '18px',
      background: dark ? '#0b1220' : '#fff', color: dark ? '#fff' : '#0f172a', font: '600 40px system-ui, sans-serif', textAlign: 'center' });
    if (title) {
      const h = document.createElement('div');
      h.textContent = title;
      css(h, { font: '800 72px system-ui, sans-serif' });
      el.append(h);
    }
    for (const l of lines) {
      const p = document.createElement('div');
      p.textContent = l;
      css(p, { opacity: '.9' });
      el.append(p);
    }
    document.getElementById('rec-layer')!.append(el);
  }, { title, lines, dark });
}
const hideCard = (page: Page) => page.evaluate(() => document.getElementById('rec-card')?.remove());

/** The ecosystem diagram: Simulator -> CityPulse -> PagerLens, identity beside CityPulse; `stage` lights up more of it. */
async function diagram(page: Page, stage: 1 | 2 | 3) {
  await page.evaluate((stage) => {
    document.getElementById('rec-card')?.remove();
    const css = (el: HTMLElement, o: Record<string, string>) => Object.assign(el.style, o);
    const root = document.createElement('div');
    root.id = 'rec-card';
    css(root, { position: 'fixed', inset: '0', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '40px',
      background: '#0b1220', color: '#fff', font: '600 34px system-ui, sans-serif' });
    const node = (t: string, sub: string, on: boolean, hot = false) => {
      const d = document.createElement('div');
      d.textContent = t;
      const s = document.createElement('div');
      s.textContent = sub;
      css(s, { font: '400 22px system-ui, sans-serif', color: '#94a3b8', marginTop: '6px' });
      d.append(s);
      css(d, { border: `4px solid ${hot ? '#f59e0b' : '#38bdf8'}`, borderRadius: '16px', padding: '22px 36px', minWidth: '300px', textAlign: 'center', opacity: on ? '1' : '.18' });
      return d;
    };
    const arrow = (on: boolean) => {
      const a = document.createElement('div');
      a.textContent = '→';
      css(a, { fontSize: '64px', opacity: on ? '1' : '.18' });
      return a;
    };
    const row = document.createElement('div');
    css(row, { display: 'flex', alignItems: 'center', gap: '30px' });
    row.append(node('CityPulse Simulator', 'feeds it sensors', stage >= 1, stage === 1), arrow(stage >= 1), node('CityPulse', 'sensors, verdict, bursts', true), arrow(stage >= 3), node('PagerLens', 'opens and investigates the incident', stage >= 3, stage === 3));
    const idp = node('Identity provider', 'signs people in: admin, operator, viewer', stage >= 2, stage === 2);
    root.append(row, idp);
    document.getElementById('rec-layer')!.append(root);
  }, stage);
}

/** A terminal card: types the command, then shows the real output. */
class Term {
  constructor(private page: Page) {}
  async open(title: string) {
    await this.page.evaluate((title) => {
      const css = (el: HTMLElement, o: Record<string, string>) => Object.assign(el.style, o);
      const t = document.createElement('div');
      t.id = 'rec-term';
      css(t, { position: 'fixed', left: '120px', right: '120px', bottom: '70px', top: '170px', background: '#0b1220', color: '#e2e8f0', borderRadius: '14px',
        boxShadow: '0 12px 50px rgba(0,0,0,.55)', display: 'flex', flexDirection: 'column', overflow: 'hidden' });
      const bar = document.createElement('div');
      bar.textContent = title;
      css(bar, { background: '#1e293b', padding: '10px 16px', font: '600 20px system-ui, sans-serif', color: '#94a3b8' });
      const body = document.createElement('pre');
      body.id = 'rec-term-body';
      css(body, { margin: '0', padding: '20px 24px', font: '24px/1.45 ui-monospace, Menlo, monospace', whiteSpace: 'pre-wrap', overflow: 'hidden', flex: '1' });
      t.append(bar, body);
      document.getElementById('rec-layer')!.append(t);
    }, title);
  }
  async append(text: string, color?: string) {
    await this.page.evaluate(({ text, color }) => {
      const b = document.getElementById('rec-term-body')!;
      const s = document.createElement('span');
      s.textContent = text;
      if (color) s.style.color = color;
      b.append(s);
    }, { text, color });
  }
  async typeCmd(cmd: string) {
    await this.append('$ ', '#38bdf8');
    for (const ch of cmd) {
      await this.append(ch);
      await sleep(Math.round(28 * Math.max(PACE, 0.2)));
    }
    await this.append('\n');
    await pause(400);
  }
  async out(output: string, color?: string) {
    await this.append(output.trimEnd() + '\n\n', color);
  }
  async run(cmd: string, output: string) {
    await this.typeCmd(cmd);
    await this.out(output);
  }
  async close() {
    await this.page.evaluate(() => document.getElementById('rec-term')?.remove());
  }
}

async function curl(args: string[], env: Record<string, string> = {}): Promise<string> {
  const { stdout } = await exec('curl', args, { env: { PATH: '/usr/bin:/bin', ...env } });
  return stdout;
}

// ---------------------------------------------------------------- the recording --------------------------------------

test('record docs/VIDEO-SCRIPT.md', async ({ browser }) => {
  const outDir = path.resolve(__dirname, 'out');
  fs.mkdirSync(outDir, { recursive: true });
  const stack = await startStack();
  let calib = 0;
  try {
    // Off camera: the fleet reports while nobody is signed in (the simulator runs from the first second).
    await sleep(WARMUP_S * 1000);

    const context = await browser.newContext({
      viewport: { width: 1920, height: 1080 },
      deviceScaleFactor: 1,
      ...(process.env.RECORD_NO_VIDEO ? {} : { recordVideo: { dir: process.env.VIDEO_DIR || outDir, size: { width: 1920, height: 1080 } } }),
    });
    await context.addInitScript(INIT);
    const page = await context.newPage();
    const nav = (tab: string) => page.locator(`a[data-tab="${tab}"]`);
    const term = new Term(page);
    const banner = page.locator('.status-banner');
    const kpis = page.locator('.kpis');
    const heat = page.locator('.heat');
    const chart1 = page.locator('.chartbox').first();

    await page.goto(stack.url + '/');
    await expect(page.getByRole('heading', { name: /Set up CityPulse/ })).toBeVisible();
    // Clock calibration for the editor: a dark frame whose video time build.py can find.
    await showCard(page, '', []);
    calib = Date.now();
    await sleep(700);
    await hideCard(page);
    await sleep(300);

    // ---- Scene 2 (recorded first: the Overview needs a signed-in session; build.py cuts scene 1 to the front)
    await scene(2);
    await at(0, 500);
    const code = stack.logs().match(/^[ \t]{2,}([A-Za-z0-9]{3,8}(?:-[A-Za-z0-9]{3,8}){1,4})[ \t]*$/m)?.[1];
    expect(code, 'the setup code in the server log').toBeTruthy();
    await term.open('Terminal: ~/CityPulse');
    await term.typeCmd('docker compose up --build -d');
    await term.out(' Container citypulse-api-1  Started');
    await at(2);
    await term.typeCmd('docker compose logs api | grep -A2 "first-run setup"');
    await term.out(`=== CityPulse first-run setup ===\nOpen the app in your browser and enter this one-time code to create the admin password:\n    ${code}`);
    await at(3);
    await callout(page, 'One-time code, memory only.', page.locator('#rec-term'), until(4, 3000));
    await term.close();
    await type(page, page.locator('#setup-code'), code!);
    await at(5);
    await callout(page, 'Password: 12+ characters.', page.locator('#setup-pw'), 1800);
    await type(page, page.locator('#setup-pw'), DEMO_PASSWORD, 60);
    await type(page, page.locator('#setup-pw2'), DEMO_PASSWORD, 60);
    await at(8);
    await click(page, page.locator('#auth-card button[type="submit"], #auth-card button.primary').first());
    await expect(page.locator('#nav')).toBeVisible();

    // ---- Scene 1: title card, then the full Overview and a slow sweep (cut to the front of the video)
    await scene(1);
    await showCard(page, 'CityPulse on Postgres', ['Sample project on Postgres, extensions optional.']);
    await expect(banner).toBeVisible();
    await expect(page.locator('.heat td button:not(.none)').first()).toBeVisible();
    await at(2, 800);
    await hideCard(page);
    await moveTo(page, banner, -300, 0, 0.8);
    await moveTo(page, banner, 300, 0, 1.6);
    await at(4);
    await moveTo(page, heat, -250, 0, 1.2);
    await moveTo(page, heat, 250, 60, 2.2);
    await at(5);
    await callout(page, 'Sample project on Postgres, extensions optional.', undefined, until(7));
    await wander(page, [chart1, heat], 7);
    await moveTo(page, banner, 0, 0, 1);

    // ---- Scene 3: read the Overview (a heatmap cell opening Sensors is its last beat)
    await scene(3);
    await at(0);
    await click(page, nav('overview'));
    await expect(banner).toBeVisible();
    await moveTo(page, page.locator('.status-banner .big'));
    await at(2);
    await callout(page, 'Operational: 97% healthy or more', banner, until(4));
    await callout(page, 'Degraded: below 97%', banner, until(5));
    await callout(page, 'Partial outage: below 85%', banner, until(6));
    await callout(page, 'Major outage: below 50%', banner, until(7));
    await moveTo(page, kpis, -300, 0);
    await callout(page, 'Each tile counts sensors by state', kpis, until(8));
    await callout(page, 'stale = 3 missed reports, offline = 12', kpis, until(9));
    await callout(page, 'Fault: a physically impossible value', kpis, until(10));
    await callout(page, 'Stuck: the same number again and again', kpis, until(11));
    const cell = page.locator('.heat td button:not(.none)').first();
    await moveTo(page, heat, -200, 0, 0.8);
    await at(12);
    await moveTo(page, cell);
    const cellTitle = (await cell.getAttribute('title')) ?? '';
    expect(cellTitle).toMatch(/healthy/);
    await callout(page, cellTitle, cell, until(13));
    await moveTo(page, chart1, 0, 0, 1);
    await callout(page, 'Last hour: health and incoming readings', chart1, until(14));
    await callout(page, 'Re-judged every 10 s.', heat, until(16));
    await click(page, cell);
    await expect(page).toHaveURL(/#sensors\/[^/]+\/[^/]+/);
    await expect(page.locator('#f-type')).not.toHaveValue('');
    await callout(page, 'A heatmap cell opens its sensors.', page.locator('#f-type'), holdEnd());

    // ---- Scene 4: Sensors
    await scene(4);
    await at(0);
    await wander(page, [page.locator('#f-type')], 1);
    await page.locator('#f-state').selectOption('problem');
    await page.locator('#f-type').selectOption('');
    await page.locator('#f-district').selectOption('');
    await page.locator('#f-q').fill('');
    await pause(500);
    const emptyMsg = await page.locator('#view-sensors .empty').textContent().catch(() => null);
    await callout(page, 'Default filter: problems only.', page.locator('#f-state'), until(3));
    await click(page, page.locator('#f-state'));
    await page.locator('#f-state').selectOption('all');
    await pause(500);
    await type(page, page.locator('#f-q'), 'temperature');
    await expect(page.locator('#view-sensors tbody tr.clickable').first()).toBeVisible();
    await at(4);
    await click(page, page.locator('#view-sensors tbody tr.clickable').first());
    await expect(page.locator('.facts')).toBeVisible();
    await callout(page, 'Last readings, and why it was judged so', page.locator('.facts'), holdEnd());

    // ---- Scene 5: Connect, token, push readings
    await scene(5);
    await at(1);
    await click(page, nav('connect'));
    await at(2);
    for (let i = 0; i < 6; i++) { await page.mouse.wheel(0, 150); await sleep(350); }
    await at(3);
    await click(page, nav('settings'));
    await pause(1500); // the tab redraws itself once on arrival; typing before that would be lost
    await type(page, page.locator('#tk-name'), 'demo sender');
    await page.locator('#tk-scope').selectOption('ingest');
    await at(4);
    await click(page, page.getByRole('button', { name: 'Create token' }));
    const tokenEl = page.locator('#view-settings .codewrap code').first();
    await expect(tokenEl).toBeVisible();
    const ingestToken = ((await tokenEl.textContent()) ?? '').trim();
    expect(ingestToken.startsWith('cp_')).toBeTruthy();
    await click(page, page.locator('#view-settings .codewrap button', { hasText: 'Copy' }));
    await at(5);
    await callout(page, 'Ingest token: push only.', page.locator('#tk-scope'), until(6));
    const rows = [
      { sensor: 'lyon.temperature-presqu-0001', type: 'temperature', district: 'presqu', city: 'lyon', value: 21.4 },
      { sensor: 'lyon.humidity-presqu-0002', type: 'humidity', district: 'presqu', city: 'lyon', value: 58.0 },
      { sensor: 'lyon.rain-vaise-0003', type: 'rain', district: 'vaise', city: 'lyon', value: 0.2 },
      { sensor: 'lyon.wind-vaise-0004', type: 'wind_speed', district: 'vaise', city: 'lyon', value: 3.1 },
      { sensor: 'lyon.temperature-vaise-0005', type: 'temperature', district: 'vaise', value: 19.0 },
    ];
    const lyon = JSON.stringify({ readings: rows });
    const push = (token: string) => curl(['-sS', '-X', 'POST', stack.url + '/api/readings', '-H', `Authorization: Bearer ${token}`,
      '-H', 'Content-Type: application/json', '-d', lyon]);
    const answer = JSON.parse(await push(ingestToken));
    expect(answer.accepted).toBe(4);
    expect(answer.rejected).toBe(1);
    expect(answer.errors).toEqual(['1 x city is required']);
    expect(String(answer.hint)).toMatch(/^Every reading needs a/);
    await term.open('Terminal: ~/CityPulse');
    await term.typeCmd('bash lyon.sh   # POST /api/readings, Authorization: Bearer $CITYPULSE_INGEST_TOKEN');
    await at(7);
    for (const r of rows) {
      const line = `  ${r.sensor.padEnd(30)} ${r.type.padEnd(11)} district=${r.district.padEnd(7)} city=${(r as { city?: string }).city ?? '(missing)'}`;
      await term.append(line + '\n', (r as { city?: string }).city ? undefined : '#f87171');
      await sleep(900);
    }
    await term.append('\n');
    await at(10);
    await term.out(JSON.stringify(answer, null, 2));
    await at(11);
    await callout(page, 'accepted 4, rejected 1', page.locator('#rec-term'), until(12));
    await callout(page, 'The hint says how to fix the sender.', page.locator('#rec-term'), until(13));
    await callout(page, 'Bad data never fails the whole batch.', page.locator('#rec-term'), holdEnd());
    await term.close();

    // ---- Scene 6: city picker
    await scene(6);
    await at(0);
    await click(page, nav('overview'));
    await expect(banner).toBeVisible();
    await at(1);
    await click(page, page.locator('#city-picker button.city-btn'));
    await at(2);
    await type(page, page.locator('#city-q'), 'ly');
    const lyonOpt = page.locator('#city-panel li.city-opt', { hasText: 'lyon' });
    await expect(lyonOpt).toBeVisible();
    await pause(700);
    await click(page, lyonOpt);
    await expect(page.locator('.scope-chip', { hasText: 'lyon' }).first()).toBeVisible();
    await callout(page, 'City is required on every reading.', page.locator('.scope-chip').first(), until(3));
    await click(page, page.locator('#city-picker button.city-btn'));
    const allOpt = page.locator('#city-panel li.city-opt', { hasText: 'All cities' });
    await expect(allOpt).toBeVisible();
    await click(page, allOpt);
    await expect(page.locator('#city-picker button.city-btn')).toHaveText(/All cities/);
    await at(4);
    await callout(page, 'Cities needing attention, worst first.', page.getByText('Cities needing attention').first(), holdEnd());

    // ---- Scene 7: Burst
    await scene(7);
    await at(0);
    await click(page, nav('burst'));
    await expect(page.locator('#storm-secs')).toHaveValue('180');
    await at(2);
    await callout(page, '7 faults, auto-expire.', page.locator('.burst-big'), until(3));
    const kind = (name: string) => page.locator('.kinds .kind', { has: page.locator('h3', { hasText: new RegExp(`^${name}$`) }) });
    const faults: [number, string, string][] = [[3, 'flood', 'Flood of readings'], [4, 'slow db', 'Slow database'], [5, 'corrupt', 'Corrupt messages'],
      [6, 'noise', 'Noisy sensors'], [7, 'stuck', 'Stuck sensors'], [8, 'late', 'Late data'], [9, 'dropout', 'One district going dark (sensors go silent)']];
    for (const [k, name, label] of faults) {
      await at(k);
      await callout(page, label, kind(name), until(k + 1, 1800));
    }
    await page.evaluate(() => window.scrollTo(0, 0));
    await at(10);
    await callout(page, 'Duration: 1 to 10 minutes.', page.locator('#storm-secs'), until(11));
    await wander(page, [page.locator('#view-burst .hint')], 13);
    await at(13);
    await moveTo(page, page.locator('#storm-secs'));
    await at(14);
    const tBurst = Date.now();
    await click(page, page.locator('button.burst'));
    await expect(page.locator('#toast')).toContainText('Burst started');
    await expect(page.locator('#view-burst table tbody tr')).toHaveCount(7);
    await expect(page.locator('#view-burst table tbody')).toContainText(/\d+ min|\d+s/);
    await at(15);
    await callout(page, 'Seven faults, each with an "Ends in" countdown.', page.locator('#view-burst table'), until(16));
    await moveTo(page, page.locator('#view-burst table'), 0, 0, 1.5);

    // ---- Scene 8: observe
    await scene(8);
    const state = async () => ((await (await context.request.get(stack.url + '/api/status')).json()).state as string);
    const timeline: string[] = [];
    let sampling = true;
    const sampler = (async () => {
      while (sampling) {
        try {
          const j = await (await context.request.get(stack.url + '/api/status')).json();
          timeline.push(`${Math.round((Date.now() - tBurst) / 1000)}s ${j.state} ${(j.health * 100).toFixed(1)}% ok=${j.counts.ok} stale=${j.counts.stale} offline=${j.counts.offline} fault=${j.counts.fault} stuck=${j.counts.stuck}`);
        } catch { /* keep sampling */ }
        await sleep(5000);
      }
    })();
    checks.push(`scene 8 starts ${Math.round((Date.now() - tBurst) / 1000)} s after the BURST press`);
    await at(0);
    await click(page, nav('overview'));
    await expect(page.locator('.banner', { hasText: 'Burst active' })).toBeVisible();
    await callout(page, 'Amber banner: a burst is active.', page.locator('.banner', { hasText: 'Burst active' }), until(1));
    await wander(page, [kpis, heat, chart1], 3);
    await callout(page, 'Stale sensors climb first.', kpis, () => wander(page, [kpis, banner], 4));
    const at3 = await (await context.request.get(stack.url + '/api/status')).json();
    checks.push(`scene 8 sentence 4 ("There it is", verdict degraded): ${Math.round((Date.now() - tBurst) / 1000)} s after press, verdict ${at3.state}, health ${(at3.health * 100).toFixed(1)}%`);
    if (at3.state === 'operational') checks.push('FAIL: verdict still operational when the narration says degraded');
    await callout(page, 'Verdict follows the sensors.', banner, until(6));
    await moveTo(page, kpis, 200, 0, 1);
    await at(8);
    await callout(page, 'Stale keeps climbing, offline comes later.', kpis, until(9));
    await wander(page, [kpis], 10);
    await callout(page, 'The banner names the worst types and districts.', banner, until(11));
    await click(page, nav('sensors'));
    await page.locator('#f-state').selectOption('problem');
    await expect(page.locator('#view-sensors tbody tr.clickable').first()).toBeVisible();
    await at(12);
    await callout(page, 'Reason: no reading for 3+ intervals.', page.locator('#view-sensors tbody tr').first(), until(13));
    const probs = await (await context.request.get(stack.url + '/api/sensors?state=problem&limit=500')).json();
    const reasons = [...new Set((probs as { reason: string }[]).map((r) => r.reason))];
    checks.push(`problem-sensor reasons ${JSON.stringify(reasons)}`);
    // Back-pressure: push batches in parallel until the gateway answers 429.
    await term.open('Terminal: ~/CityPulse');
    let saw429 = false;
    const codes: string[] = [];
    let retry = '';
    const one = async () => {
      const o = await curl(['-s', '-o', '/dev/null', '-D', '-', '-w', '\nCODE=%{http_code}', '-X', 'POST', stack.url + '/api/readings',
        '-H', `Authorization: Bearer ${ingestToken}`, '-H', 'Content-Type: application/json', '-d', lyon]);
      const code = /CODE=(\d+)/.exec(o)![1];
      if (code === '429' && !retry) retry = o.split(/\r?\n/).filter((l) => /^(HTTP|retry-after)/i.test(l)).join('\n');
      return code;
    };
    for (let round = 0; round < 8 && !saw429; round++) {
      const rs = await Promise.all(Array.from({ length: 12 }, one));
      codes.push(...rs);
      saw429 = rs.includes('429');
    }
    checks.push(`429 seen=${saw429}; headers of the 429: ${retry.replace(/\n/g, ' | ')}`);
    expect(saw429, 'the gateway answered 429 under the parallel push').toBeTruthy();
    expect(retry.toLowerCase()).toContain('retry-after');
    await term.typeCmd('for i in $(seq 12); do curl -s -o /dev/null -w "%{http_code}\\n" -X POST ... & done; wait');
    await term.out(codes.slice(-12).join('\n'));
    await term.out(retry, '#fbbf24');
    await at(14);
    await callout(page, 'HTTP 429 + Retry-After = back-pressure.', page.locator('#rec-term'), until(15));
    await callout(page, 'The sender backs off; the gateway stays up.', page.locator('#rec-term'), until(16));
    await term.close();
    await click(page, nav('overview'));
    await wander(page, [banner, kpis, chart1], 19);
    await callout(page, 'Every symptom has a visible cause.', banner, holdEnd());

    // ---- Scene 9: stop and recover
    await scene(9);
    const tStop = Date.now();
    await at(0);
    await click(page, nav('burst'));
    await click(page, page.getByRole('button', { name: 'Stop everything' }));
    await expect(page.locator('#toast')).toContainText('All bursts stopped');
    await at(1);
    await click(page, nav('overview'));
    await callout(page, 'The backlog drains first.', banner, until(3));
    await wander(page, [banner, kpis, chart1, heat], 4);
    await callout(page, 'The simulator drains its backlog.', kpis, () => wander(page, [kpis, banner, chart1], 5));
    const back = await state();
    const burstGone = (await page.locator('.banner', { hasText: 'Burst active' }).count()) === 0;
    checks.push(`scene 9 sentence 5 ("There", operational): ${Math.round((Date.now() - tStop) / 1000)} s after Stop, ${Math.round((Date.now() - tBurst) / 1000)} s after press; verdict ${back}; burst banner gone ${burstGone}`);
    if (back !== 'operational' || !burstGone) checks.push('FAIL: not yet recovered when the narration says so');
    sampling = false;
    await sampler;
    checks.push(`timeline (s since BURST press; stop pressed at ${Math.round((tStop - tBurst) / 1000)} s): ${timeline.join(' | ')}`);
    await callout(page, 'Operational again.', banner, until(7));
    await callout(page, 'Recovery in under a minute.', banner, until(8));
    await callout(page, 'Nothing is latched.', banner, holdEnd());

    // ---- Scene 10: health and metrics
    await scene(10);
    await at(0);
    await click(page, nav('settings'));
    await pause(1500); // the tab redraws itself once on arrival; typing before that would be lost
    await at(1);
    await type(page, page.locator('#tk-name'), 'demo metrics');
    await page.locator('#tk-scope').selectOption('metrics');
    await callout(page, 'Metrics scope only.', page.locator('#tk-scope'), 900);
    await click(page, page.getByRole('button', { name: 'Create token' }));
    await expect(page.locator('#view-settings .codewrap code').first()).toBeVisible();
    const metricsToken = ((await page.locator('#view-settings .codewrap code').first().textContent()) ?? '').trim();
    const healthz = await curl(['-s', stack.url + '/healthz']);
    const metrics = (await curl(['-s', '-H', `Authorization: Bearer ${metricsToken}`, stack.url + '/metrics']))
      .split('\n').filter((l) => l.includes('citypulse_fleet')).join('\n');
    expect(metrics).toContain('citypulse_fleet_health_ratio');
    await at(2);
    await term.open('Terminal: ~/CityPulse');
    await term.run('curl -s $CITYPULSE_URL/healthz', healthz);
    await callout(page, 'No login: /healthz.', page.locator('#rec-term'), until(4));
    await term.typeCmd('curl -s -H "Authorization: Bearer $METRICS_TOKEN" $CITYPULSE_URL/metrics | grep citypulse_fleet');
    await term.out(metrics);
    await callout(page, 'Metrics token: /metrics.', page.locator('#rec-term'), until(6));
    await term.close();
    await click(page, nav('connect'));
    for (let i = 0; i < 8 && early(8); i++) { await page.mouse.wheel(0, 500); await sleep(450); }
    await at(8);
    await callout(page, 'Snippets: pagerlens-agent and PAGERLENS_URL.', page.locator('#view-connect .codewrap').last(), until(9));
    await diagram(page, 1);
    await at(10);
    await diagram(page, 2);
    await at(11);
    await diagram(page, 3);
    await endScene();

    // ---- Scene 11: recap
    await scene(11);
    await at(0);
    await hideCard(page);
    await click(page, nav('overview'));
    await expect(banner).toBeVisible();
    await wander(page, [banner, heat], 2);
    const recap = ['One command to start it', 'Scoped tokens to feed it', 'One dashboard to read it', 'One button to break it'];
    for (let k = 2; k <= 5; k++) { await at(k); await showCard(page, 'Recap', recap.slice(0, k - 1)); }
    await at(6);
    await showCard(page, 'Try it yourself', ['docker compose up']);
    await at(8);
    await showCard(page, 'Try it yourself', ['docker compose up', 'Press BURST']);
    await at(9);
    await showCard(page, 'CityPulse', ['docker compose up', 'Press BURST', 'docs/GUIDE.md']);
    await endScene();
    await pause(800);

    checks.push(`facts: sensors-empty-message=${JSON.stringify(emptyMsg)} heat-cell-title=${JSON.stringify(cellTitle)}`);
    const endMs = Date.now();
    await context.close(); // flushes the video
    const timings = { scene_start_ms: marks, calib_ms: calib, end_ms: endMs, checks };
    fs.writeFileSync(process.env.TIMINGS_OUT || path.join(outDir, 'timings.json'), JSON.stringify(timings, null, 2));
    for (const c of checks) console.log('CHECK', c);
  } finally {
    await stack.stop();
  }
});
