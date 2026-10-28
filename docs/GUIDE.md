# CityPulse guide

Version described: CityPulse 0.1.0 (`src/citypulse/__init__.py`), built on `agentpg-kit` 0.2.5 as pinned in the Dockerfile and the CI workflow; the code also uses the kit's 0.3.0 features (database insights, `migrate.run(after_migrate=...)`), so the pin must be raised to `>=0.3.0` at release (a test asserts the installed kit is at least 0.3.0). New in this version: [7.8 Map](#78-map-tab-postgis), [7.9 Insights](#79-insights-tab-admin), [7.10 read-only access](#710-what-a-read-only-user-and-agentpgs-restricted-mcp-can-see) and [13 EDB and AgentPG capabilities used by this app](#edb-and-agentpg-capabilities-used-by-this-app).
Everything below was checked against the code in this repository and, for the ecosystem parts, against
`ecosystem/ecosystem.yaml` and `ecosystem/src/ecoctl`. Where something could not be verified it is listed under
[Known limitations](#16-known-limitations-and-unverified-points). No live URL appears in this document on purpose: sandbox URLs change
whenever an app is recreated, so every address below is a variable.

Contents: [1 Purpose](#1-purpose-and-audience) · [2 Ecosystem](#2-how-citypulse-fits-the-ecosystem) · [3 Architecture](#3-architecture) ·
[4 Install](#4-install) · [5 Configuration](#5-configuration-reference) · [6 First run](#6-first-run-walkthrough) · [7 Features](#7-using-citypulse) ·
[8 Roles](#8-roles-scopes-and-permissions) · [9 Security](#9-security-notes) · [10 Operations](#10-operations) · [11 Troubleshooting](#11-troubleshooting) ·
[12 Testing](#12-testing-and-development) · [13 AIDB and AgentPG](#13-aidb-and-agentpg-integration-points) · [14 HTTP reference](#14-http-reference) ·
[15 Data model](#15-data-model) · [16 Known limitations](#16-known-limitations-and-unverified-points)

---

## 1. Purpose and audience

CityPulse is a smart-city sensor platform on Postgres (plain Postgres is enough; it also shows PostGIS, EDB's query insights and AgentPG's read-only hardening where the platform offers them). Sensors (real, or simulated) push readings to an **ingest gateway**; a
**status engine** judges every sensor every 10 seconds and rolls the verdicts up per sensor type, per district, per city and for the whole
fleet; a **dashboard** shows the result; and a **burst** button injects seven kinds of fault on purpose so that a monitoring tool can find,
explain and report the incident.

It is a sample project. It exists to show three things:

1. **A realistic data path on Postgres.** Validation, back-pressure (HTTP 429 with `Retry-After`), batched writes, a retention job and a status engine, all in SQL and Python, no queue and no time-series database.
2. **A target worth monitoring.** The fault catalogue (flood, noise, dropout, stuck, late, corrupt, slow database) produces the three patterns [PagerLens](#2-how-citypulse-fits-the-ecosystem) diagnoses: ingest overload, sensor-fleet outage and bad data.
3. **A small app built on the shared kit.** Sign-in, roles, tokens, migrations and UI components come from `agentpg-kit`; the rest is about 4,000 lines of Python plus a dependency-free browser UI.

Who it is for:

| Reader | Use |
|---|---|
| Customer or presenter | Press the burst button, watch the verdict change, see PagerLens react (the [video script](VIDEO-SCRIPT.md) follows exactly that). |
| Developer | A readable example of an ingest path, a status engine and an OIDC-federated FastAPI app on `agentpg-kit`. |
| Operator of the ecosystem | Deployed by `ecoctl` as `citypulse`; fed by the CityPulse Simulator; reports to PagerLens. |

CityPulse contains **no AI code** today (no LLM calls, no embeddings, no MCP server). Its AI role in the showcase is indirect: it is the
workload that PagerLens investigates with AI. See [section 13](#13-aidb-and-agentpg-integration-points).

---

## 2. How CityPulse fits the ecosystem

The fleet is described in `ecosystem/ecosystem.yaml`. CityPulse's entry:

```yaml
citypulse:
  name: CityPulse
  env: {CITYPULSE_ROLE: api}        # no built-in simulator: data comes from CityPulse Simulator
  roles: [admin, operator, viewer]
  exposes: [citypulse.ingest, citypulse.read, citypulse.metrics]
  calls:   {pagerlens: [pagerlens.ingest]}
```

Each app in the fleet runs on its own AgentPG sandbox with its own database. CityPulse has no `llm: true`, no `generate:` secrets (nothing to
encrypt) and no extensions requested in the manifest (`extensions` is empty, so plain Postgres is enough). The app itself tries `CREATE EXTENSION IF NOT EXISTS postgis` at every start ([7.8](#78-map-tab-postgis)); if the platform refuses, only the geographic features switch off.

```mermaid
flowchart LR
  subgraph field[Field side]
    SIM[CityPulse Simulator<br/>cpsim]
    COL[Other collectors<br/>OTel Collector, scripts]
  end
  IDP[identity<br/>OIDC provider]
  CP[CityPulse<br/>role api]
  PL[PagerLens]
  USER[Browser user]
  SIM -- "POST /api/readings<br/>client_credentials, citypulse.ingest" --> CP
  COL -- "/api/readings or /v1/metrics<br/>ingest token" --> CP
  USER -- "PKCE sign-in, roles admin/operator/viewer" --> IDP
  USER --> CP
  CP -- "client_credentials, pagerlens.ingest<br/>services, metrics, alerts, deploy" --> PL
  PL -. "optional: scrape /metrics<br/>(citypulse.metrics or metrics token)" .-> CP
  CP -- "token request" --> IDP
  SIM -- "token request" --> IDP
```

### Interaction with each project

| Project | Direction | Mechanism | Details (verified in code or manifest) |
|---|---|---|---|
| **identity** | CityPulse relies on it | Browser: authorization code + PKCE (`/auth/login`, `/auth/callback`). Service: `client_credentials` to get CityPulse's own PagerLens token. | Enabled only when `CITYPULSE_OIDC_ISSUER` is set (`core.fed = from_env("CITYPULSE", "citypulse")`). The web client id is `CITYPULSE_OIDC_CLIENT_ID` (default `citypulse`); redirect URI is `<public url>/auth/callback`. Tokens are verified locally against the provider's JWKS (issuer, audience, expiry, algorithm allow-list). Roles come from the `roles` claim of the ID token (callback) and are mapped in `core.ROLE_PERMISSIONS`. A user with none of this app's roles is turned away with "your account has no role in CityPulse". `ecoctl` sets `CITYPULSE_OIDC_ISSUER`, `CITYPULSE_OIDC_CLIENT_ID`, `CITYPULSE_OIDC_CLIENT_SECRET`, `CITYPULSE_SERVICE_CLIENT_ID` and `CITYPULSE_SERVICE_CLIENT_SECRET` (`ecosystem/src/ecoctl/plan.py`). |
| **CityPulse Simulator (cpsim)** | cpsim calls CityPulse | HTTPS `POST /api/readings` with a bearer token | cpsim's service client requests scope `citypulse.ingest` (manifest `cpsim.calls`), which maps to the single permission `ingest:write`: it can push readings and read nothing. Its sender must include `city` on every reading. cpsim receives CityPulse's address as `CITYPULSE_URL` (injected by `ecoctl`; a recreate of CityPulse cascades to cpsim). The simulator itself is documented in its own project; from CityPulse's side it is just another authenticated collector, and the optional `protocol` field of a reading (`modbus_tcp`, `ethernet_ip`, `ethercat`, `profinet`, `profibus`, `http`, `otlp`, `direct`) is how it labels the emulated field protocol. |
| **PagerLens** | CityPulse calls PagerLens | HTTPS JSON to `/api/ingest/*` with a bearer token | Enabled when `PAGERLENS_URL` is set and either `PAGERLENS_TOKEN[_FILE]` or the service client is configured. `ecoctl` injects `PAGERLENS_URL` (callee address) because the manifest lists `calls: {pagerlens: [pagerlens.ingest]}`. Token source: a static `PAGERLENS_TOKEN` wins; otherwise `TokenSource(issuer, citypulse-service, scope pagerlens.ingest, audience pagerlens)` fetches and refreshes one. Calls made: `POST /api/ingest/services` (register `citypulse-ingest`, `citypulse-fleet`, `citypulse-fleet-<city>`), `POST /api/ingest/deploys` (once, on start), `POST /api/ingest/metrics` (every 15 s), `POST /api/ingest/alerts` (`SensorFleetDegraded`). Details in [7.7](#77-reporting-to-pagerlens). PagerLens may also scrape CityPulse's `/metrics` (scope `citypulse.metrics`, or a local `metrics` token); that is configured on the PagerLens side (`pagerlens-agent`), not here. |
| **agentpg-kit** | Library | Python dependency `agentpg-kit>=0.2.5` | Provides `Database` (retrying connect), `migrate`, `settings_store`, `auth` (`AppAuth`, `LocalAuth`, `Authenticator`, relying party, token verifier, `TokenSource`), `guard.SecurityGuard` (headers, body cap, CSRF), `metrics`, `web` helpers and the UI kit served at `/kit/`. CityPulse does not use the kit's vault, LLM client, SSRF guard or MCP helpers. |
| **ecoctl** | Deploys it | Manifest entry above | `ecoctl` creates the database and sandbox, registers the web and service clients in identity, injects the environment, and replaces the placeholder redirect URI with the real `/auth/callback`. See [4.3](#43-deploy-with-ecoctl). |
| **llm-gateway** | None | | CityPulse does not call it (no `llm: true` in the manifest, no `LLM_*` variables read). |
| **TrustDesk** | None | | No interaction. (The simulator, not CityPulse, can generate TrustDesk requests.) |
| **PayBroker, ShopFront** | None | | No interaction. |

Scopes CityPulse **exposes** (what other services may request from it): `citypulse.ingest`, `citypulse.read`, `citypulse.metrics`. Scope it
**requests** from others: `pagerlens.ingest`. Roles it registers in identity: `admin`, `operator`, `viewer`.

Deployment role: in the fleet CityPulse runs with `CITYPULSE_ROLE=api` (gateway, dashboard, status engine, reporter; no built-in simulator).
Without the Simulator running, an `api`-only CityPulse shows "No sensors have reported yet".

---

## 3. Architecture

```mermaid
flowchart TB
  subgraph senders[Senders]
    S1[Push API<br/>POST /api/readings]
    S2[OTLP/HTTP<br/>POST /v1/metrics]
    S3[Built-in simulator<br/>role sim or all]
  end
  subgraph app[CityPulse process - FastAPI, one image]
    GW[Ingest gateway<br/>validate, slots, 429]
    CH[Burst controls<br/>chaos rows]
    SE[Status engine<br/>every 10 s, advisory lock]
    API[Dashboard API<br/>status, cities, sensors, history]
    UI[Static UI<br/>no inline script or style]
    REP[PagerLens reporter<br/>every 15 s]
    MET[/metrics + /healthz + /readyz/]
    AUTHN[Authenticator<br/>session, tokens, JWT]
  end
  DB[(Postgres<br/>sensor, reading, status_history,<br/>chaos, app_setting, api_token, web_session)]
  PL[PagerLens]
  S1 --> GW
  S2 --> GW
  S3 -->|direct call in role all<br/>HTTP in role sim| GW
  GW --> DB
  CH --> DB
  GW -. reads bursts .-> CH
  S3 -. reads bursts .-> CH
  SE --> DB
  API --> DB
  UI --> API
  AUTHN --- API
  AUTHN --- GW
  REP --> SE
  REP --> PL
  MET --> SE
```

### Components (source: `src/citypulse/`)

| Module | Responsibility |
|---|---|
| `cli.py` | `citypulse migrate`, `serve`, `start` (migrate then serve; the container default), `reset-admin`, `seed-history` (optional demo history for the pattern memory). `serve` runs uvicorn with `HOST` (default `127.0.0.1`), `PORT` (default `8000`), `LOG_LEVEL` (default `info`), `proxy_headers=True`. |
| `api.py` | FastAPI app. Lifespan starts the background tasks according to `CITYPULSE_ROLE`. All routes ([section 14](#14-http-reference)). OpenAPI docs are disabled (`docs_url`, `redoc_url`, `openapi_url` are `None`). |
| `gateway.py` | Validates each reading, applies back-pressure (`INGEST_CONCURRENCY` write slots, 3 s wait for a pooled connection), writes one transaction per batch (`reading` insert plus `sensor` upsert), enforces the city rules and the sensor cap. |
| `otlp.py` | Decodes OTLP/HTTP metrics (protobuf or JSON, gzip bounded at 8 MB) into the same readings and hands them to the gateway. |
| `status.py` | The status engine: judging SQL, roll-ups, tiers, history snapshots, retention pruning. Runs every 10 s; only the holder of advisory lock `727402` works, so several replicas are safe. |
| `chaos.py` | Burst definitions and the `chaos` table logic; `storm()` is the red button. |
| `sim.py` | The built-in simulator (27 sensor types), with a bounded retry buffer and back-off. `DirectSink` (role `all`) or `HttpSink` (role `sim`). Writes a heartbeat to `app_setting` every 5 s. |
| `catalog.py` | The 27 sensor types with limits, daily curves and report intervals; id patterns; `LEGACY_CITY = "default"`. |
| `geo.py` | Geography: built-in city centres, deterministic district polygons and sensor positions, the PostGIS schema hook (`ensure`) and the queries behind the Map (`nearby`, `districts_geojson`, `sensors_geojson`). Degrades without PostGIS. |
| `timeseries.py` | Roll-ups of `reading` (`rollup_5m`, partitioned by month, and `rollup_1h`) with `date_bin`, refreshed by the engine; partition creation and retention; the series query ([7.13](#713-time-series-on-core-postgres)). |
| `memory.py`, `history.py` | Episode fingerprints (16-number vector per district and 15 minutes), the pgvector schema hook, outcome labelling and similarity search; `history.py` is the deterministic demo history behind `citypulse seed-history` ([7.14](#714-pattern-memory-have-we-seen-this-before)). |
| `explain.py`, `tools.py` | The one-statement explain query across five data models and the tool definitions (JSON Schema) an agent can call ([7.15](#715-explain-this-district-one-query-across-all-five-models)). |
| `hardening.py` | After-migrate hook: REVOKE SELECT from AgentPG's `a1p_readonly` role on every table that holds secrets ([7.10](#710-what-a-read-only-user-and-agentpgs-restricted-mcp-can-see)). |
| `reporter.py` | Optional PagerLens reporter. |
| `purge.py` | Chunked city deletion (advisory lock `727403`). |
| `core.py`, `auth.py`, `settings_store.py`, `db.py`, `migrate.py`, `metrics.py` | The app's instances of the kit blocks, the permission/role/scope tables, a lazily opened psycopg pool (`min 1`, `max 10`), migrations (advisory lock `727401`), Prometheus collectors. |
| `static/` | The UI: `index.html` plus ES modules (`overview.js`, `map.js`, `explain.js`, `sensors.js`, `burst.js`, `connect.js`, `settings.js`, `cities.js`, `districts.js`, `citydelete.js`, `charts.js`, `fmt.js`, `app.js`). Built with DOM text nodes only; the CSP forbids inline script and style. Shared widgets come from `/kit/` (including `insights.js`). |

### Roles of one image (`CITYPULSE_ROLE`)

| Role | Gateway, dashboard, status engine, reporter | Built-in simulator | Notes |
|---|---|---|---|
| `all` (default) | yes | yes, writes straight into the gateway (`DirectSink`) | Needs `CITYPULSE_CITY`. |
| `api` | yes | no | The fleet setting. Needs nothing else. |
| `sim` | no | yes, pushes over HTTP (`HttpSink`) | Needs `CITYPULSE_CITY`, `CITYPULSE_INGEST_URL`, `CITYPULSE_INGEST_TOKEN`. Every dashboard route answers 404 ("this instance runs the simulator only"). `/healthz`, `/readyz`, `/metrics` still work. |

### Status judgement (per sensor, first match wins)

`offline` (no reading for 12 reporting intervals) → `stale` (3 intervals) → `fault` (latest value outside the type's physical limits) →
`stuck` (five or more identical readings within six intervals, continuous types only) → `ok`. A city or the fleet is **operational** at
≥97% ok, **degraded** at ≥85%, **partial outage** at ≥50%, otherwise **major outage**. A district with at least 20 sensors and under 50% ok
forces at least a partial outage. A sensor type under 85% ok (at least 5 sensors) is listed as a reason but never changes the verdict.
Nothing is smoothed. Each sensor type's report interval is in `catalog.py` (for example 30 s for temperature, 60 s for pressure).

### Data retention (`status.prune`, run every 60 s by the engine)

- `reading` rows older than `RETENTION_HOURS` (default 6) are deleted in batches of 50,000.
- `status_history` rows older than `RETENTION_HOURS` are deleted.
- Sensors silent for 24 hours are deleted, except those in the legacy city `default`.
- Expired `chaos` rows (over an hour past their end) are deleted.
- `rollup_5m` keeps 35 days (whole monthly partitions are dropped), `rollup_1h` 400 days; `episode` rows are never pruned (about 1,150 rows a day per 12 districts) and go with their city ([7.13](#713-time-series-on-core-postgres), [7.14](#714-pattern-memory-have-we-seen-this-before)).

---

## 4. Install

Requirements: Python 3.11 or newer (`pyproject.toml`; the image uses 3.14), Postgres (plain Postgres is enough; the compose file uses `postgres:17`; CI uses `pgvector/pgvector:pg17` but CityPulse itself needs no extension; PostGIS is optional, see [7.8](#78-map-tab-postgis)), Node.js only if you want the UI script tests to run.

### 4.1 Local development with a throwaway Postgres

The kit is a sibling checkout: `pyproject.toml` has `[tool.uv.sources] agentpg-kit = { path = "../agentpg-kit", editable = true }`, so keep `agentpg-kit` next to `CityPulse`
(both under `dev-cloud/`). Without that layout, install the published tag instead (the Dockerfile uses
`https://github.com/elchinoo/agentpg-kit/archive/refs/tags/v0.2.5.tar.gz`).

```bash
# 1. a disposable database (any local Postgres works; this one is removed with the container)
docker run -d --rm --name citypulse-pg -e POSTGRES_PASSWORD=devonly -e POSTGRES_USER=citypulse \
  -e POSTGRES_DB=citypulse -p 127.0.0.1:5434:5432 postgres:17

# 2. the app
uv venv && uv pip install -e ".[dev]"          # or: python -m venv .venv && .venv/bin/pip install -e ../agentpg-kit -e ".[dev]"
export DATABASE_URL=postgresql://citypulse:devonly@127.0.0.1:5434/citypulse
export CITYPULSE_CITY=paris                     # required for roles all and sim; use CITYPULSE_ROLE=api to skip the simulator
export PORT=8002
.venv/bin/citypulse start                       # migrate, then serve on http://127.0.0.1:8002
```

The one-time setup code is printed to the server's stderr at startup (see [6](#6-first-run-walkthrough)). Stop with Ctrl-C; remove the database with
`docker stop citypulse-pg`.

`make serve` runs `.venv/bin/citypulse start`, so export the variables first. `make up` and `make down` start and stop the compose database only.

### 4.2 Docker Compose

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(24))"     # put the output in POSTGRES_PASSWORD in .env
# .env.example already sets CITYPULSE_CITY=paris; compose refuses to start without it
docker compose up --build
docker compose logs api                                            # the setup code is here
```

What `docker-compose.yml` does: service `db` (`postgres:17`, user/database `citypulse`, password from `POSTGRES_PASSWORD`, published on
`127.0.0.1:5434`, volume `pgdata`, healthcheck `pg_isready`); service `api` (built from the `Dockerfile`; `CITYPULSE_ROLE=all`,
`CITYPULSE_CITY` from `.env`, `SIM_SENSORS=3000`, published on `127.0.0.1:8002` mapped to container port 8000). Both ports are bound to
loopback only.

The image: `python:3.14-slim`, runs as the non-root user `app`, `ENV HOST=0.0.0.0 PORT=8000`, `HEALTHCHECK` on `/healthz`, `CMD ["citypulse", "start"]`.
It contains no secrets: the connection comes from `DATABASE_URL`, `DB_EXTERNAL_DSN`, `PSQL_CONNECTION_STRING` (first one set wins) or, if none is set, the
libpq `PG*` variables.

The GitHub workflow `.github/workflows/build-image.yml` runs ruff and pytest against a pgvector Postgres service, then builds and pushes
`ghcr.io/<owner>/citypulse` with tags `latest`, `sha-…` and any `v*` tag (nightly rebuild without cache).

### 4.3 Deploy with ecoctl

`ecoctl` (in `ecosystem/`) deploys the whole fleet; CityPulse is one entry of the manifest. It needs the `agentpg` CLI logged in, and the public
image `ghcr.io/elchinoo/citypulse`.

```bash
cd ecosystem
.venv/bin/ecoctl validate
.venv/bin/ecoctl plan                         # every command and API call; changes nothing
.venv/bin/ecoctl env citypulse                # the environment CityPulse would be created with (secrets masked; --reveal shows them)
.venv/bin/ecoctl apply --execute              # asks first; safe to run again
.venv/bin/ecoctl status                       # health and readiness of each app, drift from identity
.venv/bin/ecoctl recreate citypulse           # new image or changed environment; cascades to apps that name it (cpsim)
.venv/bin/ecoctl db citypulse                 # print the database key of CityPulse's instance
```

What `apply` does for CityPulse: identity first, then (callees before callers) PagerLens, then CityPulse (because it calls PagerLens), then cpsim (which calls CityPulse).
For CityPulse it registers the web client `citypulse` and the service client `citypulse-service` (allowed to request `pagerlens.ingest`), creates
the database and sandbox, sets `CITYPULSE_ROLE=api` and the OIDC and `PAGERLENS_URL` variables, and points the redirect URI at the real `https://<sandbox>/auth/callback`.
Environment of a running sandbox cannot be changed in place; delete and recreate it (the database keeps its data).

Manual deployment on AgentPG without `ecoctl` is in the README (section "Deploy on AgentPG"): `agentpg create`, `agentpg sandbox create citypulse --image … --instance <id> --port 8000 --env CITYPULSE_ROLE=all --env CITYPULSE_CITY=<city>`. `--port 8000` is required and `--env` is not a secret store.

---

## 5. Configuration reference

All variables are read in `config.py`, `core.py`, `cli.py`, `reporter.py`, the kit's `config.py`/`auth/env.py`/`auth/local.py`, or `Dockerfile`. Every secret
accepts a `<NAME>_FILE` variant that names a file whose first line is the value (kit `read_secret`).
Invalid numeric values silently fall back to the default; out-of-range values are clamped.

### Core and database

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL`, `DB_EXTERNAL_DSN`, `PSQL_CONNECTION_STRING` | none | Postgres DSN; first one set wins. If none is set, libpq uses `PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` (AgentPG sandboxes linked to an instance provide these). |
| `CITYPULSE_ROLE` | `all` | `api`, `sim` or `all` (case-insensitive). Any other value exits at start. |
| `HOST` | `127.0.0.1` (the image sets `0.0.0.0`) | Bind address of `citypulse serve/start`. |
| `PORT` | `8000` | Bind port. The image's healthcheck reads it. |
| `LOG_LEVEL` | `info` | uvicorn log level. |
| `PYTHONUNBUFFERED` | set to `1` in the image | So the setup code reaches the log immediately. |
| `SSL_CERT_FILE` | `/etc/ssl/certs/ca-certificates.crt` in the image | Lets `sslrootcert=system` find roots with the bundled psycopg. |

### Simulator and gateway

| Variable | Default | Range | Meaning |
|---|---|---|---|
| `CITYPULSE_CITY` | none | `[a-z0-9][a-z0-9_-]{0,31}`, not `default` | City of the built-in simulator. Required for roles `sim` and `all` (start fails with the fix in the message). Sensor ids become `<city>.<type>-<district>-<n>`. |
| `SIM_SENSORS` | `3000` | 10 to 50,000 | Number of simulated sensors. |
| `SIM_SPEED` | `1` | 1 to 20 | Reporting-rate multiplier. |
| `CITYPULSE_INGEST_URL` | none | | Role `sim`: base URL to push to. |
| `CITYPULSE_INGEST_TOKEN[_FILE]` | none (file default `/etc/citypulse/ingest_token`) | | Role `sim`: an `ingest`-scope token. Without URL and token the `sim` role exits. |
| `RETENTION_HOURS` | `6` | 0.1 to 168 | How long raw readings and status history are kept. |
| `MAX_SENSORS` | `50000` | 100 to 1,000,000 | Cap on auto-registered sensors; readings of further new sensors are rejected with "sensor limit reached". |
| `INGEST_CONCURRENCY` | `8` | 1 to 64 | Batches written at once before the gateway answers 429. |

### Authentication and single sign-on

| Variable | Default | Meaning |
|---|---|---|
| `CITYPULSE_OIDC_ISSUER` | none | Identity provider URL. Federation is off when unset. |
| `CITYPULSE_OIDC_CLIENT_ID` | `citypulse` | Web client id (also the expected token audience). |
| `CITYPULSE_OIDC_CLIENT_SECRET[_FILE]` | none | Web client secret. |
| `CITYPULSE_PUBLIC_URL` | none | The URL CityPulse is reached at; used to build the callback and logout return. If unset, the request's own host (and forwarded protocol) is used. `ecoctl` does not set it (not found in `ecosystem/src/ecoctl`). |
| `CITYPULSE_LOCAL_LOGIN` | `true` | With SSO on, `false` makes the server refuse the admin-password and API-key sign-in (`403`). |
| `CITYPULSE_API_KEY[_FILE]` | none (file default `/etc/citypulse/api_key`) | Optional static full-access key for automation; also usable as a sign-in key in the UI. |
| `CITYPULSE_ALLOW_ANONYMOUS` | `false` | Demo only: disables authentication entirely. Accepts `1`, `true`, `yes`, `on`. |
| `CITYPULSE_SERVICE_CLIENT_ID` | `citypulse-service` | CityPulse's own service client, used to get its PagerLens token. |
| `CITYPULSE_SERVICE_CLIENT_SECRET[_FILE]` | none | Its secret. |

### PagerLens reporting

| Variable | Default | Meaning |
|---|---|---|
| `PAGERLENS_URL` | none | PagerLens base URL. `http://` is refused unless the host is `localhost` or `127.0.0.1` (a message is logged and the reporter does not start). |
| `PAGERLENS_TOKEN[_FILE]` | none (file default `/etc/citypulse/pagerlens_token`) | An ingest token created in PagerLens. A static token wins over the identity-minted one. |

The reporter starts only in roles `api` and `all`, and only if the URL is set and a token or the service-client credentials (issuer plus service client secret) exist.

No variable named `LLM_*`, `EMBEDDING_*`, `AIDB_*` or `*_ENCRYPTION_KEY` is read by CityPulse.

---

## 6. First-run walkthrough

### Standalone (no identity provider)

1. Start the app ([4.1](#41-local-development-with-a-throwaway-postgres) or [4.2](#42-docker-compose)). Until an admin password exists, every protected route is denied.
2. Read the one-time setup code from the log (stderr):
   ```
   === CityPulse first-run setup ===
       <code>
   ```
   Compose: `docker compose logs api`. The code lives in memory only and changes on every restart until setup is complete.
3. Open the app. The page offers "Setup code" and a password field. Enter the code and an admin password of at least 12 characters. You are signed in.
4. With role `all`, the simulator needs about half a minute for every sensor to report once (each type reports every 30 to 60 s or so, first reports spread over one interval). The Overview then shows the heatmap.
5. Open **Burst** and press the red button.
6. Create tokens for real senders under **Settings** (see [7.5](#75-settings-tokens-and-admin-password)).

Lost the admin password: `citypulse reset-admin` (needs `DATABASE_URL`), then restart; a new setup code is printed (standalone only: with SSO on, set a new password under Settings after an SSO sign-in).

### Federated (the ecosystem)

1. `ecoctl apply --execute` created everything ([4.3](#43-deploy-with-ecoctl)). No setup code is printed when `CITYPULSE_OIDC_ISSUER` is set (the setup flow is skipped when federation is on).
2. Open CityPulse and press **Sign in with single sign-on**. After sign-in the roles from identity decide what you see: `admin` all tabs, `operator` all but Settings, `viewer` Overview, Sensors and Connect.
3. If you see "your account has no role in CityPulse", add the person to a group that carries a CityPulse role (the manifest's `ecosystem-admins` group is created by `ecoctl`).
4. With the Simulator running and pushing to this CityPulse, readings appear within seconds.
5. A federated instance starts with **no local admin password**: no setup code is printed and the login page shows only the SSO button. To have a break-glass, sign in with SSO as an admin once and set a password under **Settings, Admin password** (or provide `CITYPULSE_API_KEY`). The login page then also offers "Administrator password" (and the key) unless `CITYPULSE_LOCAL_LOGIN=false`, which refuses both with `403`. Do this before you need it: with the identity provider down and neither a password nor a key set, there is no way in locally except adding `CITYPULSE_API_KEY` and restarting.

---

## 7. Using CityPulse

### 7.1 Overview tab

- **Status banner**: "All systems operational", "Degraded", "Partial outage" or "Major outage", the percentage healthy, and up to eight reasons (fleet percentage, a district that forced the verdict, sensor types under 85%).
- **KPI tiles**: Sensors, OK, Stale, Offline, Fault, Stuck, and (only when a built-in simulator reports a heartbeat) **Simulator buffer**.
- **Cities needing attention** (when two or more cities exist): worst cities first; click a row to drill in.
- **Heatmap**: districts by sensor type, coloured by share ok (five levels). Cells are buttons that appear to link to the Sensors tab filtered by type (not verified in the browser).
- **Charts**: "Fleet health, last hour (%)" and "Readings per 30 s (by event time)". A city's health series only exists while that city was tracked (see [10](#10-operations)).
- **By sensor type** and, if more than one protocol reports, **By collection protocol** tables.
- A **Burst active** banner lists active bursts with time left. The page refreshes every few seconds; a failed first load shows the error and a "Try again" button.
- **City menu** (top bar, remembered in the browser): searchable, asks the server for the first 12 matches, least healthy first. It filters Overview, Sensors and Burst.
- **Delete city (Admin)**: on the Overview of a chosen city, shown only to holders of `city:delete`. Opens a panel that requires typing the city id.

### 7.2 Sensors tab

Filters: State (`problem` by default, `all`, `ok`, `stale`, `offline`, `fault`, `stuck`), Type, District (built from what the city really reports), and a text search that matches the sensor id (substring, case-insensitive). Up to 200 rows
(the API allows 500). Columns: Sensor, City, Type, District, Protocol, State, Last value, Last reading, Why. Click a row for the sensor detail (last 60 readings, and its position when it has one). A link from the Map (`#sensors/<district>/-`) opens the list with state "All sensors" filtered to that district.

### 7.3 Burst tab (needs `city:control`)

- **BURST** starts seven faults at once for the chosen duration (1, 3, 5 or 10 minutes; default 3): flood level 3, slow_db 3, corrupt 2, noise 2, dropout 5 (on one random district, or on the whole city if a city is picked), stuck 2, late 2. With a city picked in the City menu, all but `slow_db` target that city; `slow_db` always slows the whole gateway.
- **Stop everything** ends all active bursts.
- **Pick your own fault**: one fault, a target (`All cities`, `City <id>`, a district, a sensor type), a level 1 to 5 and a duration (1, 2 or 5 minutes), then Start.

| Kind | Effect (from `chaos.py`, `sim.py`, `gateway.py`) |
|---|---|
| `flood` | Sensors report 1 + 2 × level times as often: stresses the gateway. |
| `noise` | Noise grows and a share of readings spike past physical limits: sensors turn `fault`. |
| `dropout` | Sensors go silent: `stale`, then `offline`. |
| `stuck` | A share of stuckable sensors repeats one value: `stuck`. |
| `late` | Readings carry timestamps 90 s × level in the past (data arrives late). |
| `corrupt` | A share of items is malformed: rejected and counted (`citypulse_readings_total{result="rejected"}`). |
| `slow_db` | The gateway sleeps 0.5 s × level per batch while holding its slot: back-pressure and 429s. |

Limits: duration 5 to 1800 s, intensity 1 to 5, at most 50 active bursts. **Bursts only change the simulated fleet and the gateway's own delay; readings from real sensors
are never altered** (except that `slow_db` delays their batches too, and `corrupt`, `noise` and the others act only inside the built-in simulator, i.e. role `all` or `sim`; the CityPulse Simulator, a separate project, does not read CityPulse's burst table, so the burst button affects it only through `slow_db`
at the gateway). Verified against the Simulator's source: it never calls `/api/chaos` or reads CityPulse's database; it only POSTs readings (to `/api/readings`, through the protocol gateways) and backs off on `429`/`503`. The only readers of the `chaos` table are `sim.make`, `gateway._bursts` and the API.

**What that means for a fleet-fed demo** (CityPulse with `CITYPULSE_ROLE=api`, readings from the CityPulse Simulator): pressing BURST starts seven rows, but only `slow_db` has an effect on the Simulator's readings. The gateway holds each batch for 1.5 s, the Simulator sees `429`s and backs off (its buffer grows), and sensors can go stale once its batches queue up; there is no flood, corruption, noise, stuck sensors, late data or dark district from the button. To break a Simulator-fed city, use the Simulator's own burst percentage per group, or its *Burst the whole city* button. The Overview's red "Burst active" banner and the Active table still list all seven rows even though six of them do nothing to that data. For the complete seven-fault demo, run CityPulse with role `all` (the compose default), where the built-in simulator feeds the city and obeys every burst.

Targets: `all`, `city:<id>`, `district:<id>`, `type:<key>`. A city or district target must have reported (the built-in catalogue's `d01`..`d12` are always accepted).

### 7.4 Connect tab

Copy-paste snippets with the page's own address filled in: push readings with curl, an OpenTelemetry Collector `otlphttp` exporter, a separate simulator process,
a `pagerlens-agent` target, and PagerLens reporting variables. Needs `city:read`.

**Push API**

```bash
curl -sS -X POST "$CITYPULSE_URL/api/readings" \
  -H "Authorization: Bearer $CITYPULSE_INGEST_TOKEN" -H "Content-Type: application/json" \
  -d '{"readings":[{"sensor":"paris.temperature-d01-0001","type":"temperature","district":"d01","city":"paris","value":21.4}]}'
```

Per reading: `sensor` (`^[a-z0-9][a-z0-9_.-]{2,63}$`), `type` (one of the 27 keys, see `GET /api/catalog`), `district` (`^[a-z0-9][a-z0-9_-]{0,23}$`), `city` (required, `^[a-z0-9][a-z0-9_-]{0,31}$`, not `default`),
`value` (finite number, magnitude at most 1e12; booleans rejected), optional `protocol` (default `http`; one of `http otlp direct modbus_tcp ethernet_ip ethercat profinet profibus`), optional `ts` (epoch seconds or milliseconds, or ISO 8601; at most 24 h old and 2 min ahead; a future time within 2 min is clamped to now).
Up to 1000 readings per request; body at most 2 MB. A sensor's type, district and city are fixed by its first reading; a reading that names another city for a known sensor is rejected (`city does not match the sensor's city`).

Response: `200 {"accepted": n, "rejected": n, "errors": ["N x reason", ...up to 5], "hint": "..."}` (`hint` only when readings lacked a city). Whole-request errors: `422` (empty or oversize list), `429` with `Retry-After: 2` (gateway saturated; keep the batch and retry), `503` with `Retry-After: 5` (database unavailable), `401` / `403` (credentials).

**OTLP/HTTP**: `POST /v1/metrics`, `Content-Type: application/x-protobuf` (or `application/protobuf`) or `application/json`; `Content-Encoding: gzip` accepted (inflated with an 8 MB bound). Gauge and sum data points become readings. Attributes
(data point first, then resource): `sensor.id` (required), `sensor.type` (or a metric named after a type), `district` (default `d00`), `city` or `city.id` (required). Logs and traces are not accepted.

### 7.5 Settings: tokens and admin password

Needs `settings:manage` (admin, or a `full` token).

- **API tokens**: name (1 to 60 characters) and scope. The token (`cp_…`) is shown once; only its SHA-256 is stored. The list shows prefix, scope, created, last used; **Revoke** ends a token. Scopes: `ingest`, `metrics`, `read`, `full` ([section 8](#8-roles-scopes-and-permissions)).
- **Admin password**: set (first time, when signed in with a static key or as an SSO admin on a federated instance, which has no password until you set one) or change (current password required, new at least 12 characters). Changing it deletes every session.

### 7.6 Cities, districts and legacy data

Every sensor belongs to one city, set by its first reading; there is no default city. Districts are whatever the senders report. Read endpoints accept `?city=<id>`.
`GET /api/cities` lists cities (prefix search `q`, `limit` 1 to 200 default 50, `offset`, `sort=name|worst`). `GET /api/cities/<id>/districts` lists up to 200 districts, biggest first.

Deleting a city (`DELETE /api/cities/<id>`, `city:delete`): removes burst targets, readings, status history, roll-ups, pattern-memory episodes and sensors in transactions of at most 5,000 rows, one deletion at a time (`409` if another runs, `404` if no sensor reports for it).
Stop the city's senders first or they recreate it. Answer: `{"city","deleted":{"chaos","readings","status_history","rollups","rollups_1h","episodes","sensors"},"batches"}` (the roll-ups and pattern-memory episodes of the city go with it).

Databases created before cities became mandatory may hold a `default` city; it is flagged "legacy, no city reported", never auto-decommissioned, and removed by an admin with the delete control or API. See README "Upgrading a deployment that has a default city".

### 7.7 Reporting to PagerLens

When enabled ([5](#pagerlens-reporting)), every 15 s the reporter sends:

| What | Detail |
|---|---|
| Services (registered once, `tier: critical`, owner `city-platform`) | `citypulse-ingest`; `citypulse-fleet` (depends on `citypulse-ingest`); `citypulse-fleet-<city>` (depends on `citypulse-ingest`) for each tracked city. |
| Deploy marker | Once at start: service `citypulse-ingest`, version `0.1.0`, summary "CityPulse started". |
| Metrics for `citypulse-ingest` | `up`, `ingest_rate` (accepted readings/s), `reject_pct`, `overloaded_pct`, `latency_p95_ms` (rates start from the second sample). |
| Metrics for `citypulse-fleet` and each city service | `worst_district_pct`, `fleet_health_pct`, `offline_pct`, `stale_pct`, `fault_pct`, `stuck_pct`. |
| Alert `SensorFleetDegraded` | Per city that has a problem, on service `citypulse-fleet-<city>`, source `citypulse`, labels `state`, `health_pct`, `city`. Severity: degraded → `sev3`, partial outage → `sev2`, major outage → `sev1`. Raised when the verdict worsens and re-raised every 10 minutes while it lasts. |

At most 20 cities (the least healthy with a problem, plus recently recovered ones) are tracked at once. A city that recovers gets one last healthy metric point and is dropped.
The reporter never follows redirects, never logs the token, and an error talking to PagerLens is logged and retried on the next cycle without affecting the platform.

### 7.8 Map tab (PostGIS)

Needs `city:read` and a chosen city (use the City menu). Draws the city's **district polygons** coloured by the health of the sensors inside them (the same five levels and colours as the heatmap; grey when no located sensor lies inside) and the sensors as dots (choose "Sensors with a problem", the default, or "All located sensors", up to 1500). Everything is inline SVG built from GeoJSON with the DOM API: no map tiles, no external request, no inline style attribute (colours are CSS classes), so it works under the strict CSP and offline. **Keyboard:** each district is focusable (`Tab`), has an accessible name ("Harbor: 94% healthy, 36 located sensors. Press Enter ...") and `Enter` or `Space` (or a click) opens the Sensors tab for that district, as a heatmap cell does. **Text alternative:** below the map, a table of the districts (located sensors, share healthy, worst status, sensors reported under another district id, a Sensors button) and a table of the plotted sensors with their positions. The page refreshes every 10 s unless a district has focus. Without PostGIS the tab shows a "Map not available" card with the reason, and nothing else changes.

**Where the geography comes from.** `geo.py` holds real centres for ten cities (`paris`, `lyon`, `marseille`, `london`, `berlin`, `madrid`, `rome`, `amsterdam`, `barcelona`, `lisbon`). For each, the twelve built-in districts `d01`..`d12` are the cells of a 4 x 3 grid centred on the city centre, each about 1.4 km square; grid corners are shared by neighbouring cells and shifted by a fixed hash of city and corner, so the polygons tile without gaps but are not rectangles (the grid is about 5.6 x 4.2 km). The built-in simulator places each sensor from a hash of its id inside the middle half of its district's cell, so a sensor is always inside its own polygon. Nothing is random per run (sha256, not `hash()`). The polygons are stored by the startup hook in table `district_area` (source `builtin`; a row with another `source` is never overwritten). Any other city id (a real sender's city, `testville`) has no built-in polygons: its sensors appear as dots if they send a position, and no district is coloured.

**Sending a position.** A reading may carry optional `lat` and `lon` (decimal degrees, WGS 84; JSON fields, or OTLP attributes `lat` and `lon` on the data point or resource). They come together: one without the other, a non-number (a string, a boolean), a non-finite number, a latitude outside +-90 or a longitude outside +-180 rejects the reading with a reason (`lat and lon must be sent together`, `lat must be within +-90 and lon within +-180 (decimal degrees)`). A reading without them never erases a stored position, and one with them moves it. The built-in simulator sends the position with a sensor's first reading and every 50th after that.

**PostGIS, and what happens without it.** The positions are stored in two plain columns (`sensor.lat`, `sensor.lon`, migration `005_position.sql`, so they are kept on any database). At every start `geo.ensure` (an `after_migrate` hook) runs `CREATE EXTENSION IF NOT EXISTS postgis` and, if it works: adds `sensor.geom` (`geography(Point, 4326)`, **generated** from lat/lon, so a sender only ever writes lat/lon and PostGIS added later fills it for existing rows), a GiST index on `geom` (nearby and distance) and a GiST expression index on `geom::geometry` (containment), creates `district_area` (`geometry(Polygon, 4326)` with a GiST index) and stores the built-in polygons. If the extension cannot be created (plain Postgres, CI, a platform that refuses) the hook logs and returns, and every geographic route answers `200 {"available": false, "reason": "..."}` instead of failing (the sensor data and the rest of the app are unaffected). `btree_gist` is not used: there is no exclusion constraint or mixed scalar-and-spatial index that needs it.

| Route | Purpose |
|---|---|
| `GET /api/sensors/nearby?lat=&lon=&radius_m=&limit=&city=` | Sensors with a position within `radius_m` metres (default 1000, at most 1,000,000), nearest first (`limit` default 20, at most 200), each with `distance_m`. `ST_DWithin` and `ST_Distance` on geography (exact spheroidal metres): Paris to Lyon is about 391.7 km. |
| `GET /api/districts/geojson?city=` | A GeoJSON `FeatureCollection` of district polygons (`ST_AsGeoJSON`) for cities that have sensors. Each feature's `properties`: `city`, `district`, `name`, `total`, `ok`, `health` (null with no sensor inside), `worst_status`, `counts`, `reported_elsewhere`. **Which district a sensor is in is decided by `ST_Contains` on its position**, not by the district id it reports; `reported_elsewhere` counts sensors whose reported id differs (a wiring or labelling problem a map shows at once). The response also has `sensors_without_location`. |
| `GET /api/sensors/geojson?city=&state=&limit=` | Located sensors as GeoJSON points (`state` `problem` by default, `all` or one status; `limit` default 500, at most 2000; `matching` counts all that fit). |

All three need `city:read` and sit before `/api/sensors/{id}` in the router (otherwise `nearby` would be taken for a sensor id). Example: `curl -H "Authorization: Bearer $READ_TOKEN" "$URL/api/sensors/nearby?lat=48.8566&lon=2.3522&radius_m=2000&limit=5"`.

### 7.9 Insights tab (admin)

Needs `settings:manage` (admin): statement text can reveal table names and the shape of the workload. The tab mounts the kit's `initInsights`: top queries (`edb_stat_monitor` where installed, else `pg_stat_statements`), `edb_pg_tuner` recommendations and `query_advisor` index hints, each source showing "Not available on this database." with the reason when it is missing or the login role may not read it (plain Postgres has none of the EDB extensions). `GET /api/insights?order=total|mean|calls&limit=` returns `apgkit.insights.insights_payload` (a bad `order` becomes `total`, `limit` is clamped to 1..200); it never fails because an extension is absent. `/metrics` also exports `db_insights_available{app}`, `db_insights_top_query_mean_seconds{app,queryid}`, `db_insights_statement_calls{app}` and `db_insights_tuner_recommendations{app}` (cached 30 s; `db_insights_available 0` where statistics cannot be read), so PagerLens can alert on a rising per-query mean time.

### 7.10 What a read-only user and AgentPG's restricted MCP can see

AgentPG gives every instance the group role `a1p_readonly` (SELECT on every public table through default privileges). Direct database users made with `agentpg db-users create --permission read` are members of it, and the restricted MCP server (`agentpg mcp serve --config` with `access_mode: restricted`) runs its queries as such a user, so an AI agent or analyst connected that way is bound by the same grants. Default privileges would let that role read the credentials too, so CityPulse takes them away at every start (`hardening.py`, after the migrations and the kit's auth tables exist; `migrate.run(after_migrate=[geo.ensure, hardening.hook])`, then once more after the auth tables):

| Table | In the read-only role's reach? | Why |
|---|---|---|
| `sensor`, `reading`, `status_history`, `chaos`, `district_area`, `schema_migrations` | **yes** (SELECT) | The fleet data an agent should be able to query: sensors with their status and position, readings, history, bursts, polygons. No personal data. |
| `app_setting` | **no** (`permission denied`) | Key/value settings: admin password hash, the vault's wrapped data key and salts, encrypted values, setup state. |
| `api_token` | **no** | sha256 of every API token. |
| `web_session` | **no** | sha256 of browser session tokens, signed-in identity, the identity provider's id_token hint. |
| `oidc_login` | **no** | Single sign-on state, PKCE verifier and nonce. |

There is **no masked view**: CityPulse stores no personal data (sensors, readings and districts only), and nothing in the four hidden tables is something an agent needs. `apgkit.masking.mask_view` is the tool for apps that do hold personal data; the kit has no revoke-only helper, so `hardening.deny_secret_tables` issues the REVOKE itself and reads the privilege back with `has_table_privilege` (it reports `still readable` if the REVOKE did not take). It is idempotent, does nothing and logs when the role `a1p_readonly` does not exist (plain Postgres), and skips a table that does not exist. Limits: only the table owner can revoke (the app role owns the tables on AgentPG); default privileges re-grant SELECT on a **new** table, so a table added by a later migration is readable until the next start revokes it (add it to `SECRET_TABLES` if it holds secrets; a test fails if a table with a credential-looking column is not listed); the app's own connection, `a1p_readwrite` members and the platform owner role see everything; writes by a read-only user are refused anyway. Statement text in `/api/insights` is not reachable this way: it is behind `settings:manage` in the app, and `pg_stat_statements` hides other users' text from a role without `pg_read_all_stats`.

### 7.11 One engine, five data models

CityPulse keeps relational, time-series, JSON, geographic and vector data in **one Postgres** and answers questions across all five with plain SQL. Nothing here needs a second database: no TimescaleDB, no ClickHouse (neither exists on AgentPG), no search engine, no vector store on the side.

```
                         ingest (POST /api/readings, OTLP)
                                      |
                 one transaction per batch: validate, insert, upsert
                                      |
   +----------------------------------+------------------------------------------+
   |                                  v                                          |
   |   RELATIONAL            sensor (id, city, district, type, status ...)        |
   |   current state         PK + (city, district, type) B-tree, fillfactor 70    |
   |                                  |                                          |
   |   JSON                  sensor.attrs jsonb  (vendor, firmware, calibration)  |
   |   device metadata       GIN (jsonb_path_ops)       <- always on              |
   |                                  |                                          |
   |   GEO                   sensor.geom geography(Point) GENERATED from lat/lon  |
   |   where                 district_area geometry(Polygon)    GiST              |
   |                                                    <- PostGIS, if present    |
   |                                                                             |
   |   TIME SERIES           reading (sensor_id, ts, value)  BRIN(ts), B-tree     |
   |   raw stream     ---->  rollup_5m  (partitioned by month, PK incl. bucket)   |
   |                  ---->  rollup_1h  (date_bin roll-ups, refreshed by the app) |
   |                                                    <- always on              |
   |   VECTOR                episode (district x 15 min window)                   |
   |   pattern memory        features real[16]  ->  embedding vector(16)          |
   |                         HNSW (cosine)                   <- pgvector, if present
   +-----------------------------------------------------------------------------+
                                      |
              GET /api/districts/{id}/explain   =  ONE SQL statement, five models
```

| Model | Tables and columns | How it is indexed | Needs |
|---|---|---|---|
| Relational | `sensor` (state, `fillfactor 70`, HOT updates), `status_history`, `chaos` | primary key, `(city, district, type)` for lookups and city prefix search; the per-reading columns carry no index | nothing |
| JSON | `sensor.attrs jsonb` ([7.12](#712-device-metadata-json)) | GIN with `jsonb_path_ops`: serves `@>` containment and the `@?` / `@@` jsonpath operators; no index on the columns that change on every reading | nothing |
| Time series | `reading` (raw), `rollup_5m`, `rollup_1h` ([7.13](#713-time-series-on-core-postgres)) | BRIN on `reading.ts` (append-only, physically ordered by time) plus `(sensor_id, ts DESC)`; roll-ups keyed `(city, district, type, bucket)` | nothing |
| Geo | `sensor.geom` (generated), `district_area` ([7.8](#78-map-tab-postgis)) | GiST on the geography and on `geom::geometry` | PostGIS |
| Vector | `episode.features real[]` and, with pgvector, a generated `embedding vector(16)` ([7.14](#714-pattern-memory-have-we-seen-this-before)) | HNSW `vector_cosine_ops` (iterative scans on pgvector 0.8+) | pgvector |

Each extension is **guarded**: the startup hooks `geo.ensure` and `memory.ensure` run `CREATE EXTENSION IF NOT EXISTS ...` and, when the platform refuses, log and carry on. The three always-on models (relational, JSON, time series) never depend on one. Every route that needs a missing extension answers `200` with `{"available": false, "reason": "..."}` and the explain query still returns the other models.

### 7.12 Device metadata (JSON)

A reading may carry `attrs`: a JSON object describing the **device** (not the measurement). The built-in simulator sends it with a sensor's first reading and every 50th after that (like the position), for every city; the Simulator, a collector or a script may do the same.

```json
{"sensor": "paris.temperature-d03-0007", "type": "temperature", "district": "d03", "city": "paris", "value": 18.4,
 "attrs": {"vendor": "Meteosense", "firmware": "2.1.4", "install_date": "2022-12-07",
           "calibration": {"offset": 0.116, "scale": 1.0055, "date": "2023-07-28"}, "tags": ["kerb", "north"]}}
```

**Validation** (`gateway.check_attrs`; a bad `attrs` rejects that reading with a reason, counted like any other bad item, the rest of the batch is stored): an object; at most 16 keys per object; keys `^[a-z][a-z0-9_]{0,39}$`; two levels at most (an object may hold one nested object, and lists of at most 8 scalars); strings up to 120 characters; numbers finite; 1024 bytes in all. **No secrets**: a key naming a credential (`password`, `secret`, `token`, `api_key`, `credential`, `private_key`, `bearer`, `cookie`, `session`, `auth`, `authorization`, `signature`, `certificate`, as a whole word between underscores) or a value that looks like one (`Bearer ...`, `cp_...` tokens, JWTs, `-----BEGIN`, AWS and GitHub key shapes, any run of 40 or more key characters) is refused. That is a tripwire for the obvious cases, not a scanner: the column is readable by every `city:read` caller and by AgentPG's read-only role, so credentials do not belong in it. OTLP senders use attributes named `attrs.<key>` (flat); a CHECK constraint (`sensor_attrs_shape`: an object of at most 4096 bytes) backs the gateway up.

**Storage.** `sensor.attrs jsonb` (migration `006_attrs.sql`, a metadata-only change), GIN index `sensor_attrs_gin` with `jsonb_path_ops`. Attrs written by a batch **replace** the stored object wholesale (`coalesce(EXCLUDED.attrs, sensor.attrs)`: a reading without attrs never erases them), and they are only part of the upsert in batches that carry some, so the usual per-reading update does not touch the column and stays HOT (a test asserts `n_tup_hot_upd = n_tup_upd`).

**Filter the API.** `GET /api/sensors` takes two more parameters, combinable with every other filter:

```bash
# containment (attrs @> '{"vendor":"Aerolab"}'); it also reaches into nested objects and arrays
curl -H "Authorization: Bearer $READ_TOKEN" -G "$URL/api/sensors" --data-urlencode 'state=all' --data-urlencode 'attrs={"vendor":"Aerolab"}'
# a jsonpath predicate (attrs @? '$.calibration ? (@.offset > 0.1)'): sensors whose calibration offset is above 0.1
curl -H "Authorization: Bearer $READ_TOKEN" -G "$URL/api/sensors" --data-urlencode 'attrs_path=$.calibration ? (@.offset > 0.1)'
```

A malformed `attrs` or `attrs_path` is a `422`. Sensor rows (and `/api/sensors/{id}`) carry their `attrs`. **Deterministic seed:** `catalog.sensor_attrs` derives vendor (three per sensor group), firmware (four versions, the oldest `1.4.2` in the minority), install date, calibration and tags from a sha256 of the sensor id, so every run and machine gives the same sensor the same metadata. One small story is built in on purpose: firmware `1.4.2` misreads 2.5 times as often under a `noise` burst, which is what the "over-represented firmware" part of the explain card finds.

### 7.13 Time series on core Postgres

What a real time-series workload needs, with no extension:

| Need | What CityPulse does |
|---|---|
| Cheap range scans on an append-only stream | `reading` keeps a **BRIN** index on `ts` (a few kilobytes instead of a B-tree the size of the table) next to `(sensor_id, ts DESC)` for per-sensor detail |
| Roll-ups at several resolutions | `date_bin` into `rollup_5m` and `rollup_1h`, per `(city, district, type, bucket)`: readings, distinct sensors, out-of-range readings, mean, min, max |
| Declarative partitioning | `rollup_5m` is `PARTITION BY RANGE (bucket)`, one partition per month (`rollup_5m_YYYYMM`), created by the app before it writes (`timeseries.ensure_partitions`, also at every start). There is no default partition: a row outside every partition is an error, not a silent pile-up |
| Retention that does not bloat | whole monthly partitions older than 35 days are **dropped** (`DROP TABLE`, no vacuum debt), stray rows deleted; `rollup_1h` keeps 400 days; raw `reading` keeps `RETENTION_HOURS` |

**Why roll-up tables and not materialized views.** `reading` is pruned after `RETENTION_HOURS` (6 by default). `REFRESH MATERIALIZED VIEW` recomputes everything from the rows that are left, so a view over `reading` would forget history at every refresh. The engine instead recomputes only the newest buckets (the last 30 minutes for 5m, 2 hours for 1h, inside its once-a-minute pass, before the prune) and **upserts** them: late data lands in an old bucket as long as it is inside that horizon, and a bucket whose raw rows may already be pruned is never overwritten with a smaller count (the window starts at the first whole bucket inside the retention).

**Why `reading` is not partitioned.** Partitioning pays only where a migration is safe and the queries benefit. `reading` is the hot ingest table and it is pruned with `DELETE ... WHERE ctid IN (...)` in 50,000-row batches (`status.prune` and `purge.delete_city` rely on `ctid`, which is only unique inside one partition). Making it a partitioned table means rewriting or copying it under load (rows cannot be attached in place), a primary key that includes `ts`, and a rewrite of both deletion paths; with 6 hours of retention the gain (dropping a partition instead of batched deletes) is small and the risk is not. So `reading` stays a plain table with BRIN, and the tables that grow for months (the roll-ups, the episodes) are the ones that are partitioned or small. `status_history` stays plain for the same reason (it is pruned by the same engine pass). If you raise `RETENTION_HOURS` to weeks, partition `reading` by day first: create `reading_new PARTITION BY RANGE (ts)`, point the gateway at it, drain the old table, and switch `prune` to `DROP TABLE`.

**Refresh and retention** run in the status engine (`status.tick`, every 60 s, under advisory lock `727402`): `timeseries.refresh(conn, "5m")`, `refresh(conn, "1h")`, then `prune`. A failure there is logged and never stops the engine.

`GET /api/districts/{id}/series?city=&bucket=5m|1h&from=&to=&type=` (needs `city:read`): the points of one district, oldest first. `city` is **required** (a district id is only unique inside a city). `bucket` defaults to `5m`; `from`/`to` are ISO 8601 (default: the last 6 hours for 5m, 48 hours for 1h); at most 2100 buckets (7 days of 5m, 87 days of 1h) per request, else `422`. Each point has `bucket`, `readings`, `sensors`, `out_of_range`; with `type=<key>` (a mean is only meaningful for one unit) also `avg`, `min`, `max`. Empty buckets are absent, not zero.

```bash
curl -H "Authorization: Bearer $READ_TOKEN" "$URL/api/districts/d03/series?city=paris&bucket=5m&from=2026-10-28T10:00:00Z&to=2026-10-28T11:00:00Z"
```

### 7.14 Pattern memory: "have we seen this before?"

**The fingerprint.** For every district and every 15-minute window the app computes a fixed-size vector of 16 numbers, each in [-1, 1], from the sensors' states and the window's readings (`memory.to_vector`: one function, used by the live engine and by the demo history alike). Distance is **cosine**, so only the direction matters and the weights decide how loud a feature is.

| Dim | Name | Definition (shares are over the district's sensors) |
|---|---|---|
| 0 | `ok` | share of sensors with status `ok` |
| 1-4 | `stale`, `offline`, `fault`, `stuck` | square root of the share in that status (lifts a few bad sensors above the noise floor: 1 % gives 0.10, 25 % gives 0.50) |
| 5 | `delta_mean` | per sensor the mean `abs(v[i] - v[i-1]) / (hi - lo)` over the window, averaged over sensors, x 20, clipped to [0, 1] |
| 6 | `delta_p95` | the 95th percentile of the same per-sensor figure, x 20, clipped |
| 7 | `burst` | share of the window during which a burst aimed at the district, its city or `all` was active |
| 8 | `silent` | share of sensors with no reading at all in the window |
| 9 | `rate` | readings received / readings expected (window / interval, summed over sensors), clipped to [0, 2], halved |
| 10 | `overload` | share of ingest batches answered `429` since the previous fingerprint (process-wide; 0 when idle) |
| 11-12 | `hour_sin`, `hour_cos` | 0.5 x sin / cos of 2 pi x hour of day / 24 (UTC, fractional) |
| 13-14 | `dow_sin`, `dow_cos` | 0.25 x sin / cos of 2 pi x day of week / 7 (Monday 0, fractional) |
| 15 | `verdict` | the district's tier (ok share: operational >= 97 %, degraded >= 85 %, partial outage >= 50 %, else major) / 3 |

The clock features are deliberately quiet, so two incidents at different hours still look alike while a quiet night and a quiet noon do not collapse onto one point. Each burst kind moves a different group of dimensions (a `dropout` moves `offline`, `silent` and `rate`; `noise` moves `fault` and the deltas; `flood` moves `rate` and `overload`), which is why the neighbours of a dropout are dropouts. Built-in bursts only count when aimed at `all`, a city or a district; a burst aimed at a sensor type shows up through the sensor states it causes.

**When and where.** A fingerprint is taken for every district at each quarter hour (trigger `schedule`; only in the two minutes after the boundary, because the statuses are read as they are *now*) and whenever a district's verdict tier changes (trigger `verdict_change`, window ending now), by the status engine (`memory.tick`, every 30 s, under the same advisory lock). At most 400 districts per pass (the least healthy first) and 100 verdict-change fingerprints per pass. Stored in `episode` (migration `007`): `features real[]` always, so nothing is lost on a database without pgvector; with the extension, `memory.ensure` (every start) runs `CREATE EXTENSION IF NOT EXISTS vector`, adds `embedding vector(16) GENERATED ALWAYS AS (features::vector(16)) STORED` (so pgvector added later fills it for existing rows) and the index `episode_embedding_hnsw USING hnsw (embedding vector_cosine_ops)`.

**Outcomes are labelled when episodes end** (`memory.label`, after each quarter-hour pass): `verdict_1h`, the district's verdict one hour later (the newest fingerprint within the following hour), and, for a run of non-operational verdicts, `episode_start`, `ended_at` and `duration_min` once an operational fingerprint closes it. A run already going on at the start of the data has an unknown beginning and stays unlabelled.

**Search.** `GET /api/districts/{id}/similar?city=&k=5&scope=all|city` returns the most similar **past** episodes to the district's latest fingerprint: older than one hour (so the outcome is known), not this district's own last three hours (that is the same incident), and while the district is in trouble only troubled ones. One row per past episode (a two-hour incident leaves several near-identical fingerprints; only its closest one is listed). Each has `distance` and `outcome` (`verdict_1h`, `episode_lasted_min`, `resolved_after_min`). Without pgvector: `{"available": false, "reason": "pgvector is not available on this database ..."}`.

**Demo history.** A fresh install has no past, so run once (optional, idempotent, deterministic):

```bash
citypulse seed-history                       # paris and lyon, 28 days: 64,512 episodes, outcomes labelled
citypulse seed-history --cities berlin,rome --days 14
citypulse seed-history --reset               # first delete the episodes this command wrote (trigger 'seed'); live ones stay
```

Every number is a pure function of (city, district, calendar day, window) through sha256, so two runs write the same history. The model (`history.py`): each district has 200-299 sensors and, on about 30 % of the days, one incident (`dropout`, `noise`, `stuck`, `flood` or `storm`) of 2 to 9 windows that ramps up, holds and ramps down, bending exactly the statistics the real fingerprint reads. It is demo data, realistic in shape, not a replay of anything. Cities must be built-in ones (`geo.CITIES`).

### 7.15 Explain this district: one query across all five models

The **Explain** tab (needs `city:read`; choose a city, then a district) and `GET /api/districts/{id}/explain?city=&k=3` answer in **one SQL statement and one round trip**:

| Part | Question | Source |
|---|---|---|
| `relational` | what is the status of its sensors right now, and which types suffer? | `sensor` |
| `timeseries` | what did the last hour look like? | `rollup_5m` (`date_bin`), with the last 15 minutes against the 45 before |
| `json` | which vendor or firmware is over-represented among the problem sensors? | `sensor.attrs` (`jsonb_each_text`); `lift` = share of the problem sensors / share of the city, flagged at >= 1.5 with at least 2 problem sensors |
| `geo` | where is the trouble, which healthy sensors and districts are nearest? | PostGIS: centroid of the problem sensors, `<->` nearest healthy sensors with `ST_Distance`, the three nearest districts from `district_area` |
| `vector` | have we seen this before, and what happened next? | pgvector: the `k` most similar past episodes with `next_hour_verdict`, `episode_lasted_min`, `resolved_after_min`, plus a `prediction` (how often each verdict followed, the median length) |

The response also has `verdict`, `health`, `round_trips: 1`, `models: 5` and the statement itself in `sql`. A model whose extension is missing contributes `NULL::jsonb` instead of its CTE and answers `{"available": false, "reason": ...}` while the other four still come back. The statement below is the full one (PostGIS and pgvector 0.8+; parameters `%(city)s`, `%(district)s`, `%(k)s`); `explain.build_sql` assembles it from fixed fragments and user input never reaches the text. A test keeps this listing identical to the code.

```sql
WITH
rel AS (
  SELECT jsonb_build_object(
    'sensors', count(*),
    'with_attrs', count(*) FILTER (WHERE attrs IS NOT NULL),
    'counts', jsonb_build_object('ok', count(*) FILTER (WHERE status = 'ok'), 'stale', count(*) FILTER (WHERE status = 'stale'),
                                 'offline', count(*) FILTER (WHERE status = 'offline'), 'fault', count(*) FILTER (WHERE status = 'fault'),
                                 'stuck', count(*) FILTER (WHERE status = 'stuck')),
    'problem_types', (SELECT coalesce(jsonb_agg(jsonb_build_object('type', q.type, 'problems', q.n) ORDER BY q.n DESC, q.type), '[]'::jsonb)
                      FROM (SELECT type, count(*) AS n FROM sensor
                            WHERE city = %(city)s AND district = %(district)s AND status <> 'ok'
                            GROUP BY type ORDER BY n DESC, type LIMIT 3) q)
  ) AS j
  FROM sensor WHERE city = %(city)s AND district = %(district)s
),
ts AS (
  SELECT jsonb_build_object('bucket', '5m', 'points', coalesce(jsonb_agg(jsonb_build_object(
           'bucket', q.bucket, 'readings', q.readings, 'sensors', q.sensors, 'out_of_range', q.oor) ORDER BY q.bucket), '[]'::jsonb)) AS j
  FROM (SELECT bucket, sum(readings)::int AS readings, sum(sensors)::int AS sensors, sum(out_of_range)::int AS oor
        FROM rollup_5m
        WHERE city = %(city)s AND district = %(district)s
          AND bucket >= date_bin('5 minutes', now() - interval '1 hour', TIMESTAMPTZ '2000-01-01')
        GROUP BY bucket) q
),
attr AS (
  SELECT a.key, a.val, (s.district = %(district)s) AS here, (s.status <> 'ok') AS bad
  FROM sensor s CROSS JOIN LATERAL jsonb_each_text(s.attrs) AS a(key, val)
  WHERE s.city = %(city)s AND s.attrs IS NOT NULL AND a.key IN ('vendor', 'firmware')
), cnt AS (
  SELECT key, val, count(*) AS city_n, count(*) FILTER (WHERE here AND bad) AS problem_n
  FROM attr GROUP BY key, val
), tot AS (
  SELECT key, sum(city_n) AS city_total, sum(problem_n) AS problem_total FROM cnt GROUP BY key
), js AS (
  SELECT jsonb_build_object('basis', 'problem sensors of the district against all sensors of the city that report the attribute',
           'attributes', coalesce(jsonb_agg(jsonb_build_object(
             'key', q.key, 'value', q.val, 'problem_sensors', q.problem_n, 'city_sensors', q.city_n,
             'share_of_problems', q.sp, 'share_of_city', q.sc, 'lift', q.lift, 'over_represented', q.lift >= 1.5 AND q.problem_n >= 2
           ) ORDER BY q.lift DESC, q.problem_n DESC, q.key, q.val), '[]'::jsonb)) AS j
  FROM (SELECT c.key, c.val, c.problem_n, c.city_n,
               round(c.problem_n::numeric / t.problem_total, 3) AS sp, round(c.city_n::numeric / t.city_total, 3) AS sc,
               round((c.problem_n::numeric / t.problem_total) / (c.city_n::numeric / t.city_total), 2) AS lift
        FROM cnt c JOIN tot t USING (key) WHERE t.problem_total > 0 AND c.problem_n > 0
        ORDER BY 7 DESC, c.problem_n DESC LIMIT 5) q
),
centre AS (
  SELECT coalesce(p.g, a.g) AS g, CASE WHEN p.g IS NOT NULL THEN 'problem sensors' ELSE 'all located sensors' END AS basis
  FROM (SELECT ST_Centroid(ST_Collect(geom::geometry))::geography AS g FROM sensor
        WHERE city = %(city)s AND district = %(district)s AND status <> 'ok' AND geom IS NOT NULL) p,
       (SELECT ST_Centroid(ST_Collect(geom::geometry))::geography AS g FROM sensor
        WHERE city = %(city)s AND district = %(district)s AND geom IS NOT NULL) a
), geo AS (
  SELECT jsonb_build_object(
    'centre', CASE WHEN c.g IS NULL THEN NULL
                   ELSE jsonb_build_object('lat', round(ST_Y(c.g::geometry)::numeric, 5), 'lon', round(ST_X(c.g::geometry)::numeric, 5), 'basis', c.basis) END,
    'nearest_healthy', (SELECT coalesce(jsonb_agg(jsonb_build_object('sensor', n.id, 'type', n.type, 'district', n.district, 'distance_m', n.m)
                                                 ORDER BY n.m, n.id), '[]'::jsonb)
                        FROM (SELECT h.id, h.type, h.district, round(ST_Distance(h.geom, c.g))::int AS m FROM sensor h
                              WHERE c.g IS NOT NULL AND h.city = %(city)s AND h.status = 'ok' AND h.geom IS NOT NULL
                              ORDER BY h.geom <-> c.g, h.id LIMIT 5) n),
    'neighbours', (SELECT coalesce(jsonb_agg(jsonb_build_object('district', n.district, 'distance_m', n.m, 'sensors', n.total, 'health', n.health)
                                             ORDER BY n.m, n.district), '[]'::jsonb)
                   FROM (SELECT b.district, round(ST_Distance(ST_Centroid(b.geom)::geography, ST_Centroid(a.geom)::geography))::int AS m,
                                h.total, round(h.ok::numeric / NULLIF(h.total, 0), 3) AS health
                         FROM district_area a JOIN district_area b ON b.city = a.city AND b.district <> a.district
                         LEFT JOIN LATERAL (SELECT count(*) AS total, count(*) FILTER (WHERE status = 'ok') AS ok FROM sensor x
                                            WHERE x.city = b.city AND x.district = b.district) h ON true
                         WHERE a.city = %(city)s AND a.district = %(district)s
                         ORDER BY m, b.district LIMIT 3) n)
  ) AS j
  FROM centre c
),
cfg AS MATERIALIZED (SELECT set_config('hnsw.iterative_scan', 'strict_order', true) AS v),
cur AS (
  SELECT e.id, e.window_end, e.verdict, e.health, e.embedding FROM episode e, cfg
  WHERE e.city = %(city)s AND e.district = %(district)s AND e.embedding IS NOT NULL AND cfg.v IS NOT NULL ORDER BY e.window_end DESC LIMIT 1
), sim AS (
  SELECT jsonb_build_object('window_end', cur.window_end, 'verdict', cur.verdict, 'health', cur.health,
                            'similar', coalesce(x.arr, '[]'::jsonb)) AS j
  FROM cur CROSS JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
             'city', q.city, 'district', q.district, 'window_end', q.window_end, 'verdict', q.verdict, 'distance', round(q.distance::numeric, 6),
             'next_hour_verdict', q.verdict_1h, 'episode_lasted_min', round(q.duration_min::numeric, 1),
             'resolved_after_min', round((extract(epoch FROM q.ended_at - q.window_end) / 60)::numeric, 1)) ORDER BY q.distance) AS arr
    FROM (SELECT * FROM (
            -- one row per past episode: a 2-hour incident leaves eight near-identical fingerprints, and the answer should list incidents
            SELECT DISTINCT ON (c.city, c.district, coalesce(c.episode_start, c.window_end)) c.*
            FROM (SELECT e.city, e.district, e.window_end, e.verdict, e.verdict_1h, e.duration_min, e.ended_at, e.episode_start,
                         e.embedding <=> cur.embedding AS distance
                  FROM episode e
                  WHERE e.id <> cur.id AND e.embedding IS NOT NULL AND e.verdict_1h IS NOT NULL AND e.window_end < now() - interval '1 hour'
                    AND NOT (e.city = %(city)s AND e.district = %(district)s AND e.window_end > now() - interval '3 hours')
                    AND (cur.verdict = 'operational' OR e.verdict <> 'operational')
                  ORDER BY e.embedding <=> cur.embedding LIMIT %(k)s * 10) c
            ORDER BY c.city, c.district, coalesce(c.episode_start, c.window_end), c.distance) d
          ORDER BY d.distance LIMIT %(k)s) q
  ) x
)
SELECT (SELECT j FROM rel) AS relational, (SELECT j FROM ts) AS timeseries, (SELECT j FROM js) AS json_attrs,
       (SELECT j FROM geo) AS geo,
       (SELECT j FROM sim) AS vector
```

On pgvector before 0.8 the `cfg` CTE and its two references are left out: an HNSW index returns at most `hnsw.ef_search` (40) candidates and the filters are applied after it, so a rare kind of episode could come back empty. 0.8 adds iterative scans, which the first CTE switches on inside the statement (`strict_order`, so the order stays exact). Even then a very large history is capped by `hnsw.max_scan_tuples` (20,000) and can return fewer than `k` rows. The fingerprint the search starts from is the district's newest stored one, so the vector part is at most 15 minutes old (or as old as the last verdict change).

**The agent's substrate.** The statement is the evidence an agent needs in a reasoning loop ("what is wrong here, has it happened before, how did it end") without chaining queries. It is registered as a tool-ready function, with two narrower ones, in `tools.py`: `GET /api/tools` lists them with their JSON Schema and `POST /api/tools/{name}` with `{"arguments": {...}}` calls one (`city:read`; the arguments are checked against the schema, `422` otherwise). All three are read-only. **No LLM agent is built here**; an AIDB pipeline, an MCP server or any agent can call these later.

```json
{
  "name": "explain_district",
  "description": "Everything known about one district in a single database round trip: current sensor status counts (relational), the last hour of readings from the 5-minute roll-up (time series), which vendor or firmware is over-represented among its problem sensors (JSON), the nearest healthy sensors and neighbouring districts (geo, where PostGIS exists) and the most similar past episodes with what happened next (vector, where pgvector exists). Call it first when asked what is wrong with a district and whether it has happened before. Parts whose extension is missing say so in `available: false` and `reason`.",
  "input_schema": {
    "type": "object",
    "properties": {
      "city":     {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,31}$", "maxLength": 32, "description": "City id, e.g. paris"},
      "district": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,23}$", "maxLength": 24, "description": "District id within the city, e.g. d03 or alder-quay"},
      "k":        {"type": "integer", "minimum": 1, "maximum": 10, "default": 3, "description": "How many similar past episodes to return"}
    },
    "required": ["city", "district"],
    "additionalProperties": false
  }
}
```

`similar_episodes` takes `city`, `district`, `k` (1-20, default 5) and `scope` (`all` or `city`); `district_series` takes `city`, `district`, `bucket` (`5m` or `1h`), `hours` (0.25-168, default 6) and an optional sensor `type`. The exact definitions are what `GET /api/tools` returns.

```bash
curl -H "Authorization: Bearer $READ_TOKEN" "$URL/api/districts/d03/explain?city=paris"
curl -X POST -H "Authorization: Bearer $READ_TOKEN" -H "X-CityPulse-CSRF: 1" -H "Content-Type: application/json" \
     -d '{"arguments": {"city": "paris", "district": "d03"}}' "$URL/api/tools/explain_district"
```

**Honest limits.** There is no TimescaleDB (no continuous aggregates, no compression, no `time_bucket`) and no ClickHouse on AgentPG, and none is added: the time series is native Postgres (BRIN, `date_bin`, declarative partitioning, roll-up tables the app refreshes). That is plenty for this workload (thousands of sensors, minutes to months of history) and is not a claim about billions of rows. pgvector is only there when the platform or image has it (the CI image `pgvector/pgvector:pg17` does; a plain `postgres` image does not): without it fingerprints are still recorded and everything else answers, but similarity search and the vector part of Explain say so. PostGIS is optional in the same way. The fingerprint is hand-designed, not a learned embedding: it can say "this looks like those", not why. The demo history is synthetic.

**A signature moment for the tutorial video** (proposal, not recorded): start a `storm` burst on one district of a city (the Burst tab), open **Explain**, choose that district and press the button. One answer: about half of its sensors are down; readings fell by half in the last quarter hour; **firmware 1.4.2 is 80 % of the faulty sensors but 20 % of the city**; the nearest healthy sensors are 190 m away; and "of the 3 most similar past episodes (one in Lyon) all were still a partial outage an hour later and lasted about 90 minutes". Then open "Show the SQL": one statement, five data models, one Postgres.

---

## 8. Roles, scopes and permissions

Permissions (`core.PERMISSIONS`): `ingest:write`, `city:read`, `city:control`, `metrics:scrape`, `settings:manage`, `city:delete`. The Map needs `city:read`; Insights (and `/api/insights`) need `settings:manage`.

| Principal | Holds |
|---|---|
| Role `admin` (identity) | everything (`*`), including tokens, admin password and city deletion |
| Role `operator` | `city:read`, `city:control` (see everything, start and stop bursts) |
| Role `viewer` | `city:read` |
| Local token `ingest` / scope `citypulse.ingest` | `ingest:write` only |
| Local token `read` / scope `citypulse.read` | `city:read` only |
| Local token `metrics` / scope `citypulse.metrics` | `metrics:scrape` only |
| Local token `full`, static key, local admin session | everything |

Which route needs what: see [section 14](#14-http-reference). The UI hides tabs the user cannot use (`NEEDS` in `app.js`), but the server enforces the same checks.
With no credential configured the app denies everything unless `CITYPULSE_ALLOW_ANONYMOUS=true`.

---

## 9. Security notes

The full model is in [SECURITY.md](../SECURITY.md). Highlights, each checked in code:

- **Fail closed.** No credential means no access; the setup code appears only in the server log.
- **Passwords** are stored as salted scrypt hashes (12-character minimum). Sessions are random cookies (`HttpOnly`, `SameSite=Strict` for local sign-in and `Lax` for SSO, `Secure` over HTTPS), stored as SHA-256, valid 12 hours, all removed when the password changes.
- **Tokens** are random, shown once, stored hashed, revocable, one scope each. A valid token is cached for 30 s per process, so a revoked token may keep working that long on other replicas.
- **Failed sign-ins** are limited to 10 per minute per client address, in memory and per process.
- **JWTs** are verified locally (algorithm allow-list, issuer, audience, expiry); only this app's roles count.
- **CSRF.** Cookie-authenticated writes under `/api/` need the header `X-CityPulse-CSRF` (any value; the UI sends `1`); requests that carry an `Authorization` header are exempt (kit `SecurityGuard`).
- **Headers and limits.** Strict CSP without inline script or style, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`; bodies capped at 2 MB under `/api/` and `/v1/`.
- **Read-only access.** Tables with secrets are closed to AgentPG's read-only role and so to its restricted MCP ([7.10](#710-what-a-read-only-user-and-agentpgs-restricted-mcp-can-see)); the Insights page and `/api/insights` are admin only.
- **SQL** is parameterised; city ids are validated by regex and by database CHECK constraints; the UI writes data only with `textContent`.
- **Outbound.** The PagerLens reporter and the simulator's HTTP sink send a token to one configured URL, do not follow redirects, and refuse plain `http` to non-local PagerLens.
- **Container** runs as a non-root user and holds no secrets.
- Keep `CITYPULSE_ALLOW_ANONYMOUS` off on any reachable host. Use one ingest token per collector so each can be revoked alone.
- Report vulnerabilities through GitHub private vulnerability reporting, not a public issue.

---

## 10. Operations

### Health and metrics

| Endpoint | Auth | Meaning |
|---|---|---|
| `GET /healthz` | none | `{"status":"ok","version","role"}`; the process is up. |
| `GET /readyz` | none | `{"status":"ready"}`, or `503 {"status":"waking-database"}` when `SELECT 1` fails (a scaled-to-zero database waking). No connection details are echoed. |
| `GET /metrics` | `metrics:scrape` (token scope `metrics` or `citypulse.metrics`, or any full-access credential) | Prometheus text. |

Metric names: the shared family (`http_requests_total`, `http_request_duration_seconds`, `db_up{app}`, `app_info`, `process_*`, the same names TrustDesk and PagerLens use) plus
`citypulse_readings_total{result=accepted|rejected|late}`, `citypulse_readings_by_protocol_total{protocol}`, `citypulse_ingest_batches_total{result=ok|overloaded|error}`, `citypulse_ingest_batch_seconds`,
`citypulse_ingest_inflight`, `citypulse_sensors{status}`, `citypulse_fleet_health_ratio`, `citypulse_district_health_ratio{district}`, the built-in simulator's `citypulse_sim_*` series, and `db_insights_*` ([7.9](#79-insights-tab-admin)).
There is no per-city label (cardinality); per-city numbers are in the API. Fleet gauges are computed from the database at scrape time, so all replicas report the same.

### Backup and restore

All state is in Postgres: `sensor`, `reading`, `status_history`, `chaos`, `app_setting` (settings and the simulator heartbeat), `api_token`, `web_session`, plus the kit's tables (see [15](#15-data-model)). Use ordinary Postgres backups (`pg_dump`, or the platform's instance backups). Readings are disposable (pruned after `RETENTION_HOURS`); the data worth keeping is `api_token` and `app_setting`.
There is no CityPulse-specific backup command. Restoring into a new database and starting the app re-applies any missing migrations (`citypulse start`).

### Rotation

- **Admin password**: Settings, or `citypulse reset-admin` then restart and use the new setup code.
- **API tokens**: create the new token, switch the sender, revoke the old one in Settings. Allow up to 30 s for other replicas.
- **Static key** (`CITYPULSE_API_KEY`): change the variable (or file) and restart.
- **OIDC and service client secrets**: rotate in the fleet with `ecoctl apply --rotate` (the manifest docs say a client secret is rotated only with `--rotate`), then `ecoctl recreate citypulse` because a sandbox's environment cannot change in place. Not exercised for this guide.
- **Vault**: CityPulse stores no encrypted secrets and uses no vault or encryption key. (`ecosystem.yaml` has `generate: []` for it.)

### Migrations and upgrades

`citypulse start` (the container default) runs `citypulse migrate` first. Migrations are plain SQL in `src/citypulse/migrations/` (001 schema, 002 `sensor.protocol`, 003 `sensor.city` and city burst targets, 004 drops the city column default, 005 adds the optional `sensor.lat` and `sensor.lon`, 006 adds `sensor.attrs` (JSONB) with its GIN index, 007 adds the roll-up tables, the monthly partitioned `rollup_5m` and `episode`), tracked by the kit and serialised with advisory lock `727401`, so replicas can start together. After the migrations, hooks run at every start (`after_migrate`): `geo.ensure` (PostGIS, geography column, indexes, district polygons; skipped quietly without PostGIS), `memory.ensure` (pgvector, the generated `embedding` column and its HNSW index; skipped quietly without pgvector), `timeseries.ensure` (this month's and the next two months' `rollup_5m` partitions) and `hardening.hook` (REVOKE on the secret tables; skipped quietly without the role `a1p_readonly`). Then the kit's auth schema (settings, sessions, tokens, OIDC state) is ensured and the hardening runs once more, because `oidc_login` only exists from that point.
Advisory locks in use: `727401` migrations, `727402` status engine, `727403` city deletion.

### Scaling and pausing

Several replicas are safe: the engine and migrations use advisory locks; the gateway's slots and token cache are per process. Free-tier AgentPG sandboxes auto-pause when idle and do not wake on a request (`agentpg sandbox resume <id>`), which also pauses a built-in simulator. The fleet manifest sets `auto_pause: false`. A resume may reuse a stale image: recreate the sandbox to pull `:latest`.

### Per-city history

The status engine records a health series per city every 30 s: for all cities while there are at most 200, otherwise only the 200 least healthy that have a problem. A city without a series shows "collecting data" in its chart.

---

## 11. Troubleshooting

| Symptom | Likely cause | Check or fix |
|---|---|---|
| Start fails: "needs an explicit CITYPULSE_CITY" | Role `all` or `sim` without a city | Set `CITYPULSE_CITY=paris` (valid id, not `default`), or use `CITYPULSE_ROLE=api`. |
| Start fails: "CITYPULSE_CITY must match ..." | Invalid city id | Use lowercase letters, digits, `_`, `-`, 1 to 32 characters, starting with a letter or digit. |
| Compose refuses to start | `POSTGRES_PASSWORD` or `CITYPULSE_CITY` unset | Fill `.env`. |
| No setup code in the log | SSO is enabled, or an admin password already exists, or the database was unreachable at start | Setup is skipped under SSO (sign in with SSO and set the break-glass password in Settings). Standalone: the code is also printed on the first `GET /api/session` that finds setup needed; run `citypulse reset-admin` to start over. |
| Overview says "No sensors have reported yet" | Role `api` with no sender, or the simulator is still warming up | With role `all` wait half a minute. Otherwise start the CityPulse Simulator or push readings (Connect tab). |
| Sender gets `200` but `rejected` is high, error "N x city is required" | Readings lack `city` | Add `"city"` to every reading, set the OTLP `city` attribute, or set `CITYPULSE_CITY` on the built-in simulator. |
| Error "city does not match the sensor's city" | Same sensor id used in two cities | Make sensor ids unique across cities. |
| Error "sensor limit reached" | `MAX_SENSORS` hit | Raise `MAX_SENSORS` or delete a city. |
| `401` on `/api/readings` | Missing, wrong or revoked token | Create an `ingest` token in Settings; for services check the scope `citypulse.ingest`. |
| `403` on `/api/readings` | Token has the wrong scope (for example `read`) | Use an `ingest` (or `full`) token. |
| `403 missing CSRF header` | Cookie-authenticated write without `X-CityPulse-CSRF` | Add the header, or use a bearer token. |
| `429` with `Retry-After` | Gateway saturated (`INGEST_CONCURRENCY`, pool exhausted, or a `slow_db` burst) | Retry with backoff; stop bursts; raise `INGEST_CONCURRENCY`. |
| `503` and `/readyz` returns `waking-database` | Database asleep or down | Wait, check the database; on AgentPG resume it. |
| "your account has no role in CityPulse" | Signed in through SSO without a CityPulse role | Grant `admin`, `operator` or `viewer` in identity. |
| "the identity provider is unavailable" after pressing SSO | Issuer unreachable | Check `CITYPULSE_OIDC_ISSUER` and the provider. |
| "local sign-in is turned off; use single sign-on" | `CITYPULSE_LOCAL_LOGIN=false` | Use SSO, or set the variable back. |
| Redirect mismatch at the identity provider | Redirect URI registered does not match the app's real URL | Re-run `ecoctl apply --execute`; set `CITYPULSE_PUBLIC_URL` if a proxy hides the host. |
| "too many failed attempts; wait a minute" | 10 failures per minute from one address | Wait; behind a proxy all clients may share an address. |
| `404 this instance runs the simulator only` | Role `sim` | Use another instance for the dashboard. |
| Simulator buffer growing, sim shows a "problem" | The sim's ingest token was refused (`Fatal`, backs off 30 s) or the gateway is saturated | Check the token and scope; its retry buffer holds 100,000 readings and drops the oldest beyond that. |
| City stays degraded after a burst | Burst still active, or the sensors have not reported yet | Burst tab, **Stop everything**; recovery takes about 15 to 40 s once readings flow (the simulator drains its backlog, then every sensor is re-judged within 10 s; the page refreshes every 5 s). |
| PagerLens shows nothing | Reporter not started | Check `PAGERLENS_URL` and a token or service-client credentials; the log says "refusing to send a token over plain http" or "reporter: PagerLens answered HTTP ...". |
| Alert for city `default` | Legacy sensors went offline | Delete the legacy city (admin). |
| Delete city answers `409` | Another deletion is running | Retry later. |

---

## 12. Testing and development

```bash
make lint            # ruff check src tests
make test            # pytest -q; DB tests skip without TEST_DATABASE_URL
make test-db         # needs POSTGRES_PASSWORD exported; recreates database citypulse_test on the compose Postgres and runs everything
```

Without compose, run the database tests against any disposable database:
`TEST_DATABASE_URL=postgresql://user:pw@127.0.0.1:5434/citypulse_test .venv/bin/pytest -q`. **The DB tests drop and recreate the `public` schema of that database**, so never point them at real data.
`tests/conftest.py` forces `CITYPULSE_ROLE=api` so tests never start a simulator.

| File | Covers |
|---|---|
| `tests/test_pure.py` | Validation, status tiers, simulator behaviour under every burst, back-pressure and retry, reporter logic (no database). |
| `tests/test_geo.py` | No database: built-in polygons (closed, counter-clockwise, tiling, convex, centred on the real city centre), deterministic sensor positions inside their own district, `lat`/`lon` validation, the simulator's position, the Python side of the PostGIS queries with a fake connection, degradation messages, kit version >= 0.3.0. |
| `tests/test_multimodel_pure.py` | No database: `attrs` validation (shape, caps, secrets), the simulator's seeded metadata, the 16-number fingerprint (every dimension, clipping, separation in cosine distance), the deterministic demo history (fixed expectations), series ranges, partition names, the assembled explain statement per availability, tool schemas and argument checking. |
| `tests/test_multimodel_db.py` | Need `TEST_DATABASE_URL`: JSONB storage, replacement and the GIN index (`EXPLAIN` uses it for `@>` and `@?`), HOT updates preserved, the `attrs` / `attrs_path` filters, roll-ups (upsert, late data, retention guard, partitions created, dropped and refused outside), the series API, the engine hooks, labelling of outcomes, similarity search with its filters (pgvector), the verdict-change and quarter-hour fingerprints, `seed-history` (deterministic, idempotent, removable), the explain statement (one statement, part-by-part degradation), the tool endpoints, migration 007 upgrade. pgvector tests skip without the extension; the PostGIS part of Explain runs with `CITYPULSE_EXPECT_POSTGIS=1`. |
| `tests/test_edb_features.py` | Need `TEST_DATABASE_URL`: `/api/insights` permissions and graceful absence of the EDB extensions, `db_insights_*` in `/metrics`, read-only hardening with a real `a1p_readonly` member (secrets denied, fleet readable, idempotent, skipped without the role), positions stored with or without PostGIS, routes degrading without PostGIS. **PostGIS tests** (schema, seed, `ST_Contains`, nearby ordering with Paris to Lyon about 391 km, GeoJSON validity, backfill) are skipped on a server without PostGIS. |
| `tests/test_cities.py`, `tests/test_federation_db.py`, `tests/test_db.py` | Need `TEST_DATABASE_URL`: gateway, status engine, bursts, scopes, OTLP in three encodings including a gzip bomb, `/metrics`, cities, chunked deletion, upgrades from pre-city and pre-004 schemas, federated roles and service scopes, SSO with a mocked provider. |
| `tests/test_ui_js.py` and `tests/fakedom/*.mjs` | Need `node`: every script parses as a module, no HTML injection patterns, delete-city confirmation, refresh behaviour and revoke dialog against a fake DOM, `fakedom/map.mjs` for the Map tab (SVG built without style attributes, colour classes, keyboard, table, not-available card) and `fakedom/explain.mjs` for the Explain tab (one request per click, five models as text, honest not-available parts, the SQL disclosure). |

`scripts/bench_ingest.py` measures gateway throughput (`--readings`, `--sensors`, `--cities`, `--threads`, `--runs`); it **drops and recreates the `public` schema**, so use a scratch database. It is not a capacity benchmark.

Code style: ruff, line length 100, rules `E F I B UP` (`pyproject.toml`). Adding a sensor type: edit `catalog.py` (limits, curve, interval, weight, `stuckable`); the API and UI read the catalogue. Adding a permission: edit the tables in `core.py` and put `require("<permission>")` on the route.

---

## 13. AIDB and AgentPG integration points

**What exists today**

- **AgentPG**: CityPulse runs as a public image on an AgentPG sandbox linked to its own Postgres instance, reading the injected connection details (`DB_EXTERNAL_DSN` or `PG*`), waiting for a scaled-to-zero database (kit `Database` retries; the pool opens lazily so the process starts while the database sleeps; `/readyz` reports `waking-database`). It needs no extension, so it works on a plain AgentPG instance; PostGIS, when the instance offers it, adds the Map ([below](#edb-and-agentpg-capabilities-used-by-this-app)); pgvector adds the pattern memory, and JSONB, partitioning and roll-ups work on any Postgres ([7.11](#711-one-engine-five-data-models)).
- **Postgres as the whole platform**: queue-less ingest with back-pressure, per-reading upserts tuned for HOT updates (`fillfactor = 70` on `sensor`), BRIN and B-tree indexes on `reading`, advisory locks for leader election, date-binned time series, retention by batched deletes.
- **AIDB**: not used (the instance role currently lacks the `aidb` grants). The source holds no LLM or MCP code; the vectors are hand-designed fingerprints computed by the app, not model embeddings. The substrate an agent would call (`explain_district` and two more tools with JSON Schemas, [7.15](#715-explain-this-district-one-query-across-all-five-models)) exists and is read-only. The CI database image is `pgvector/pgvector:pg17`, which now also serves the pattern memory.

### EDB and AgentPG capabilities used by this app

Facts below were probed on a throwaway AgentPG instance (Postgres 18, role without superuser) and are recorded in `ecosystem/docs/06-edb-and-agentpg-capabilities.md`.

| Capability | Where it shows | Behaviour when it is not there |
|---|---|---|
| **PostGIS 3.6** (`geography`, `ST_DWithin`, `ST_Distance`, `ST_Contains`, `ST_AsGeoJSON`, GiST indexes) | Map tab, `/api/sensors/nearby`, `/api/districts/geojson`, `/api/sensors/geojson`, containment-based district health ([7.8](#78-map-tab-postgis)) | Routes answer `available: false`; positions are still stored in `lat`/`lon`; the tab says so; PostGIS added later backfills. |
| **pgvector** (`vector(16)`, HNSW `vector_cosine_ops`, `<=>`, iterative scans from 0.8) | Pattern memory: `/api/districts/{id}/similar`, the vector part of Explain ([7.14](#714-pattern-memory-have-we-seen-this-before)) | Fingerprints are still recorded (`real[]`); search answers `available: false` with the reason; pgvector added later fills the generated column. |
| **JSONB with a GIN index** (`jsonb_path_ops`, `@>`, jsonpath `@?`) | Device metadata, `/api/sensors?attrs=`, the vendor/firmware part of Explain ([7.12](#712-device-metadata-json)) | core Postgres: always on. |
| **Declarative partitioning, BRIN, `date_bin`** | Roll-up tables and the series API ([7.13](#713-time-series-on-core-postgres)) | core Postgres: always on. |
| **EDB query insights** (`edb_stat_monitor`, `edb_pg_tuner`, `query_advisor`, falling back to `pg_stat_statements`) via the kit | Insights tab, `/api/insights`, `db_insights_*` in `/metrics` ([7.9](#79-insights-tab-admin)) | Each source shows "Not available on this database." with a reason; `db_insights_available 0`. |
| **AgentPG read-only role `a1p_readonly` and the restricted MCP server** | Credentials and sessions are closed to it; the fleet data stays readable ([7.10](#710-what-a-read-only-user-and-agentpgs-restricted-mcp-can-see)) | Skipped quietly when the role does not exist. |
| AgentPG sandboxes and instances | How the app is deployed (above) | n/a |

Not used, with the reason: TimescaleDB and ClickHouse (not on AgentPG; the time series is native Postgres, [7.13](#713-time-series-on-core-postgres)), `btree_gist` (nothing here needs a mixed scalar and spatial index or an exclusion constraint), `data_redaction` (superuser only; the grant-based approach in 7.10 needs none), AIDB (no grant yet), `tcn`, `unaccent`, `fuzzystrmatch` (no search or live-update feature needs them in this app).

**What is planned** (design only, from `ecosystem/docs/03-aidb-integration.md` section 5.4 and `04-showcase-readiness-plan.md`; nothing below is implemented in this repository)

- A **semantic knowledge base over CityPulse's own schema**, so an operator or an agent can ask in natural language "which districts lost the most sensors during the last burst?" (AIDB semantic KB / text-to-SQL), plus health analysis with AIDB's native tools and a pipeline over incident and alert text.
- The Simulator becoming the **load generator for AIDB itself** (pipelines under sustained inserts, retrieval latency with a 10,000-sensor city), producing published benchmarks.
- Prerequisite noted in that document: AIDB 7.8.0 installs on an AgentPG instance, but the instance login role lacks the `aidb_users` grant; the platform team has to provide it.

Today the AI part of the story is PagerLens: CityPulse supplies the metrics, alerts and deploy markers, and PagerLens investigates them.

---

## 14. HTTP reference

Auth column: permission required (`-` = none). Bearer credentials: a local `cp_` token, the static key, or an identity access token; browsers use the session cookie `cp_session`.
Routes marked "dash" answer 404 under role `sim`.

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /healthz`, `GET /readyz` | - | Liveness and readiness. |
| `GET /metrics` | `metrics:scrape` | Prometheus metrics. |
| `GET /` | - (dash) | The UI. Static assets under `/static/`, shared UI under `/kit/`. |
| `GET /api/session` | - (dash) | Which screen to show; user, roles, permissions when signed in. |
| `GET /auth/login`, `GET /auth/callback` | - | SSO start and callback (404 when SSO is off). |
| `POST /api/setup` | - (dash) | Body `{code, password}`; complete first-run setup. |
| `POST /api/login` | - (dash) | Body `{password}` or `{api_key}`. |
| `POST /api/logout` | - | Ends the session; with SSO returns `logout_url`. |
| `POST /api/admin-password` | `settings:manage` | Set or change the admin password; signs everyone out. |
| `GET/POST /api/tokens`, `DELETE /api/tokens/{id}` | `settings:manage` | List, create (`{name, scope}`), revoke. |
| `POST /api/readings` | `ingest:write` | Push readings (`{"readings":[...]}`, at most 1000; optional `lat`/`lon` and optional device metadata `attrs` per reading, [7.12](#712-device-metadata-json)). |
| `POST /v1/metrics` | `ingest:write` | OTLP/HTTP metrics. |
| `GET /api/catalog` | `city:read` | Sensor types and the built-in simulator's districts. |
| `GET /api/status?city=` | `city:read` | Verdict, counts, per-type and per-district roll-ups, matrix (at most 200 districts), reasons, simulator heartbeat, active bursts. |
| `GET /api/history?minutes=5..720&city=` | `city:read` | Health series and throughput. |
| `GET /api/cities?q&limit&offset&sort` | `city:read` | Paged city list. |
| `GET /api/cities/{id}/districts` | `city:read` | Districts of a city. |
| `DELETE /api/cities/{id}` | `city:delete` | Delete a city (irreversible). |
| `GET /api/sensors?state&type&district&city&q&limit&attrs&attrs_path` | `city:read` | Sensor list (default state `problem`, limit 100, max 500), with each sensor's `attrs`. `attrs` is a JSON object the metadata must contain (`@>`, GIN); `attrs_path` a jsonpath predicate (`@?`); `422` when malformed ([7.12](#712-device-metadata-json)). |
| `GET /api/districts/{id}/series?city&bucket&from&to&type` | `city:read` | Roll-up series of one district (`city` required; `bucket` `5m` or `1h`; at most 2100 buckets) ([7.13](#713-time-series-on-core-postgres)). |
| `GET /api/districts/{id}/similar?city&k&scope` | `city:read` | The most similar past episodes with their outcomes (pgvector; `available: false` without it) ([7.14](#714-pattern-memory-have-we-seen-this-before)). |
| `GET /api/districts/{id}/explain?city&k` | `city:read` | One SQL statement across relational, time-series, JSON, geo and vector data, with the statement in `sql` ([7.15](#715-explain-this-district-one-query-across-all-five-models)). |
| `GET /api/tools`, `POST /api/tools/{name}` | `city:read` | Read-only tool definitions with JSON Schema (`explain_district`, `similar_episodes`, `district_series`) and a call endpoint (`{"arguments": {...}}`). |
| `GET /api/sensors/{id}` | `city:read` | One sensor with its last 60 readings (and `lat`, `lon` when it has a position). |
| `GET /api/sensors/nearby?lat&lon&radius_m&limit&city` | `city:read` | Nearest located sensors with `distance_m` (PostGIS; `available: false` without it). |
| `GET /api/sensors/geojson?city&state&limit` | `city:read` | Located sensors as GeoJSON points (PostGIS). |
| `GET /api/districts/geojson?city` | `city:read` | District polygons with per-district health, as GeoJSON (PostGIS). |
| `GET /api/insights?order&limit` | `settings:manage` | Database insights (EDB extensions where installed). |
| `GET /api/chaos?city=` | `city:read` | Active bursts and the kinds. |
| `POST /api/chaos` | `city:control` | Body `{kind, target, intensity 1-5, seconds 5-1800, city?}`. |
| `POST /api/chaos/storm` | `city:control` | Body `{seconds, city?}`: the red button. |
| `POST /api/chaos/stop` | `city:control` | Stop all bursts. |

There is no MCP endpoint and no OpenAPI page (disabled).

---

## 15. Data model

Tables created by migrations: `app_setting` (key/value: settings and `sim_heartbeat`), `api_token`, `web_session` (the kit's auth schema upgrades these and adds its OIDC state tables), `sensor` (one row per sensor: id, type, district, `city`, `protocol`, `last_ts`, `last_value`, `status`, `reason`, `status_since`, optional `lat`/`lon`, optional `attrs` JSONB with a GIN index, and with PostGIS the generated `geom`; `fillfactor 70`), `reading` (`sensor_id`, `ts`, `value`; index `(sensor_id, ts DESC)` and BRIN on `ts`), `status_history` (`scope` = `overall`, `district:<id>`, `type:<key>` or `city:<id>`; primary key `(scope, ts)`), `chaos` (`kind`, `target`, `intensity`, `started_at`, `until`; an active burst has `until > now()`), `rollup_5m` (partitioned by month) and `rollup_1h` (`city`, `district`, `type`, `bucket`, `readings`, `sensors`, `out_of_range`, `avg_value`, `min_value`, `max_value`; primary key `(city, district, type, bucket)`), `episode` (`city`, `district`, `window_end`, `trigger`, `verdict`, `health`, `features real[16]`, the outcome labels `verdict_1h`, `episode_start`, `ended_at`, `duration_min`; unique `(city, district, window_end)`). The multi-model picture, with a diagram, is in [7.11](#711-one-engine-five-data-models).
The `sensor.city` column has no default (migration 004). `district_area` (`city`, `district`, `geom geometry(Polygon, 4326)`, `source`) is created at startup by `geo.ensure` when PostGIS is available, and `episode.embedding` (a generated `vector(16)`) with its HNSW index by `memory.ensure` when pgvector is, not by a migration file, because a migration cannot be conditional on an extension and must not block startup without it.

---

## 16. Known limitations and unverified points

Stated by the project (README "Honest limits") and confirmed in code:

- The simulator and gateway share one Postgres; this demonstrates failure modes, it is not a capacity benchmark. Raw readings are a plain table (BRIN on `ts`) pruned by delete, not partitions, on purpose ([7.13](#713-time-series-on-core-postgres)); only the roll-ups are partitioned.
- OTLP has been tested with synthetic payloads, not a live OpenTelemetry SDK or Collector.
- Failed-sign-in rate limiting and the 30 s token cache are per process.
- Per-city detail is API-only; `/metrics` has no city label. PagerLens sees per-city detail only for the 20 least healthy cities.
- No per-city access control: `city:read` sees every city.
- Reading timestamps are validated against 24 h back and 2 min ahead; a sender that is offline longer loses the oldest data.

Found while writing this guide:

- **Burst button and the CityPulse Simulator (resolved).** The Simulator's source was read: it does not consult CityPulse's `chaos` table or API. With a fleet-fed CityPulse (role `api`) the button only has an effect through `slow_db` (gateway delay, `429`s, Simulator backs off); the other six faults act on the built-in simulator only (role `all` or `sim`). Documented in 7.3 and in the Burst tab's footnote; not changed in code because making the Simulator obey would be a change in another project.
- **Federated instances and the local admin password (verified by a test).** Under SSO the first-run setup is skipped and no setup code is printed, so a purely federated instance starts with no local admin password and no local sign-in (the login page shows only the SSO button). Break-glass is created, not found: while the identity provider works, sign in as an `admin`, open Settings, **Admin password**, and set one (12+ characters); the login page then offers "Administrator password" as long as `CITYPULSE_LOCAL_LOGIN` is not `false`. If the identity provider is down and no password or `CITYPULSE_API_KEY` was ever set, nobody can sign in locally: set `CITYPULSE_API_KEY` (or `CITYPULSE_API_KEY_FILE` / `/etc/citypulse/api_key`) on the container and restart, then sign in with the key, and use Settings. Tokens that already exist (ingest, read, metrics) keep working throughout. `citypulse reset-admin` under SSO prints no setup code (it now says so). Prepare the break-glass before an outage, not during one.
- **PostGIS and EDB-extension paths were not executed in the automated tests on the developer machine** (its Postgres 17 has no PostGIS and no `edb_*` extension). What was run: the degradation paths against plain Postgres; the exact PostGIS SQL (generated column, both GiST indexes, polygon upsert, containment join, health aggregation, nearby query; Paris to Lyon came back 391.7 km) read-only against a throwaway AgentPG instance with PostGIS 3.6.3, using temporary tables; the Map tab rendered in a browser with the PostGIS queries replaced by pure-Python equivalents. The PostGIS tests in `tests/test_edb_features.py` run wherever the test database offers PostGIS (CI image `postgis/postgis` or an AgentPG instance); the `edb_*` insight sources have only the kit's own tests.
- **Multi-model additions (7.11 to 7.15): what was and was not verified.** Run on a Postgres 17 with pgvector 0.8.0 and no PostGIS (a fresh cluster, also under scram-sha-256 like CI, and as a login role that may not create extensions, which is the real "no pgvector" path): JSONB, GIN, roll-ups, partitions, fingerprints, labelling, `seed-history`, similarity search, the explain statement without its geo part, the tool endpoints, the Explain tab against a fake DOM. The exact PostGIS part of the explain statement (and the whole statement with pgvector 0.8.7, including `set_config('hnsw.iterative_scan', ...)`, the labelling SQL and the vendor/firmware query) was executed read-only on a throwaway AgentPG instance (PostgreSQL 18.6, PostGIS 3.6.3) against TEMP tables only; the real-table PostGIS test (`test_explain_geo_part_with_postgis`) runs with `CITYPULSE_EXPECT_POSTGIS=1` on a server that has PostGIS. Not verified: behaviour with pgvector older than 0.8 against a real server (the statement without the `cfg` CTE is built and checked as text and runs on 0.8 without it; the iterative path is the one that is exercised), partition maintenance over a real month change, and the status engine's quarter-hour pass over several hours of live traffic (the pass is tested by calling `memory.tick` with chosen times). The fingerprint's 429 rate is process-wide (several replicas each see their own); a burst aimed at a sensor type is not counted in `burst` (its effect appears in the sensor states).
- **`CITYPULSE_PUBLIC_URL`** is not set by `ecoctl` (not found in the ecoctl source); the app then uses the request's own host and forwarded protocol, which AgentPG's proxy provides.
- **`ecoctl apply --rotate`** and **identity group names** are described from the ecosystem README, not exercised here.
- **Static key as a bearer token** is stated in `core.require`'s docstring and the kit's `LocalAuth`; I did not read the kit's `Authenticator` end to end.
- **`pagerlens-agent` scrape** of `/metrics` is configured on the PagerLens side and was not verified from here; the snippet in the Connect tab is the project's own.
- Nothing was deployed or run against a live service for this guide. The burst, SSO and heatmap statements were afterwards checked locally (throwaway Postgres, Playwright rehearsal of the video script, new tests); the rest comes from reading the code and manifests.
