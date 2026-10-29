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
  feature("d03", "Financial Quarter", null, 0, sq(2.34, 48.84)), feature("d04", "University Hill", 0.8, 5, sq(2.30, 48.85), { reported_elsewhere: 2 }),
  feature("x9", "X9", 1, 3, sq(2.36, 48.85), { source: "derived" })] };
const point = (id, status, lon, lat) => ({ type: "Feature", id, geometry: { type: "Point", coordinates: [lon, lat] }, properties: { id, type: "temperature", district: "d01", city: "paris", status, reason: null } });
const SENSORS = { available: true, type: "FeatureCollection", city: "paris", state: "problem", matching: 30, features: [point("paris.a", "offline", 2.305, 48.845), point("paris.b", "fault", 2.325, 48.842)] };

let off = false, ctxMode = "ok";
const urls = [];
const CONTEXT = { type: "FeatureCollection", city: "brussels", attribution: "© OpenStreetMap contributors (ODbL)", features: [
  { type: "Feature", properties: { kind: "water" }, geometry: { type: "Polygon", coordinates: [[[4.31, 50.84], [4.32, 50.84], [4.32, 50.85], [4.31, 50.85], [4.31, 50.84]]] } },
  { type: "Feature", properties: { kind: "park" }, geometry: { type: "Polygon", coordinates: [[[4.34, 50.84], [4.35, 50.84], [4.35, 50.85], [4.34, 50.85], [4.34, 50.84]]] } },
  { type: "Feature", properties: { kind: "waterway" }, geometry: { type: "LineString", coordinates: [[4.30, 50.83], [4.33, 50.86]] } },
  { type: "Feature", properties: { kind: "road_main" }, geometry: { type: "MultiLineString", coordinates: [[[4.30, 50.84], [4.36, 50.84]], [[4.30, 50.85], [4.36, 50.85]]] } },
  { type: "Feature", properties: { kind: "road_major" }, geometry: { type: "LineString", coordinates: [[4.33, 50.83], [4.33, 50.86]] } },
  { type: "Feature", properties: { kind: "castle" }, geometry: { type: "LineString", coordinates: [[0, 0], [1, 1]] } }] };
let boundary = false;
globalThis.fetch = async (url) => {
  const u = String(url);
  urls.push(u);
  if (u.startsWith("/api/geo/context")) {
    if (ctxMode === "fail") return { ok: false, status: 500, json: async () => ({ detail: "boom" }) };
    return { ok: true, status: 200, json: async () => (boundary ? CONTEXT : {}) };
  }
  const body = off ? { available: false, type: "FeatureCollection", features: [], reason: "PostGIS is not available on this database" }
    : u.startsWith("/api/districts/geojson") ? DISTRICTS : u.startsWith("/api/sensors/geojson") ? SENSORS : {};
  return { ok: true, status: 200, json: async () => body };
};

const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };
const { setCity } = await import("/static/cities.js");
const { initMap, stopMap, healthClass, makeProjection, drawMap, districtLink, resetContexts, widestPoint, placeLabels, contextLayer, UNDER, OVER } = await import("/static/map.js");
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
assert.equal(groups.length, 5, "one group per district");
assert.deepEqual(groups.map((g) => cls(g).split(" ")[1]), ["h0", "h4", "none", "h2", "h4"], "colour classes follow the health");
assert.deepEqual(groups.map((g) => /(^| )derived( |$)/.test(cls(g))), [false, false, false, false, true], "only the derived area is marked");
assert.ok(groups[4].attrs["aria-label"].includes("outline derived from sensor positions") && !groups[0].attrs["aria-label"].includes("derived"));
assert.ok(box.textContent.includes("outline derived from sensor positions"), "table and legend say so");
assert.ok(svg.all().some((e) => e.tag === "path") && svg.all().some((e) => e.tag === "circle"), "a chosen city draws polygons and points");
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
assert.equal(rowButtons.length, 5);
await rowButtons.find((b) => b.attrs["aria-label"].includes("Harbor")).click();
assert.equal(location.hash, "#sensors/d01/-");
location.hash = "#map";
// the list is sorted worst first: Harbor (40%) before University Hill (80%) before Old Town (100%), no-sensor districts last
const firstCells = all.filter((e) => e.tag === "td" && cls(e) === "title").map((e) => e.textContent);
assert.deepEqual(firstCells, ["Harbor", "University Hill", "Old Town", "X9 · outline derived from sensor positions", "Financial Quarter"]);

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

// 4. real boundaries and a base map (Brussels): outlines marked as real, base layers under and over the districts, names on top, attribution
const bsq = (x, y, w = 0.02, h = 0.012) => ({ type: "Polygon", coordinates: [[[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]] });
const bfeat = (district, name, health, total, geometry) => ({ type: "Feature", id: `brussels/${district}`, geometry, properties: { city: "brussels", district, name, source: "boundary",
  total, ok: Math.round((health ?? 0) * total), health, worst_status: "ok", counts: {}, reported_elsewhere: 0 } });
off = false;
SENSORS.features = [point("brussels.a", "offline", 4.37, 50.825), point("brussels.b", "fault", 4.31, 50.845)];
DISTRICTS.city = "brussels";
DISTRICTS.attribution = "© OpenStreetMap contributors (ODbL)";
DISTRICTS.features = [bfeat("ixelles", "Ixelles", 0.9, 20, bsq(4.36, 50.82)), bfeat("molenbeek-saint-jean", "Molenbeek-Saint-Jean", 1, 12, bsq(4.30, 50.84)),
  bfeat("koekelberg", "Koekelberg", 0.5, 4, bsq(4.33, 50.86, 0.004, 0.003)), bfeat("saint-gilles", "Saint-Gilles", null, 0, bsq(4.33, 50.83))];
boundary = true;
const ctxBefore = urls.filter((u) => u.startsWith("/api/geo/context")).length;
setCity("brussels");
await initMap();
await settle();
const b = box.all();
const bsvg = b.find((e) => e.tag === "svg");
for (const e of b) assert.ok(!("style" in e.attrs), `no inline style attribute on <${e.tag}> (real boundaries)`);
assert.ok(urls.filter((u) => u.startsWith("/api/geo/context?city=brussels")).length === 1 && urls.filter((u) => u.startsWith("/api/geo/context")).length === ctxBefore + 1, "one context request for the city");
const kids = bsvg.children.map((c) => cls(c).split(" ").slice(0, 2).join(" "));
assert.deepEqual(kids, ["base-map base-under", "districts", "base-map base-over", "sensors", "area-names"], "park tint, districts, water and roads, dots, names last");
const classes = bsvg.all().filter((e) => e.tag === "path" && /(^| )base( |$)/.test(cls(e))).map((e) => cls(e).split(" ")[1]);
assert.deepEqual(classes, ["base-park", "base-water", "base-waterway", "base-road_main", "base-road_major"], "one path per kind, unknown kinds ignored");
assert.ok(bsvg.children.filter((c) => /base-map/.test(cls(c))).every((c) => c.attrs["aria-hidden"] === "true"), "the base map is decoration");
assert.ok(bsvg.all().filter((e) => e.tag === "g" && /district /.test(cls(e) + " ")).every((g) => /(^| )boundary( |$)/.test(cls(g)) && !/derived/.test(cls(g))), "real boundaries are marked");
const btext = box.textContent;
assert.ok(btext.includes("solid outline: real district boundary") && !btext.includes("outline derived from sensor positions") && !btext.includes("simulated outline"), "legend names the sources that are on the map");
assert.ok(btext.includes("Map data: © OpenStreetMap contributors (ODbL)"), "attribution under the map");
assert.ok(btext.includes("water") && btext.includes("main roads"), "legend explains the base map");
const named = bsvg.all().filter((e) => e.tag === "text" && cls(e) === "area-name");
assert.ok(named.length >= 3, "names are drawn");
assert.ok(named.every((t) => t.attrs.x != null && t.attrs.y != null && !("style" in t.attrs)));
// a second render does not ask again
for (const fn of [...timers.values()]) await fn();
await settle();
assert.equal(urls.filter((u) => u.startsWith("/api/geo/context")).length, ctxBefore + 1, "context is fetched once per city");
stopMap();

// the base map failing to load is not an error: the map is drawn, the attribution of the districts is shown
ctxMode = "fail";
resetContexts();
await initMap();
await settle();
assert.ok(box.all().some((e) => e.tag === "svg") && !box.all().some((e) => e.tag === "path" && /base-/.test(cls(e))), "no base map, still a map");
assert.ok(box.textContent.includes("Map data: © OpenStreetMap contributors (ODbL)"), "attribution still comes with the districts");
stopMap();
ctxMode = "ok";

// 5. names: widest point, no overlaps, away from sensors, tiny districts left out
const L = [[0, 0], [100, 0], [100, 20], [20, 20], [20, 100], [0, 100], [0, 0]];                // an L: the centroid (about 38, 38) is outside it
const [lx, ly] = widestPoint(L);
const inL = (x, y) => (x >= 0 && x <= 100 && y >= 0 && y <= 20) || (x >= 0 && x <= 20 && y >= 0 && y <= 100);
assert.ok(inL(lx, ly), `the widest point of an L is inside it (${lx}, ${ly})`);
const [sx, sy] = widestPoint([[0, 0], [60, 0], [60, 60], [0, 60], [0, 0]]);
assert.ok(Math.abs(sx - 30) < 8 && Math.abs(sy - 30) < 8, "and at the middle of a square");
const rect = (x, y, w, hgt) => [[x, y], [x + w, y], [x + w, y + hgt], [x, y + hgt], [x, y]];
const cands = [
  { key: "big", text: "Molenbeek-Saint-Jean", ring: rect(10, 10, 300, 120), area: 36000 },
  { key: "mid", text: "Ixelles", ring: rect(320, 10, 120, 80), area: 9600 },
  { key: "tiny", text: "Saint-Josse-ten-Noode", ring: rect(445, 10, 14, 10), area: 140 },
  { key: "next", text: "Koekelberg", ring: rect(10, 140, 100, 40), area: 4000 }];
const bounds = { width: 800, height: 400 };
const placed = placeLabels(cands, [], bounds);
const keys = placed.map((p) => p.key);
assert.ok(keys.includes("big") && keys.includes("mid") && keys.includes("next"), keys.join());
for (let i = 0; i < placed.length; i++) {
  const a = placed[i].box;
  assert.ok(a.x0 >= 0 && a.y0 >= 0 && a.x1 <= bounds.width && a.y1 <= bounds.height, "inside the map");
  for (let j = i + 1; j < placed.length; j++) {
    const c = placed[j].box;
    assert.ok(!(a.x0 < c.x1 && a.x1 > c.x0 && a.y0 < c.y1 && a.y1 > c.y0), `${placed[i].key} and ${placed[j].key} do not overlap`);
  }
}
assert.ok(placed.every((p) => p.lines.length >= 1 && p.lines.every((l) => l.text && Number.isFinite(l.x) && Number.isFinite(l.y))));
assert.deepEqual(placeLabels(cands, [], bounds).map((p) => p.key), keys, "deterministic");
// dots under the default spot push the name aside
const [bx, by] = widestPoint(cands[0].ring);
const cluster = [];
for (let i = -30; i <= 30; i += 6) for (let j = -4; j <= 4; j += 4) cluster.push([bx + i, by + j]);
const moved = placeLabels([cands[0]], cluster, bounds)[0];
const hiddenBy = (p, dots) => dots.filter(([x, y]) => x >= p.box.x0 && x <= p.box.x1 && y >= p.box.y0 && y <= p.box.y1).length;
assert.ok(hiddenBy(moved, cluster) < hiddenBy(placeLabels([cands[0]], [], bounds)[0], cluster), "the name moves off the sensors when it can");
// a long name that does not fit in one line is split at a hyphen
const narrow = placeLabels([{ key: "n", text: "Berchem-Sainte-Agathe", ring: rect(0, 0, 90, 50), area: 4500 }], [], { width: 200, height: 100 });
assert.equal(narrow.length, 1);
assert.equal(narrow[0].lines.length, 2, "two lines");
assert.equal(placeLabels([{ key: "z", text: "Nowhere", ring: rect(0, 0, 5, 5), area: 25 }], [], { width: 4, height: 4 }).length, 0, "no room: no name");
// base layers: unknown kinds and empty input
assert.equal(contextLayer({ features: [] }, makeProjection([[[0, 0], [1, 1]]], []), OVER), null);
assert.equal(contextLayer(null, null, UNDER), null);

// 6. the polygons without sensors and no sensors at all: still drawn; a city with nothing says so
const empty = drawMap({ features: [] }, { features: [] });
assert.ok(empty.textContent.includes("Nothing to draw"));
console.log("ok");
