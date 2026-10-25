import { api } from "/kit/api.js";
import { initBurst } from "/static/burst.js";
import { onCityChange, renderCityPicker } from "/static/cities.js";
import { initConnect } from "/static/connect.js";
import { showError, showLoading } from "/static/fmt.js";
import { initOverview, stopOverview } from "/static/overview.js";
import { initSensors } from "/static/sensors.js";
import { can, refreshSession, renderLogin, renderSetup, state } from "/kit/session.js";
import { initSettings } from "/static/settings.js";
import { $, h } from "/kit/ui.js";

const TABS = ["overview", "sensors", "burst", "connect", "settings"];
const INIT = { overview: initOverview, sensors: initSensors, burst: initBurst, connect: initConnect, settings: initSettings };
// What each tab needs; the server enforces it, this only hides what would be refused.
const NEEDS = { overview: "city:read", sensors: "city:read", burst: "city:control", connect: "city:read", settings: "settings:manage" };
const SECTIONS = ["auth-card", "nav", "city-picker", "signout", ...TABS.map((t) => "view-" + t)];

function hideAll() { for (const id of SECTIONS) $(id).hidden = true; }

function showUnavailable(err) {
  hideAll();
  $("auth-card").replaceChildren(
    h("img", { class: "hero-logo", src: "/static/logo.svg", alt: "", width: "72", height: "72" }),
    h("h1", {}, "CityPulse can't be reached"),
    h("p", { class: "muted" }, err.message),
    h("p", { class: "hint" }, "If this server runs on a platform that pauses idle apps, resume it first (for AgentPG: agentpg sandbox resume <sandbox-id>), then retry."),
    h("button", { class: "primary", type: "button", onclick: () => boot() }, "Retry"));
  $("auth-card").hidden = false;
}

async function boot(notice = "") {
  hideAll();
  stopOverview();
  let s;
  try { s = await refreshSession(); } catch (err) { showUnavailable(err); return; }
  if (s.state === "setup_required") renderSetup(() => boot(), "CityPulse");
  else if (s.state === "logged_out") renderLogin(() => boot(), notice, "CityPulse");
  else showApp();
}

let pickerReady = false;
onCityChange(() => route());                 // overview, sensors and burst redraw for the chosen city

function showApp() {
  $("auth-card").hidden = true;
  for (const a of document.querySelectorAll("#nav a")) a.hidden = !can(NEEDS[a.dataset.tab]);
  $("nav").hidden = false;
  $("signout").hidden = false;
  $("city-picker").hidden = !can("city:read");
  if (can("city:read") && !pickerReady) { pickerReady = true; renderCityPicker($("city-picker")); }
  route();
}

function route() {
  if (!state.session || state.session.state !== "logged_in") return;
  const [name, ...arg] = location.hash.slice(1).split("/");
  const allowed = TABS.filter((t) => can(NEEDS[t]));
  const tab = allowed.includes(name) ? name : (allowed[0] || "overview");
  if (tab !== "overview") stopOverview();
  for (const t of TABS) $("view-" + t).hidden = t !== tab;
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.dataset.tab === tab);
  const box = $("view-" + tab);
  if (!box.firstChild) showLoading(box);         // after sign-in the first data takes a moment: never a blank page
  const start = () => Promise.resolve(INIT[tab](tab === "sensors" ? arg : undefined)).catch((err) => { if (state.session) showError(box, err, start); });
  start();
}

$("signout").addEventListener("click", async () => {
  let out = {};
  try { out = await api("/api/logout", { method: "POST" }); } catch { /* the cookie is cleared server-side regardless */ }
  if (out.logout_url) { location.assign(out.logout_url); return; }   // also end the single sign-on session
  boot();
});

let lostTimer;
window.addEventListener("apg-auth-lost", () => {
  clearTimeout(lostTimer);
  lostTimer = setTimeout(() => boot("Your session ended. Please sign in again."), 50);
});
window.addEventListener("hashchange", route);

boot();
