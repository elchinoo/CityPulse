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

// How a failed first load reads in the kit's errorState: what failed, then the server's own words.
export const loadError = (err) => `Could not load this page: ${(err && err.message) || err || "unknown error"}`;
