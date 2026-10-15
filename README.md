# CityPulse

A smart-city sensor platform on plain Postgres. Thousands of simulated sensors report through a real collection
path, a status engine turns their readings into a city-wide verdict, and a **burst button** stresses the whole
thing on purpose so you can watch a monitoring tool such as [PagerLens](https://github.com/elchinoo/PagerLens)
find, explain and report the incident.

It is a sample project: small enough to read in an afternoon, built to be extended, and safe to expose publicly.

```
 sensors / OTel Collector ──HTTP──▶ ┌──────────────┐   ┌──────────────────────────┐
 simulator (role sim or all) ─────▶ │ ingest       │──▶│ Postgres                 │
                                    │ gateway      │   │ sensor · reading · chaos │
 browser ──▶ dashboard + burst ───▶ │ (validates,  │   └────────────┬─────────────┘
                                    │  back-pressure)│               │
 PagerLens ◀── /metrics, alerts ──  └──────────────┘   status engine (every 10 s)
```

* **One image, two roles.** `CITYPULSE_ROLE=api` serves the gateway, dashboard and status engine; `sim` runs only
  the simulator; `all` (default) runs both in one process.
* **27 sensor types** across 12 districts: rain, light, humidity, temperature, traffic cameras, traffic lights and
  speed radars, plus wind, pressure, UV, air quality, CO₂, noise, soil moisture, water level/flow/pressure, power
  meters, street lights, gas leaks, waste bins, bridge vibration, seismic, pedestrians, bike-share, EV chargers and
  parking bays. See `src/citypulse/catalog.py`.
* **Seven bursts**, individually or all at once, that end by themselves: `flood`, `noise`, `dropout`, `stuck`,
  `late`, `corrupt`, `slow_db`.

## Quick start (local)

```bash
cp .env.example .env            # then put a generated password in POSTGRES_PASSWORD
docker compose up --build       # Postgres on 127.0.0.1:5434, app on http://127.0.0.1:8002
docker compose logs api         # the one-time setup code is printed here
```

Open the app, enter the setup code, choose an admin password. About half a minute later the sensors have all
reported and the Overview shows the heatmap. Open **Burst** and press the red button.

Without Docker: `pip install -e ".[dev]"`, export `DATABASE_URL`, run `citypulse start`.

## Industrial protocols and the simulator

CityPulse itself speaks HTTP: the push API, OTLP and `/metrics`. Field equipment that speaks **Modbus TCP, EtherNet/IP,
EtherCAT, PROFINET or PROFIBUS** reaches it through an **edge gateway** that terminates those protocols and forwards readings
over HTTPS, as a plant gateway does (and as a platform that only exposes HTTP requires). The companion project
[CityPulse Simulator](https://github.com/elchinoo/CityPulseSimulator) is such a gateway plus the field devices behind it: you
create cities of simulated sensors (27 types, any protocol, a range, a frequency and a burst percentage) and it sends their data
here. Each reading may carry an optional `protocol` field (`http` by default, `otlp`, `direct`, `modbus_tcp`, `ethernet_ip`,
`ethercat`, `profinet`, `profibus`); the Overview breaks sensor health down by it.

## What the status engine decides

Per sensor, first match wins: **offline** (nothing for 12 reporting intervals), **stale** (3 intervals),
**fault** (the latest value is outside the physical limits of its type), **stuck** (five or more identical recent
readings, continuous types only), otherwise **ok**. The city is *operational* at ≥97% ok, *degraded* ≥85%,
*partial outage* ≥50%, else *major outage*; a district below 50% ok forces at least a partial outage.

## Collecting data

Create an **ingest** token in Settings, then use any of these.

**Push API** (JSON, up to 1000 readings per request):

```bash
curl -sS -X POST "$CITYPULSE_URL/api/readings" \
  -H "Authorization: Bearer $CITYPULSE_INGEST_TOKEN" -H "Content-Type: application/json" \
  -d '{"readings":[{"sensor":"temperature-d01-0001","type":"temperature","district":"d01","value":21.4}]}'
```

Bad items are rejected one by one and counted (`{"accepted":…,"rejected":…,"errors":[…]}`); the rest are stored.
`ts` is optional (epoch seconds or milliseconds, or ISO 8601; up to 24 h old). **HTTP 429 with `Retry-After`** means
the gateway is saturated: keep the batch and retry with backoff, which is what the simulator does. A sensor's type and
district are fixed by its first reading.

**OpenTelemetry** (OTLP/HTTP, protobuf or JSON, gzip accepted) at `POST /v1/metrics`. Gauge and sum data points become
readings. Identity comes from the attributes `sensor.id`, `sensor.type` (or a metric named after the type) and
`district`, on the data point or the resource. With the OTel Collector, point an `otlphttp` exporter at the base URL
and map your attributes with the `attributes`/`resource` processors. (Logs and traces are not accepted here.)

**The simulator as a separate process**, pushing through the same HTTP API as any collector:

```
CITYPULSE_ROLE=sim  CITYPULSE_INGEST_URL=https://your-citypulse  CITYPULSE_INGEST_TOKEN=<ingest token>  SIM_SENSORS=3000
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

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` / `DB_EXTERNAL_DSN` / `PSQL_CONNECTION_STRING` | – | Postgres connection (the PG* variables also work) |
| `CITYPULSE_ROLE` | `all` | `api`, `sim` or `all` |
| `SIM_SENSORS` | `3000` | Simulated sensors (10 to 50,000) |
| `SIM_SPEED` | `1` | Reporting-rate multiplier (1 to 20) |
| `CITYPULSE_INGEST_URL`, `CITYPULSE_INGEST_TOKEN` | – | Where the `sim` role pushes, and its token |
| `RETENTION_HOURS` | `6` | How long raw readings are kept |
| `MAX_SENSORS` | `50000` | Cap on auto-registered sensors (bounds abuse) |
| `INGEST_CONCURRENCY` | `8` | Batches written at once before the gateway answers 429 |
| `PAGERLENS_URL`, `PAGERLENS_TOKEN` | – | Report to PagerLens (https required unless localhost) |
| `CITYPULSE_API_KEY` | – | Optional static full-access key for automation |
| `CITYPULSE_ALLOW_ANONYMOUS` | `false` | Demo only: disables authentication entirely |

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
  --env CITYPULSE_ROLE=all --env SIM_SENSORS=2000
agentpg sandbox status <sandbox-id>                        # prints the public HTTPS URL
agentpg sandbox logs <sandbox-id>                          # contains the one-time setup code
```

Notes: `--port 8000` is required; the image must be public on GHCR; and free-tier sandboxes auto-pause when idle and do
not wake on a request (`agentpg sandbox resume <sandbox-id>`), which pauses the simulator too. `--env` is not a secret
store: it is fine for `CITYPULSE_ROLE` and sizes, but prefer running anything that holds a token off the sandbox.
A resume may reuse a stale image; delete and recreate the sandbox to pull `:latest`.

## Tests

```bash
make lint && make test           # unit tests, no database needed
make test-db                     # everything, against a throwaway database on the compose Postgres
```

The suite covers validation, the simulator's behaviour under every burst, back-pressure and retry, each status
verdict, token scopes (ingest cannot read, read cannot write, metrics only scrapes), OTLP in all three encodings
including a gzip bomb, and `/metrics`.

## Honest limits

* The simulator and the gateway share one Postgres; this demonstrates failure modes, it is not a capacity benchmark.
  Raw readings are plain tables pruned by a periodic delete, not partitions.
* The OTLP receiver has been tested with synthetic payloads, not with a live OpenTelemetry SDK or Collector.
* Rate limiting of failed sign-ins is in memory, per process.
* Tokens are cached for 30 seconds per process, so a revoked token can still work briefly on other replicas.
* PagerLens diagnoses the three sensor-fleet patterns (ingest overload, sensor-fleet outage, bad data) from the metrics above; it does not see per-district detail beyond the worst-district share.

Licensed under Apache-2.0. See [SECURITY.md](SECURITY.md).
