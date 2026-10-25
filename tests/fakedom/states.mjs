// First-paint states and time wording: a content area is never blank (Loading... until the first data, a readable error if the first
// request fails, and it heals when the server comes back), durations are never raw seconds, and the simulator tile exists only when
// the API reports a built-in simulator.
import assert from "node:assert/strict";
import { El, install } from "./dom.mjs";

const byId = install();
const overview = byId["view-overview"] = new El("section");
const timers = new Map();
let nextTimer = 1;
globalThis.setInterval = (fn) => { timers.set(nextTimer, fn); return nextTimer++; };
globalThis.clearInterval = (id) => { timers.delete(id); };
globalThis.location = { origin: "http://x", hash: "#overview", search: "", pathname: "/" };

const { duration, agoSeconds, ago } = await import("/static/fmt.js");
assert.equal(duration(0), "0s");
assert.equal(duration(42), "42s");
assert.equal(duration(240), "4 min");
assert.equal(duration(7200), "2 h");
assert.equal(agoSeconds(264064), "3 days ago");
assert.equal(duration(86400 * 1.2), "29 h");
assert.equal(duration(86400 * 1.6), "2 days");
assert.equal(ago(new Date(Date.now() - 240000).toISOString()), "4 min ago");
for (const s of [0, 89, 90, 5399, 5400, 129599, 129600, 264064]) assert.ok(!/^\d{3,}s/.test(duration(s)), `${s}s is humanized`);

const STATUS = (sim) => ({
  city: null, legacy: false, state: "partial_outage", health: 0.91, total: 100, counts: { ok: 91, stale: 0, offline: 9, fault: 0, stuck: 0 },
  reasons: ["fleet is 91% healthy"], bursts: [], sim, protocols: [],
  types: [{ key: "temperature", label: "Temperature", total: 100, ok: 91, stale: 0, offline: 9, fault: 0, stuck: 0 }],
  districts: [{ key: "a", label: "A", total: 100 }], districts_total: 1, matrix: [{ district: "a", type: "temperature", total: 100, ok: 91 }],
});
let sim = null, failing = false, hold = false;
const waiting = [];
globalThis.fetch = async (url) => {
  const u = String(url);
  if (hold) await new Promise((r) => waiting.push(r));
  if (failing) return { ok: false, status: 503, json: async () => ({ detail: "database unavailable" }) };
  let body = {};
  if (u.startsWith("/api/catalog")) body = { districts: [], groups: [], types: [{ key: "temperature", label: "Temperature", unit: "C" }] };
  else if (u.startsWith("/api/status")) body = STATUS(sim);
  else if (u.startsWith("/api/history")) body = { health: [], throughput: [] };
  else if (u.startsWith("/api/cities")) body = { total: 1, limit: 8, offset: 0, legacy_city: null, cities: [] };
  return { ok: true, status: 200, json: async () => body };
};
const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };
const { initOverview, stopOverview } = await import("/static/overview.js");
const tick = async () => { for (const fn of [...timers.values()]) await fn(); await new Promise((r) => setTimeout(r, 20)); };
const tile = () => overview.textContent.includes("Simulator buffer");

// 1. first request pending: "Loading..." in the content area
hold = true;
const first = initOverview();
await new Promise((r) => setTimeout(r, 20));
assert.ok(overview.textContent.includes("Loading"), "the page says it is loading while the first data is on its way");
hold = false;
waiting.splice(0).forEach((r) => r());       // answer the pending requests
await first;
assert.ok(!overview.textContent.includes("Loading"), "loaded: the loading line is gone");
assert.ok(overview.textContent.includes("Partial outage") && overview.textContent.includes("91%"));
assert.ok(!tile(), "no simulator reported (role api): the tile is hidden entirely");
stopOverview();

// 2. a built-in simulator that stopped: humanized age, not raw seconds
sim = { running: false, age: 264064, buffer: 0, sensors: 0 };
await initOverview();
assert.ok(tile() && overview.textContent.includes("stopped 3 days ago") && !overview.textContent.includes("264064"));
sim = { running: true, age: 2, buffer: 5, sensors: 300 };
await tick();
assert.ok(overview.textContent.includes("running, 300 sensors"));
stopOverview();

// 3. the first request fails: a readable error with a retry, never a blank area; it heals when the server is back
failing = true;
await initOverview();
assert.ok(overview.textContent.includes("Could not load this page") && overview.textContent.includes("database unavailable"), "the error is readable");
assert.ok(overview.all().some((e) => e.tag === "button" && e.textContent === "Try again"), "and can be retried");
failing = false;
await tick();
assert.ok(overview.textContent.includes("Partial outage") && !overview.textContent.includes("Could not load"), "the next refresh replaces the error");
stopOverview();
console.log("ok");
process.exit(0);
