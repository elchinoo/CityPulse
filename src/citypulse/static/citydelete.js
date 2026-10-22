// Admin only: delete a city with everything in it. The API refuses anyone without city:delete; this control only keeps
// the button out of sight of people who would be refused. Deleting needs the city id typed in full.
// City ids and counts are data: they only ever reach the page through textContent (h() uses text nodes).
import { api } from "/kit/api.js";
import { fill, h, setBusy, toast } from "/kit/ui.js";

export const LEGACY_TEXT = "legacy, no city reported";
export const confirmed = (typed, city) => typeof typed === "string" && typed.trim() === city;
const remove = (city) => api(`/api/cities/${encodeURIComponent(city)}`, { method: "DELETE" });

export const summarize = (out) => {
  const d = out.deleted || {};
  return `Deleted city ${out.city}: ${(d.sensors || 0).toLocaleString()} sensors, ${(d.readings || 0).toLocaleString()} readings, `
    + `${(d.status_history || 0).toLocaleString()} history rows, ${(d.chaos || 0).toLocaleString()} burst targets`;
};

// `call` and `onDone` are injectable so the flow can be exercised without a server.
export function deleteCityControl(city, { total = 0, call = remove, onDone = () => {} } = {}) {
  const box = h("div", { class: "city-delete" });
  const ask = h("button", { class: "danger small", type: "button", onclick: () => open() }, `Delete city ${city} (Admin)`);
  const typed = h("input", { id: "city-delete-confirm", type: "text", autocomplete: "off", maxlength: "32", "aria-label": "Type the city id to confirm" });
  const go = h("button", { class: "danger small", type: "button", disabled: true, onclick: () => run() }, "Delete everything");
  const cancel = h("button", { class: "secondary small", type: "button", onclick: () => close() }, "Cancel");
  const err = h("p", { class: "error", role: "alert" });
  const panel = h("div", { class: "card city-delete-panel", hidden: true },
    h("p", {}, h("strong", {}, `This permanently deletes city ${city}`),
      ` with ${total ? total.toLocaleString() + " sensors, " : "its sensors, "}all their readings, status history and burst targets. It cannot be undone. `,
      "Stop the senders of this city first, or they will recreate it."),
    h("label", { for: "city-delete-confirm" }, "Type the city id to confirm: ", h("code", {}, city)),
    typed, h("div", { class: "row left" }, go, cancel), err);

  function open() { err.textContent = ""; typed.value = ""; go.disabled = true; panel.hidden = false; ask.hidden = true; typed.focus?.(); }
  function close() { panel.hidden = true; ask.hidden = false; typed.value = ""; go.disabled = true; }
  typed.addEventListener("input", () => { go.disabled = !confirmed(typed.value, city); });
  async function run() {
    if (!confirmed(typed.value, city)) return;                  // the button is disabled too; never rely on that alone
    setBusy(go, true, "Deleting…");
    cancel.disabled = true;
    try {
      const out = await call(city);
      toast(summarize(out));
      close();
      onDone(out);
    } catch (e) {
      err.textContent = e.message;                              // 409 busy, 404 already gone, 403 not an admin ...
    } finally {
      setBusy(go, false);
      cancel.disabled = false;
      go.disabled = !confirmed(typed.value, city);
    }
  }
  fill(box, ask, panel);
  return box;
}
