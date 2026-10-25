import { fill, h } from "/kit/ui.js";

// One place for how the dashboard words a length of time: "42s", "4 min", "3 h", "3 days". Never raw seconds past a minute or so.
export function duration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s}s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 129600) return `${Math.round(s / 3600)} h`;
  const d = Math.round(s / 86400);
  return `${d} day${d === 1 ? "" : "s"}`;
}

export const agoSeconds = (seconds) => `${duration(seconds)} ago`;
export const ago = (iso, now = Date.now()) => agoSeconds((now - new Date(iso).getTime()) / 1000);

// A content area is never blank: a visible "Loading..." until the first data arrives, a readable message (with a retry) if it fails.
export function showLoading(el, text = "Loading…") {
  fill(el, h("p", { class: "muted cp-loading", role: "status", "aria-live": "polite" }, text));
}

export function showError(el, err, onRetry) {
  fill(el, h("div", { class: "cp-error", role: "alert" },
    h("p", { class: "error" }, `Could not load this page: ${(err && err.message) || err || "unknown error"}`),
    onRetry ? h("button", { class: "secondary", type: "button", onclick: () => onRetry() }, "Try again") : null));
}
