// The Map tab: the district polygons (coloured by the health of the sensors inside them) and the sensors, drawn as inline SVG from
// GeoJSON served by the app (PostGIS: ST_AsGeoJSON, ST_Contains). No tiles, no external requests, no inline style attributes: every
// node is built with the DOM API and colours come from CSS classes. A district is a keyboard-focusable control (Enter or Space opens
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
const isDerived = (p) => p.source === "derived";

function districtLabel(p) {
  const tail = isDerived(p) ? ` (${DERIVED_NOTE})` : "";
  return (p.total ? `${p.name}: ${pct(p.health)} healthy, ${p.total.toLocaleString()} located sensors. Press Enter to list its sensors.` : `${p.name}: no located sensors. Press Enter to list its sensors.`) + tail;
}

export function drawMap(districts, sensors) {
  const polys = districts.features.map((f) => ({ f, rings: outerRings(f.geometry) }));
  const pts = sensors.features.filter((f) => f.geometry && f.geometry.type === "Point");
  const project = makeProjection(polys.flatMap((p) => p.rings), pts.map((f) => f.geometry.coordinates));
  if (!project) return h("p", { class: "empty" }, "Nothing to draw: this city has no district polygons and no sensor reported a position.");
  const svg = svgEl("svg", { viewBox: `0 0 ${project.width} ${project.height}`, class: "map-svg", role: "group", "aria-label": "Map of the districts, coloured by health. Each district can be focused and opened." });
  const layer = svgEl("g", { class: "districts" });
  for (const { f, rings } of polys) {
    const p = f.properties, label = districtLabel(p);
    const g = svgEl("g", { class: `district ${healthClass(p.health)}${isDerived(p) ? " derived" : ""}`, tabindex: "0", role: "link", "aria-label": label, "data-district": p.district,
      onclick: () => open(p.district), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(p.district); } } },
    svgEl("title", {}, label), ...rings.map((r) => svgEl("path", { class: "area", d: ringPath(r, project) })));
    const [cx, cy] = project.point(...centroid(rings[0]));
    g.append(svgEl("text", { class: "area-name", x: cx, y: cy, "text-anchor": "middle", "aria-hidden": "true" }, p.name));
    layer.append(g);
  }
  const dots = svgEl("g", { class: "sensors", "aria-hidden": "true" });
  for (const f of pts) {
    const [x, y] = project.point(...f.geometry.coordinates);
    dots.append(svgEl("circle", { class: `pt st-${f.properties.status}`, cx: x, cy: y, r: 3.2 }, svgEl("title", {}, `${f.properties.id}: ${f.properties.status}`)));
  }
  svg.append(layer, dots);
  return svg;
}

function centroid(ring) {
  const n = Math.max(ring.length - 1, 1);                       // the ring repeats its first point
  let lon = 0, lat = 0;
  for (let i = 0; i < n; i++) { lon += ring[i][0]; lat += ring[i][1]; }
  return [lon / n, lat / n];
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

function legend() {
  return h("div", { class: "legend" }, "District health:", ...["h0", "h1", "h2", "h3", "h4"].map((c, i) =>
    h("span", {}, h("i", { class: `heat-key ${c}` }), ["<50%", "50–75%", "75–90%", "90–98%", "≥98%"][i])),
  h("span", {}, h("i", { class: "heat-key none" }), "no located sensor"),
  h("span", {}, h("i", { class: "heat-key derived" }), `dashed: ${DERIVED_NOTE}`));
}

async function render(view) {
  if (view.busy) return;
  view.busy = true;
  const scoped = currentCity();
  const mode = view.state.value === "all" ? "all" : "problem";
  try {
    let districts, sensors;
    try {
      [districts, sensors] = await Promise.all([api(withCity("/api/districts/geojson")), api(withCity(`/api/sensors/geojson?state=${mode}&limit=${mode === "all" ? 1500 : 500}`))]);
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
      fill(view.body, h("div", { class: "card" }, h("p", { class: "empty" }, scoped ? `City ${scoped} has no built-in district polygons and no sensor has reported a position yet (districts of other cities are outlined from the sensors' positions). Send lat and lon with the readings (see Connect).` : "No city has a position yet.")));
      return;
    }
    const note = districts.sensors_without_location ? `${districts.sensors_without_location.toLocaleString()} sensors have no position and are not drawn. ` : "";
    const shown = sensors.matching > sensors.features.length ? `Showing ${sensors.features.length.toLocaleString()} of ${sensors.matching.toLocaleString()} matching sensors. ` : "";
    fill(view.body, h("div", { class: "card map-card" }, drawMap(districts, sensors), legend(), h("p", { class: "muted" }, `${shown}${note}Click or focus a district and press Enter to list its sensors.`)),
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
