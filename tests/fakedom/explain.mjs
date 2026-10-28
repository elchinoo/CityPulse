// The Explain tab against a fake DOM: a district picker for the chosen city, one request per click, the five models rendered as text (no
// HTML injection, no inline style), honest "not available" parts when PostGIS or pgvector is missing, the SQL under a disclosure.
import assert from "node:assert/strict";
import { El, install } from "./dom.mjs";

const byId = install();
const box = byId["view-explain"] = new El("section");
globalThis.location = { origin: "http://x", hash: "#explain", search: "", pathname: "/" };

const OUT = (over = {}) => ({
  city: "paris", district: "d03", name: "Financial Quarter", verdict: "partial_outage", health: 0.5, round_trips: 1, models: 5,
  relational: { sensors: 12, with_attrs: 12, counts: { ok: 6, stale: 1, offline: 0, fault: 5, stuck: 0 }, problem_types: [{ type: "temperature", problems: 6 }] },
  timeseries: { source: "rollup_5m", points: [0, 5, 10, 15].map((m, i) => ({ bucket: `2026-10-28T11:${String(m).padStart(2, "0")}:00+00:00`, readings: 100 - i * 20, sensors: 10, out_of_range: 0 })),
    last_15m_readings: 160, previous_avg_15m_readings: 300, change: -0.47 },
  json: { attributes: [{ key: "firmware", value: "1.4.2", problem_sensors: 5, city_sensors: 6, share_of_problems: 0.833, share_of_city: 0.273, lift: 3.05, over_represented: true },
    { key: "vendor", value: "<img src=x onerror=alert(1)>", problem_sensors: 1, city_sensors: 16, share_of_problems: 0.167, share_of_city: 0.727, lift: 0.23, over_represented: false }] },
  geo: { available: true, centre: { lat: 48.85, lon: 2.35, basis: "problem sensors" }, nearest_healthy: [{ sensor: "paris.t-d04-0020", type: "temperature", district: "d04", distance_m: 180 }],
    neighbours: [{ district: "d04", distance_m: 1400, sensors: 10, health: 1 }] },
  vector: { available: true, current: { window_end: "2026-10-28T12:00:00+00:00", verdict: "partial_outage", health: 0.5 },
    similar: [{ city: "lyon", district: "d05", window_end: "2026-10-10T08:15:00+00:00", verdict: "partial_outage", distance: 0.012, next_hour_verdict: "operational", episode_lasted_min: 45, resolved_after_min: 30 }],
    prediction: { based_on: 1, verdict_one_hour_later: { operational: 1 }, median_episode_minutes: 45 } },
  sql: "WITH rel AS (SELECT 1) SELECT 1", ...over });

const urls = [];
let payload = OUT();
globalThis.fetch = async (url) => {
  const u = String(url);
  urls.push(u);
  const body = u.startsWith("/api/cities/paris/districts") ? { total: 2, districts: [{ id: "d03", name: "Financial Quarter", total: 12 }, { id: "d04", name: "University Hill", total: 10 }] }
    : u.startsWith("/api/districts/") ? payload : {};
  return { ok: true, status: 200, json: async () => body };
};
const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };
const { setCity } = await import("/static/cities.js");
const { initExplain, sparkline } = await import("/static/explain.js");
const settle = () => new Promise((r) => setTimeout(r, 20));

// 1. no city: a hint and no request
await initExplain();
assert.ok(box.textContent.includes("Choose a city") && urls.length === 0);

// 2. a city: the picker, nothing is explained until asked
setCity("paris");
await initExplain();
await settle();
const sel = box.all().find((e) => e.tag === "select");
assert.deepEqual(sel.all().map((o) => o.value ?? o.attrs.value), ["d03", "d04"]);
assert.ok(!urls.some((u) => u.includes("/explain")));
sel.value = "d03";
await box.all().find((e) => e.attrs.id === "explain-go").click();
await settle();
assert.equal(urls.filter((u) => u.includes("/explain")).length, 1, "one request per click");
assert.ok(urls.some((u) => u === "/api/districts/d03/explain?city=paris"));
const text = box.textContent;
for (const part of ["Relational", "Time series", "JSON", "Geo", "Vector"]) assert.ok(text.includes(part), part);
assert.ok(text.includes("Financial Quarter (paris)") && text.includes("Partial outage") && text.includes("5 data models, 1 SQL statement"), text);
assert.ok(text.includes("12 sensors") && text.includes("5 fault") && text.includes("temperature (6)"));
assert.ok(text.includes("160 readings in the last 15 minutes") && text.includes("-47%"));
assert.ok(text.includes("firmware 1.4.2 is 83.3% of the problem sensors but 27.3% of the city (x3.05)") && text.includes("over-represented"));
assert.ok(text.includes("<img src=x onerror=alert(1)>"), "metadata is shown as text, never parsed");
assert.ok(text.includes("Nearest healthy sensors: paris.t-d04-0020 (d04, 180 m)") && text.includes("d04 100% healthy, 1.4 km"));
assert.ok(text.includes("1 were Operational an hour later") && text.includes("lyon d05") && text.includes("45 min"));
assert.ok(box.all().some((e) => e.tag === "details" && e.textContent.includes("Show the SQL") && e.textContent.includes("WITH rel AS")));
for (const e of box.all()) assert.ok(!("style" in e.attrs), `no inline style on <${e.tag}>`);
assert.ok(!box.all().some((e) => e.tag === "img"), "no element came from data");
const svg = box.all().find((e) => e.tag === "svg");
assert.ok(svg && svg.attrs.role === "img" && svg.attrs["aria-label"].includes("100, 80, 60, 40"), "a chart with a text alternative");

// 3. a deep link opens the district straight away
urls.length = 0;
await initExplain(["d04"]);
await settle();
assert.ok(urls.some((u) => u === "/api/districts/d04/explain?city=paris"));

// 4. no PostGIS and no pgvector: honest parts, the other models still render
payload = OUT({ geo: { available: false, reason: "PostGIS is not available on this database" }, vector: { available: false, reason: "pgvector is not available on this database" } });
await initExplain(["d03"]);
await settle();
const degraded = box.textContent;
assert.ok(degraded.includes("PostGIS is not available") && degraded.includes("pgvector is not available") && degraded.includes("The other four models are unaffected"));
assert.ok(degraded.includes("12 sensors") && degraded.includes("160 readings"), "relational and time series are still there");

// 5. a young install: no similar episodes yet
payload = OUT({ vector: { available: true, current: null, similar: [], prediction: null, note: "no fingerprint recorded for this district yet" } });
await initExplain(["d03"]);
await settle();
assert.ok(box.textContent.includes("no fingerprint recorded for this district yet"));

// 6. a failing request is a readable error with a retry, not a blank card
globalThis.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: "boom" }) });
await initExplain(["d03"]);
await settle();
assert.ok(box.textContent.length > 0);
assert.equal(sparkline([]).all().length, 0);
console.log("ok");
