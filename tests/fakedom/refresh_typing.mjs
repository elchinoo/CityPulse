// Regression: a page that redraws itself (a timer, a finished request, a "back" button) must never throw away what the user is
// typing. For every page with an input or a select, this types into it, lets the refresh run, and checks that the very same
// element is still in the page with its value, its focus and its open panel. Against the old code the Overview test fails:
// the refresh replaced the whole page, including the delete-city confirmation, so the typed city id and the focus were lost.
import assert from "node:assert/strict";
import { El, install } from "./dom.mjs";

const byId = install();
const views = {};
for (const v of ["overview", "sensors", "burst", "settings", "city-picker"]) views[v] = byId[v === "city-picker" ? v : "view-" + v] = new El("section");

// timers: collected, fired by hand
const timers = new Map();
let nextTimer = 1;
globalThis.setInterval = (fn) => { timers.set(nextTimer, fn); return nextTimer++; };
globalThis.clearInterval = (id) => { timers.delete(id); };
const tick = async () => { for (const fn of [...timers.values()]) await fn(); await new Promise((r) => setTimeout(r, 20)); };
globalThis.location = { origin: "http://x", hash: "#overview", search: "", pathname: "/" };

const STATUS = (city) => ({
  city, legacy: false, state: "degraded", health: 0.8, total: 7, counts: { ok: 5, stale: 0, offline: 0, fault: 2, stuck: 0 }, reasons: [], bursts: [], sim: null,
  types: [{ key: "temperature", label: "Temperature", total: 7, ok: 5, stale: 0, offline: 0, fault: 2, stuck: 0 }], protocols: [],
  districts: [{ key: "alder-quay", label: "Alder Quay", total: 5 }, { key: "north-mill", label: "North Mill", total: 2 }], districts_total: 2,
  matrix: [{ district: "alder-quay", type: "temperature", total: 5, ok: 3 }, { district: "north-mill", type: "temperature", total: 2, ok: 2 }],
});
const calls = [];
let sensorRows = [];
let chaosActive = [];
globalThis.fetch = async (url, init = {}) => {
  calls.push([init.method || "GET", url]);
  const u = String(url);
  let body = {};
  if (u.startsWith("/api/catalog")) body = { districts: [{ id: "d01", name: "Harbor" }], groups: [], types: [{ key: "temperature", label: "Temperature", unit: "C" }] };
  else if (u.startsWith("/api/status")) body = STATUS(new URL(u, "http://x").searchParams.get("city"));
  else if (u.startsWith("/api/history")) body = { health: [], throughput: [] };
  else if (u.startsWith("/api/chaos")) body = { active: chaosActive, kinds: { noise: "Noise." } };
  else if (u === "/api/cities/oslo/districts") body = { city: "oslo", total: 2, limit: 200, districts: [{ id: "alder-quay", name: "Alder Quay", total: 5 }, { id: "north-mill", name: "North Mill", total: 2 }] };
  else if (u.startsWith("/api/cities")) body = { total: 1, limit: 12, offset: 0, legacy_city: null, cities: [{ city: "oslo", total: 7, counts: { ok: 5 }, health: 0.7, worst_status: "fault" }] };
  else if (u.startsWith("/api/sensors/")) body = { id: "oslo.t0", status: "ok", type: "temperature", city: "oslo", district: "alder-quay", protocol: "http", last_value: 1, last_ts: new Date().toISOString(), status_since: new Date().toISOString(), readings: [] };
  else if (u.startsWith("/api/sensors")) body = sensorRows;
  else if (u.startsWith("/api/session")) body = { has_admin_password: false };
  else if (u.startsWith("/api/tokens")) body = init.method === "POST" ? { token: "tok" } : [];
  return { ok: true, status: 200, json: async () => body };
};

const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };
const { setCity } = await import("/static/cities.js");
setCity("oslo");
const attached = (view, el) => view.all().includes(el);
const byAttr = (view, id) => view.find((e) => e.attrs.id === id);
const btn = (view, text) => view.all().find((e) => e.tag === "button" && e.textContent.includes(text));

// ---- Overview: the delete-city confirmation -----------------------------------------------------------------
{
  const { initOverview, stopOverview } = await import("/static/overview.js");
  await initOverview();
  const v = views.overview;
  assert.ok(v.textContent.includes("Alder Quay") && v.textContent.includes("North Mill"), "the matrix lists the districts the sensors report");
  assert.ok(!v.textContent.includes("Harbor"), "no catalogue district is invented");
  await btn(v, "Delete city oslo").click();
  const input = byAttr(v, "city-delete-confirm");
  assert.ok(input, "the confirmation opens");
  input.focus();
  await input.type("os");                                  // half way through typing the city id
  const panel = v.find((e) => e.className.includes("city-delete-panel"));
  assert.equal(timers.size, 1, "the overview refreshes on a timer");
  await tick(); await tick(); await tick();                // three refreshes while the user is typing
  assert.ok(attached(v, input), "the typed-in input must still be in the page after a refresh (it was replaced)");
  assert.equal(input.value, "os", "what was typed must survive the refresh");
  assert.equal(document.activeElement, input, "the focus must stay in the input");
  assert.equal(panel.hidden, false, "the open confirmation must stay open");
  assert.equal(btn(v, "Delete everything").disabled, true);
  await input.type("oslo");                                // finish: the button arms, and survives another refresh
  await tick();
  assert.equal(btn(v, "Delete everything").disabled, false);
  assert.ok(attached(v, input) && input.value === "oslo");
  assert.ok(calls.filter(([m]) => m === "DELETE").length === 0, "nothing is deleted by a refresh");
  stopOverview();
  timers.clear();
}

// ---- Burst: target, level and duration menus beside the live "active" list -----------------------------------
{
  location.hash = "#burst";
  chaosActive = [{ id: 1, kind: "slow_db", target: "all", intensity: 3, seconds_left: 150 }];
  const { initBurst } = await import("/static/burst.js");
  await initBurst();
  await new Promise((r) => setTimeout(r, 20));
  const v = views.burst;
  // Regression: the "Lasts" menu once shadowed the duration formatter, so the Active list silently never drew.
  assert.ok(v.find((e) => e.tag === "tr" && e.textContent.includes("slow db") && e.textContent.includes("3") && e.textContent.includes("3 min")),
    "an active burst is listed with its time left");
  const target = v.find((e) => e.tag === "select" && e.attrs["aria-label"] === "noise target");
  const opts = target.all().filter((e) => e.tag === "option").map((e) => [e.value, e.textContent]);
  assert.ok(opts.some(([val, t]) => val === "district:alder-quay" && t === "Alder Quay"), "districts come from the selected city's sensors");
  assert.ok(opts.some(([val]) => val === "district:north-mill") && !opts.some(([val]) => val === "district:d01"), "not the fixed catalogue");
  target.value = "district:north-mill";
  target.focus();
  await tick(); await tick();
  assert.ok(attached(v, target) && target.value === "district:north-mill" && document.activeElement === target, "a refresh keeps the chosen target");
  calls.length = 0;
  await btn(v, "Start").click();
  const post = calls.find(([m, u]) => m === "POST" && u === "/api/chaos");
  assert.ok(post, "starting a burst posts it");
  chaosActive = [];
  timers.clear();
}

// ---- Sensors: filters survive the trip to a sensor and back ---------------------------------------------------
{
  location.hash = "#sensors";
  sensorRows = [{ id: "oslo.t0", city: "oslo", type: "temperature", district: "alder-quay", protocol: "http", status: "ok", last_value: 1, last_ts: new Date().toISOString(), reason: "" }];
  const { initSensors } = await import("/static/sensors.js");
  await initSensors(["alder-quay", "-"]);
  const v = views.sensors;
  const q = byAttr(v, "f-q"), dist = byAttr(v, "f-district");
  assert.equal(dist.value, "alder-quay", "the district of the link is selected");
  assert.deepEqual(dist.all().filter((e) => e.tag === "option").map((e) => e.textContent), ["Any district", "Alder Quay", "North Mill"]);
  await q.type("t0");
  q.focus();
  await new Promise((r) => setTimeout(r, 300));
  const row = v.all().find((e) => e.tag === "tr" && e.className === "clickable");
  assert.ok(row, "the list shows the sensor");
  await row.dispatch("click");
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(v.textContent.includes("back to the list"));
  await btn(v, "back to the list").click();
  assert.ok(attached(v, q) && q.value === "t0" && dist.value === "alder-quay", "back to the list must not redraw the filters");
  for (let n = q; n; n = n.parent) assert.equal(n.hidden, false, "the filters are visible again");
}

// ---- City picker: a search result arriving must not touch what is typed -----------------------------------------
{
  const { renderCityPicker } = await import("/static/cities.js");
  const box = views["city-picker"];
  renderCityPicker(box);
  await box.find((e) => e.tag === "button").click();
  const search = byAttr(box, "city-q");
  await new Promise((r) => setTimeout(r, 20));
  await search.type("os");
  await new Promise((r) => setTimeout(r, 300));
  assert.ok(attached(box, search) && search.value === "os" && document.activeElement === search);
}

// ---- Settings: creating a token redraws the token list, not the password form ----------------------------------------
{
  const { initSettings } = await import("/static/settings.js");
  await initSettings();
  const v = views.settings;
  const pw = byAttr(v, "pw-new");
  await pw.type("a-long-new-password");
  await byAttr(v, "tk-name").type("edge");
  await v.find((e) => e.tag === "form" && e.listeners.submit).dispatch("submit");
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(attached(v, pw) && pw.value === "a-long-new-password", "the password being typed survives a token being created");
}
console.log("ok");
process.exit(0);
