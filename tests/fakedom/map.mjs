// The Map tab against a fake DOM: the SVG is built with the DOM API only (no inline style attribute, no HTML assignment), districts are
// coloured by health through classes, every district is keyboard operable and opens the Sensors tab, the table is a text alternative,
// and a database without PostGIS gets an honest "not available" card.
import assert from "node:assert/strict";
import { El, install } from "./dom.mjs";

const byId = install();
const box = byId["view-map"] = new El("section");
const timers = new Map();
let nextTimer = 1;
globalThis.setInterval = (fn) => { timers.set(nextTimer, fn); return nextTimer++; };
globalThis.clearInterval = (id) => { timers.delete(id); };
globalThis.location = { origin: "http://x", hash: "#map", search: "", pathname: "/" };

const sq = (x, y) => ({ type: "Polygon", coordinates: [[[x, y], [x + 0.01, y], [x + 0.01, y + 0.01], [x, y + 0.01], [x, y]]] });
const feature = (district, name, health, total, geometry, extra = {}) => ({ type: "Feature", id: `paris/${district}`, geometry,
  properties: { city: "paris", district, name, source: "builtin", total, ok: Math.round((health ?? 0) * total), health, worst_status: health == null ? null : health < 1 ? "offline" : "ok",
    counts: {}, reported_elsewhere: 0, ...extra } });
const DISTRICTS = { available: true, type: "FeatureCollection", city: "paris", sensors_without_location: 4, features: [
  feature("d01", "Harbor", 0.4, 10, sq(2.30, 48.84)), feature("d02", "Old Town", 1, 10, sq(2.32, 48.84)),
  feature("d03", "Financial Quarter", null, 0, sq(2.34, 48.84)), feature("d04", "University Hill", 0.8, 5, sq(2.30, 48.85), { reported_elsewhere: 2 })] };
const point = (id, status, lon, lat) => ({ type: "Feature", id, geometry: { type: "Point", coordinates: [lon, lat] }, properties: { id, type: "temperature", district: "d01", city: "paris", status, reason: null } });
const SENSORS = { available: true, type: "FeatureCollection", city: "paris", state: "problem", matching: 30, features: [point("paris.a", "offline", 2.305, 48.845), point("paris.b", "fault", 2.325, 48.842)] };

let off = false;
const urls = [];
globalThis.fetch = async (url) => {
  const u = String(url);
  urls.push(u);
  const body = off ? { available: false, type: "FeatureCollection", features: [], reason: "PostGIS is not available on this database" }
    : u.startsWith("/api/districts/geojson") ? DISTRICTS : u.startsWith("/api/sensors/geojson") ? SENSORS : {};
  return { ok: true, status: 200, json: async () => body };
};

const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };
const { setCity } = await import("/static/cities.js");
const { initMap, stopMap, healthClass, makeProjection, drawMap, districtLink } = await import("/static/map.js");
const cls = (e) => e.attrs.class ?? e.className ?? "";
const settle = () => new Promise((r) => setTimeout(r, 20));

// pure helpers
assert.deepEqual([0.1, 0.5, 0.74, 0.8, 0.95, 0.99, 1, null].map(healthClass), ["h0", "h1", "h1", "h2", "h3", "h4", "h4", "none"]);
assert.equal(districtLink("d01"), "#sensors/d01/-");
const proj = makeProjection([[[2.30, 48.84], [2.32, 48.84], [2.32, 48.86], [2.30, 48.86]]], [[2.31, 48.85]]);
const [xw, yn] = proj.point(2.30, 48.86), [xe, ys] = proj.point(2.32, 48.84);
assert.ok(xe > xw && ys > yn, "east is to the right and north is up");
assert.ok(xw >= 0 && xe <= proj.width && yn >= 0 && ys <= proj.height, "everything fits the viewBox");
assert.equal(makeProjection([], []), null);

// 1. no city chosen: a hint, no request
await initMap();
assert.ok(box.textContent.includes("Choose a city"), "asks for a city");
assert.equal(urls.length, 0);

// 2. a city: the map
setCity("paris");
await initMap();
await settle();
assert.ok(urls.some((u) => u.startsWith("/api/districts/geojson?city=paris")) && urls.some((u) => u.includes("/api/sensors/geojson?state=problem&limit=500&city=paris")), urls.join(" "));
const all = box.all();
const svg = all.find((e) => e.tag === "svg");
assert.ok(svg && svg.attrs.viewBox && svg.attrs.role === "group" && svg.attrs["aria-label"], "an inline SVG with a role and a name");
for (const e of all) assert.ok(!("style" in e.attrs), `no inline style attribute on <${e.tag}>`);
const groups = svg.all().filter((e) => e.tag === "g" && /(^| )district /.test(cls(e) + " "));
assert.equal(groups.length, 4, "one group per district");
assert.deepEqual(groups.map((g) => cls(g).split(" ")[1]), ["h0", "h4", "none", "h2"], "colour classes follow the health");
for (const g of groups) {
  assert.equal(g.attrs.tabindex, "0");
  assert.equal(g.attrs.role, "link");
  assert.ok(g.attrs["aria-label"].includes("Press Enter"), "an accessible name that says what happens");
  assert.ok(g.all().some((e) => e.tag === "path" && /^M[\d. -]+L/.test(e.attrs.d)), "a path from the polygon ring");
}
assert.ok(groups[0].attrs["aria-label"].startsWith("Harbor: 40% healthy, 10 located sensors"));
assert.ok(groups[2].attrs["aria-label"].includes("no located sensors"));
assert.equal(svg.all().filter((e) => e.tag === "circle").length, 2, "one dot per sensor");
assert.ok(svg.all().filter((e) => e.tag === "circle").some((c) => cls(c).includes("st-fault")));
// keyboard and pointer: both open the Sensors tab filtered to the district
await groups[1].dispatch("keydown", { key: "Tab" });
assert.equal(location.hash, "#map", "other keys do nothing");
await groups[1].dispatch("keydown", { key: "Enter" });
assert.equal(location.hash, "#sensors/d02/-");
location.hash = "#map";
await groups[3].dispatch("keydown", { key: " " });
assert.equal(location.hash, "#sensors/d04/-");
location.hash = "#map";
await groups[0].dispatch("click");
assert.equal(location.hash, "#sensors/d01/-");
location.hash = "#map";
// the text alternative
const text = box.textContent;
for (const name of ["Harbor", "Old Town", "Financial Quarter", "University Hill"]) assert.ok(text.includes(name), name);
assert.ok(text.includes("Showing 2 of 30 matching sensors") && text.includes("4 sensors have no position"), "honest counts");
const captions = all.filter((e) => e.tag === "caption").map((c) => c.textContent);
assert.ok(captions.some((c) => c.includes("Districts on the map")), "table captions for screen readers");
const rowButtons = all.filter((e) => e.tag === "button" && (e.attrs["aria-label"] || "").startsWith("List the sensors of"));
assert.equal(rowButtons.length, 4);
await rowButtons.find((b) => b.attrs["aria-label"].includes("Harbor")).click();
assert.equal(location.hash, "#sensors/d01/-");
location.hash = "#map";
// the list is sorted worst first: Harbor (40%) before University Hill (80%) before Old Town (100%), no-sensor districts last
const firstCells = all.filter((e) => e.tag === "td" && cls(e) === "title").map((e) => e.textContent);
assert.deepEqual(firstCells, ["Harbor", "University Hill", "Old Town", "Financial Quarter"]);

// the select asks for every located sensor
const sel = box.all().find((e) => e.tag === "select");
sel.value = "all";
await sel.dispatch("change");
await settle();
assert.ok(urls.some((u) => u.includes("state=all&limit=1500")), "all located sensors");

// the refresh tick does not redraw while focus is inside the map (it would drop the focused district)
let draws = urls.length;
document.activeElement = box.all().find((e) => e.tag === "g" && /district /.test(cls(e) + " "));
for (const fn of [...timers.values()]) await fn();
await settle();
assert.equal(urls.length, draws, "no refresh while a district has focus");
document.activeElement = null;
for (const fn of [...timers.values()]) await fn();
await settle();
assert.ok(urls.length > draws, "refreshes otherwise");
stopMap();
assert.equal(timers.size, 0);

// 3. no PostGIS: an honest card, the rest of the page is unaffected
off = true;
await initMap();
await settle();
assert.ok(box.textContent.includes("Map not available") && box.textContent.includes("PostGIS is not available on this database"));
assert.ok(!box.all().some((e) => e.tag === "svg"));
stopMap();

// 4. the polygons without sensors and no sensors at all: still drawn; a city with nothing says so
const empty = drawMap({ features: [] }, { features: [] });
assert.ok(empty.textContent.includes("Nothing to draw"));
console.log("ok");
