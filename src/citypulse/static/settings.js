import { api } from "/kit/api.js";
import { $, copy, field, fill, fmtDate, h, toast } from "/kit/ui.js";

export async function initSettings() {
  const box = $("view-settings");
  let session;
  try { session = await api("/api/session"); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const reveal = h("div", { hidden: true });
  const tName = h("input", { id: "tk-name", maxlength: "60", required: true, placeholder: "e.g. edge gateway, district 3" });
  const tScope = h("select", { id: "tk-scope" },
    h("option", { value: "ingest" }, "Ingest only (push readings; reads nothing). For sensors and collectors"),
    h("option", { value: "metrics" }, "Metrics only (scrape /metrics; reads nothing else). For Prometheus or pagerlens-agent"),
    h("option", { value: "read" }, "Read-only (dashboard data)"),
    h("option", { value: "full" }, "Full access (can start bursts and manage tokens)"));
  const tErr = h("p", { class: "error", role: "alert" });
  const tokenForm = h("form", { class: "grid-inline", onsubmit: async (e) => {
    e.preventDefault();
    tErr.textContent = "";
    try {
      const t = await api("/api/tokens", { method: "POST", json: { name: tName.value, scope: tScope.value } });
      tName.value = "";
      reveal.replaceChildren(
        h("p", { class: "warn-text" }, "Copy this token now. It is shown only once and cannot be recovered; only a hash is stored."),
        h("div", { class: "codewrap" }, h("pre", { class: "code" }, h("code", {}, t.token)), h("button", { class: "secondary small", type: "button", onclick: () => copy(t.token, "Token copied") }, "Copy")));
      reveal.hidden = false;
      drawTokens();
    } catch (ex) { tErr.textContent = ex.message; }
  } }, field("Name", tName), field("Access", tScope), h("button", { class: "primary", type: "submit" }, "Create token"));

  const table = h("div", {});
  async function drawTokens() {
    const list = await api("/api/tokens");
    fill(table, list.length ? h("div", { class: "card flush" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Name", "Token", "Access", "Created", "Last used", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, ...list.map((t) => h("tr", {},
        h("td", { class: "title" }, t.name), h("td", {}, h("code", {}, `${t.prefix}…`)), h("td", {}, t.scope),
        h("td", {}, fmtDate(t.created_at)), h("td", {}, fmtDate(t.last_used_at)),
        h("td", { class: "actions" }, t.revoked_at ? h("span", { class: "muted" }, "revoked") : h("button", { class: "danger small", type: "button", onclick: async () => {
          if (!confirm(`Revoke "${t.name}"? Clients using it stop working.`)) return;
          try { await api(`/api/tokens/${t.id}`, { method: "DELETE" }); toast("Token revoked"); drawTokens(); } catch (err) { toast(err.message, true); }
        } }, "Revoke"))))))) : h("p", { class: "hint" }, "No tokens yet."));
  }

  const has = session.has_admin_password;
  const cur = h("input", { id: "pw-cur", type: "password", autocomplete: "current-password" });
  const nw = h("input", { id: "pw-new", type: "password", autocomplete: "new-password", minlength: "12", required: true });
  const nw2 = h("input", { id: "pw-new2", type: "password", autocomplete: "new-password", minlength: "12", required: true });
  const pErr = h("p", { class: "error", role: "alert" });
  const pwForm = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    pErr.textContent = "";
    if (nw.value !== nw2.value) { pErr.textContent = "The new passwords do not match."; return; }
    try {
      await api("/api/admin-password", { method: "POST", json: { current: has ? cur.value : null, new: nw.value } });
      cur.value = nw.value = nw2.value = "";
      toast("Password saved. You were signed out everywhere.");
      window.dispatchEvent(new CustomEvent("apg-auth-lost"));
    } catch (ex) { pErr.textContent = ex.message; }
  } },
  has ? field("Current password", cur) : null,
  field(has ? "New password (12+ characters)" : "Admin password (12+ characters)", nw),
  field("Repeat", nw2),
  h("button", { class: "primary", type: "submit" }, has ? "Change password" : "Set admin password"), pErr);

  fill(box, h("h1", {}, "Settings"),
    h("h2", { class: "section-title" }, "API tokens"),
    h("p", { class: "muted" }, "Prefer the narrowest token. Sensors and collectors need only ingest; scrapers need only metrics."),
    h("div", { class: "card" }, tokenForm, tErr), reveal, table,
    h("h2", { class: "section-title" }, "Admin password"),
    h("p", { class: "muted" }, "Changing it signs every browser out."),
    h("div", { class: "card" }, pwForm));
  drawTokens();
}
