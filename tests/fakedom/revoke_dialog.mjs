// Revoking a token asks through the kit's confirmDialog (never window.confirm): the dialog lives under <body>, so a refresh timer or
// a redraw of the Settings page cannot wipe it; Cancel and Escape send nothing, Revoke sends exactly one DELETE.
import assert from "node:assert/strict";
import { El, install, modals } from "./dom.mjs";

const byId = install();
const view = byId["view-settings"] = new El("section");
globalThis.confirm = globalThis.alert = globalThis.prompt = () => { throw new Error("a native dialog was used"); };
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {};
globalThis.location = { origin: "http://x", hash: "#settings", search: "", pathname: "/" };

const calls = [];
globalThis.fetch = async (url, init = {}) => {
  calls.push([init.method || "GET", String(url)]);
  const u = String(url);
  let body = {};
  if (u.startsWith("/api/session")) body = { has_admin_password: false };
  else if (u.startsWith("/api/tokens")) body = (init.method && init.method !== "GET") ? {} : [{ id: 7, name: "edge", prefix: "cpt_ab", scope: "ingest", created_at: "2026-01-01T00:00:00Z", last_used_at: null, revoked_at: null }];
  return { ok: true, status: 200, json: async () => body };
};
const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };
const { initSettings } = await import("/static/settings.js");
await initSettings();
await new Promise((r) => setTimeout(r, 20));

const body = document.body;
const overlay = () => body.find((e) => e.attrs.role === "alertdialog");   // the kit's native <dialog>
const dbtn = (label) => overlay().find((e) => e.tag === "button" && e.textContent === label);
const tick = () => new Promise((r) => setTimeout(r, 10));
const revoke = () => view.find((e) => e.tag === "button" && e.textContent === "Revoke");
let pending;
const deletes = () => calls.filter(([m]) => m === "DELETE");

pending = revoke().click(); await tick();   // the handler waits for the answer: do not await it yet
assert.ok(overlay(), "Revoke opens the shared dialog");
assert.ok(overlay().tag === "dialog" && overlay().open && modals.at(-1) === overlay(), "it is a native <dialog> opened with showModal()");
assert.ok(overlay().textContent.includes('Revoke "edge"? Clients using it stop working.'), "same message as before");
assert.equal(dbtn("Revoke").className, "danger-solid", "destroying access is a danger dialog");
await tick(); await tick();                                  // time passes: the dialog is not part of the page, nothing wipes it
assert.ok(overlay() && body.contains(overlay()) && !view.contains(overlay()), "the dialog sits under <body>, not in the view");
await dbtn("Cancel").click(); await pending;
assert.ok(!overlay() && modals.length === 0 && deletes().length === 0, "Cancel removes the dialog and revokes nothing");

pending = revoke().click(); await tick();   // the handler waits for the answer: do not await it yet
const cancel = await overlay().dispatch("cancel"); await pending;   // native Escape fires 'cancel' on the dialog
assert.ok(cancel.defaultPrevented, "the browser's own close is prevented; the kit closes it");
assert.ok(!overlay() && modals.length === 0 && deletes().length === 0, "Escape cancels");

pending = revoke().click(); await tick();   // the handler waits for the answer: do not await it yet
await dbtn("Revoke").click(); await pending; await tick();
assert.deepEqual(deletes(), [["DELETE", "/api/tokens/7"]], "confirming revokes exactly once");
console.log("ok");
process.exit(0);
