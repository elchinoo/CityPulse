// The Explain tab: pick a district of the chosen city and get ONE answer assembled by ONE SQL statement from five data models
// (relational, time series, JSON, geo, vector). Everything arrives from /api/districts/{id}/explain; every value is data and reaches the
// page through text nodes only. The statement is shown under "Show the SQL", because that is the point of the card.
import { api } from "/kit/api.js";
import { currentCity } from "/static/cities.js";
import { loadDistricts } from "/static/districts.js";
import { loadError } from "/static/fmt.js";
import { $, errorState, fill, h, loading } from "/kit/ui.js";

const NS = "http://www.w3.org/2000/svg";
const VERDICT = { operational: "Operational", degraded: "Degraded", partial_outage: "Partial outage", major_outage: "Major outage" };
const pct = (x) => (x == null ? "n/a" : `${Math.round(x * 1000) / 10}%`);
const minutes = (m) => (m == null ? "unknown" : m < 90 ? `${Math.round(m)} min` : `${Math.round(m / 6) / 10} h`);
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "");

// Readings per 5-minute bucket as a small inline SVG line: classes only, no style attribute.
export function sparkline(points) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 420 90");
  svg.setAttribute("class", "chart spark");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Readings per 5 minutes over the last hour: ${points.map((p) => p.readings).join(", ")}`);
  if (points.length < 2) return svg;
  const t = points.map((p) => new Date(p.bucket).getTime()), v = points.map((p) => p.readings);
  const x0 = Math.min(...t), x1 = Math.max(...t), hi = Math.max(...v, 1);
  const line = document.createElementNS(NS, "polyline");
  line.setAttribute("points", points.map((p, i) => `${(6 + ((t[i] - x0) / Math.max(x1 - x0, 1)) * 408).toFixed(1)},${(84 - (v[i] / hi) * 78).toFixed(1)}`).join(" "));
  line.setAttribute("class", "series");
  line.setAttribute("fill", "none");
  svg.append(line);
  return svg;
}

const unavailable = (what, part) => h("p", { class: "muted" }, `${part.reason || `${what} is not available on this database`}. The other four models are unaffected.`);
const section = (model, title, ...body) => h("section", { class: "model" }, h("h3", {}, h("span", { class: "tag" }, model), ` ${title}`), ...body);

export function relational(rel) {
  const c = rel.counts;
  return section("Relational", "Right now", h("p", {}, `${rel.sensors.toLocaleString()} sensors: `,
    ...["ok", "stale", "offline", "fault", "stuck"].filter((k) => c[k]).map((k) => [h("span", { class: `st ${k}` }, `${c[k]} ${k}`), " "])),
  rel.problem_types.length ? h("p", { class: "muted" }, "Most affected types: " + rel.problem_types.map((p) => `${p.type} (${p.problems})`).join(", ")) : h("p", { class: "muted" }, "No sensor has a problem."));
}

export function timeseries(ts) {
  const change = ts.change == null ? "" : ` ${ts.change >= 0 ? "+" : ""}${Math.round(ts.change * 100)}% against the 15 minutes before.`;
  return section("Time series", "The last hour", sparkline(ts.points),
    h("p", { class: "muted" }, ts.last_15m_readings == null ? "No readings in the roll-up yet." : `${ts.last_15m_readings.toLocaleString()} readings in the last 15 minutes.${change}`));
}

export function jsonPart(js) {
  const rows = js.attributes.filter((a) => a.problem_sensors > 0);
  if (!rows.length) return section("JSON", "Vendor and firmware", h("p", { class: "muted" }, "No problem sensor reports vendor or firmware metadata, or none has a problem."));
  return section("JSON", "Vendor and firmware among the problem sensors", h("ul", { class: "evidence" }, ...rows.slice(0, 4).map((a) => h("li", {},
    h("b", {}, `${a.key} ${a.value}`), ` is ${pct(a.share_of_problems)} of the problem sensors but ${pct(a.share_of_city)} of the city (x${a.lift})`,
    a.over_represented ? [" ", h("span", { class: "st fault" }, "over-represented")] : null))));
}

export function geoPart(g) {
  if (g.available === false) return section("Geo", "Where", unavailable("PostGIS", g));
  const near = (g.nearest_healthy || []).slice(0, 3).map((n) => `${n.sensor} (${n.district}, ${n.distance_m} m)`).join(", ");
  const nb = (g.neighbours || []).map((n) => `${n.district} ${pct(n.health)} healthy, ${(n.distance_m / 1000).toFixed(1)} km`).join("; ");
  return section("Geo", "Where", g.centre ? h("p", {}, `Centre of the ${g.centre.basis}: ${g.centre.lat}, ${g.centre.lon}.`) : h("p", { class: "muted" }, "No sensor of this district has a position."),
    near ? h("p", { class: "muted" }, `Nearest healthy sensors: ${near}.`) : null, nb ? h("p", { class: "muted" }, `Neighbours: ${nb}.`) : null);
}

export function vectorPart(v) {
  if (v.available === false) return section("Vector", "Have we seen this before?", unavailable("pgvector", v));
  if (!v.similar.length) return section("Vector", "Have we seen this before?", h("p", { class: "muted" }, v.note || "No similar past episode with a known outcome yet. Run citypulse seed-history for demo history, or wait for the district to build its own."));
  const p = v.prediction;
  return section("Vector", "Have we seen this before?",
    p ? h("p", {}, `Of ${p.based_on} similar past episodes: ` + Object.entries(p.verdict_one_hour_later).map(([k, n]) => `${n} were ${VERDICT[k] || k} an hour later`).join(", ")
      + (p.median_episode_minutes == null ? "." : `; the bad stretches lasted ${minutes(p.median_episode_minutes)} (median).`)) : null,
    h("ol", { class: "evidence" }, ...v.similar.map((s) => h("li", {}, h("b", {}, `${s.city} ${s.district}`), ` on ${when(s.window_end)}: ${VERDICT[s.verdict] || s.verdict}, distance ${s.distance}. `,
      `An hour later: ${VERDICT[s.next_hour_verdict] || s.next_hour_verdict}; the episode lasted ${minutes(s.episode_lasted_min)}.`))));
}

export function renderExplain(out) {
  return h("div", { class: "card explain" },
    h("div", { class: "row" }, h("h2", { class: "section-title" }, `${out.name} (${out.city})`), h("span", { class: `st ${out.verdict === "operational" ? "ok" : out.verdict === "degraded" ? "stale" : "fault"}` }, VERDICT[out.verdict] || out.verdict)),
    h("p", { class: "muted" }, `${pct(out.health)} of the sensors are ok. ${out.models} data models, ${out.round_trips} SQL statement.`),
    relational(out.relational), timeseries(out.timeseries), jsonPart(out.json), geoPart(out.geo), vectorPart(out.vector),
    h("details", { class: "sqlview" }, h("summary", {}, "Show the SQL"), h("pre", { tabindex: "0" }, out.sql)));
}

export async function initExplain(arg = []) {
  const box = $("view-explain");
  const city = currentCity();
  if (!city) {
    fill(box, h("h1", {}, "Explain a district"), h("div", { class: "card" }, h("p", { class: "empty" }, "Choose a city in the City menu, then a district to explain.")));
    return;
  }
  let found = { list: [] };
  try { found = await loadDistricts(city); } catch (err) { errorState(box, loadError(err), () => initExplain(arg)); return; }
  const want = arg[0] && arg[0] !== "-" ? arg[0] : "";
  const sel = h("select", { id: "explain-district", "aria-label": "District to explain" }, ...found.list.map((d) => h("option", { value: d.id }, d.name)));
  if (want && found.list.some((d) => d.id === want)) sel.value = want;
  const out = h("div", { role: "status" });
  let seq = 0;
  const run = async () => {
    const mine = ++seq;
    loading(out, "Asking the database once…");
    try {
      const res = await api(`/api/districts/${encodeURIComponent(sel.value)}/explain?city=${encodeURIComponent(city)}`);
      if (mine === seq) fill(out, renderExplain(res));
    } catch (err) { if (mine === seq) errorState(out, loadError(err), run); }
  };
  const go = h("button", { id: "explain-go", type: "button", onclick: run }, "Explain this district");
  fill(box, h("div", { class: "row" }, h("h1", {}, "Explain", h("small", { class: "scope-chip" }, ` · ${city}`))),
    h("p", { class: "muted" }, "One SQL statement reads relational, time-series, JSON, geographic and vector data at once: status counts, the last hour, which vendor or firmware stands out, what is nearby and whether this has happened before."),
    h("div", { class: "row" }, sel, go), out);
  if (!found.list.length) { fill(out, h("p", { class: "empty" }, "No district reports in this city yet.")); return; }
  if (want) await run();
}
