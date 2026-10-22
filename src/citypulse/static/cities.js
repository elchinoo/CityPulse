// The city selector and the chosen city. With thousands of cities nothing here lists them all: the picker asks the server
// (/api/cities?q=…) for the first few matches as you type. City ids are data: they only ever reach the page through textContent.
import { api } from "/kit/api.js";
import { fill, h } from "/kit/ui.js";

const KEY = "citypulse.city";
const VALID = /^[a-z0-9][a-z0-9_-]{0,31}$/;
export const PICKER_LIMIT = 12;      // options shown at once
const listeners = new Set();
let current = "";                    // "" = all cities

try { const saved = localStorage.getItem(KEY); if (saved && VALID.test(saved)) current = saved; } catch { /* storage blocked: start with all cities */ }

export const currentCity = () => current;
export const onCityChange = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
// Add the chosen city to an API path (nothing when all cities are shown).
export const withCity = (path) => (current ? `${path}${path.includes("?") ? "&" : "?"}city=${encodeURIComponent(current)}` : path);
export const cityLabel = () => current || "All cities";

export function setCity(id) {
  const next = id && VALID.test(id) ? id : "";
  if (next === current) return;
  current = next;
  try { if (next) localStorage.setItem(KEY, next); else localStorage.removeItem(KEY); } catch { /* not remembered */ }
  for (const fn of [...listeners]) fn(current);
}

export function renderCityPicker(box) {
  let open = false, seq = 0, timer;
  const button = h("button", { class: "ghost city-btn", type: "button", "aria-haspopup": "listbox", "aria-expanded": "false", "aria-controls": "city-panel" });
  const search = h("input", { id: "city-q", type: "search", placeholder: "Search cities…", maxlength: "32", autocomplete: "off", "aria-label": "Search cities" });
  const list = h("ul", { class: "city-list", role: "listbox", "aria-label": "Cities" });
  const note = h("p", { class: "hint city-note", role: "status" });
  const panel = h("div", { id: "city-panel", class: "city-panel card", hidden: true }, search, list, note);

  const choose = (id) => { setCity(id); close(); };
  const item = (id, text, sub, cls) => h("li", { role: "option", "aria-selected": String(id === current), class: `city-opt ${id === current ? "sel" : ""}`, tabindex: "-1",
    onclick: () => choose(id), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(id); } } },
  h("span", { class: "city-name" }, text), sub ? h("span", { class: `st ${cls || ""}` }, sub) : null);

  async function load() {
    const mine = ++seq;
    const q = search.value.trim().toLowerCase();
    let out;
    try { out = await api(`/api/cities?limit=${PICKER_LIMIT}&sort=${q ? "name" : "worst"}&q=${encodeURIComponent(q)}`); } catch (err) {
      if (mine === seq) { fill(list); note.textContent = err.message; }
      return;
    }
    if (mine !== seq) return;                                   // a newer search is in flight
    const rows = out.cities.map((c) => item(c.city, c.city, c.worst_status === "ok" ? "ok" : `${c.total - c.counts.ok} down`, c.worst_status));
    fill(list, q ? null : item("", "All cities", `${out.total.toLocaleString()} cities`), ...rows);
    note.textContent = !out.total ? (q ? "No city starts with that." : "No city has reported yet.")
      : out.total > out.cities.length ? `Showing ${out.cities.length} of ${out.total.toLocaleString()}${q ? "" : ", least healthy first"}. Type to narrow down.` : "";
  }

  function close() { open = false; panel.hidden = true; button.setAttribute("aria-expanded", "false"); }
  function openIt() { open = true; panel.hidden = false; button.setAttribute("aria-expanded", "true"); search.value = ""; load(); search.focus(); }
  const draw = () => { button.textContent = `City: ${cityLabel()}`; button.title = current ? "Showing one city; click to change" : "Showing every city; click to pick one"; };

  button.addEventListener("click", () => (open ? close() : openIt()));
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 200); });
  search.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { close(); button.focus(); }
    else if (e.key === "Enter") {                                // an exact id typed in full is accepted even if it is not listed
      const q = search.value.trim().toLowerCase();
      if (VALID.test(q)) choose(q); else if (!q) choose("");
    }
  });
  document.addEventListener("click", (e) => { if (open && !box.contains(e.target)) close(); });
  onCityChange(draw);
  draw();
  fill(box, button, panel);
}
