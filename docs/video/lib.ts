/**
 * Local-only helpers for the recorder: a throwaway Postgres 17 cluster on 127.0.0.1 (no unix socket, a free port, databases whose
 * names end in `test`), a real PagerLens next to it (the alert hand-off of scene 5) and CityPulse itself (role `all`: the built-in
 * simulator reports through the real gateway). Nothing here reads a real environment: every process gets an explicit environment
 * and a private working directory, and everything is deleted at the end.
 */
import { ChildProcess, execFileSync, spawn, spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as net from 'node:net';
import * as os from 'node:os';
import * as path from 'node:path';

export const REPO = path.resolve(__dirname, '..', '..');
const PAGERLENS = path.resolve(REPO, '..', 'pagerlens');
export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const used = new Set<number>();
/** A free loopback port from CITYPULSE_PORT_RANGE (default 41500-41599). */
export async function freePort(): Promise<number> {
  const m = /^(\d+)-(\d+)$/.exec(process.env.CITYPULSE_PORT_RANGE ?? '41500-41599');
  if (!m) throw new Error('CITYPULSE_PORT_RANGE must look like 41500-41599');
  const tryPort = (port: number) =>
    new Promise<number | undefined>((resolve) => {
      const srv = net.createServer();
      srv.unref();
      srv.on('error', () => resolve(undefined));
      srv.listen(port, '127.0.0.1', () => {
        const got = (srv.address() as net.AddressInfo).port;
        srv.close(() => resolve(got));
      });
    });
  const lo = Number(m[1]);
  const hi = Number(m[2]);
  for (let i = 0; i < 300; i++) {
    const port = lo + Math.floor(Math.random() * (hi - lo + 1));
    if (used.has(port)) continue;
    if (await tryPort(port)) {
      used.add(port);
      return port;
    }
  }
  throw new Error(`no free port in ${lo}-${hi}`);
}

export async function waitFor<T>(what: string, fn: () => Promise<T | false | undefined> | T | false | undefined, ms: number): Promise<T> {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v as T;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await sleep(250);
  }
}

/** Directory with initdb, postgres, psql (pgvector and pg_trgm installed). RECORD_PG_BIN overrides; else whatever is on PATH. */
function pgBin(): string {
  if (process.env.RECORD_PG_BIN) return process.env.RECORD_PG_BIN;
  try {
    return path.dirname(execFileSync('/usr/bin/which', ['initdb'], { encoding: 'utf8' }).trim());
  } catch {
    throw new Error('Postgres binaries not found: put initdb on PATH or set RECORD_PG_BIN to their directory');
  }
}

export interface Cluster {
  port: number;
  url(db: string): string;
  /** Runs psql as `user` (default postgres) and returns its text output (errors included, the exit code is ignored). */
  psql(db: string, sql: string, user?: string): string;
  /** Does the cluster offer this extension? (pg_available_extensions) */
  offers(ext: string): boolean;
  stop(): void;
}

export async function startCluster(dir: string): Promise<Cluster> {
  const bin = pgBin();
  const env = { PATH: `${bin}:/usr/bin:/bin`, LANG: 'C', LC_ALL: 'C' };
  const data = path.join(dir, 'pgdata');
  const port = await freePort();
  execFileSync(path.join(bin, 'initdb'), ['-D', data, '-E', 'UTF8', '--locale=C', '-A', 'trust', '-U', 'postgres'], { env, stdio: 'pipe' });
  const log = fs.openSync(path.join(dir, 'postgres.log'), 'a');
  const proc: ChildProcess = spawn(
    path.join(bin, 'postgres'),
    ['-D', data, '-c', 'listen_addresses=127.0.0.1', '-c', `port=${port}`, '-c', 'unix_socket_directories=', '-c', 'fsync=off', '-c', 'max_connections=200'],
    { env, stdio: ['ignore', log, log] },
  );
  const run = (db: string, sql: string, user = 'postgres') => {
    const r = spawnSync(path.join(bin, 'psql'), ['-X', '-h', '127.0.0.1', '-p', String(port), '-U', user, '-d', db, '-c', sql], { env, encoding: 'utf8' });
    return (r.stdout ?? '') + (r.stderr ?? '');
  };
  const strict = (db: string, sql: string) =>
    execFileSync(path.join(bin, 'psql'), ['-X', '-h', '127.0.0.1', '-p', String(port), '-U', 'postgres', '-d', db, '-v', 'ON_ERROR_STOP=1', '-c', sql], { env, stdio: 'pipe' });
  const stop = () => {
    try {
      proc.kill('SIGINT');
    } catch {}
    try {
      execFileSync(path.join(bin, 'pg_ctl'), ['-D', data, 'stop', '-m', 'immediate', '-s'], { env, stdio: 'pipe', timeout: 15_000 });
    } catch {}
    try {
      proc.kill('SIGKILL');
    } catch {}
  };
  try {
    await waitFor('postgres', () => {
      try {
        execFileSync(path.join(bin, 'pg_isready'), ['-h', '127.0.0.1', '-p', String(port)], { env, stdio: 'pipe' });
        return true;
      } catch {
        return false;
      }
    }, 30_000);
    strict('postgres', "CREATE DATABASE citypulse_video_test ENCODING 'UTF8' TEMPLATE template0");
    strict('postgres', "CREATE DATABASE pagerlens_video_test ENCODING 'UTF8' TEMPLATE template0");
    for (const ext of ['vector', 'pg_trgm']) strict('pagerlens_video_test', `CREATE EXTENSION IF NOT EXISTS ${ext}`);
    // CityPulse: pgvector yes (the pattern memory), PostGIS and the edb_* extensions are NOT offered by a stock Postgres.
    strict('citypulse_video_test', 'CREATE EXTENSION IF NOT EXISTS vector');
    // What AgentPG gives an instance: the read-only group role a1p_readonly (and its default SELECT on new tables) and a login user in
    // it, like the one `agentpg db-users create --permission read` makes. The app's hardening revokes the secret tables from the group.
    strict('citypulse_video_test', `CREATE ROLE a1p_readonly NOLOGIN; CREATE ROLE cp_reader LOGIN IN ROLE a1p_readonly;
      GRANT CONNECT ON DATABASE citypulse_video_test TO a1p_readonly; GRANT USAGE ON SCHEMA public TO a1p_readonly;
      ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO a1p_readonly;`);
  } catch (e) {
    stop();
    throw e;
  }
  const offers = (ext: string) => run('postgres', `SELECT 1 FROM pg_available_extensions WHERE name = '${ext}'`).includes('(1 row)');
  return { port, url: (db) => `postgresql://postgres@127.0.0.1:${port}/${db}`, psql: run, offers, stop };
}

export interface Running {
  url: string;
  logs(): string;
  stop(): Promise<void>;
}

const BASE_ENV = (home: string) => ({ PATH: '/usr/bin:/bin', HOME: home, TMPDIR: home, LANG: 'en_US.UTF-8', PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1', NO_COLOR: '1', HOST: '127.0.0.1' });

function launch(cmd: string, args: string[], env: Record<string, string>, cwd: string, url: string, ready: string): Promise<Running> {
  const p = spawn(cmd, args, { env, cwd, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
  let buf = '';
  let exited: number | undefined;
  p.stdout!.on('data', (d) => (buf += d));
  p.stderr!.on('data', (d) => (buf += d));
  p.on('exit', (c) => (exited = c ?? -1));
  const running: Running = {
    url,
    logs: () => buf,
    async stop() {
      if (exited !== undefined) return;
      try {
        process.kill(-p.pid!, 'SIGTERM');
      } catch {}
      const end = Date.now() + 8000;
      while (exited === undefined && Date.now() < end) await sleep(100);
      if (exited === undefined) {
        try {
          process.kill(-p.pid!, 'SIGKILL');
        } catch {}
      }
    },
  };
  return waitFor(`${path.basename(cmd)} ${ready}`, async () => {
    if (exited !== undefined) throw new Error(`${cmd} exited early (${exited})\n${buf.slice(-2000)}`);
    try {
      return (await fetch(url + ready)).ok;
    } catch {
      return false;
    }
  }, 120_000).then(() => running, async (e) => {
    await running.stop();
    throw e;
  });
}

const CODE = /^[ \t]{2,}([A-Za-z0-9]{3,8}(?:-[A-Za-z0-9]{3,8}){1,4})[ \t]*$/m;
const PASSWORD = 'citypulse-demo-2026'; // throwaway: both databases are deleted after the take

export interface Stack {
  url: string;
  cluster: Cluster;
  pagerlensUrl: string;
  /** Cookie header of a signed-in CityPulse admin session (off camera) and of PagerLens, for the recording context. */
  cityCookie: { name: string; value: string };
  pagerCookie: { name: string; value: string };
  /** An ingest token of CityPulse (never shown on screen; terminal cards use a shell variable). */
  ingestToken: string;
  /** Everything CityPulse printed, for run notes only (it held the one-time setup code). */
  serverLog(): string;
  stop(): Promise<void>;
}

async function setupAndSession(base: string, logs: () => string, csrfHeader: string): Promise<{ name: string; value: string }> {
  const code = await waitFor('the setup code', () => logs().match(CODE)?.[1], 30_000);
  const r = await fetch(base + '/api/setup', { method: 'POST', headers: { 'content-type': 'application/json', [csrfHeader]: '1' }, body: JSON.stringify({ code, password: PASSWORD }) });
  if (!r.ok) throw new Error(`setup of ${base} failed: HTTP ${r.status}`);
  const set = r.headers.getSetCookie().map((c) => c.split(';')[0]);
  const first = set[0];
  if (!first) throw new Error('no session cookie after setup');
  const eq = first.indexOf('=');
  return { name: first.slice(0, eq), value: first.slice(eq + 1) };
}

/** Database, PagerLens (with its demo stand-in model, only there because PagerLens wants one) and CityPulse, all on loopback. */
export async function startStack(): Promise<Stack> {
  const work = fs.mkdtempSync(path.join(process.env.E2E_SCRATCH || os.tmpdir(), 'citypulse-video-'));
  const cluster = await startCluster(work);
  const stoppers: (() => Promise<void> | void)[] = [() => cluster.stop()];
  const stopAll = async () => {
    for (const s of stoppers.reverse()) {
      try {
        await s();
      } catch {}
    }
    fs.rmSync(work, { recursive: true, force: true });
  };
  try {
    // PagerLens
    const plPy = path.join(PAGERLENS, '.venv', 'bin', 'python');
    const plBin = path.join(PAGERLENS, '.venv', 'bin', 'pagerlens');
    if (!fs.existsSync(plBin)) throw new Error(`${plBin} not found: install PagerLens (uv venv && uv pip install -e ".[dev]") first`);
    const llmPort = await freePort();
    const llm = await launch(plPy, [path.join(PAGERLENS, 'scripts', 'fake_llm.py'), '--port', String(llmPort)], BASE_ENV(work), work, `http://127.0.0.1:${llmPort}`, '/v1/models');
    stoppers.push(() => llm.stop());
    const plPort = await freePort();
    const plEnv = { ...BASE_ENV(work), PORT: String(plPort), DATABASE_URL: cluster.url('pagerlens_video_test'), LLM_BASE_URL: `${llm.url}/v1`, LLM_API_KEY: 'demo', LLM_CHAT_MODEL: 'demo-chat', EMBEDDING_MODEL: 'demo-embed' };
    execFileSync(plBin, ['migrate'], { env: plEnv, cwd: work, stdio: 'pipe' });
    const pl = await launch(plBin, ['serve'], plEnv, work, `http://127.0.0.1:${plPort}`, '/readyz');
    stoppers.push(() => pl.stop());
    const pagerCookie = await setupAndSession(pl.url, pl.logs, 'X-PagerLens-CSRF');
    const tk = await fetch(pl.url + '/api/tokens', { method: 'POST', headers: { 'content-type': 'application/json', 'X-PagerLens-CSRF': '1', cookie: `${pagerCookie.name}=${pagerCookie.value}` }, body: JSON.stringify({ name: 'citypulse', scope: 'ingest' }) });
    if (!tk.ok) throw new Error(`PagerLens token: HTTP ${tk.status}`);
    const pagerToken = String((await tk.json()).token);

    // CityPulse: the venv's own binary, role all (API + built-in simulator), reporting to the PagerLens above
    const cpPort = await freePort();
    const cpBin = process.env.RECORD_CITYPULSE_BIN || path.join(REPO, '.venv', 'bin', 'citypulse');
    const cpEnv = {
      ...BASE_ENV(work),
      PORT: String(cpPort),
      DATABASE_URL: cluster.url('citypulse_video_test'),
      CITYPULSE_ROLE: 'all',
      CITYPULSE_CITY: 'paris',
      SIM_SENSORS: '3000',
      SIM_SPEED: process.env.RECORD_SIM_SPEED ?? '1',
      PAGERLENS_URL: pl.url,
      PAGERLENS_TOKEN: pagerToken,
    };
    const cp = await launch(cpBin, ['start'], cpEnv, work, `http://127.0.0.1:${cpPort}`, '/readyz');
    stoppers.push(() => cp.stop());
    const cityCookie = await setupAndSession(cp.url, cp.logs, 'X-CityPulse-CSRF');
    // an ingest token for the "new vendor" batch (made through the API, off camera; it never appears on screen)
    const it = await fetch(cp.url + '/api/tokens', { method: 'POST', headers: { 'content-type': 'application/json', 'X-CityPulse-CSRF': '1', cookie: `${cityCookie.name}=${cityCookie.value}` }, body: JSON.stringify({ name: 'new vendor', scope: 'ingest' }) });
    if (!it.ok) throw new Error(`CityPulse token: HTTP ${it.status}`);
    const ingestToken = String((await it.json()).token);
    return { url: cp.url, cluster, pagerlensUrl: pl.url, cityCookie, pagerCookie, ingestToken, serverLog: () => cp.logs().replace(CODE, '  (setup code hidden)'), stop: stopAll };
  } catch (e) {
    await stopAll();
    throw e;
  }
}
