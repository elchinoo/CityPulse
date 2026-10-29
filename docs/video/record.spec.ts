/**
 * Records docs/VIDEO-SCRIPT.md: "My data is time series, JSON, geo and vectors", in a real Chrome, 1920x1080, with Playwright's video
 * (written to docs/video/out/, git-ignored) and callouts drawn over the page.
 *
 * What runs and what is captured, honestly:
 *  - RUNNING LOCALLY (a throwaway Postgres on 127.0.0.1, CityPulse with its built-in simulator, a real PagerLens next to it): the
 *    sensor detail, the Map tab saying PostGIS is missing, the Explain tab (relational, time series and JSON answer; geo and
 *    vector say why not) and its SQL, a new vendor's batch (JSON attributes + position), the JSONB query, the burst, the verdict
 *    falling, the alert hand-off to PagerLens, the Insights tab saying "not available", the read-only database user.
 *  - CAPTURED from the live AgentPG fleet, 28 and 29 Oct 2026 (videos/live-captures/, shown as stills with a slow zoom and highlight boxes,
 *    under a banner): from 29 Oct 2026 the Map of Brussels (real communes, OpenStreetMap) and the Explain card for Ixelles (five models,
 *    the geo part with distances in metres); from 28 Oct 2026 the vector part of an earlier Explain card (three similar past episodes,
 *    labelled with its own date), PayBroker's Insights tab (same kit, same fleet), the extension list and the `ecoctl insights` advice
 *    for CityPulse's own database.
 *  - NOT SHOWN IN THIS VIDEO: HA/failover, AIDB.
 *  A local Postgres has no PostGIS and no edb_* extensions (checked: `CREATE EXTENSION postgis` fails here), so those parts are
 *  never faked. CityPulse has no AI model, so there is no stand-in model in this video.
 *
 *   cd docs/video && npm install && python3 build.py            (the normal way: narration, recording and mux)
 *   RECORD_WARMUP_S=60 HOLD=0.6 npx playwright test             (rehearsal: not the final timing)
 */
import { expect, test, type Page, type Locator } from '@playwright/test';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { sleep, startStack, type Stack } from './lib';

const HOLD = Number(process.env.HOLD ?? 1); // multiplies every scene length and cue time (rehearsal)
const WARMUP_S = Number(process.env.RECORD_WARMUP_S ?? '330'); // the fleet reports this long before the camera starts (charts need history)
const DB = 'citypulse_video_test';

let stack: Stack;
test.beforeAll(async () => {
  stack = await startStack();
});
test.afterAll(async () => {
  await stack?.stop();
});

/**
 * Scene pacing and sync (as in the other videos). build.py runs narrate.py first and hands the result over (NARRATION_JSON). A scene
 * lasts LEAD + its narration + TAIL = narration + 0.7 s. Every on-screen action waits for the sentence that illustrates it:
 * `await at(page, 'words of the sentence')` blocks until that sentence starts (minus `lead`: the action itself takes a moment to
 * show). If the picture is already late, the action runs at once and the delay is logged and written to TIMES_JSON ("sync").
 */
const LEAD = 0.2;
const TAIL = 0.5;
type Cue = { text: string; start: number; end: number };
const NARRATION: Record<string, { duration: number; cues: Cue[] }> = process.env.NARRATION_JSON ? JSON.parse(fs.readFileSync(process.env.NARRATION_JSON, 'utf8')) : {};
const T0 = { at: 0 };
const SC = { n: 0, start: 0, targetMs: 0 };
const times: { scene: number; start: number; end: number }[] = [];
const sync: { scene: number; key: string; cueAt: number; actualAt: number; lateBy: number }[] = [];
const rel = () => (Date.now() - T0.at) / 1000;
function begin(n: number) {
  SC.n = n;
  SC.start = Date.now();
  SC.targetMs = (LEAD + (NARRATION[String(n)]?.duration ?? 20) + TAIL) * 1000 * HOLD;
  times.push({ scene: n, start: rel(), end: 0 });
}
/** Waits until the sentence containing `key` starts (the voice starts LEAD seconds into the scene); `lead` = how early the action begins. */
async function at(page: Page, key: string, lead = 0.25) {
  const cues = NARRATION[String(SC.n)]?.cues;
  if (!cues) return;
  const cue = cues.find((c) => c.text.includes(key));
  if (!cue) throw new Error(`scene ${SC.n}: no narrated sentence contains "${key}"`);
  const due = (LEAD + cue.start - lead) * 1000 * HOLD;
  const now = Date.now() - SC.start;
  if (now < due) await page.waitForTimeout(due - now);
  const late = (Date.now() - SC.start - due) / 1000;
  sync.push({ scene: SC.n, key, cueAt: +(LEAD + cue.start).toFixed(2), actualAt: +((Date.now() - SC.start) / 1000).toFixed(2), lateBy: +late.toFixed(2) });
  if (late > 0.5) console.log(`scene ${SC.n}: "${key}" is ${late.toFixed(1)} s late`);
}
const pause = (page: Page, seconds: number) => page.waitForTimeout(seconds * 1000 * HOLD);
async function endScene(page: Page) {
  const left = SC.targetMs - (Date.now() - SC.start);
  if (left > 0) await page.waitForTimeout(left);
  await page.evaluate(() => (window as any).__clear?.()).catch(() => {});
  times[times.length - 1].end = rel();
}

/** Overlay helpers, installed in every page of the context (top layer through popover, so they show above everything). */
const OVERLAY = () => {
  const mk = (css: string, text: string, keep = false) => {
    const el = document.createElement('div');
    el.setAttribute('popover', 'manual');
    el.setAttribute(keep ? 'data-keep' : 'data-recorder', '');
    el.style.cssText = 'position:fixed;inset:auto;margin:0;border:0;padding:0;background:none;overflow:visible;pointer-events:none;' + css;
    el.textContent = text;
    document.documentElement.append(el);
    (el as any).showPopover?.();
    return el;
  };
  const clear = () => document.querySelectorAll('[data-recorder]').forEach((e) => e.remove());
  (window as any).__callout = (text: string, selector: string | null, nth = 0, ms = 3500) => {
    clear();
    clearTimeout((window as any).__ct);
    (window as any).__ct = setTimeout(clear, ms);
    const target = selector ? (document.querySelectorAll(selector)[nth] as HTMLElement | undefined) : undefined;
    if (target) {
      target.scrollIntoView({ block: 'center', behavior: 'instant' as ScrollBehavior });
      const r = target.getBoundingClientRect();
      mk(`left:${r.left - 6}px;top:${r.top - 6}px;width:${r.width + 12}px;height:${r.height + 12}px;border:4px solid #ff9f1c;border-radius:10px;box-shadow:0 0 0 6px rgba(255,159,28,.25)`, '');
      const below = r.bottom + 70 < window.innerHeight;
      mk(
        `left:${Math.max(16, Math.min(r.left, window.innerWidth - 640))}px;${below ? `top:${r.bottom + 14}px` : `top:${Math.max(8, r.top - 64)}px`};max-width:620px;` +
          'background:#1b2430;color:#fff;font:600 24px/1.3 system-ui,sans-serif;padding:10px 18px;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.35)',
        text,
      );
    } else {
      mk('left:50%;transform:translateX(-50%);bottom:56px;max-width:1200px;text-align:center;background:#1b2430;color:#fff;font:600 30px/1.3 system-ui,sans-serif;padding:14px 28px;border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.35)', text);
    }
  };
  (window as any).__lower = (title: string, sub: string, ms = 0) => {
    clear();
    clearTimeout((window as any).__ct);
    if (ms) (window as any).__ct = setTimeout(clear, ms);
    const el = mk('left:48px;bottom:48px;background:rgba(27,36,48,.94);color:#fff;font:700 34px/1.25 system-ui,sans-serif;padding:16px 26px;border-left:8px solid #ff9f1c;border-radius:6px;max-width:1500px', title);
    if (sub) {
      const d = document.createElement('div');
      d.textContent = sub;
      d.style.cssText = 'font:600 26px/1.3 system-ui,sans-serif;opacity:.9;margin-top:6px';
      el.append(d);
    }
  };
  // The persistent label of a locally running scene (top right); __badge('') removes it.
  (window as any).__badge = (text: string) => {
    document.querySelectorAll('[data-keep]').forEach((e) => e.remove());
    if (text) mk('right:28px;top:76px;background:#12372a;color:#d1fae5;font:700 24px/1.2 system-ui,sans-serif;padding:10px 18px;border-radius:999px;border:3px solid #34d399;box-shadow:0 4px 16px rgba(0,0,0,.3)', text, true);
  };
  (window as any).__clear = clear;
  // A visible cursor (a headless video has none).
  addEventListener('DOMContentLoaded', () => {
    const dot = mk('width:26px;height:26px;border-radius:50%;border:3px solid #fff;background:rgba(37,99,235,.55);box-shadow:0 0 0 2px #1d4ed8,0 2px 8px rgba(0,0,0,.4);transform:translate(-50%,-50%);left:-50px;top:-50px', '', true);
    dot.setAttribute('data-cursor', '');
    dot.removeAttribute('data-keep');
    addEventListener('mousemove', (e) => { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; (dot as any).showPopover?.(); }, true);
  });
};

const callout = (page: Page, text: string, selector: string | null, nth = 0, seconds = 3.5) =>
  page.evaluate(([t, s, n, ms]) => (window as any).__callout(t, s, n, ms), [text, selector, nth, seconds * 1000 * HOLD] as const);
const lower = (page: Page, title: string, sub = '', seconds = 0) => page.evaluate(([t, s, ms]) => (window as any).__lower(t, s, ms), [title, sub, seconds * 1000 * HOLD] as const);
const badge = (page: Page, text: string) => page.evaluate((t) => (window as any).__badge(t), text);
const LOCAL = 'RUNNING LOCALLY: throwaway Postgres on this laptop';

// A visible, continuous cursor movement.
let mx = 960;
let my = 540;
async function glide(page: Page, x: number, y: number, sec: number) {
  const n = Math.max(2, Math.round(sec * 25));
  const x0 = mx;
  const y0 = my;
  for (let i = 1; i <= n; i++) {
    const t = i / n;
    const e = t * t * (3 - 2 * t);
    mx = x0 + (x - x0) * e;
    my = y0 + (y - y0) * e;
    await page.mouse.move(mx, my);
    await sleep((sec * 1000 * HOLD) / n);
  }
}
async function boxOf(loc: Locator) {
  let last: unknown;
  for (let i = 0; i < 8; i++) {
    try {
      await loc.scrollIntoViewIfNeeded();
      const b = await loc.boundingBox();
      if (b) return b;
    } catch (e) {
      last = e;
    }
    await sleep(300);
  }
  throw new Error(`element has no box: ${String(last)}`);
}
async function moveTo(page: Page, loc: Locator, sec = 0.6) {
  const b = await boxOf(loc);
  await glide(page, b.x + b.width / 2, b.y + b.height / 2, sec);
}
async function click(page: Page, loc: Locator, sec = 0.6) {
  await moveTo(page, loc, sec);
  await page.mouse.down();
  await sleep(70);
  await page.mouse.up();
}

const CARD_CSS = `html,body{margin:0;height:100%;background:#0f2a43;color:#fff;font-family:system-ui,sans-serif}
.c{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;text-align:center}
h1{font-size:96px;margin:0}p{font-size:40px;margin:0;opacity:.92}.dim{opacity:0;transition:opacity .6s}.on{opacity:.95}`;
const blank = (page: Page) => page.goto('about:blank'); // setContent keeps the old URL: leave the app first
const show = (page: Page, id: string) => page.evaluate((i) => document.getElementById(i)!.classList.add('on'), id);

/** Scene 1: the problem, as four databases appearing around the one the city already had. */
const PROBLEM = `<style>${CARD_CSS}
.row{display:flex;gap:26px;margin-top:20px}.db{width:340px;border:4px solid #7aa7d6;border-radius:22px;padding:26px 10px;font-size:40px;font-weight:700;background:#14395c}
.rel{border-style:dashed;opacity:.9}.n{font-size:56px;font-weight:800;color:#ffb347;margin-top:26px}
</style><div class="c"><h1 id="t">A city, and its sensors</h1><p id="s" class="dim" style="font-size:60px;font-weight:700;color:#ffb347;margin-top:14px">vendors, readings, firmware, locations</p>
<div class="row"><div class="db dim" id="d1">Time series<br>database</div><div class="db dim" id="d2">Document<br>store</div><div class="db dim" id="d3">Geo<br>database</div><div class="db dim" id="d4">Vector<br>database</div></div>
<div class="db rel dim" id="d0" style="width:auto;padding:14px 40px;font-size:34px">plus the relational one you already had</div>
<div class="n dim" id="n">Four databases to run and keep in sync?</div></div>`;

/** Scene 1: what is what. Same three colours as the other videos of the ecosystem. */
const DIAGRAM = `<style>
html,body{margin:0;height:100%;background:#f4f6f9;font-family:system-ui,sans-serif;color:#1b2430}
.w{padding:44px 90px;display:flex;flex-direction:column;gap:26px;height:100%;box-sizing:border-box;justify-content:center}
.box{border:4px solid;border-radius:18px;padding:22px 32px;transition:box-shadow .5s,opacity .5s,transform .5s;position:relative}
.tag{position:absolute;top:-20px;left:28px;font:800 24px system-ui;padding:3px 16px;border-radius:999px;color:#fff;letter-spacing:.04em;text-transform:uppercase}
h2{margin:6px 0 4px;font-size:44px}p{margin:0;font-size:30px;line-height:1.35}
.app{border-color:#e07b00;background:#fff4e5}.app .tag{background:#e07b00}
.scf{border-color:#6b7787;border-style:dashed;background:#fff}.scf .tag{background:#6b7787}
.plat{border-color:#0b5cad;background:#e8f1fb}.plat .tag{background:#0b5cad}
.row{display:flex;gap:22px}.chip{flex:1;border:3px dashed #6b7787;border-radius:12px;padding:14px 18px;font-size:30px;text-align:center;background:#fafbfc}
.ext{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px}.ext span{background:#0b5cad;color:#fff;border-radius:10px;padding:6px 16px;font:600 28px ui-monospace,Menlo,monospace;transition:all .5s}
.arrow{text-align:center;font-size:36px;color:#6b7787;margin:-12px 0}
.dim{opacity:.35}#tpl{position:absolute;top:6px;right:90px;background:#e07b00;color:#fff;font:800 30px system-ui;padding:8px 24px;border-radius:999px;opacity:0;transition:opacity .6s}#tpl.on{opacity:1}.hl{box-shadow:0 0 0 10px rgba(255,159,28,.45);transform:scale(1.01)}.ext span.hot{background:#ff9f1c;color:#111;outline:6px solid rgba(255,159,28,.4)}
</style>
<div id="tpl">Templates, not only demos: Apache 2.0, take it, adapt it, run it</div>
<div class="w">
 <div class="box app" id="app"><span class="tag">Sample app</span><h2>CityPulse and its simulator</h2><p>Sensors, status, bursts, one question per district. It exists to show the platform.</p></div>
 <div class="arrow">&#8595;</div>
 <div class="box scf" id="scf"><span class="tag">Reference scaffolding: swap any of it</span>
  <div class="row"><div class="chip">identity provider (OIDC)</div><div class="chip">gateway</div><div class="chip">vault</div></div></div>
 <div class="arrow">&#8595;</div>
 <div class="box plat" id="plat"><span class="tag">Platform: EDB Postgres on AgentPG</span>
  <h2>One Postgres does the work</h2>
  <div class="ext"><span>time series (roll-ups)</span><span>JSONB + GIN</span><span>PostGIS</span><span>pgvector</span><span>edb_stat_monitor</span><span>edb_pg_tuner</span><span>query_advisor</span></div>
  <p style="margin-top:12px">Time series, JSON, geo and vectors: one database.</p></div>
</div>`;

const TERM_CSS = `html,body{margin:0;height:100%;background:#12161c;color:#d8dee9;font:28px/1.4 ui-monospace,Menlo,monospace}
pre{margin:0;padding:120px 60px 40px;white-space:pre-wrap;word-break:break-all}.p{color:#7fd1ae}.hl{background:#ff9f1c;color:#111;padding:2px 8px;border-radius:4px}.dim{color:#7b8794}.err{color:#ff7b72;font-weight:700}.ok{color:#7fd1ae}
.note{position:fixed;left:60px;right:60px;bottom:40px;background:#1b2430;border-left:8px solid #ff9f1c;border-radius:8px;padding:16px 26px;font:600 28px/1.35 system-ui,sans-serif;color:#fff}
#bar{position:fixed;left:0;right:0;top:0;height:80px;display:flex;align-items:center;justify-content:center;background:#0f1a2e;border-bottom:2px solid #1e293b;font:700 30px system-ui;color:#0b1220}
#bar span{padding:6px 20px;border-radius:10px;background:#34d399}`;
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
const termPage = async (page: Page, body: string, label = 'RUNNING LOCALLY: real commands against the throwaway Postgres on this laptop') => {
  await blank(page);
  await page.setContent(`<style>${TERM_CSS}</style><div id="bar"><span>${esc(label)}</span></div><pre id="t">${body}</pre>`);
};
const setTerm = (page: Page, html: string) => page.evaluate((t) => (document.getElementById('t')!.innerHTML = t), html);
/** Types a shell line in one in-page animation of `seconds`, then returns the new page text. */
async function typeLine(page: Page, current: string, prompt: string, cmd: string, seconds: number): Promise<string> {
  const prefix = current + `<span class="p">${prompt}</span>`;
  await page.evaluate(
    async ([pre, text, ms]) => {
      const el = document.getElementById('t')!;
      const e = (x: string) => x.replace(/&/g, '&amp;').replace(/</g, '&lt;');
      const step = Math.max(1, Math.ceil(text.length / (ms / 40)));
      for (let i = 0; i < text.length; i += step) {
        el.innerHTML = pre + e(text.slice(0, i + step));
        await new Promise((r) => setTimeout(r, ms / Math.ceil(text.length / step)));
      }
    },
    [prefix, cmd, seconds * 1000 * HOLD] as const,
  );
  return prefix + esc(cmd) + '\n';
}

/** Real captures from the live AgentPG fleet (videos/live-captures/), shown with a slow zoom and a highlight box. */
const LIVE_DIR = path.resolve(__dirname, '..', '..', '..', 'videos', 'live-captures');
const CAPTURED = 'CAPTURED from the live AgentPG fleet, 28 Oct 2026';
const CAPTURED29 = 'CAPTURED from the live AgentPG fleet, 29 Oct 2026';
const IW = 1481, IH = 812; // the screenshots' size; boxes are read off them in pixels
const STILL_HTML = `<style>
html,body{margin:0;height:100%;background:#0b1220;overflow:hidden;font-family:system-ui,sans-serif}
#bar{position:fixed;left:0;right:0;top:0;height:80px;display:flex;align-items:center;justify-content:center;gap:24px;background:#0f1a2e;border-bottom:2px solid #1e293b;font:700 32px system-ui}
.tg{padding:6px 20px;border-radius:10px;color:#0b1220}.cap{background:#fb923c;box-shadow:0 0 0 4px rgba(251,146,60,.35)}.pl{background:#38bdf8}
#stage{position:absolute;left:48px;top:80px;width:1824px;height:1000px;overflow:hidden;background:#fff}
#w{position:absolute;inset:0;transform-origin:50% 50%}#w img{width:100%;height:100%;display:block}
#box{position:absolute;border:6px solid #f59e0b;border-radius:8px;opacity:0;transition:opacity .4s}
</style><div id="bar"><span class="tg cap" id="tag1"></span><span class="tg pl" id="tag2"></span></div>
<div id="stage"><div id="w"><img id="img"><div id="box"></div></div></div>`;
async function stillPage(page: Page, tag: string, files: string[], banner = CAPTURED) {
  await blank(page);
  await page.setContent(STILL_HTML);
  const srcs: Record<string, string> = {};
  for (const f of files) srcs[f] = 'data:image/jpeg;base64,' + fs.readFileSync(path.join(LIVE_DIR, f)).toString('base64');
  await page.evaluate(([t, m, b]) => { document.getElementById('tag2')!.textContent = t as string; document.getElementById('tag1')!.textContent = b as string; (window as any).__srcs = m; }, [tag, srcs, banner] as const);
}
const pct = (x0: number, y0: number, x1: number, y1: number) => [(x0 / IW) * 100, (y0 / IH) * 100, ((x1 - x0) / IW) * 100, ((y1 - y0) / IH) * 100];
/** a shot: which capture, zoom origin (% of the image), zoom from/to over ms, highlight box (pixels of the capture: x0, y0, x1, y1). */
const shot = (page: Page, img: string, ox: number, oy: number, z0: number, z1: number, ms: number, box?: number[]) =>
  page.evaluate(([i, a, b, c, d, e, f]) => {
    const im = document.getElementById('img') as HTMLImageElement, w = document.getElementById('w')!, bx = document.getElementById('box')!;
    const src = (window as any).__srcs[i as string];
    if (im.src !== src) im.src = src;
    w.style.transition = 'none'; w.style.transformOrigin = `${a}% ${b}%`; w.style.transform = `scale(${c})`;
    void w.offsetWidth;
    w.style.transition = `transform ${(e as number) * 1}ms linear`; w.style.transform = `scale(${d})`;
    if (f) { const g = f as number[]; bx.style.left = g[0] + '%'; bx.style.top = g[1] + '%'; bx.style.width = g[2] + '%'; bx.style.height = g[3] + '%'; bx.style.opacity = '1'; } else bx.style.opacity = '0';
  }, [img, ox, oy, z0, z1, ms * HOLD, box ? pct(box[0], box[1], box[2], box[3]) : null] as const);
const MAP = 'citypulse-map-brussels-2026-10-29.jpg', EXTOP = 'citypulse-explain-top-brussels-2026-10-29.jpg', EXGEO = 'citypulse-explain-geo-brussels-2026-10-29.jpg';
const EXGV = 'citypulse-explain-geo-vector-2026-10-28.jpg'; // 28 Oct: used for the vector part only, under its own date
const setBanner = (page: Page, text: string, tag: string) => page.evaluate(([b, t]) => { document.getElementById('tag1')!.textContent = b; document.getElementById('tag2')!.textContent = t; }, [text, tag] as const);
const TOPQ = 'paybroker-insights-top-queries-2026-10-28.jpg', TUNE = 'paybroker-insights-tuner-advisor-2026-10-28.jpg';

/** A captured text (real command output saved from the live fleet) or source text, drawn as a card under the same banner. */
const TEXT_HTML = (banner: string, tag: string, title: string, lines: string[], hot: number[]) => `<style>
html,body{margin:0;height:100%;background:#0b1220;overflow:hidden;font-family:system-ui,sans-serif;color:#e6edf6}
#bar{position:fixed;left:0;right:0;top:0;height:80px;display:flex;align-items:center;justify-content:center;gap:24px;background:#0f1a2e;border-bottom:2px solid #1e293b;font:700 32px system-ui}
.tg{padding:6px 20px;border-radius:10px;color:#0b1220}.cap{background:#fb923c;box-shadow:0 0 0 4px rgba(251,146,60,.35)}.pl{background:#38bdf8}
h2{margin:120px 60px 14px;font:700 40px system-ui}
pre{margin:0 60px;padding:26px 30px;background:#12161c;border-radius:14px;font:30px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all;color:#d8dee9}
.l{display:block;padding:0 8px;border-radius:6px;transition:all .4s}.l.hot{background:#ff9f1c;color:#111}
</style><div id="bar"><span class="tg cap">${esc(banner)}</span><span class="tg pl">${esc(tag)}</span></div>
<h2>${esc(title)}</h2><pre>${lines.map((l, i) => `<span class="l" id="l${i}">${esc(l) || '&nbsp;'}</span>`).join('')}</pre>`;
const textCard = async (page: Page, banner: string, tag: string, title: string, lines: string[]) => {
  await blank(page);
  await page.setContent(TEXT_HTML(banner, tag, title, lines, []));
};
const hotLines = (page: Page, ids: number[]) =>
  page.evaluate((a) => document.querySelectorAll('.l').forEach((e, i) => e.classList.toggle('hot', a.includes(i))), ids);

/** One section of a capture file: the lines after the header that contains `key` up to the next blank line / next header. */
function section(file: string, key: string): string[] {
  const all = fs.readFileSync(path.join(LIVE_DIR, file), 'utf8').split('\n');
  const i = all.findIndex((l) => l.includes(key));
  if (i < 0) throw new Error(`${file}: no section "${key}"`);
  const out = [all[i]];
  for (let j = i + 1; j < all.length && all[j].trim() !== ''; j++) out.push(all[j]);
  return out;
}

const iso = (n: number) => n;
test('CityPulse: time series, JSON, geo and vectors in one Postgres', async ({ page, context }) => {
  test.setTimeout(40 * 60_000);
  await context.addInitScript(OVERLAY);
  const notes: string[] = [];
  const checks: string[] = [];
  await context.addCookies([
    { name: stack.cityCookie.name, value: stack.cityCookie.value, url: stack.url },
    { name: stack.pagerCookie.name, value: stack.pagerCookie.value, url: stack.pagerlensUrl },
  ]);
  const api = (p: string) => context.request.get(stack.url + p).then((r) => r.json());
  const nav = (tab: string) => page.locator(`a[data-tab="${tab}"]`);
  const banner = page.locator('.status-banner');

  // ---- Off camera: choose the city (it is remembered by the browser), let the fleet report for a while -----------------------
  await page.goto(stack.url + '/');
  await expect(page.locator('#nav')).toBeVisible();
  await page.locator('#city-picker button.city-btn').click();
  await page.locator('#city-q').fill('par');
  await page.locator('#city-panel li.city-opt', { hasText: 'paris' }).click();
  await expect(page.locator('.scope-chip', { hasText: 'paris' }).first()).toBeVisible();
  await expect(page.locator('.heat td button:not(.none)').first()).toBeVisible({ timeout: 60_000 });
  const warmEnd = Date.now() + WARMUP_S * 1000;
  console.log(`warming up the fleet for ${WARMUP_S} s`);
  while (Date.now() < warmEnd) await sleep(1000);
  const st0 = await api('/api/status');
  checks.push(`warm-up done: ${st0.state}, ${st0.total} sensors, ok=${st0.counts.ok}`);
  expect(st0.state).toBe('operational');

  T0.at = Date.now(); // the recorder's clock starts at the first card; build.py cuts the video there
  let cardAt = 0;
  // ---- Scene 1: the problem, then what is what ----------------------------------------------------------------------------
  begin(1);
  await blank(page);
  await page.setContent(PROBLEM);
  cardAt = rel();
  await at(page, 'Sensors from many vendors', 0.1);
  await show(page, 's');
  await at(page, 'The usual answer', 0.2);
  await show(page, 'd0');
  await at(page, 'One for time series', 0.2);
  await show(page, 'd1');
  await at(page, 'One for documents', 0.2);
  await show(page, 'd2');
  await at(page, 'One for maps', 0.2);
  await show(page, 'd3');
  await at(page, 'And one for vectors', 0.2);
  await show(page, 'd4');
  await show(page, 'n');
  await at(page, 'This is CityPulse', 0.3);
  await blank(page);
  await page.setContent(DIAGRAM);
  await page.evaluate(() => ['scf', 'plat'].forEach((i) => document.getElementById(i)!.classList.add('dim')));
  await page.evaluate(() => document.getElementById('app')!.classList.add('hl'));
  await at(page, 'The identity provider', 0.2);
  await page.evaluate(() => {
    document.getElementById('app')!.classList.remove('hl');
    document.getElementById('scf')!.classList.remove('dim');
    document.getElementById('scf')!.classList.add('hl');
  });
  await at(page, 'What you are watching', 0.2);
  await page.evaluate(() => {
    document.getElementById('scf')!.classList.remove('hl');
    document.getElementById('plat')!.classList.remove('dim');
    document.getElementById('plat')!.classList.add('hl');
  });
  await at(page, 'One database, doing all of it', 0.2);
  await page.evaluate(() => document.querySelectorAll('.ext span').forEach((e, i) => { if (i < 4) e.classList.add('hot'); }));
  await at(page, 'These apps are templates', 0.2);
  await show(page, 'tpl');
  await endScene(page);

  // ---- Scene 2: the map. Local: a sensor with its position; the Map tab without PostGIS. Captured: the real map; the SQL ----------
  await page.goto(stack.url + '/#sensors');
  await expect(page.locator('#f-state')).toBeVisible();
  await page.waitForTimeout(1200); // the tab redraws once on arrival; acting before that loses the click
  begin(2);
  await badge(page, LOCAL);
  await at(page, 'First, where things are', 0.1);
  await page.locator('#f-state').selectOption('all');
  await expect(page.locator('#view-sensors tbody tr.clickable').first()).toBeVisible();
  await at(page, 'Here is one', 0.8);
  await click(page, page.locator('#view-sensors tbody tr.clickable').first(), 0.5);
  await expect(page.locator('.facts')).toBeVisible();
  await expect(page.locator('.facts')).toContainText('Position');
  await callout(page, 'Position: latitude, longitude (plain columns)', '.facts', 0, 4);
  await at(page, 'On this laptop there is no PostGIS', 0.3);
  await click(page, nav('map'), 0.5);
  await expect(page.locator('#view-map')).toContainText('Map not available');
  await callout(page, 'Local Postgres: no PostGIS, so no map', '#view-map .card', 0, 4.5);
  await at(page, 'This is the live fleet instead', 0.3);
  await badge(page, '');
  await stillPage(page, 'Map tab: Brussels', [MAP, EXGEO], CAPTURED29);
  await shot(page, MAP, 50, 45, 1.0, 1.08, 3200);
  await at(page, 'This is Brussels', 0.2);
  await shot(page, MAP, 50, 45, 1.04, 1.14, 5200, [460, 140, 1010, 690]);
  await at(page, 'The sensors are simulated', 0.2);
  await shot(page, MAP, 22, 95, 1.0, 1.9, 1900, [243, 770, 475, 788]);
  await at(page, 'The base map has the water', 0.2);
  await shot(page, MAP, 56, 30, 1.5, 1.7, 2200, [700, 145, 860, 330]);
  await at(page, 'The parks', 0.2);
  await shot(page, MAP, 62, 75, 1.5, 1.6, 1100, [745, 500, 1010, 660]);
  await at(page, 'And the communes', 0.2);
  await shot(page, MAP, 50, 45, 1.2, 1.3, 2000, [460, 140, 1010, 690]);
  await at(page, 'The colour is the health', 0.2);
  await shot(page, MAP, 40, 100, 1.0, 1.4, 2400, [243, 708, 1130, 728]);
  await at(page, 'Two communes stand out', 0.2);
  await shot(page, MAP, 45, 45, 1.5, 1.6, 1500, [600, 310, 682, 360]);
  await pause(page, 1.5);
  await shot(page, MAP, 62, 60, 1.5, 1.6, 1500, [808, 462, 1012, 600]);
  await at(page, 'The simulator places each sensor', 0.2);
  await shot(page, MAP, 50, 45, 1.0, 1.12, 6000);
  await at(page, 'So you can ask what is near a point', 0.3);
  await setBanner(page, CAPTURED29, 'Explain tab for Ixelles (brussels): the geo part');
  await shot(page, EXGEO, 40, 55, 1.0, 1.15, 2500);
  await at(page, 'The nearest healthy sensors', 0.2);
  await shot(page, EXGEO, 40, 60, 1.15, 1.3, 7000, [251, 538, 1215, 578]);
  await at(page, 'The neighbouring communes are', 0.2);
  await shot(page, EXGEO, 40, 62, 1.3, 1.35, 5500, [251, 590, 1000, 612]);
  await at(page, 'PostGIS answers', 0.2);
  await shot(page, EXGEO, 40, 55, 1.2, 1.1, 3000, [251, 472, 1219, 612]);
  await endScene(page);

  // ---- Scene 3: Explain, one statement, five models --------------------------------------------------------------------------
  await page.goto(stack.url + '/#explain');
  await expect(page.locator('#explain-go')).toBeVisible();
  await page.waitForTimeout(800);
  begin(3);
  await badge(page, LOCAL);
  await at(page, 'Now the question an operator asks', 0.1);
  await moveTo(page, page.locator('#explain-district'), 0.8);
  await at(page, 'On my laptop', 0.6);
  await click(page, page.locator('#explain-go'), 0.5);
  await expect(page.locator('.explain')).toContainText('data models');
  await expect(page.locator('.explain section.model')).toHaveCount(5);
  const nSql = await page.locator('.explain').innerText();
  notes.push(`scene 3 explain header: ${(nSql.match(/\d+ data models, \d+ SQL statement/) ?? ['?'])[0]}`);
  await callout(page, 'One statement, one round trip, five models', '.explain', 0, 4);
  await at(page, 'Relational:', 0.2);
  await callout(page, 'Relational: sensors by state', '.explain section.model', 0, 4);
  await at(page, 'Time series:', 0.2);
  await callout(page, 'Time series: the 5-minute roll-up', '.explain section.model', 1, 5);
  await at(page, 'JSON:', 0.2);
  await callout(page, 'JSON: vendor and firmware (JSONB)', '.explain section.model', 2, 6);
  await at(page, 'No PostGIS here', 0.2);
  await callout(page, 'Geo: PostGIS missing, says so', '.explain section.model', 3, 3.5);
  await at(page, 'And this fresh database', 0.2);
  await callout(page, 'Vector: no past episodes yet, says so', '.explain section.model', 4, 3.5);
  await at(page, 'The other models still answer', 0.2);
  await callout(page, 'The other models still answer', '.explain', 0, 2.5);
  await at(page, 'Open Show the SQL', 0.3);
  await click(page, page.locator('details.sqlview summary'), 0.6);
  await expect(page.locator('details.sqlview[open]')).toBeVisible();
  await callout(page, 'One statement: five models', 'details.sqlview', 0, 5);
  await page.evaluate(() => { const p = document.querySelector('details.sqlview pre') as HTMLElement; p.scrollTo({ top: p.scrollHeight, behavior: 'smooth' }); });
  await at(page, 'Here is the same card on the live fleet', 0.3);
  await badge(page, '');
  await stillPage(page, 'Explain tab for Ixelles (brussels): five models, one statement', [EXTOP, EXGEO], CAPTURED29);
  await shot(page, EXTOP, 50, 30, 1.0, 1.08, 3600);
  await at(page, 'Five data models', 0.2);
  await shot(page, EXTOP, 30, 35, 1.1, 1.25, 3000, [251, 298, 720, 320]);
  await at(page, 'Geo: the centre', 0.2);
  await shot(page, EXGEO, 40, 60, 1.0, 1.3, 5200, [251, 472, 1219, 612]);
  await at(page, 'Vector: three', 0.2);
  await setBanner(page, CAPTURED, 'Explain tab, vector part: an earlier capture, 28 Oct');
  await shot(page, EXGV, 40, 88, 1.25, 1.45, 3400, [251, 545, 1219, 700]);
  await at(page, 'That part is from the capture', 0.2);
  await shot(page, EXGV, 40, 88, 1.45, 1.55, 6000, [251, 545, 1219, 700]);
  await endScene(page);

  // ---- Scene 4: live data, a new vendor, the burst, the verdict --------------------------------------------------------------
  begin(4);
  await page.goto(stack.url + '/#overview');
  await expect(banner).toBeVisible();
  await badge(page, LOCAL);
  await at(page, 'Now, live data', 0.1);
  await moveTo(page, page.locator('.chartbox').first(), 1.0);
  await callout(page, 'The built-in simulator: thousands of sensors', '.kpis', 0, 4);
  const vendorBatch = {
    readings: [
      { sensor: 'paris.air-quality-d03-9001', type: 'air_quality', district: 'd03', city: 'paris', value: 14.2, lat: 48.8719, lon: 2.3321, attrs: { vendor: 'Zephyr Metering', firmware: '0.9.1', tags: ['pilot', 'rooftop'] } },
      { sensor: 'paris.noise-d03-9002', type: 'noise', district: 'd03', city: 'paris', value: 54.0, lat: 48.8723, lon: 2.3338, attrs: { vendor: 'Zephyr Metering', firmware: '0.9.1', calibration: { date: '2026-10-01', scale: 1.002 } } },
    ],
  };
  const SHELL = '<span class="p">$ </span>';
  let term = '<span class="dim"># a vendor we have never seen: a position and any JSON, in one batch</span>\n';
  await at(page, 'A new vendor can send anything', 0.1);
  await termPage(page, term);
  term = await typeLine(page, term, '$ ', 'cat new-vendor.json', 1.2);
  term += esc(JSON.stringify(vendorBatch.readings[0], null, 1).replace(/\n\s*/g, ' ')) + '\n' + esc(JSON.stringify(vendorBatch.readings[1], null, 1).replace(/\n\s*/g, ' ')) + '\n\n';
  await setTerm(page, term);
  const pushed = await fetch(stack.url + '/api/readings', { method: 'POST', headers: { 'content-type': 'application/json', authorization: `Bearer ${stack.ingestToken}` }, body: JSON.stringify(vendorBatch) });
  const answer = await pushed.json();
  expect(answer.accepted).toBe(2);
  expect(answer.rejected).toBe(0);
  await pause(page, 1.2);
  term = await typeLine(page, term, '$ ', 'curl -sS -X POST $CITYPULSE_URL/api/readings -H "Authorization: Bearer $INGEST_TOKEN" -d @new-vendor.json', 2.0);
  term += esc(JSON.stringify(answer)) + '\n\n';
  await setTerm(page, term);
  await at(page, 'It lands in a JSONB column', 0.2);
  const q1 = "SELECT attrs->>'vendor' AS vendor, count(*) FROM sensor GROUP BY 1 ORDER BY 2 DESC;";
  const out1 = stack.cluster.psql(DB, q1, 'cp_reader').trimEnd();
  expect(out1).toContain('Zephyr Metering');
  notes.push(`scene 4 vendor query:\n${out1}`);
  term += '<span class="dim"># sensor.attrs is JSONB with a GIN index, next to the relational columns</span>\n';
  term = await typeLine(page, term, 'citypulse=> ', q1, 2.4);
  term += esc(out1).replace(/(.*Zephyr Metering.*)/, '<span class="hl" id="zep">$1</span>') + '\n';
  await setTerm(page, term);
  await at(page, 'A plain query groups', 0.2);
  await callout(page, 'The new vendor, grouped like any other', '#zep', 0, 4);
  await at(page, 'Now press burst', 0.2);
  await page.goto(stack.url + '/#burst');
  await expect(page.locator('button.burst')).toBeVisible();
  await pause(page, 0.4);
  await moveTo(page, page.locator('button.burst'), 0.7);
  await at(page, 'A flood of readings', 0.3);
  const tBurst = Date.now();
  await click(page, page.locator('button.burst'), 0.2);
  await expect(page.locator('#toast')).toContainText('Burst started');
  const timeline: string[] = [];
  let sampling = true;
  const sampler = (async () => {
    while (sampling) {
      try {
        const j = await api('/api/status');
        timeline.push(`${Math.round((Date.now() - tBurst) / 1000)}s ${j.state} ${(j.health * 100).toFixed(1)}% stale=${j.counts.stale} offline=${j.counts.offline}`);
      } catch {}
      await sleep(5000);
    }
  })();
  await at(page, 'The flood and the slow database', 0.3);
  await click(page, nav('overview'), 0.5);
  await expect(page.locator('.banner', { hasText: 'Burst active' })).toBeVisible();
  await callout(page, 'Amber banner: a burst is active', '.banner', 0, 5);
  await at(page, 'A sensor is stale after', 0.2);
  await callout(page, 'stale = 3 missed reports, offline = 12', '.kpis', 0, 5);
  await at(page, 'Operational means', 0.2);
  await callout(page, 'Operational: 97% of sensors healthy or more', '.status-banner', 0, 4);
  await at(page, 'Below that, degraded', 0.2);
  await callout(page, 'Below 97%: degraded', '.status-banner', 0, 2.5);
  await at(page, 'Below eighty-five', 0.2);
  await callout(page, 'Below 85%: partial outage', '.status-banner', 0, 2.5);
  await at(page, 'Below fifty', 0.2);
  await callout(page, 'Below 50%: major outage', '.status-banner', 0, 2.5);
  await at(page, 'The stale tile climbs first', 0.2);
  await callout(page, 'Stale climbs first', '.kpis', 0, 3);
  await at(page, 'Watch the verdict', 0.2);
  await callout(page, 'The verdict follows the sensors', '.status-banner', 0, 3);
  await at(page, 'There it is', 0.4);
  const s4 = await api('/api/status');
  checks.push(`scene 4 "There it is": ${Math.round((Date.now() - tBurst) / 1000)} s after BURST, verdict ${s4.state}, health ${(s4.health * 100).toFixed(1)}%`);
  if (s4.state === 'operational') checks.push('FAIL: verdict still operational when the narration says degraded');
  await callout(page, `Verdict: ${String(s4.state).replace('_', ' ')}`, '.status-banner', 0, 4);
  // the incident must exist at PagerLens before scene 5 shows it (polled off camera)
  const incident = (async () => {
    for (let i = 0; i < 120; i++) {
      const r = await context.request.get(stack.pagerlensUrl + '/api/incidents');
      const j = r.ok() ? await r.json() : { incidents: [] };
      if (j.incidents?.length) return j.incidents[0];
      await sleep(1000);
    }
    return null;
  })();
  await endScene(page);

  // ---- Scene 5: the alert hand-off to PagerLens (a real, local PagerLens) ------------------------------------------------------
  begin(5);
  await at(page, 'CityPulse reports its own health', 0.1);
  await callout(page, 'Reports to PagerLens every 15 s', '.status-banner', 0, 5);
  await at(page, 'No one copies anything', 0.2);
  await callout(page, 'Two settings: PAGERLENS_URL and a token', null, 0, 3.5);
  await at(page, 'PagerLens opens an incident', 0.6);
  const inc = await incident;
  expect(inc, 'PagerLens opened an incident').toBeTruthy();
  checks.push(`scene 5: PagerLens incident ${JSON.stringify({ title: inc.title, severity: inc.severity, service: inc.service })} ${Math.round((Date.now() - tBurst) / 1000)} s after BURST`);
  await page.goto(stack.pagerlensUrl + '/#incidents');
  await expect(page.locator('tbody tr.clickable').first()).toBeVisible({ timeout: 20_000 });
  await badge(page, 'RUNNING LOCALLY: PagerLens, a separate sample app');
  await callout(page, 'An incident for the city, opened by the alert', 'tbody tr.clickable', 0, 5);
  await at(page, 'The alert says which city', 0.3);
  await click(page, page.locator('tbody tr.clickable td.title a').first(), 0.6);
  await expect(page.locator('.detail-head')).toContainText('paris');
  await callout(page, 'Which city, and the verdict', '.detail-head', 0, 5);
  await at(page, 'PagerLens investigates from there', 0.2);
  await callout(page, 'Investigation: another video', null, 0, 3.5);
  await endScene(page);
  // stop the burst (off camera, not narrated): the fleet should be healthy again for the rest of the video
  await fetch(stack.url + '/api/chaos/stop', { method: 'POST', headers: { cookie: `${stack.cityCookie.name}=${stack.cityCookie.value}`, 'X-CityPulse-CSRF': '1' } });
  sampling = false;

  // ---- Scene 6: Insights: local says not available; the captured fleet shows what EDB's extensions know --------------------------
  begin(6);
  await page.goto(stack.url + '/#insights');
  await expect(page.locator('#view-insights')).toContainText('Not available on this database', { timeout: 20_000 });
  await badge(page, LOCAL);
  await at(page, 'On this laptop', 0.3);
  await callout(page, 'Local Postgres: these extensions do not exist here', '#view-insights .insights-na', 0, 4);
  await at(page, 'On the live fleet', 0.3);
  await badge(page, '');
  const extLines = section('extensions-installed.txt', '# citypulse');
  await textCard(page, CAPTURED, 'agentpg extensions list, CityPulse database', 'Extensions installed on CityPulse\'s database', extLines);
  await at(page, 'Stat monitor', 0.2);
  const hot = extLines.map((l, i) => (/edb_stat_monitor|edb_pg_tuner|query_advisor/.test(l) ? i : -1)).filter((i) => i >= 0);
  await hotLines(page, hot);
  await at(page, 'This is the Insights tab of PayBroker', 0.3);
  await stillPage(page, "PayBroker's Insights tab: same kit, same fleet", [TOPQ, TUNE]);
  await shot(page, TOPQ, 50, 30, 1.0, 1.08, 5000);
  await pause(page, 0.5);
  await shot(page, TOPQ, 40, 55, 1.0, 1.3, 4500, [265, 388, 1205, 720]);
  await at(page, 'The tuner suggests settings', 0.2);
  await shot(page, TUNE, 40, 38, 1.0, 1.35, 4000, [265, 235, 1205, 428]);
  await at(page, 'The query advisor suggests indexes', 0.2);
  await shot(page, TUNE, 45, 82, 1.0, 1.35, 4500, [265, 480, 1205, 718]);
  await at(page, 'For CityPulse itself', 0.3);
  const adv = section('ecoctl-insights.txt', 'citypulse (pg-');
  await textCard(page, CAPTURED, 'ecoctl insights, CityPulse database', 'What query_advisor suggested for CityPulse itself', adv);
  const idx = adv.findIndex((l) => l.includes('sensor USING btree (last_ts)'));
  expect(idx).toBeGreaterThan(0);
  await at(page, 'About one percent', 0.2);
  await hotLines(page, [idx]);
  await endScene(page);

  // ---- Scene 7: a read-only user: reads the fleet, never the secrets (real psql, local database) -----------------------------------
  begin(7);
  const sel = [
    ['SELECT count(*) FROM sensor;', 'ok'],
    ["SELECT attrs->>'vendor' AS vendor, count(*) FROM sensor GROUP BY 1 ORDER BY 2 DESC LIMIT 3;", 'ok'],
    ['SELECT * FROM api_token;', 'err'],
  ];
  const outs = sel.map(([q]) => stack.cluster.psql(DB, q, 'cp_reader').trimEnd());
  expect(outs[0]).toMatch(/\d{4}/);
  expect(outs[1]).toContain('(3 rows)');
  expect(outs[2]).toMatch(/permission denied for table api_token/);
  notes.push(`scene 7 outputs:\n${outs.join('\n--\n')}`);
  const ready = await (await fetch(stack.url + '/readyz')).json();
  expect(ready.masking.ok).toBe(true);
  term = '<span class="dim"># a read-only database user, member of a1p_readonly (local Postgres)</span>\n';
  await termPage(page, term);
  await badge(page, '');
  await at(page, 'One more thing', 0.1);
  await at(page, 'I want an analyst', 0.2);
  await lower(page, 'An analyst, or an AI agent: read the fleet, never the secrets', '', 5);
  await at(page, 'This database user can only read', 1.8);
  term = await typeLine(page, term, '$ ', 'psql -U cp_reader citypulse', 1.5);
  term += '\n';
  await setTerm(page, term);
  await callout(page, 'cp_reader: a login in a1p_readonly, read-only', null, 0, 2.6);
  await at(page, 'Count the sensors', 0.2);
  term = await typeLine(page, term, 'citypulse=> ', sel[0][0], 1.2);
  term += esc(outs[0]) + '\n\n';
  await setTerm(page, term);
  await at(page, 'Group them by vendor', 0.2);
  term = await typeLine(page, term, 'citypulse=> ', sel[1][0], 2.4);
  term += esc(outs[1]) + '\n\n';
  await setTerm(page, term);
  await at(page, 'Ask for the API tokens', 0.2);
  term = await typeLine(page, term, 'citypulse=> ', sel[2][0], 1.2);
  term += esc(outs[2]).replace(/(ERROR:.*)/, '<span class="err" id="errl">$1</span>') + '\n';
  await setTerm(page, term);
  await callout(page, 'Denied by the database', '#errl', 0, 4);
  await at(page, 'CityPulse holds no personal data', 0.2);
  await page.evaluate(() => {
    const d = document.createElement('div');
    d.className = 'note';
    d.textContent = 'Nothing to mask column by column. SELECT is revoked on app_setting, api_token, web_session and oidc_login.';
    document.body.append(d);
  });
  await at(page, 'And its readiness check', 0.2);
  term += '\n<span class="p">$ </span>curl -s $CITYPULSE_URL/readyz\n' + esc(JSON.stringify(ready)) + '\n';
  await setTerm(page, term);
  await page.evaluate(() => (document.querySelector('.note') as HTMLElement).textContent = 'The readiness check reports the masking status: ok, drift 0.');
  await endScene(page);

  // ---- Scene 8: look how much easier this is ------------------------------------------------------------------------------------
  begin(8);
  await blank(page);
  await page.setContent(`<style>${CARD_CSS}.b{font-size:84px;font-weight:800}.s{font-size:46px;color:#ffb347}.row{display:flex;gap:22px}.k{border:4px solid #7aa7d6;border-radius:18px;padding:16px 30px;font-size:44px;font-weight:700}
    .x{text-decoration:line-through;opacity:0}.x.on{opacity:.55}.tag{display:inline-block;border:4px solid;border-radius:14px;padding:10px 26px;margin:8px;font-size:34px}
    .a{border-color:#34d399;color:#d1fae5}.f{border-color:#fb923c;color:#ffedd5}</style><div class="c">
    <p class="b dim" id="b1">Look how much easier this is.</p>
    <div class="row dim" id="b2"><span class="k">time series</span><span class="k">JSON</span><span class="k">geo</span><span class="k">vectors</span></div>
    <p class="s dim" id="b3">One Postgres. One statement: what is wrong, where, seen before?</p>
    <p class="b x dim" id="b4" style="font-size:56px">four databases to keep in sync</p>
    <p class="b dim" id="b5" style="font-size:64px">docker compose up</p>
    <p class="s dim" id="b6">docs/GUIDE.md</p>
    <p class="dim" id="b7"><span class="tag a">RAN LOCALLY: this laptop</span><span class="tag f">CAPTURED: live AgentPG fleet, 28 and 29 Oct 2026</span></p></div>`);
  await at(page, 'So, look how much easier', 0.1);
  await show(page, 'b1');
  await at(page, 'Time series, JSON, geography', 0.2);
  await show(page, 'b2');
  await at(page, 'One statement answers', 0.2);
  await show(page, 'b3');
  await at(page, 'No four databases', 0.2);
  await page.evaluate(() => document.getElementById('b4')!.classList.add('on'));
  await at(page, 'Try it yourself', 0.2);
  await show(page, 'b5');
  await at(page, 'The guide is in the docs folder', 0.2);
  await show(page, 'b6');
  await at(page, 'On this video', 0.2);
  await show(page, 'b7');
  await endScene(page);

  await sampler;
  checks.push(`timeline since BURST: ${timeline.join(' | ')}`);
  if (process.env.TIMES_JSON) {
    fs.mkdirSync(path.dirname(process.env.TIMES_JSON), { recursive: true });
    fs.writeFileSync(process.env.TIMES_JSON, JSON.stringify({ cardAt, scenes: times, sync, checks, notes }, null, 2));
  }
  console.log('checks:\n- ' + checks.join('\n- '));
  if (notes.length) console.log('run notes:\n- ' + notes.join('\n- '));
  void iso;
});
