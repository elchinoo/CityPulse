import { api } from "/kit/api.js";
import { currentCity, withCity } from "/static/cities.js";
import { loadDistricts } from "/static/districts.js";
import { $, fill, h, toast } from "/kit/ui.js";

let timer = null;
let catalog = null;

export async function initBurst() {
  const box = $("view-burst");
  clearInterval(timer);
  if (!catalog) catalog = await api("/api/catalog");
  const city = currentCity();                    // "" = all cities; a chosen city becomes the default target
  const chaos = await api("/api/chaos");
  let districts = { list: [], total: 0 };         // the districts that really report (in the chosen city, else anywhere)
  try { districts = await loadDistricts(city); } catch { /* the target menu then offers no district */ }
  const duration = h("select", { id: "storm-secs" }, ...[[60, "1 minute"], [180, "3 minutes"], [300, "5 minutes"], [600, "10 minutes"]].map(([v, t]) => h("option", { value: v, selected: v === 180 }, t)));
  const err = h("p", { class: "error", role: "alert" });
  const run = async (fn, ok) => { err.textContent = ""; try { await fn(); toast(ok); refresh(); } catch (e) { err.textContent = e.message; } };

  const big = h("div", { class: "card burst-big" },
    h("button", { class: "burst", type: "button", onclick: () => run(() => api("/api/chaos/storm", { method: "POST", json: { seconds: Number(duration.value), ...(city ? { city } : {}) } }), city ? `Burst started in ${city}` : "Burst started") }, "BURST"),
    h("div", {}, h("strong", {}, "Stress the city on purpose."),
      h("p", { class: "muted" }, "Seven faults at once: a flood of readings, a slow database, corrupt payloads, noisy and stuck sensors, late data, and one district going dark. With a city picked in the City menu, they hit that city only (the slow database still slows the whole gateway). They end by themselves. Run PagerLens beside CityPulse and watch it open an incident."),
      h("div", { class: "row left" }, h("label", { for: "storm-secs" }, "Lasts"), duration,
        h("button", { class: "secondary", type: "button", onclick: () => run(() => api("/api/chaos/stop", { method: "POST" }), "All bursts stopped") }, "Stop everything"))), err);

  const activeBox = h("div", { class: "card flush" });
  const kinds = h("div", { class: "kinds" }, ...Object.entries(chaos.kinds).map(([kind, text]) => {
    const target = h("select", { "aria-label": `${kind} target` }, h("option", { value: "all", selected: !city }, "All cities"),
      city ? h("optgroup", { label: "City" }, h("option", { value: `city:${city}`, selected: true }, `City ${city}`)) : null,
      districts.list.length ? h("optgroup", { label: districts.total > districts.list.length ? `District (${districts.list.length} biggest of ${districts.total.toLocaleString()})` : "District" },
        ...districts.list.map((d) => h("option", { value: `district:${d.id}` }, d.name))) : null,
      h("optgroup", { label: "Sensor type" }, ...catalog.types.map((t) => h("option", { value: `type:${t.key}` }, t.label))));
    const level = h("select", { "aria-label": `${kind} level` }, ...[1, 2, 3, 4, 5].map((n) => h("option", { value: n, selected: n === 3 }, `Level ${n}`)));
    const secs = h("select", { "aria-label": `${kind} duration` }, ...[[60, "1 min"], [120, "2 min"], [300, "5 min"]].map(([v, t]) => h("option", { value: v, selected: v === 120 }, t)));
    return h("div", { class: "kind" }, h("h3", {}, kind.replace("_", " ")), h("p", {}, text),
      h("div", { class: "pillrow" }, target, level, secs),
      h("div", { class: "btns" }, h("button", { class: "secondary small", type: "button",
        onclick: () => run(() => api("/api/chaos", { method: "POST", json: { kind, target: target.value, ...(city ? { city } : {}), intensity: Number(level.value), seconds: Number(secs.value) } }), `${kind} started`) }, "Start")));
  }));

  async function refresh() {
    try {
      const a = (await api(withCity("/api/chaos"))).active;
      fill(activeBox, a.length ? h("table", {}, h("thead", {}, h("tr", {}, ...["Burst", "Target", "Level", "Ends in"].map((t) => h("th", {}, t)))),
        h("tbody", {}, ...a.map((b) => h("tr", {}, h("td", { class: "title" }, b.kind.replace("_", " ")), h("td", {}, b.target), h("td", {}, b.intensity), h("td", {}, `${b.seconds_left}s`))))) :
        h("p", { class: "empty" }, "Nothing is running. The city is having a normal day."));
    } catch { /* keep the last view; the next tick retries */ }
  }
  fill(box, h("h1", {}, "Burst", city ? h("small", { class: "scope-chip" }, ` · ${city}`) : null), big, h("h2", { class: "section-title" }, "Active"), activeBox,
    h("h2", { class: "section-title" }, "Pick your own fault"), kinds,
    h("p", { class: "hint" }, "Bursts bend the simulated fleet and the gateway. Real sensors you connect are never altered, only the simulator is."));
  refresh();
  timer = setInterval(() => { if (location.hash.slice(1).split("/")[0] !== "burst") clearInterval(timer); else refresh(); }, 2000);
}
