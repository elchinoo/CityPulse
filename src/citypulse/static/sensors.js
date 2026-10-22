import { api } from "/kit/api.js";
import { currentCity, withCity } from "/static/cities.js";
import { $, field, fill, h } from "/kit/ui.js";

let catalog = null;
const ago = (iso) => {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 90 ? `${s}s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
};

function spark(readings) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 420 90");
  svg.setAttribute("class", "chart spark");
  if (readings.length < 2) return svg;
  const pts = [...readings].reverse().map((r) => [new Date(r.ts).getTime(), r.value]);
  const x0 = pts[0][0], x1 = pts[pts.length - 1][0];
  const lo = Math.min(...pts.map((p) => p[1])), hi = Math.max(...pts.map((p) => p[1]));
  const X = (t) => 6 + ((t - x0) / Math.max(x1 - x0, 1)) * 408;
  const Y = (v) => 84 - ((v - lo) / Math.max(hi - lo, 1e-9)) * 78;
  const line = document.createElementNS(NS, "polyline");
  line.setAttribute("points", pts.map(([t, v]) => `${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join(" "));
  line.setAttribute("class", "series");
  line.setAttribute("fill", "none");
  svg.append(line);
  return svg;
}

async function showDetail(box, id, back) {
  const s = await api(`/api/sensors/${encodeURIComponent(id)}`);
  fill(box, h("button", { class: "ghost", type: "button", onclick: back }, "← back to the list"),
    h("div", { class: "card" }, h("div", { class: "row" }, h("h2", { class: "section-title" }, s.id), h("span", { class: `st ${s.status}` }, s.status)),
      h("dl", { class: "facts" }, h("dt", {}, "Type"), h("dd", {}, s.type), h("dt", {}, "City"), h("dd", {}, s.city), h("dt", {}, "District"), h("dd", {}, s.district), h("dt", {}, "Protocol"), h("dd", {}, (s.protocol || "").replace("_", " ")),
        h("dt", {}, "Last value"), h("dd", {}, String(s.last_value)), h("dt", {}, "Last reading"), h("dd", {}, ago(s.last_ts)),
        h("dt", {}, "Verdict"), h("dd", {}, s.reason || "healthy"), h("dt", {}, "Since"), h("dd", {}, ago(s.status_since))),
      spark(s.readings)),
    h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Event time"), h("th", { class: "num" }, "Value"))),
      h("tbody", {}, ...s.readings.slice(0, 20).map((r) => h("tr", {}, h("td", {}, new Date(r.ts).toLocaleTimeString()), h("td", { class: "num" }, String(r.value))))))));
}

export async function initSensors(arg = []) {
  const box = $("view-sensors");
  if (!catalog) catalog = await api("/api/catalog");
  const [district, type] = arg;
  const stateSel = h("select", { id: "f-state" }, ...[["problem", "Problems only"], ["all", "All sensors"], ["ok", "Healthy"], ["stale", "Stale"], ["offline", "Offline"], ["fault", "Fault"], ["stuck", "Stuck"]].map(([v, t]) => h("option", { value: v }, t)));
  const typeSel = h("select", { id: "f-type" }, h("option", { value: "" }, "Any type"), ...catalog.types.map((t) => h("option", { value: t.key }, t.label)));
  const distSel = h("select", { id: "f-district" }, h("option", { value: "" }, "Any district"), ...catalog.districts.map((d) => h("option", { value: d.id }, d.name)));
  const q = h("input", { id: "f-q", placeholder: "sensor id contains…", maxlength: "64" });
  if (type && type !== "-") { typeSel.value = type; stateSel.value = "all"; }
  if (district && district !== "-") distSel.value = district;
  const out = h("div", { class: "card flush" });
  const draw = async () => {
    const p = new URLSearchParams({ state: stateSel.value, limit: "200" });
    if (typeSel.value) p.set("type", typeSel.value);
    if (distSel.value) p.set("district", distSel.value);
    if (q.value.trim()) p.set("q", q.value.trim());
    try {
      const rows = await api(withCity(`/api/sensors?${p}`));
      fill(out, rows.length ? h("table", {}, h("thead", {}, h("tr", {}, ...["Sensor", "City", "Type", "District", "Protocol", "State", "Last value", "Last reading", "Why"].map((t) => h("th", {}, t)))),
        h("tbody", {}, ...rows.map((r) => h("tr", { class: "clickable", onclick: () => showDetail(box, r.id, () => initSensors(arg)) },
          h("td", {}, h("code", {}, r.id)), h("td", {}, r.city), h("td", {}, r.type), h("td", {}, r.district), h("td", {}, (r.protocol || "").replace("_", " ")), h("td", {}, h("span", { class: `st ${r.status}` }, r.status)),
          h("td", { class: "num" }, String(r.last_value)), h("td", {}, ago(r.last_ts)), h("td", { class: "muted" }, r.reason || ""))))) :
        h("p", { class: "empty" }, stateSel.value === "problem" ? "No sensor has a problem right now." : "No sensors match."));
    } catch (err) { fill(out, h("p", { class: "error" }, err.message)); }
  };
  for (const c of [stateSel, typeSel, distSel]) c.addEventListener("change", draw);
  let t;
  q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(draw, 250); });
  fill(box, h("h1", {}, "Sensors", currentCity() ? h("small", { class: "scope-chip" }, ` · ${currentCity()}`) : null), h("div", { class: "card" }, h("div", { class: "grid3" }, field("State", stateSel), field("Type", typeSel), field("District", distSel)), field("Search", q)), out);
  draw();
}
