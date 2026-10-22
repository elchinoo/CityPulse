// Exercises the delete-city confirmation against a fake DOM and a fake server: nothing is sent until the exact id is typed.
import assert from "node:assert/strict";
import { El, install } from "./dom.mjs";

const dom = install();
const { deleteCityControl, confirmed, summarize, LEGACY_TEXT } = await import("/static/citydelete.js");

assert.equal(LEGACY_TEXT, "legacy, no city reported");
assert.ok(confirmed("paris", "paris") && !confirmed("Paris", "paris") && !confirmed("pari", "paris") && !confirmed("", "paris") && !confirmed(null, "paris"));

const buttons = (box) => box.all().filter((e) => e.tag === "button");
const byText = (box, t) => buttons(box).find((b) => b.textContent.includes(t));

// 1. the happy path
let calls = [], done = [];
let box = deleteCityControl("paris", { total: 1234, call: async (id) => { calls.push(id); return { city: id, deleted: { sensors: 1234, readings: 99000, status_history: 7, chaos: 1 } }; }, onDone: (o) => done.push(o) });
const ask = byText(box, "Delete city paris (Admin)");
const panel = box.find((e) => e.className.includes("city-delete-panel"));
const input = box.find((e) => e.tag === "input");
const go = byText(box, "Delete everything");
assert.ok(ask && panel && input && go);
assert.equal(panel.hidden, true, "the confirmation starts hidden");
assert.equal(go.disabled, true);
await ask.click();
assert.equal(panel.hidden, false); assert.equal(ask.hidden, true);
assert.ok(panel.textContent.includes("1,234 sensors") || panel.textContent.includes("1234 sensors"));
assert.ok(panel.textContent.includes("cannot be undone"));
for (const wrong of ["", "pari", "Paris", "paris2", "paris;drop", " pari s"]) {
  await input.type(wrong);
  assert.equal(go.disabled, true, `must stay disabled for ${JSON.stringify(wrong)}`);
}
await go.dispatch("click");                                  // even a forced click does nothing without the exact id
assert.deepEqual(calls, []);
await input.type("paris");
assert.equal(go.disabled, false);
await go.click();
assert.deepEqual(calls, ["paris"]);
assert.equal(done.length, 1);
assert.equal(panel.hidden, true, "closed after success");
assert.equal(dom.toast.textContent, summarize(done[0]));
assert.ok(dom.toast.textContent.includes("1,234 sensors") && dom.toast.textContent.includes("99,000 readings"));

// 2. Cancel closes without calling anything; reopening starts empty
box = deleteCityControl("rome", { call: async () => { throw new Error("must not be called"); } });
await byText(box, "Delete city rome").click();
await box.find((e) => e.tag === "input").type("rome");
await byText(box, "Cancel").click();
assert.equal(box.find((e) => e.className.includes("city-delete-panel")).hidden, true);
await byText(box, "Delete city rome").click();
assert.equal(box.find((e) => e.tag === "input").value, "");
assert.equal(byText(box, "Delete everything").disabled, true);

// 3. a refused deletion keeps the panel open and shows the reason as text
box = deleteCityControl("oslo", { call: async () => { throw new Error("another city deletion is running"); }, onDone: () => assert.fail("not done") });
await byText(box, "Delete city oslo").click();
await box.find((e) => e.tag === "input").type("oslo");
await byText(box, "Delete everything").click();
assert.equal(box.find((e) => e.className === "error").textContent, "another city deletion is running");
assert.equal(box.find((e) => e.className.includes("city-delete-panel")).hidden, false);
assert.equal(byText(box, "Delete everything").disabled, false, "can retry");

// 4. ids are data: a hostile id becomes text, never markup (and innerHTML would have thrown)
const evil = '<img src=x onerror=alert(1)>';
box = deleteCityControl(evil, { total: 1 });
assert.equal(box.all().filter((e) => e.tag === "img").length, 0);
assert.ok(box.textContent.includes(evil));

// 5. the default call is DELETE /api/cities/<id> with the CSRF header, and the id is encoded
const seen = [];
globalThis.fetch = async (url, init) => { seen.push([url, init]); return { ok: true, json: async () => ({ city: "paris", deleted: { sensors: 1, readings: 2, status_history: 3, chaos: 4 } }) }; };
box = deleteCityControl("paris");
await byText(box, "Delete city paris").click();
await box.find((e) => e.tag === "input").type("paris");
await byText(box, "Delete everything").click();
assert.equal(seen.length, 1);
assert.equal(seen[0][0], "/api/cities/paris");
assert.equal(seen[0][1].method, "DELETE");
assert.equal(seen[0][1].headers["X-CityPulse-CSRF"], "1");
assert.equal(seen[0][1].credentials, "same-origin");
console.log("ok");
