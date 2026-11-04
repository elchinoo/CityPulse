# CityPulse

A smart-city sensor platform on Postgres (plain Postgres is enough; PostGIS, EDB's query insights and AgentPG's read-only hardening light up where the platform offers them). Thousands of simulated sensors report through a real collection
path, a status engine turns their readings into a city-wide verdict, and a **burst button** stresses the whole
thing on purpose so you can watch a monitoring tool such as [PagerLens](https://github.com/elchinoo/PagerLens)
find, explain and report the incident.

It is a sample project: small enough to read in an afternoon, built to be extended, and safe to expose publicly.

**Documentation:** the complete [guide](docs/GUIDE.md) (ecosystem integration, architecture, install, configuration, operations,
troubleshooting) and a [tutorial video script](docs/VIDEO-SCRIPT.md). EDB and AgentPG features: the [Map](docs/GUIDE.md#78-map-tab-postgis)
(PostGIS), [multi-model data and Explain](docs/GUIDE.md#711-one-engine-five-data-models) (JSONB, partitions, pgvector), [database insights](docs/GUIDE.md#79-insights-tab-admin), [read-only and AI hardening](docs/GUIDE.md#710-what-a-read-only-user-and-agentpgs-restricted-mcp-can-see)
and the [list of EDB and AgentPG capabilities this app uses](docs/GUIDE.md#edb-and-agentpg-capabilities-used-by-this-app).

```
 sensors / OTel Collector ──HTTP──▶ ┌──────────────┐   ┌──────────────────────────┐
 simulator (role sim or all) ─────▶ │ ingest       │──▶│ Postgres                 │
                                    │ gateway      │   │ sensor · reading · chaos │
 browser ──▶ dashboard + burst ───▶ │ (validates,  │   └────────────┬─────────────┘
                                    │  back-pressure)│               │
 PagerLens ◀── /metrics, alerts ──  └──────────────┘   status engine (every 10 s)
```

* **One image, two roles.** `CITYPULSE_ROLE=api` serves the gateway, dashboard and status engine; `sim` runs only
  the simulator; `all` (default) runs both in one process; both of those need `CITYPULSE_CITY` (see below).
* **27 sensor types** across the 12 districts of the built-in simulator (real senders name their districts freely, see **Districts**): rain, light, humidity, temperature, traffic cameras, traffic lights and
  speed radars, plus wind, pressure, UV, air quality, CO₂, noise, soil moisture, water level/flow/pressure, power
  meters, street lights, gas leaks, waste bins, bridge vibration, seismic, pedestrians, bike-share, EV chargers and
  parking bays. See `src/citypulse/catalog.py`.
* **A map** (PostGIS): district polygons coloured by health and the sensors, drawn as inline SVG; `GET /api/sensors/nearby`
  and `GET /api/districts/geojson`. Readings may carry optional `lat` and `lon`. Brussels and Vienna come with their **real district boundaries** and a light base map (water, parks, main roads; OpenStreetMap and Stadt Wien open data, attribution on the map, no network at run time: [guide 7.8](docs/GUIDE.md#78-map-tab-postgis)); other cities without built-in polygons get district areas derived from their sensors' positions (`ST_ConvexHull`, `ST_Collect`, `ST_Buffer`, with `ST_Contains` and `ST_DWithin` for the rest), marked `derived` on the map. Without PostGIS the map says so and nothing else changes.
* **Five data models in one Postgres** ([guide 7.11](docs/GUIDE.md#711-one-engine-five-data-models)): relational state, **JSON** device
  metadata (`sensor.attrs`, GIN `jsonb_path_ops`, `GET /api/sensors?attrs={"vendor":"Aerolab"}` and a jsonpath filter),
  **time series** on core Postgres (BRIN, `date_bin` roll-ups per district at 5 minutes and 1 hour, monthly partitions, `GET /api/districts/{id}/series`),
  **geo** (PostGIS) and a **vector pattern memory** (pgvector, HNSW cosine): "have we seen this before?" finds the most similar past
  episodes of a district and what happened next (`GET /api/districts/{id}/similar`, demo history with `citypulse seed-history`).
  Each extension is guarded: without PostGIS or pgvector the routes say so and everything else works. No TimescaleDB or ClickHouse (not on AgentPG).
* **Explain this district**: one SQL statement, one round trip, all five models (`GET /api/districts/{id}/explain`, the **Explain** tab),
  also registered as a read-only tool with a JSON Schema (`GET /api/tools`) for an AIDB or MCP agent to call later. No LLM agent is built here.
* **Database insights** (admin): top queries, tuning and index hints from EDB's extensions where installed, honestly "not
  available" elsewhere; the secrets tables are closed to AgentPG's read-only role and so to its restricted MCP server.
* **Seven bursts**, individually or all at once, that end by themselves: `flood`, `noise`, `dropout`, `stuck`,
  `late`, `corrupt`, `slow_db`.

## Quick start (local)

```bash
cp .env.example .env            # then put a generated password in POSTGRES_PASSWORD
docker compose up --build       # (set CITYPULSE_CITY in .env first) Postgres on 127.0.0.1:5434, app on http://127.0.0.1:8002
docker compose logs api         # the one-time setup code is printed here
```

Open the app, enter the setup code, choose an admin password. About half a minute later the sensors have all
reported and the Overview shows the heatmap. Open **Burst** and press the red button. For "have we seen this before?" to have a past on a
fresh install, run `citypulse seed-history` once (optional: 28 deterministic days for paris and lyon; needs pgvector to search, see the guide, 7.14).

Without Docker: `pip install -e ".[dev]"`, export `DATABASE_URL` and `CITYPULSE_CITY` (or `CITYPULSE_ROLE=api`), run `citypulse start`.

## Industrial protocols and the simulator

CityPulse itself speaks HTTP: the push API, OTLP and `/metrics`. Field equipment that speaks **Modbus TCP, EtherNet/IP,
EtherCAT, PROFINET or PROFIBUS** reaches it through an **edge gateway** that terminates those protocols and forwards readings
over HTTPS, as a plant gateway does (and as a platform that only exposes HTTP requires). The companion project
[CityPulse Simulator](https://github.com/elchinoo/CityPulseSimulator) is such a gateway plus the field devices behind it: you
create cities of simulated sensors (27 types, any protocol, a range, a frequency and a burst percentage) and it sends their data
here. Each reading may carry an optional `protocol` field (`http` by default, `otlp`, `direct`, `modbus_tcp`, `ethernet_ip`,
`ethercat`, `profinet`, `profibus`); the Overview breaks sensor health down by it.

## Cities

Every sensor belongs to one **city**: a short id (`^[a-z0-9][a-z0-9_-]{0,31}$`, e.g. `paris`). The city is **required**
on every reading, and there is no default city. A reading without one is rejected and counted like any other bad item
(`city is required`, JSON and OTLP alike), and so is the id `default`, which is reserved. A sensor's city is fixed by its
first reading, like its type and district: a reading that names another city for a known sensor id is rejected and counted
too (`city does not match the sensor's city`). Sensor ids are global, so make them unique across cities (the built-in
simulator prefixes them with the city).

* **Send it:** JSON `/api/readings` item field `"city"`; OTLP attribute `city` (or `city.id`) on the data point or the
  resource (the data point wins); built-in simulator `CITYPULSE_CITY=paris` (no default: the `sim` and `all` roles refuse to
  start without it).
* **Senders that omit the city** (older simulators, collectors, scripts) get their readings rejected: the response is still
  `200 {"accepted":…,"rejected":…,"errors":["N x city is required"],"hint":"…"}`, and the `hint` lists the fix: add
  `"city"` to every reading, set the OTLP `city` attribute (the OTLP `partialSuccess.errorMessage` carries the same
  advice), set `CITYPULSE_CITY` on the built-in simulator, or upgrade the sender. Watch `citypulse_readings_total{result="rejected"}`
  after the upgrade.
* **Read it:** every read endpoint (`/api/status`, `/api/history`, `/api/sensors`, `/api/chaos`) takes an optional
  `?city=<id>`; without it you see all cities, as before. `GET /api/cities?q=<prefix>&limit=<1-200, default 50>&offset=<n>&sort=name|worst`
  lists cities (case-insensitive prefix search, paginated; `total` counts the matches) with `counts` per status, the
  city's `health` and `state` (the same tiers as the fleet), its `worst_status` and its newest reading `last_ts`.
  `sort=worst` puts the least healthy first. A city row also has `legacy` (see below), and the page has `legacy_city`
  (the id of the legacy city while sensors still sit in it, else `null`). Sensor rows and `/api/sensors/<id>` carry `city`.
* **Delete it (admin only):** `DELETE /api/cities/<id>` needs the permission `city:delete`, held by the `admin` role and by
  `full` API tokens only (operators, viewers and the narrower token scopes get `403`). It irreversibly deletes the city's
  burst targets, readings, status history, roll-ups, pattern-memory episodes and sensors, each in transactions of at most 5000 rows (a large city never
  locks the database; the sensors go last, so an interrupted deletion can simply be repeated), and answers
  `{"city":"paris","deleted":{"chaos":n,"readings":n,"status_history":n,"rollups":n,"rollups_1h":n,"episodes":n,"sensors":n},"batches":n}`. `404` if no sensor
  reports for the city, `409` if another deletion is running. Stop the city's senders first: a sender that keeps
  reporting recreates its sensors. The dashboard has a **Delete city (Admin)** button on the Overview of a chosen city;
  it asks you to type the city id.
* **Pick it:** the dashboard has a searchable **City** menu (remembered in the browser). It never lists thousands of
  options: it asks the server for the first 12 matches (least healthy first, or by prefix as you type). It filters the
  Overview, Sensors and Burst tabs; with all cities shown, the Overview lists the least healthy few.
* **Districts are whatever the sensors report.** A district id is any `[a-z0-9][a-z0-9_-]{0,23}` (`alder-quay`, `north-mill`);
  nothing assumes 12 districts or that two cities share any. `GET /api/cities/<id>/districts` lists those of one city, biggest
  first, at most 200, with `total` for all: `{city, limit, total, districts:[{id, name, total, counts, health, worst_status}]}`
  (404 if the city has no sensors). `name` is the id prettified (`alder-quay` becomes "Alder Quay"); the legacy `d01`..`d12`
  keep their catalogue names. `/api/status` returns the 200 biggest districts (`districts_total` says how many exist) and the
  matrix for those; `/api/catalog` `districts` is only the built-in simulator's. The Overview matrix, the Sensors district
  filter and the Burst target menu are built from the data.
* **Burst it:** a burst target may be `city:<id>` next to `all`, `district:<id>` and `type:<key>` (the city must have
  reported; so must a district, except the built-in catalogue ids; `POST /api/chaos` takes an optional `"city"` that a district
  target must have reported in); `POST /api/chaos/storm` takes an optional `{"city": "<id>"}`. Existing targets span every city.
* **History per city:** the status engine records a health series per city every 30 s: for all cities while there are at
  most 200, otherwise for the 200 least healthy that have a problem (a city not tracked simply has no series, so its chart
  shows "collecting data"). The throughput chart of a city is computed from its readings.
* **PagerLens:** `citypulse-fleet` carries the whole-fleet metrics; the `SensorFleetDegraded` alert is raised per city (one
  city or many behave the same), only for cities that have a problem, on service `citypulse-fleet-<city>` (depends on
  `citypulse-ingest`) with a `city` label. At most the 20 least healthy cities are tracked at once.
* **Metrics:** `/metrics` has no per-city label (the cardinality would be unbounded); per-city numbers are in the API.

### Upgrading a deployment that has a `default` city

Before cities were mandatory, readings without a city (and the built-in simulator, unless `CITYPULSE_CITY` was set) landed
in a city called `default`. Migration `004_no_default_city.sql` deletes nothing: it only drops the column default. Existing
sensors stay where they are, readable, and are flagged **legacy, no city reported** (`"legacy": true` in `/api/cities` and
`/api/status?city=default`, a warning on the Overview and in the City menu). Nothing creates that city any more (the gateway
rejects readings without a city and the explicit id `default`), the status engine no longer decommissions its silent sensors
after 24 hours, and its raw readings still expire with `RETENTION_HOURS`. Because its sensors stop reporting once their senders
are fixed, they go offline and would raise a city alert in PagerLens like any unhealthy city, until you purge them:

1. Fix the senders (give them a real city, see above) and, for the built-in simulator, set `CITYPULSE_CITY`.
2. As an admin, delete the junk: open the Overview, pick the city `default` (the banner has an **Open it** button), press
   **Delete city default (Admin)** and type `default`; or call the API with an admin session or a `full` token:

   ```bash
   curl -sS -X DELETE "$CITYPULSE_URL/api/cities/default" \
     -H "Authorization: Bearer $CITYPULSE_FULL_TOKEN" -H "X-CityPulse-CSRF: 1"
   ```

   The answer lists how many sensors, readings, history rows and burst targets were removed.

## What the status engine decides

Per sensor, first match wins: **offline** (nothing for 12 reporting intervals), **stale** (3 intervals),
**fault** (the latest value is outside the physical limits of its type), **stuck** (five or more identical recent
readings, continuous types only), otherwise **ok**. The city is *operational* at ≥97% ok, *degraded* ≥85%,
*partial outage* ≥50%, else *major outage*; a district below 50% ok (with at least 20 sensors) forces at least a partial
outage, so the headline can be worse than the percentage next to it (for example 91% healthy but a partial outage): the
first reason is the fleet figure, the next ones name the district that forced the verdict. A sensor type below 85% ok
(at least 5 sensors) is listed as a reason (`CO2: 35% healthy (under 85%)`) but never changes the verdict. Nothing is
smoothed or latched: the engine re-judges every 10 s from the readings' own timestamps, so once a restarted sensor
source reports again, the sensors turn ok on the next pass and the verdict recovers within about 10 s plus the page's 5 s refresh.

The Overview's *Simulator buffer* tile belongs to the built-in simulator: it is shown only when the API reports one (role
`all`, or a simulator running against the same database), and hidden under `CITYPULSE_ROLE=api`, where the readings come from
a separate CityPulse Simulator. Durations in the UI are humanized everywhere (`42s`, `4 min`, `3 h`, `3 days ago`). While a page's
first data is loading it shows "Loading…", and a failed first request shows the error with a *Try again* button.

## Collecting data

Create an **ingest** token in Settings, then use any of these.

**Push API** (JSON, up to 1000 readings per request):

```bash
curl -sS -X POST "$CITYPULSE_URL/api/readings" \
  -H "Authorization: Bearer $CITYPULSE_INGEST_TOKEN" -H "Content-Type: application/json" \
  -d '{"readings":[{"sensor":"paris.temperature-d01-0001","type":"temperature","district":"d01","city":"paris","value":21.4}]}'
```

Bad items are rejected one by one and counted (`{"accepted":…,"rejected":…,"errors":[…]}`, plus a `hint` when readings lacked a city); the rest are stored.
`ts` is optional (epoch seconds or milliseconds, or ISO 8601; up to 24 h old). **HTTP 429 with `Retry-After`** means
the gateway is saturated: keep the batch and retry with backoff, which is what the simulator does. A sensor's type and
district and city are fixed by its first reading. `city` is required on every reading, see **Cities**.
A reading may also carry `lat` and `lon` (decimal degrees, together; OTLP attributes of the same names): the Map and `GET /api/sensors/nearby` use them (guide 7.8).

**OpenTelemetry** (OTLP/HTTP, protobuf or JSON, gzip accepted) at `POST /v1/metrics`. Gauge and sum data points become
readings. Identity comes from the attributes `sensor.id`, `sensor.type` (or a metric named after the type) and
`district` (default `d00`) and `city` (or `city.id`, required), on the data point or the resource. With the OTel Collector, point an `otlphttp` exporter at the base URL
and map your attributes with the `attributes`/`resource` processors. (Logs and traces are not accepted here.)

**The simulator as a separate process**, pushing through the same HTTP API as any collector:

```
CITYPULSE_ROLE=sim  CITYPULSE_INGEST_URL=https://your-citypulse  CITYPULSE_INGEST_TOKEN=<ingest token>  SIM_SENSORS=3000  CITYPULSE_CITY=paris
```

It keeps a bounded retry buffer (100,000 readings), backs off exponentially, and keeps original timestamps on retry,
so delayed data arrives late instead of being rewritten.

## Monitoring CityPulse with PagerLens

Pick one (or both; the Connect tab has copy-paste snippets):

1. **External agent, no CityPulse code involved.** `pagerlens-agent` probes `/healthz` and scrapes `/metrics` with a
   **metrics** token (a scope that can read nothing else). `/metrics` uses the same metric names as TrustDesk and
   PagerLens (`http_requests_total`, `http_request_duration_seconds`, `db_up`, `app_info`, `process_*`) plus
   `citypulse_readings_total{result}`, `citypulse_ingest_batches_total{result}`, `citypulse_ingest_inflight`,
   `citypulse_sensors{status}`, `citypulse_fleet_health_ratio`, `citypulse_district_health_ratio{district}` and the
   simulator's buffer.
2. **CityPulse reports itself.** Set `PAGERLENS_URL` and `PAGERLENS_TOKEN` (an **ingest** token from PagerLens; also
   accepted as `PAGERLENS_TOKEN_FILE`). Every 15 s it pushes two services, `citypulse-ingest` (up, rate, reject %,
   overloaded %, latency_p95_ms) and `citypulse-fleet` (health, offline/stale/fault/stuck %, worst district; depends on the ingest service),
   and raises a `SensorFleetDegraded` alert when the verdict worsens (repeated every 10 minutes while it lasts).
   With the identity provider (below) no hand-copied token is needed: CityPulse mints its own PagerLens token.

## Single sign-on and roles (the ecosystem identity provider)

CityPulse can run **standalone** (the first-run setup code and an admin password, as above) or **federated** with the ecosystem's
[identity provider](https://github.com/elchinoo/agentpg-identity). Register two clients there:

* a `web` client, id `citypulse`, redirect URI `https://<citypulse>/auth/callback`, with the roles below (browser sign-in), and
* optionally a `service` client, id `citypulse-service`, allowed to call `pagerlens` (CityPulse's own outbound identity).

| Variable | Meaning |
|---|---|
| `CITYPULSE_OIDC_ISSUER` | The identity provider's URL. Federation is off when unset. |
| `CITYPULSE_OIDC_CLIENT_ID` | The web client, default `citypulse`. |
| `CITYPULSE_OIDC_CLIENT_SECRET[_FILE]` | Its secret from the provider. |
| `CITYPULSE_PUBLIC_URL` | The URL CityPulse is reached at (used to build the callback). |
| `CITYPULSE_LOCAL_LOGIN` | `false` removes the admin-password and API-key login when SSO is on (default keeps them as a break-glass). |
| `CITYPULSE_SERVICE_CLIENT_ID` | The service client, default `citypulse-service`. |
| `CITYPULSE_SERVICE_CLIENT_SECRET[_FILE]` | Its secret. With the issuer set, CityPulse asks the provider for its PagerLens token (scope `pagerlens.ingest`) instead of using `PAGERLENS_TOKEN`; a static `PAGERLENS_TOKEN` wins if both are set. |

| Role | May |
|---|---|
| `admin` | everything, including API tokens, the admin password and deleting a city (`city:delete`) |
| `operator` | see all dashboards, sensors and bursts, and start or stop bursts; no tokens, accounts, settings or city deletion |
| `viewer` | see the dashboards, sensors and active bursts; change nothing |

Services use `client_credentials` instead of a hand-copied token: scope `citypulse.ingest` pushes readings (and nothing else),
`citypulse.read` reads the dashboard data, `citypulse.metrics` scrapes `/metrics`. Local API tokens (`ingest`, `read`, `metrics`,
`full`) and the static key keep working beside them with exactly the access they always had.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` / `DB_EXTERNAL_DSN` / `PSQL_CONNECTION_STRING` | – | Postgres connection (the PG* variables also work) |
| `CITYPULSE_ROLE` | `all` | `api`, `sim` or `all` |
| `SIM_SENSORS` | `3000` | Simulated sensors (10 to 50,000) |
| `SIM_SPEED` | `1` | Reporting-rate multiplier (1 to 20) |
| `CITYPULSE_CITY` | – (**required** for roles `sim` and `all`) | City of the built-in simulator's sensors (ids become `<city>.<type>-<district>-<n>`). Without a valid id (`[a-z0-9][a-z0-9_-]{0,31}`, not `default`) the simulator refuses to start. Not needed for `api`. |
| `CITYPULSE_INGEST_URL`, `CITYPULSE_INGEST_TOKEN` | – | Where the `sim` role pushes, and its token |
| `RETENTION_HOURS` | `6` | How long raw readings are kept |
| `MAX_SENSORS` | `50000` | Cap on auto-registered sensors (bounds abuse) |
| `INGEST_CONCURRENCY` | `8` | Batches written at once before the gateway answers 429 |
| `PAGERLENS_URL`, `PAGERLENS_TOKEN` | – | Report to PagerLens (https required unless localhost) |
| `CITYPULSE_OIDC_ISSUER` and friends | – | Single sign-on and service identity; see below |
| `CITYPULSE_API_KEY` | – | Optional static full-access key for automation; at least 32 characters (a shorter one is refused), e.g. `openssl rand -base64 36` |
| `CITYPULSE_ALLOW_ANONYMOUS` | `false` | Demo only: disables authentication entirely; honoured only on a loopback bind (`HOST=127.0.0.1`) unless `CITYPULSE_ALLOW_ANONYMOUS_PUBLIC=1` |
| `CITYPULSE_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxy addresses uvicorn trusts for `X-Forwarded-*` (never `*`) |
| `CITYPULSE_TRUSTED_HOPS` | `1` | Proxies in front of the app for the client address used by the rate limiter (`0` = socket peer) |
| `CITYPULSE_ALLOW_PRIVATE_URLS` | `false` | Allow a private or local `PAGERLENS_URL` (the address is resolved once and pinned) |
| `CITYPULSE_RETRIEVAL_BACKEND` | `app` | AIDB mode switch (`app` or `aidb`; default unchanged), see [AIDB mode](#aidb-mode) |
| `CITYPULSE_ENCRYPTION_KEY` | – | AIDB mode only: the platform secret that opens the seal key (also `_FILE`); not used while the switch is `app` |
| `CITYPULSE_AIDB_ADMIN_DSN` | – | Admin connection string for `citypulse aidb-setup`, for that one command only (it prompts when unset); never put it in a compose or env file |

Every secret also accepts a `<NAME>_FILE` variant for platforms that can only mount files.

## Deploy on AgentPG

AgentPG sandboxes run any public image and can be linked to a Postgres instance, which injects the connection
details that CityPulse reads automatically. Use a dedicated instance per app (several apps create identically named
tables).

```bash
agentpg login
agentpg create --name citypulse-db --size small            # note the instance id
agentpg sandbox create citypulse \
  --image ghcr.io/<owner>/citypulse:latest \
  --instance <instance-id> --port 8000 \
  --env CITYPULSE_ROLE=all --env CITYPULSE_CITY=<city> --env SIM_SENSORS=2000
agentpg sandbox status <sandbox-id>                        # prints the public HTTPS URL
agentpg sandbox logs <sandbox-id>                          # contains the one-time setup code
```

Notes: `--port 8000` is required; the image must be public on GHCR; and free-tier sandboxes auto-pause when idle and do
not wake on a request (`agentpg sandbox resume <sandbox-id>`), which pauses the simulator too. `--env` is not a secret
store: it is fine for `CITYPULSE_ROLE` and sizes, but prefer running anything that holds a token off the sandbox.
A resume may reuse a stale image; delete and recreate the sandbox to pull `:latest`.

### Sealed boot

Anything in a deployment's environment, config files or image is readable by whoever breaches the platform. With sealed boot
the only thing deployed is one encrypted blob, `APGKIT_SEALED` (AES-256-GCM under an operator passphrase); set nothing else
(no `DATABASE_URL`, ingest or PagerLens tokens, OIDC client secrets or API keys). CityPulse starts sealed: every route answers
503 except `/_seal/*`, `/healthz`, `/health` and `/livez`. The operator sends the passphrase, encrypted to a key that exists
only in that process; the app opens the blob, runs its start-up (migrations, status, masking and reporter loops) and serves.
Create the blob and unseal with `ecoctl seal` from [agentpg-ecosystem](https://github.com/elchinoo/agentpg-ecosystem).
CityPulse has no vault for secrets, so the passphrase only opens the blob (with AIDB mode on it also opens the seal key, see [AIDB mode](#aidb-mode)). With no blob configured nothing changes.

## AIDB mode

Opt-in. With nothing set CityPulse behaves exactly as described above and none of this code runs (no vault is created, no incident text is written,
`/readyz` says `"aidb": {"enabled": false}`). With EDB AI Accelerator (AIDB) in the database, two things can move into Postgres behind **one**
switch, which falls back to the unchanged behaviour when AIDB is missing, not set up, locked or fails its seals:

| Switch | Values | What it changes |
|---|---|---|
| `CITYPULSE_RETRIEVAL_BACKEND` | `app` (default), `aidb` | `GET /api/incidents/similar`: similar past incidents by the meaning of their text, from PostgreSQL full-text search (`app`) or an AIDB knowledge base kept live by a pipeline (`aidb`, through `apgkit.retrieval.select`). With `aidb`, `POST /api/ask` also works: a question in natural language, answered by an AIDB agent over read-only views. With `app` it answers 503. |

The existing fingerprint similarity (`/api/districts/{id}/similar`, pgvector, `vector(16)`), the explain card, the status engine and the tools are
untouched.

**Ask in natural language.** `POST /api/ask {"question": "which districts lost the most sensors during the last burst?"}` (permission `city:ask`:
admins, operators, the `full` token scope and the service scope `citypulse.ask`; a `read` token gets 403) returns

```json
{"answer": "During the last burst d01 lost the most sensors (12) ...", "sql": "SELECT i.district, ... FROM cp_ask.burst_impact i ...",
 "rows": [{"district": "d01", "sensors_lost": 12}, {"district": "d02", "sensors_lost": 3}], "row_count": 3, "truncated": false,
 "sql_error": "", "steps": [{"tool": "semantic_kb_search", "ok": true}, {"tool": "run_sql_query", "ok": true}], "conversation_id": "...", "agent": "cp_ask_agent"}
```

The agent first calls `semantic_kb_search` on a semantic knowledge base over the schema `cp_ask` (the comments on the views are what it matches),
then runs one read-only SELECT with `run_sql_query` under a purpose whose role can read **only** these views: `cp_ask.districts` (now),
`sensors`, `readings_5m` (the 5-minute roll-ups: aggregates, never raw readings), `district_health` (one row per district and 15-minute window),
`incidents` (the windows that were not operational, with the shares that explain them and the outcome), `bursts` and `burst_impact` (what each burst did
to each district). `sql` and `rows` are **read back from AIDB's own action log** (`aidb_internal.action_log`, where an `aidb_users` member sees only its
own rows), so they are what ran, not what the model says it ran; rows are capped at 200. A statement the database refused (the agent was asked to read
`reading`, `app_setting`, a token table...) comes back as `sql_error` and is a `denied` decision in AIDB's governance trace.

What the views leave out: the `reading` table (no raw per-reading flood), sensor positions, `sensor.attrs` (free-form vendor metadata), API tokens,
sessions and settings (the views depend on `sensor`, `rollup_5m`, `episode` and `chaos` only; a test checks the dependency list). The purpose role is
a NOLOGIN role with `SELECT` on those seven views plus the semantic-KB metadata tables AIDB's search needs, and nothing else.

Each question is **ledgered and logged as a short hash and its length, never its text**: a row in `cp_ai.ask_log` (when, who, hash, length, outcome,
steps, rows, milliseconds; pruned after 90 days) and one log line. The limit: AIDB itself keeps the question text in its own conversation log
(`aidb_internal.action_log`), and the hash of a short question can be guessed by trying candidates; do not put secrets in questions.

**Similar past incidents.** Every window whose verdict was not operational gets a short derived text (`cp_ai.incident_text`: the dominant failure mode,
the shares, whether a burst was active, and once known the verdict an hour later and how long it lasted; no sensor id, no attribute). The text is written
by the engine's quarter-hour pass, only while the switch is `aidb`, and by `aidb-setup` for what already exists. An AIDB pipeline embeds it live (local
`bge-small-en-v1.5-f16`, no key): a new or changed text is searchable seconds later with no app code. `GET /api/incidents/similar?q=...` takes a
description, `?city=..&district=..` describes that district's current state in the same words; the answer says which backend answered and, when it fell
back, why. A small embedding model separates incidents by their dominant failure mode ("stuck sensors" finds the stuck incident first) but only weakly by
numbers and durations; the full-text backend is a legitimate answer for keyword-like questions.

**Two privilege levels.** The app's own role stays unprivileged (no superuser, no `CREATEROLE`, and it needs no `CREATE` on schema `public` at run time).
Everything that needs more is the operator's, once:

```bash
# the connection string of a role that can create roles and register models (not stored, not logged; it prompts when the variable is unset)
CITYPULSE_AIDB_ADMIN_DSN=postgresql://... LLM_BASE_URL=https://gateway... LLM_API_KEY=... LLM_CHAT_MODEL=... citypulse aidb-setup
citypulse aidb-status                      # what is requested, set up, sealed, denied; exit 1 when something is wrong (add --json)
CITYPULSE_RETRIEVAL_BACKEND=aidb citypulse start
```

`aidb-setup` is idempotent (a repeat reports every step unchanged; tested). It refreshes the views from the code, registers the gateway chat model (a
host-limited credential; `--model NAME` uses one you registered), the live incident pipeline, the semantic knowledge base with the joins between the views,
the purpose and its role, the agent, enables telemetry, creates the narrow governance view, grants the runtime role exactly what it needs, backfills the
incident texts, and **seals** every definition. The runtime role only verifies, reads status, calls `aidb.agent_converse`, retrieves, reads the action log
of its own conversations and writes the ask ledger. `/readyz` carries an `aidb` object (state and which features are ready) and never fails the app because
of AIDB; `GET /api/aidb` (`settings:manage`) has the detail.

**Governance.** With telemetry on (`aidb.otel_client = database`, set by the operator step) AIDB records every purpose decision in `aidb_otel.traces`, whose
rows carry the attempted SQL of every recorded call. The runtime role has **no access** to that table (setup revokes any grant and fails if one remains
through `PUBLIC` or a membership). It reads the narrow view `cp_ai.governance_decisions_v` instead (a security-barrier view owned by the administrator:
when, status, decision id, agent, the purpose's role, where the decision was taken, denial reason and class; no SQL text, no principal), for this app's
agent and purpose only. `aidb-status` and `GET /api/aidb` show the denied decisions of the last day.

**The seal key, and its limit.** CityPulse has no vault of its own: it stores no secret in the database and has no password unlock for one. The kit can
only sign and check AIDB definitions with a vault's data key, so `core.vault` exists for that one purpose and nothing creates or opens it while the switch is
`app`. Its random data key is stored **wrapped** in `app_setting` (a table the read-only AgentPG role cannot read) under the platform secret
`CITYPULSE_ENCRYPTION_KEY` (environment, `_FILE`, or `/etc/citypulse/encryption_key`; opened at start-up) or, with sealed boot, under the unseal passphrase
(opened by the unseal step; `aidb-setup` asks for that same passphrase when no platform secret is set). The unwrapped key lives only in the process's memory.
The definitions that decide what data is read and which model receives it (the model and its credential's host restriction, the pipeline, the semantic
knowledge base, the search tools AIDB registers for it, the purpose **and its role's relation grants**, the agent, and the `cp_ask` views and governance
view) carry a MAC under it. A definition changed by a database write is reported (counter `citypulse_aidb_tampered_total{kind=...}`, a log line, `/readyz`,
`aidb-status`) and **the AIDB paths are refused** (similarity falls back to `app`, questions answer 503) until the operator inspects it and re-runs
`citypulse aidb-setup --reseal`; an agent, purpose or tool that nobody sealed (planted) is reported too, and so is a deleted one. If the vault is locked
(no platform secret that opens it, or a different passphrase) the state is `vault_locked` and the features fall back; the app still starts. The limits, said
plainly: this **detects**, it does not prevent: whoever holds the platform secret or the running process can seal anything; a member of `aidb_users` (the
runtime role is one) can call `aidb.create_agent`/`update_agent` in AIDB 7.9, which only the seals turn into a finding; a database administrator can
still edit a definition and the app notices at its next check (cached for 30 seconds). CityPulse has no hash-chained ledger, so unlike PayBroker a
detection is not a ledger entry, only the counter, the log and the health report. The views are re-asserted from the code at every start by the
migration hook; a role that can no longer create schema objects leaves them as found, and `aidb-setup` then refuses a changed one without `--reseal`
(unless the admin connection can refresh them).

**What was verified, and what was not.** Verified against AIDB 7.9.0 in Docker (`aidb-postgres:17`, own container per test run) with an unprivileged runtime
role that owns the app's tables, **after taking `CREATE` on `public` away and handing the database to another owner**: setup and its idempotence; the purpose's
exact grants and a refusal of every base table; verify/status/readyz with a read-only seal table; both retrieval backends and the live pipeline (a new text is
embedded without app code); the semantic search finding the burst views by meaning; `/api/ask` end to end with the kit's scripted OpenAI-compatible model
(`tests/aidbfake.py`) calling `semantic_kb_search` and `run_sql_query`, with answer, SQL and rows; a refused query reported and recorded as a denied decision
in the governance view; tampering with an agent, a planted agent, a forged and a deleted seal, a rewritten view and semantic knowledge base, a missing seal
table, and the operator's repair. Plain-Postgres tests (CI) cover the switch and its default, the views and what they leave out, the incident texts, every
fallback, the vault paths (platform secret, passphrase, sealed boot) and the commands. **Not verified:** a real chat model (how well one follows AIDB's
simulated tool calling and writes SQL over these views is unknown; only scripted answers ran), the llm-gateway (the tests register a plain-http stand-in with
`aidb.allow_insecure_egress`, a gateway URL must be https), an AgentPG-hosted AIDB (no role there has the AIDB grants yet), the read-only role `a1p_readonly`
with the `cp_ask`/`cp_ai` schemas, PostGIS together with AIDB (the image has no PostGIS: the geo features are off in those tests, behaviour unchanged), sealed
boot together with a live AIDB, load. The native `analyze_db_health` tool of the plan is not used.

## Tests

```bash
make lint && make test           # unit tests, no database needed
make test-db                     # everything, against a throwaway database on the compose Postgres
make test-aidb                   # the AIDB tests (marker `aidb`) against a throwaway aidb-postgres:17 container; see AIDB mode
```

The suite covers validation, the simulator's behaviour under every burst, back-pressure and retry, each status
verdict, cities (a missing city rejected on JSON and OTLP, mismatch, every filter, `/api/cities`, city bursts, deleting a city in chunks and who may, the upgrade from the pre-city and pre-004 schemas with legacy sensors, per-city alerts, the picker and delete-confirmation scripts under node), token scopes (ingest cannot read, read cannot write, metrics only scrapes), OTLP in all three encodings
including a gzip bomb, `/metrics`, and the federated paths (roles, service scopes, SSO login with a mocked provider; those run with `make test-db`).

## Honest limits

* The simulator and the gateway share one Postgres; this demonstrates failure modes, it is not a capacity benchmark.
  Raw readings are plain tables pruned by a periodic delete, not partitions.
* The OTLP receiver has been tested with synthetic payloads, not with a live OpenTelemetry SDK or Collector.
* Rate limiting of failed sign-ins is in memory, per process.
* Tokens are cached for 30 seconds per process, so a revoked token can still work briefly on other replicas.
* PagerLens diagnoses the three sensor-fleet patterns (ingest overload, sensor-fleet outage, bad data) from the metrics above; it does not see per-district detail beyond the worst-district share (per-city detail only for the 20 least healthy cities).
* Per-city detail is API-only: `/metrics` has no city label. `scripts/bench_ingest.py` measures ingest throughput against a scratch database.

Licensed under Apache-2.0. See [SECURITY.md](SECURITY.md).
