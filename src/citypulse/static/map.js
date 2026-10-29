// The Map tab: the district polygons (coloured by the health of the sensors inside them) and the sensors, drawn as inline SVG from
// GeoJSON served by the app (PostGIS: ST_AsGeoJSON, ST_Contains). A city with real boundaries (Brussels, Vienna) also gets a light base
// map (water, parks, main roads, GET /api/geo/context, served from files bundled with the app) under the districts, and the attribution
// its data requires. District names sit on top of the sensors, on a halo, at the widest part of each polygon, and a name that would
// collide with another is left out (the table below has every district). No tiles, no external requests, no inline style attributes:
// every node is built with the DOM API and colours come from CSS classes. A district is a keyboard-focusable control (Enter or Space opens
// its sensors, like a heatmap cell); the table below the map is the text alternative and offers the same actions.
import { api } from "/kit/api.js";
import { currentCity, withCity } from "/static/cities.js";
import { loadError } from "/static/fmt.js";
import { $, errorState, fill, h, loading, scrollRegion } from "/kit/ui.js";

const NS = "http://www.w3.org/2000/svg";
const W = 800;                                        // viewBox width; the height follows the aspect of the data
const PAD = 14;
let timer = null;

const pct = (x) => `${Math.round(x * 1000) / 10}%`;
// Same buckets and colours as the heatmap on the Overview.
export const healthClass = (health) => (health == null ? "none" : health < 0.5 ? "h0" : health < 0.75 ? "h1" : health < 0.9 ? "h2" : health < 0.98 ? "h3" : "h4");

function svgEl(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, String(v));
  }
  for (const kid of kids) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}

// Equirectangular projection around the middle of the data: x grows east (scaled by cos(latitude), so a kilometre is as long
// either way), y grows south. Returns {width, height, point(lon, lat) -> [x, y]} or null when there is nothing to draw.
export function makeProjection(polygons, points) {
  const lons = [], lats = [];
  for (const ring of polygons) for (const [lon, lat] of ring) { lons.push(lon); lats.push(lat); }
  for (const [lon, lat] of points) { lons.push(lon); lats.push(lat); }
  if (!lons.length) return null;
  const lon0 = Math.min(...lons), lon1 = Math.max(...lons), lat0 = Math.min(...lats), lat1 = Math.max(...lats);
  const k = Math.cos(((lat0 + lat1) / 2) * Math.PI / 180);
  const spanX = Math.max((lon1 - lon0) * k, 1e-6), spanY = Math.max(lat1 - lat0, 1e-6);
  const scale = (W - 2 * PAD) / spanX;
  const height = Math.round(Math.min(Math.max(spanY * scale + 2 * PAD, 200), 1600));
  const fit = Math.min(scale, (height - 2 * PAD) / spanY);
  const offX = (W - spanX * fit) / 2, offY = (height - spanY * fit) / 2;
  return { width: W, height, point: (lon, lat) => [Math.round((offX + (lon - lon0) * k * fit) * 10) / 10, Math.round((offY + (lat1 - lat) * fit) * 10) / 10] };
}

const ringPath = (ring, project) => ring.map(([lon, lat], i) => `${i ? "L" : "M"}${project.point(lon, lat).join(" ")}`).join(" ") + " Z";
const outerRings = (geometry) => (geometry.type === "Polygon" ? [geometry.coordinates[0]] : geometry.type === "MultiPolygon" ? geometry.coordinates.map((p) => p[0]) : []);

// The URL a district opens: the Sensors tab filtered to it, as a click on a heatmap cell does (the tab shows its sensors in every state).
export const districtLink = (district) => `#sensors/${encodeURIComponent(district)}/-`;
const open = (district) => { location.hash = districtLink(district); };

export const DERIVED_NOTE = "outline derived from sensor positions";
export const BOUNDARY_NOTE = "real district boundary";
export const BUILTIN_NOTE = "simulated outline (no real boundary)";
const isDerived = (p) => p.source === "derived";
const isBoundary = (p) => p.source === "boundary";

function districtLabel(p) {
  const tail = isDerived(p) ? ` (${DERIVED_NOTE})` : "";
  return (p.total ? `${p.name}: ${pct(p.health)} healthy, ${p.total.toLocaleString()} located sensors. Press Enter to list its sensors.` : `${p.name}: no located sensors. Press Enter to list its sensors.`) + tail;
}

// ---- district names: where they go --------------------------------------------------------------------------------------------
const CHAR_W = 6.4, LINE_H = 12.5;                    // of the 11px bold label font, roughly
const OFFSETS = [[0, 0], [0, -9], [0, 9], [-14, 0], [14, 0], [-14, -9], [14, -9], [-14, 9], [14, 9], [0, -18], [0, 18]];

const ringArea = (ring) => { let a = 0; for (let i = 0; i < ring.length - 1; i++) a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]; return Math.abs(a) / 2; };
function inRing(ring, x, y) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
function edgeDistance(ring, x, y) {
  let best = Infinity;
  for (let i = 0; i < ring.length - 1; i++) {
    const [ax, ay] = ring[i], [bx, by] = ring[i + 1], dx = bx - ax, dy = by - ay, len = dx * dx + dy * dy;
    const t = len ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / len)) : 0;
    best = Math.min(best, Math.hypot(x - (ax + t * dx), y - (ay + t * dy)));
  }
  return best;
}

// The point of a ring (pixels) farthest from its edges, found on a grid that is refined once: a good place for a name, also for
// a crescent or an L shape where the centroid would lie outside.
export function widestPoint(ring) {
  const xs = ring.map((p) => p[0]), ys = ring.map((p) => p[1]);
  let x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const mx = (x0 + x1) / 2, my = (y0 + y1) / 2;
  let best = [mx, my], bestD = -Infinity;
  for (const n of [18, 8]) {
    const found = best;
    for (let i = 0; i <= n; i++) for (let j = 0; j <= n; j++) {
      const x = x0 + ((x1 - x0) * i) / n, y = y0 + ((y1 - y0) * j) / n;
      if (!inRing(ring, x, y)) continue;
      const d = edgeDistance(ring, x, y) - 0.01 * Math.hypot(x - mx, y - my);       // on a tie (a long strip) prefer the middle
      if (d > bestD) { bestD = d; best = [x, y]; }
    }
    if (best === found) break;
    const w = (x1 - x0) / n * 2, hgt = (y1 - y0) / n * 2;
    x0 = best[0] - w; x1 = best[0] + w; y0 = best[1] - hgt; y1 = best[1] + hgt;
  }
  return best;
}

function variants(text) {
  const out = [[text]];
  const cuts = [...text].map((c, i) => (c === "-" || c === " " ? i : -1)).filter((i) => i > 0 && i < text.length - 1);
  if (cuts.length) {
    const mid = text.length / 2, cut = cuts.reduce((a, b) => (Math.abs(b - mid) < Math.abs(a - mid) ? b : a));
    out.push([text.slice(0, cut + (text[cut] === "-" ? 1 : 0)).trim(), text.slice(cut + 1).trim()]);
  }
  return out;
}
const overlaps = (a, b) => a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;

// cands: [{key, text, ring (pixels), area}]; dots: [[x, y]]; bounds {width, height}. Largest districts choose first. A name is tried on
// one line, then on two, at the widest point of its polygon and a few steps around it, and takes the spot where it hides the fewest
// sensors; it must lie mostly inside its district (first pass) or, failing that, at least touch it (second pass), never overlap a
// name already placed, and stay inside the map. A name with no room is left out. Returns [{key, lines: [{text, x, y}], box}].
export function placeLabels(cands, dots, bounds) {
  const placed = [], taken = [];
  const order = [...cands].sort((a, b) => b.area - a.area || (a.key < b.key ? -1 : 1));
  const done = new Set();
  for (const need of [6, 2]) {
    for (const c of order) {
      if (done.has(c.key)) continue;
      const [px, py] = widestPoint(c.ring);
      for (const lines of variants(c.text)) {
        const w = Math.max(...lines.map((l) => l.length)) * CHAR_W + 4, hgt = lines.length * LINE_H + 2;
        let best = null;
        for (const [dx, dy] of OFFSETS) {
          const box = { x0: px + dx - w / 2, y0: py + dy - hgt / 2, x1: px + dx + w / 2, y1: py + dy + hgt / 2 };
          if (box.x0 < 0 || box.y0 < 0 || box.x1 > bounds.width || box.y1 > bounds.height || taken.some((t) => overlaps(t, box))) continue;
          const probes = [[0.1, 0.1], [0.5, 0.1], [0.9, 0.1], [0.1, 0.5], [0.9, 0.5], [0.1, 0.9], [0.5, 0.9], [0.9, 0.9]];
          const inside = probes.filter(([u, v]) => inRing(c.ring, box.x0 + u * w, box.y0 + v * hgt)).length;
          if (inside < need) continue;
          const hidden = dots.filter(([x, y]) => x >= box.x0 - 2 && x <= box.x1 + 2 && y >= box.y0 - 2 && y <= box.y1 + 2).length;
          const score = hidden * 1000 + Math.abs(dx) + Math.abs(dy);
          if (!best || score < best.score) best = { box, score };
        }
        if (best) {
          const { box } = best;
          taken.push(box);
          done.add(c.key);
          placed.push({ key: c.key, box, lines: lines.map((text, i) => ({ text, x: Math.round(((box.x0 + box.x1) / 2) * 10) / 10, y: Math.round((box.y0 + 1 + LINE_H * (i + 1) - 3) * 10) / 10 })) });
          break;
        }
      }
    }
  }
  return placed;
}

// ---- base map (context layers) --------------------------------------------------------------------------------------------------
const pathOf = (coords, project, close) => coords.map(([lon, lat], i) => `${i ? "L" : "M"}${project.point(lon, lat).join(" ")}`).join(" ") + (close ? " Z" : "");

// Parks are drawn under the districts (a tint behind them); water and roads ABOVE their fill (a district's official boundary runs
// through the middle of a river, which must not be painted as land), both light and inert; one path per kind keeps the DOM small.
export const UNDER = ["park"];
export const OVER = ["water", "waterway", "road_main", "road_major"];
export function contextLayer(context, project, kinds) {
  const feats = context && Array.isArray(context.features) ? context.features : [];
  if (!feats.length) return null;
  const parts = { water: [], park: [], waterway: [], road_main: [], road_major: [] };
  for (const f of feats) {
    const kind = f.properties && f.properties.kind, g = f.geometry;
    if (!parts[kind] || !g) continue;
    if (g.type === "Polygon") parts[kind].push(...g.coordinates.map((r) => pathOf(r, project, true)));
    else if (g.type === "MultiPolygon") for (const poly of g.coordinates) parts[kind].push(...poly.map((r) => pathOf(r, project, true)));
    else if (g.type === "LineString") parts[kind].push(pathOf(g.coordinates, project, false));
    else if (g.type === "MultiLineString") parts[kind].push(...g.coordinates.map((l) => pathOf(l, project, false)));
  }
  const layer = svgEl("g", { class: `base-map ${kinds === UNDER ? "base-under" : "base-over"}`, "aria-hidden": "true" });
  for (const kind of kinds) if (parts[kind].length) layer.append(svgEl("path", { class: `base base-${kind}`, d: parts[kind].join(" ") }));
  return layer;
}

export function drawMap(districts, sensors, context = null) {
  const polys = districts.features.map((f) => ({ f, rings: outerRings(f.geometry) }));
  const pts = sensors.features.filter((f) => f.geometry && f.geometry.type === "Point");
  const project = makeProjection(polys.flatMap((p) => p.rings), pts.map((f) => f.geometry.coordinates));
  if (!project) return h("p", { class: "empty" }, "Nothing to draw: this city has no district polygons and no sensor reported a position.");
  const svg = svgEl("svg", { viewBox: `0 0 ${project.width} ${project.height}`, class: "map-svg", role: "group", "aria-label": "Map of the districts, coloured by health. Each district can be focused and opened." });
  const under = contextLayer(context, project, UNDER), over = contextLayer(context, project, OVER);
  const layer = svgEl("g", { class: "districts" });
  const cands = [];
  for (const { f, rings } of polys) {
    const p = f.properties, label = districtLabel(p);
    const g = svgEl("g", { class: `district ${healthClass(p.health)}${isDerived(p) ? " derived" : ""}${isBoundary(p) ? " boundary" : ""}`, tabindex: "0", role: "link", "aria-label": label, "data-district": p.district,
      onclick: () => open(p.district), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(p.district); } } },
    svgEl("title", {}, label), ...rings.map((r) => svgEl("path", { class: "area", d: ringPath(r, project) })));
    layer.append(g);
    if (rings.length) {
      const pxRings = rings.map((r) => r.map(([lon, lat]) => project.point(lon, lat)));
      const main = pxRings.reduce((a, b) => (ringArea(b) > ringArea(a) ? b : a));
      cands.push({ key: p.district, text: p.name, ring: main, area: pxRings.reduce((a, r) => a + ringArea(r), 0) });
    }
  }
  const dots = svgEl("g", { class: "sensors", "aria-hidden": "true" });
  const xy = [];
  for (const f of pts) {
    const [x, y] = project.point(...f.geometry.coordinates);
    xy.push([x, y]);
    dots.append(svgEl("circle", { class: `pt st-${f.properties.status}`, cx: x, cy: y, r: 2.6 }, svgEl("title", {}, `${f.properties.id}: ${f.properties.status}`)));
  }
  const names = svgEl("g", { class: "area-names", "aria-hidden": "true" });
  for (const l of placeLabels(cands, xy, project)) for (const line of l.lines) names.append(svgEl("text", { class: "area-name", x: line.x, y: line.y, "text-anchor": "middle", "data-district": l.key }, line.text));
  svg.append(...(under ? [under] : []), layer, ...(over ? [over] : []), dots, names);   // names last: never hidden under a sensor
  return svg;
}

function districtTable(districts) {
  const rows = [...districts.features].sort((a, b) => (a.properties.health ?? 2) - (b.properties.health ?? 2) || (a.properties.district < b.properties.district ? -1 : 1));
  return scrollRegion(h("div", { class: "card flush" }, h("table", {},
    h("caption", { class: "sr-only" }, "Districts on the map with the health of the sensors inside each"),
    h("thead", {}, h("tr", {}, ...["District", "Located sensors", "Healthy", "Worst", "Reported elsewhere", ""].map((t) => h("th", {}, t)))),
    h("tbody", {}, ...rows.map((f) => {
      const p = f.properties;
      return h("tr", {}, h("td", { class: "title" }, p.name, isDerived(p) ? h("small", { class: "muted derived-note" }, ` · ${DERIVED_NOTE}`) : null), h("td", { class: "num" }, p.total.toLocaleString()), h("td", { class: "num" }, p.total ? pct(p.health) : ""),
        h("td", {}, p.worst_status && p.worst_status !== "ok" ? h("span", { class: `st ${p.worst_status}` }, p.worst_status) : ""),
        h("td", { class: "num" }, p.reported_elsewhere || ""),
        h("td", {}, h("button", { type: "button", class: "secondary small", "aria-label": `List the sensors of ${p.name}`, onclick: () => open(p.district) }, "Sensors")));
    })))), "Districts on the map");
}

function problemTable(sensors) {
  if (!sensors.features.length) return h("p", { class: "muted" }, "No located sensor matches this filter.");
  const rows = sensors.features.slice(0, 25);
  return scrollRegion(h("div", { class: "card flush" }, h("table", {},
    h("caption", { class: "sr-only" }, "Sensors drawn on the map"),
    h("thead", {}, h("tr", {}, ...["Sensor", "District", "State", "Position"].map((t) => h("th", {}, t)))),
    h("tbody", {}, ...rows.map((f) => h("tr", {}, h("td", {}, h("code", {}, f.properties.id)), h("td", {}, f.properties.district),
      h("td", {}, h("span", { class: `st ${f.properties.status}` }, f.properties.status)),
      h("td", { class: "num" }, `${f.geometry.coordinates[1].toFixed(4)}, ${f.geometry.coordinates[0].toFixed(4)}`)))))), "Sensors on the map");
}

// The health scale, then one entry per kind of outline that is on the map: where the polygon came from matters (a real boundary is
// data, a derived or simulated outline is not), and base-map colours when the layers are drawn.
function legend(districts, hasBase) {
  const sources = new Set(districts.features.map((f) => f.properties.source));
  return h("div", { class: "legend" }, "District health:", ...["h0", "h1", "h2", "h3", "h4"].map((c, i) =>
    h("span", {}, h("i", { class: `heat-key ${c}` }), ["<50%", "50–75%", "75–90%", "90–98%", "≥98%"][i])),
  h("span", {}, h("i", { class: "heat-key none" }), "no located sensor"),
  sources.has("boundary") ? h("span", {}, h("i", { class: "heat-key boundary" }), `solid outline: ${BOUNDARY_NOTE}`) : null,
  sources.has("builtin") ? h("span", {}, h("i", { class: "heat-key builtin" }), `thin outline: ${BUILTIN_NOTE}`) : null,
  sources.has("derived") ? h("span", {}, h("i", { class: "heat-key derived" }), `dashed: ${DERIVED_NOTE}`) : null,
  hasBase ? h("span", {}, h("i", { class: "heat-key base-water" }), "water", h("i", { class: "heat-key base-park" }), "parks", h("i", { class: "heat-key base-road" }), "main roads") : null);
}

// Context layers never change while the app runs: one request per city (a failure just means no base map).
const contexts = new Map();
export const resetContexts = () => contexts.clear();
async function contextFor(city) {
  if (!contexts.has(city)) {
    try { contexts.set(city, await api(withCity("/api/geo/context"))); } catch { return null; }
  }
  return contexts.get(city);
}

async function render(view) {
  if (view.busy) return;
  view.busy = true;
  const scoped = currentCity();
  const mode = view.state.value === "all" ? "all" : "problem";
  try {
    let districts, sensors, context;
    try {
      [districts, sensors, context] = await Promise.all([api(withCity("/api/districts/geojson")), api(withCity(`/api/sensors/geojson?state=${mode}&limit=${mode === "all" ? 1500 : 500}`)), contextFor(scoped)]);
    } catch (err) {
      if (!view.loaded) errorState(view.body, loadError(err), () => { loading(view.body); render(view); });
      return;
    }
    if (scoped !== currentCity()) return;                       // the city changed while loading
    view.loaded = true;
    if (districts.available === false) {
      fill(view.body, h("div", { class: "card" }, h("h2", { class: "section-title" }, "Map not available"),
        h("p", {}, "Geographic features need the PostGIS extension, and this database does not offer it right now."),
        h("p", { class: "muted" }, districts.reason || ""), h("p", { class: "muted" }, "Everything else keeps working; readings may still carry lat and lon, which are stored for when PostGIS is installed.")));
      return;
    }
    if (!districts.features.length && !sensors.features.length) {
      fill(view.body, h("div", { class: "card" }, h("p", { class: "empty" }, scoped ? `City ${scoped} has no district polygons yet and no sensor has reported a position (Brussels and Vienna have real boundaries; other cities are outlined from their sensors' positions). Send lat and lon with the readings (see Connect).` : "No city has a position yet.")));
      return;
    }
    const note = districts.sensors_without_location ? `${districts.sensors_without_location.toLocaleString()} sensors have no position and are not drawn. ` : "";
    const shown = sensors.matching > sensors.features.length ? `Showing ${sensors.features.length.toLocaleString()} of ${sensors.matching.toLocaleString()} matching sensors. ` : "";
    const hasBase = !!(context && Array.isArray(context.features) && context.features.length);
    const attribution = (context && context.attribution) || districts.attribution;
    fill(view.body, h("div", { class: "card map-card" }, drawMap(districts, sensors, context), legend(districts, hasBase), h("p", { class: "muted" }, `${shown}${note}Click or focus a district and press Enter to list its sensors.`),
      attribution ? h("p", { class: "muted map-attrib" }, `Map data: ${attribution}`) : null),
      h("h2", { class: "section-title" }, "Districts"), districtTable(districts),
      h("h2", { class: "section-title" }, "Sensors on the map"), problemTable(sensors));
  } finally { view.busy = false; }
}

export async function initMap() {
  const box = $("view-map");
  clearInterval(timer);
  const city = currentCity();
  if (!city) {
    fill(box, h("h1", {}, "Map"), h("div", { class: "card" }, h("p", { class: "empty" }, "Choose a city in the City menu to see its districts and sensors on a map.")));
    return;
  }
  const state = h("select", { id: "map-state", "aria-label": "Sensors to draw" }, h("option", { value: "problem" }, "Sensors with a problem"), h("option", { value: "all" }, "All located sensors"));
  const view = { body: h("div"), state, loaded: false, busy: false };
  state.addEventListener("change", () => render(view));
  loading(view.body);
  fill(box, h("div", { class: "row" }, h("h1", {}, "Map", h("small", { class: "scope-chip" }, ` · ${city}`)), state), view.body);
  await render(view);
  timer = setInterval(() => { if (document.hidden || location.hash.slice(1).split("/")[0] !== "map" || view.body.contains(document.activeElement)) return; render(view); }, 10000);
}

export function stopMap() { clearInterval(timer); }
