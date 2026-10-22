// The smallest DOM the dashboard's h()/fill()/toast() helpers need, strict where it matters: assigning HTML throws.
export class El {
  constructor(tag) {
    this.nodeType = 1; this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {};
    this.hidden = false; this.disabled = false; this.className = ""; this.value = ""; this.dataset = {}; this.parent = null;
    this.style = { setProperty() {} }; this.classList = { toggle() {}, add() {}, remove() {} };
  }
  get firstChild() { return this.children[0] || null; }
  contains(node) { return node === this || this.all().includes(node); }
  append(...kids) { for (const k of kids) { if (k.nodeType) { k.parent = this; this.children.push(k); } else this.children.push({ nodeType: 3, data: String(k) }); } }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "hidden" || k === "disabled") this[k] = true; }   // boolean attributes reflect
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async dispatch(type) { for (const fn of this.listeners[type] || []) await fn({ type, target: this, preventDefault() {} }); }
  async click() { if (!this.disabled) await this.dispatch("click"); }          // a real browser sends no click to a disabled button
  async type(text) { this.value = text; await this.dispatch("input"); }
  focus() { this.focused = true; if (globalThis.document) globalThis.document.activeElement = this; }
  matches() { return false; }
  get textContent() { return this.children.map((c) => (c.nodeType === 3 ? c.data : c.textContent)).join(""); }
  set textContent(v) { this.children = [{ nodeType: 3, data: String(v) }]; }
  set innerHTML(_) { throw new Error("innerHTML is forbidden: city ids are data"); }
  set outerHTML(_) { throw new Error("outerHTML is forbidden"); }
  insertAdjacentHTML() { throw new Error("insertAdjacentHTML is forbidden"); }
  all() { return this.children.flatMap((c) => (c.nodeType === 1 ? [c, ...c.all()] : [])); }
  find(pred) { return this.all().find(pred); }
}

export function install() {
  const byId = { toast: new El("div") };
  globalThis.document = {
    createElement: (tag) => new El(tag), createElementNS: (_ns, tag) => new El(tag), createTextNode: (data) => ({ nodeType: 3, data: String(data) }),
    getElementById: (id) => byId[id] || null, documentElement: { dataset: { csrf: "X-CityPulse-CSRF" } }, hidden: false,
    addEventListener() {},
  };
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.window = { dispatchEvent() {} };
  globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
  return byId;
}
