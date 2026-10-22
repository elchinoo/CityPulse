// Districts are whatever the sensors of a city report (`alder-quay`, `north-mill`, or the legacy d01..d12): nothing here
// assumes a fixed list or that two cities share districts. Names come from the server; ids and names are data (text only).
import { api } from "/kit/api.js";

// The id prettified, as a fallback for an id the server did not name.
export const prettyDistrict = (id) => String(id).replace(/[-_]+/g, " ").split(" ").filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" ") || String(id);

// [{id, name, total}] of the districts that report in `city` (all cities when empty), biggest first, plus how many exist in all.
export async function loadDistricts(city) {
  if (city) {
    const out = await api(`/api/cities/${encodeURIComponent(city)}/districts`);
    return { total: out.total, list: out.districts.map((d) => ({ id: d.id, name: d.name, total: d.total })) };
  }
  const s = await api("/api/status");
  const list = s.districts.map((d) => ({ id: d.key, name: d.label || prettyDistrict(d.key), total: d.total })).sort((a, b) => b.total - a.total || (a.id < b.id ? -1 : 1));
  return { total: s.districts_total ?? list.length, list };
}
