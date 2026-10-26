import { api } from "/kit/api.js";
import { lineChart } from "/static/charts.js";
import { currentCity, setCity, withCity } from "/static/cities.js";
import { deleteCityControl, LEGACY_TEXT } from "/static/citydelete.js";
import { prettyDistrict } from "/static/districts.js";
import { agoSeconds, duration, loadError } from "/static/fmt.js";
import { can } from "/kit/session.js";
import { $, errorState, fill, h, loading, scrollRegion } from "/kit/ui.js";

const LABEL = { operational: "All systems operational", degraded: "Degraded", partial_outage: "Partial outage", major_outage: "Major outage" };
let timer = null;
let catalog = null;

const pct = (x) => `${Math.round(x * 1000) / 10}%`;
const heatClass = (ok, total) => {
  if (!total) return "none";
  const r = ok / total;
  return r < 0.5 ? "h0" : r < 0.75 ? "h1" : r < 0.9 ? "h2" : r < 0.98 ? "h3" : "h4";
};
const barClass = (r) => (r < 0.5 ? "bad" : r < 0.9 ? "warn" : "");

function bar(ratio) {
  const i = h("i");
  i.style.setProperty("width", `${Math.round(ratio * 100)}%`);
  return h("div", { class: `bar ${barClass(ratio)}` }, i);
}

function banner(s) {
  const reasons = s.reasons.length ? h("ul", {}, ...s.reasons.map((r) => h("li", {}, r))) : h("p", { class: "muted" }, "Every sensor group is within its normal range.");
  return h("div", { class: `status-banner ${s.state}` },
    h("div", {}, h("h1", {}, h("span", { class: "dot" }), LABEL[s.state] || s.state), reasons),
    h("div", { class: "big" }, pct(s.health), h("small", {}, `${s.counts.ok.toLocaleString()} of ${s.total.toLocaleString()} sensors healthy`)));
}

function kpis(s) {
  const k = (label, n, cls = "") => h("div", { class: `kpi ${cls}` }, h("b", {}, n.toLocaleString()), h("span", {}, label));
  const c = s.counts, sim = s.sim;
  const simText = !sim ? "" : sim.running ? `running, ${sim.sensors.toLocaleString()} sensors` : `stopped ${agoSeconds(sim.age)}`;
  return h("div", { class: "kpis" },
    k("Sensors", s.total), k("OK", c.ok, "ok"), k("Stale", c.stale, c.stale ? "warn" : ""), k("Offline", c.offline, c.offline ? "bad" : ""),
    k("Fault", c.fault, c.fault ? "bad" : ""), k("Stuck", c.stuck, c.stuck ? "warn" : ""),
    // Only a deployment with a built-in simulator has this tile (the API tells us with `sim`; a fleet fed over HTTP has none).
    sim ? h("div", { class: `kpi ${sim.running ? "ok" : "warn"}` }, h("b", {}, sim.running ? `${sim.buffer.toLocaleString()}` : "–"), h("span", {}, `Simulator buffer · ${simText}`)) : null);
}

// A column that holds only a bar has no visible title; screen readers still get one (no empty header cells).
const hiddenTh = (text) => h("th", {}, h("span", { class: "sr-only" }, text));

function heatmap(s) {
  const cell = new Map(s.matrix.map((m) => [`${m.district}|${m.type}`, m]));
  const present = new Set(s.matrix.map((m) => m.type));
  const types = catalog.types.filter((t) => present.has(t.key));
  const names = new Map(s.districts.map((d) => [d.key, d.label]));
  const present_d = new Set(s.matrix.map((m) => m.district));
  const rows = [...present_d].sort().map((id) => ({ id, name: names.get(id) || prettyDistrict(id) }));
  const head = h("tr", {}, hiddenTh("District"), ...types.map((t) => h("th", { class: "type", title: `${t.label} (${t.unit})` }, h("span", {}, t.label))));
  const body = rows.map((d) => h("tr", {}, h("th", { class: "row", scope: "row" }, d.name),
    ...types.map((t) => {
      const m = cell.get(`${d.id}|${t.key}`);
      if (!m) return h("td", {}, h("button", { class: "none", tabindex: "-1", "aria-hidden": "true", type: "button" }));
      const label = `${d.name} · ${t.label}: ${m.ok} of ${m.total} healthy`;
      return h("td", {}, h("button", { type: "button", class: heatClass(m.ok, m.total), title: label, "aria-label": label,
        onclick: () => { location.hash = `#sensors/${d.id}/${t.key}`; } }));
    })));
  const legend = h("div", { class: "legend" }, "Health:", ...["h0", "h1", "h2", "h3", "h4"].map((c, i) =>
    h("span", {}, h("i", { class: `heat-key ${c}` }), ["<50%", "50–75%", "75–90%", "90–98%", "≥98%"][i])));
  return h("div", { class: "card heat" }, h("div", { class: "row" }, h("h2", { class: "section-title" }, "Districts × sensor types"),
    h("span", { class: "muted" }, s.districts_total > s.districts.length ? `the ${s.districts.length} biggest of ${s.districts_total.toLocaleString()} districts; click a cell to list its sensors` : "click a cell to list its sensors")),
  h("table", {}, h("thead", {}, head), h("tbody", {}, ...body)), legend);
}

function typeTable(s) {
  const rows = [...s.types].sort((a, b) => a.ok / a.total - b.ok / b.total);
  return scrollRegion(h("div", { class: "card flush" }, h("table", {},
    h("thead", {}, h("tr", {}, ...["Sensor type", "Sensors", "Health", "Health bar", "Stale", "Offline", "Fault", "Stuck"].map((t) => (t === "Health bar" ? hiddenTh(t) : h("th", {}, t))))),
    h("tbody", {}, ...rows.map((t) => h("tr", { class: "clickable", onclick: () => { location.hash = `#sensors/-/${t.key}`; } },
      h("td", { class: "title" }, t.label), h("td", { class: "num" }, t.total.toLocaleString()), h("td", { class: "num" }, pct(t.ok / t.total)),
      h("td", {}, bar(t.ok / t.total)), h("td", { class: "num" }, t.stale || ""), h("td", { class: "num" }, t.offline || ""),
      h("td", { class: "num" }, t.fault || ""), h("td", { class: "num" }, t.stuck || "")))))), "Sensor types");
}

function protocolTable(s) {
  if (!s.protocols || s.protocols.length < 2) return null;
  return scrollRegion(h("div", { class: "card flush" }, h("table", {},
    h("thead", {}, h("tr", {}, ...["Collected through", "Sensors", "Health", "Health bar"].map((t) => (t === "Health bar" ? hiddenTh(t) : h("th", {}, t))))),
    h("tbody", {}, ...s.protocols.map((p) => h("tr", {}, h("td", { class: "title" }, p.label), h("td", { class: "num" }, p.total.toLocaleString()),
      h("td", { class: "num" }, pct(p.ok / p.total)), h("td", {}, bar(p.ok / p.total))))))), "Collection protocols");
}

// A deployment upgraded from before cities were mandatory may still hold sensors in the retired city. Say so, and offer the way out.
// The delete control is not part of it: it lives in the actions region, which a refresh never redraws.
function legacyNotice(id, scoped) {
  return h("div", { class: "banner warn legacy-banner" }, h("strong", {}, `City ${id}: ${LEGACY_TEXT}. `),
    "These sensors were stored before every reading had to name its city; nothing creates this city any more, and it is never removed automatically. ",
    scoped ? (can("city:delete") ? "Delete it once you no longer need this data." : "Ask an administrator to delete it once the data is no longer needed.")
      : h("button", { class: "secondary small", type: "button", onclick: () => setCity(id) }, "Open it"));
}

// With several cities, the least healthy few (a click selects one); never the whole list.
function worstCities(out) {
  if (!out || out.total < 2) return null;
  const rows = out.cities.filter((c) => c.counts.ok < c.total);
  return scrollRegion(h("div", { class: "card flush" }, h("div", { class: "row pad" }, h("h2", { class: "section-title" }, "Cities needing attention"),
    h("span", { class: "muted" }, `${out.total.toLocaleString()} cities; pick one in the City menu to drill in`)),
  rows.length ? h("table", {}, h("thead", {}, h("tr", {}, ...["City", "Sensors", "Health", "Health bar", "Worst", "Down"].map((t) => (t === "Health bar" ? hiddenTh(t) : h("th", {}, t))))),
    h("tbody", {}, ...rows.map((c) => h("tr", { class: "clickable", onclick: () => setCity(c.city) },
      h("td", { class: "title" }, c.city), h("td", { class: "num" }, c.total.toLocaleString()), h("td", { class: "num" }, pct(c.health)),
      h("td", {}, bar(c.health)), h("td", {}, h("span", { class: `st ${c.worst_status}` }, c.worst_status)), h("td", { class: "num" }, (c.total - c.counts.ok).toLocaleString()))))) :
    h("p", { class: "empty" }, "Every city is healthy.")), "Cities needing attention");
}

// The page has three regions. `head` and `body` are redrawn by every refresh and hold no input. `actions` holds the delete-city
// control (a text input the user types into): it is only rebuilt when WHICH control applies changes, never by the timer, so what
// is typed, the focus and the caret survive a refresh.
function syncActions(actions, key, make) {
  if (actions.dataset.key === key) return;
  actions.dataset.key = key;
  fill(actions, make ? make() : null);
}

async function render(view) {
  if (view.busy) return;                                         // a slow refresh is not stacked on the next tick
  view.busy = true;
  try { await draw(view); } finally { view.busy = false; }
}

async function draw(view) {
  const { head, actions, body } = view;
  let s, hist;
  const scoped = currentCity();
  try { [s, hist] = await Promise.all([api(withCity("/api/status")), api(withCity("/api/history?minutes=60"))]); } catch (err) {
    if (!view.loaded) errorState(body, loadError(err), () => { loading(body); render(view); });   // first paint failed: say so, never leave it blank
    return;
  }
  if (scoped !== currentCity()) return;                          // the city changed while loading: a newer render is on its way
  let list = null;
  if (!scoped) { try { list = await api("/api/cities?sort=worst&limit=8"); } catch { /* the rest of the page still shows */ } }
  const cities = worstCities(list);
  const legacy = scoped ? (s.legacy ? legacyNotice(scoped, true) : null) : (list && list.legacy_city ? legacyNotice(list.legacy_city, false) : null);
  const burst = s.bursts.length ? h("div", { class: "banner" }, h("strong", {}, "Burst active: "),
    s.bursts.map((b) => `${b.kind} on ${b.target} (level ${b.intensity}, ${duration(b.seconds_left)} left)`).join(" · ")) : null;
  const health = hist.health.map((r) => [r.ts, r.total ? (100 * r.ok) / r.total : 100]);
  const flow = hist.throughput.map((r) => [r.ts, r.n]);
  const charts = h("div", { class: "panel-grid" },
    h("figure", { class: "card chartbox" }, h("figcaption", {}, "Fleet health, last hour (%)"), lineChart(health, { label: "Fleet health", yMax: 100, unit: "%" })),
    h("figure", { class: "card chartbox" }, h("figcaption", {}, "Readings per 30 s (by event time)"), lineChart(flow, { label: "Readings per 30 seconds" })));
  view.loaded = true;
  if (!s.total) {
    syncActions(actions, `${scoped}|none`, scoped && s.legacy && can("city:delete") ? () => deleteCityControl(scoped, { onDone: () => setCity("") }) : null);
    if (scoped) { fill(head); fill(body, h("div", { class: "card" }, h("p", { class: "empty" }, `No sensors have reported for city ${scoped}.`))); return; }
    fill(head, legacy);
    if (legacy) { fill(body); return; }
    fill(body, h("div", { class: "card" }, h("p", { class: "empty" }, "No sensors have reported yet. The simulator starts on its own in the 'all' role; give it half a minute, or see Connect to send real readings.")));
    return;
  }
  const mayDelete = scoped && can("city:delete");
  syncActions(actions, `${scoped}|${mayDelete ? "delete" : "none"}`, mayDelete ? () => deleteCityControl(scoped, { total: s.total, onDone: () => setCity("") }) : null);
  fill(head, burst, legacy, scoped ? h("p", { class: "muted" }, "Showing city ", h("span", { class: "scope-chip" }, scoped), ". Use the City menu to switch.") : null);
  fill(body, banner(s), kpis(s), cities, heatmap(s), charts, h("h2", { class: "section-title" }, "By sensor type"), typeTable(s),
    s.protocols && s.protocols.length > 1 ? h("h2", { class: "section-title" }, "By collection protocol") : null, protocolTable(s));
}

export async function initOverview() {
  const box = $("view-overview");
  clearInterval(timer);
  if (!catalog) {
    loading(box);
    try { catalog = await api("/api/catalog"); } catch (err) { errorState(box, loadError(err), initOverview); return; }
  }
  const view = { head: h("div"), actions: h("div", { class: "overview-actions" }), body: h("div"), loaded: false };
  loading(view.body);
  fill(box, view.head, view.actions, view.body);
  await render(view);
  timer = setInterval(() => { if (document.hidden || location.hash.slice(1).split("/")[0] !== "overview" && location.hash !== "") return; render(view); }, 5000);
}

export function stopOverview() { clearInterval(timer); }
