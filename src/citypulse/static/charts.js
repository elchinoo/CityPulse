// Tiny dependency-free SVG charts. Presentation attributes only (the CSP forbids inline styles).
const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs = {}) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };

// points: [[time, value], ...]; yMax fixes the top of the axis (e.g. 100 for percentages).
export function lineChart(points, { label, yMax, unit = "" }) {
  const W = 420, H = 120, PAD = 8;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": label });
  if (points.length < 2) {
    const t = el("text", { x: W / 2, y: H / 2, class: "peak", "text-anchor": "middle" });
    t.textContent = "collecting data…";
    svg.append(t);
    return svg;
  }
  const xs = points.map(([t]) => new Date(t).getTime());
  const ys = points.map(([, v]) => v);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), top = yMax ?? Math.max(...ys, 1e-9);
  const X = (t) => PAD + ((new Date(t).getTime() - x0) / Math.max(x1 - x0, 1)) * (W - 2 * PAD);
  const Y = (v) => H - PAD - (Math.min(v, top) / top) * (H - 2 * PAD);
  svg.append(el("line", { x1: PAD, x2: W - PAD, y1: Y(0), y2: Y(0), class: "axis" }));
  svg.append(el("polyline", { points: points.map(([t, v]) => `${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join(" "), class: "series", fill: "none" }));
  const t = el("text", { x: W - PAD, y: 14, class: "peak", "text-anchor": "end" });
  t.textContent = `now ${Math.round(ys[ys.length - 1] * 10) / 10}${unit} · max ${Math.round(Math.max(...ys) * 10) / 10}${unit}`;
  svg.append(t);
  return svg;
}
