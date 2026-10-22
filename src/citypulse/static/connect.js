import { $, copy, fill, h } from "/kit/ui.js";

const block = (text) => h("div", { class: "codewrap" }, h("pre", { class: "code" }, h("code", {}, text)),
  h("button", { class: "secondary small", type: "button", onclick: () => copy(text) }, "Copy"));

export function initConnect() {
  const base = location.origin;
  const push = `curl -sS -X POST ${base}/api/readings \\
  -H "Authorization: Bearer $CITYPULSE_INGEST_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"readings":[
    {"sensor":"temperature-d01-0001","type":"temperature","district":"d01","value":21.4},
    {"sensor":"paris.rain-d01-0002","type":"rain","district":"d01","city":"paris","value":0.0,"ts":1760000000}
  ]}'`;
  const collector = `# OpenTelemetry Collector: forward existing metrics to CityPulse.
# Map your own attributes to sensor.id / sensor.type / district (and optionally city) with the resource or attributes processor.
exporters:
  otlphttp/citypulse:
    endpoint: ${base}
    headers:
      Authorization: "Bearer \${env:CITYPULSE_INGEST_TOKEN}"
    # metrics_endpoint defaults to <endpoint>/v1/metrics
service:
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [otlphttp/citypulse]`;
  const simulate = `# A separate simulator process (CITYPULSE_ROLE=sim) pushes through the same HTTP API as any collector:
CITYPULSE_ROLE=sim
CITYPULSE_INGEST_URL=${base}
CITYPULSE_INGEST_TOKEN=<an ingest token>      # or CITYPULSE_INGEST_TOKEN_FILE=/path/to/file
SIM_SENSORS=3000
CITYPULSE_CITY=paris                          # optional: this simulator's city (default: "default"); ids become paris.<type>-<district>-<n>`;
  const agent = `# pagerlens-agent: watch CityPulse from outside (no CityPulse code involved)
targets:
  - name: citypulse-ingest
    tier: critical
    probes:
      - url: ${base}/healthz
    scrape:
      - url: ${base}/metrics
        token_env: CITYPULSE_METRICS          # a "metrics" token created here in Settings
    alert: {after_failures: 3, name: ServiceDown, severity: sev2}`;
  const report = `# Or let CityPulse report its own health and fleet verdict to PagerLens:
PAGERLENS_URL=https://your-pagerlens.example
PAGERLENS_TOKEN=<an INGEST token created in PagerLens>`;
  fill($("view-connect"),
    h("h1", {}, "Connect sensors and monitoring"),
    h("p", { class: "muted" }, "Everything that sends readings uses a token with the narrowest scope: create an ", h("strong", {}, "ingest"), " token under Settings for senders and a ", h("strong", {}, "metrics"), " token for scrapers."),
    h("h2", { class: "section-title" }, "1 · Push readings (HTTP, JSON)"),
    h("p", { class: "muted" }, "Up to 1000 readings per request. Bad items are rejected and counted, the rest are stored. HTTP 429 with Retry-After means the gateway is saturated: keep the batch and retry with backoff. A sensor's type, district and city are fixed by its first reading. The optional city is a short id (a-z, 0-9, _ and -, up to 32 characters, e.g. \"paris\"); readings without one belong to the city \"default\", and one claiming a different city for a known sensor id is rejected. Sensor ids are global, so make them unique across cities (e.g. prefix them with the city)."),
    block(push),
    h("h2", { class: "section-title" }, "2 · Or send OpenTelemetry"),
    h("p", { class: "muted" }, "OTLP/HTTP metrics at /v1/metrics (protobuf or JSON, gzip accepted). Gauge and sum data points become readings; identity comes from the attributes sensor.id, sensor.type (or a metric named after the type), district and, optionally, city (or city.id)."),
    block(collector),
    h("h2", { class: "section-title" }, "3 · Run the simulator as its own process"),
    block(simulate),
    h("h2", { class: "section-title" }, "4 · Monitor CityPulse with PagerLens"),
    h("p", { class: "muted" }, "Pick one: an external agent, or CityPulse pushing its own health."),
    block(agent), block(report));
}
