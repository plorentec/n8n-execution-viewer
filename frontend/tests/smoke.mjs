// Frontend smoke test: drives the REAL settings.js page logic in Node with a
// minimal DOM stub against a real mock backend (tests/mock_server.py).
// Verifies the whole settings flow: auth gate → config → branding → users → 2FA.
//
// Run:  python3 tests/mock_server.py &   (prints its port)
//       MOCK_URL=http://127.0.0.1:<port>/ node tests/smoke.mjs
import assert from "node:assert";

const MOCK = process.env.MOCK_URL ?? "http://127.0.0.1:8397/";

// ---- minimal DOM stub ------------------------------------------------------
globalThis.Node = class Node {};
const made = [];
const fakeEl = (tag) => {
  const e = new globalThis.Node();
  e.tag = tag;
  e.children = []; e.attrs = {}; e.listeners = {};
  e.textContent = ""; e.value = ""; e.hidden = false; e.src = "";
  e.classList = {
    _s: new Set(),
    add(...c) { c.forEach(x => this._s.add(x)); },
    remove(...c) { c.forEach(x => this._s.delete(x)); },
    toggle(c, force) { const on = force ?? !this._s.has(c); on ? this._s.add(c) : this._s.delete(c); return on; },
    contains(c) { return this._s.has(c); },
  };
  e.setAttribute = (k, v) => { e.attrs[k] = v; };
  e.removeAttribute = (k) => { delete e.attrs[k]; };
  e.append = (...c) => { e.children.push(...c); };
  e.replaceChildren = (...c) => { e.children = [...c]; };
  e.addEventListener = (t, fn) => { (e.listeners[t] ??= []).push(fn); };
  e.focus = () => {}; e.select = () => {};
  e.querySelector = () => fakeEl("div");
  e.querySelectorAll = () => [];
  made.push(e);
  return e;
};
const byId = {};
globalThis.document = {
  createElement: fakeEl,
  createElementNS: (ns, tag) => fakeEl(tag),
  createTextNode: (t) => ({ text: t, __text: true }),
  getElementById: (id) => (byId[id] ??= fakeEl("div")),
  querySelector: (sel) => { const id = sel.match(/#([\w-]+)/)?.[1]; return (byId[id] ??= fakeEl("div")); },
  querySelectorAll: () => [],
  addEventListener() {},
};
globalThis.window = { addEventListener() {} };
globalThis.location = { href: "", hash: "", pathname: "/" };
globalThis.localStorage = { _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = v; }, removeItem(k) { delete this._d[k]; } };
globalThis.sessionStorage = { ...globalThis.localStorage };
globalThis.confirm = () => true;
globalThis.FileReader = class { readAsDataURL() {} };
globalThis.qrcodegen = { QrCode: { encodeText: () => ({ size: 21, getModule: () => false }), Ecc: { MEDIUM: "m", LOW: "l", QUARTILE: "q", HIGH: "h" } } };

// ---- fetch with a cookie jar (Node does not manage cookies) ----------------
let jar = {};
const realFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  if (typeof input === "string" && !input.startsWith("http")) input = MOCK + input;
  const headers = { ...(init?.headers ?? {}) };
  const cookieHeader = Object.entries(jar).map(([k, v]) => `${k}=${v}`).join("; ");
  if (cookieHeader) headers["Cookie"] = cookieHeader;
  const res = await realFetch(input, { ...init, headers });
  for (const c of res.headers.getSetCookie?.() ?? []) {
    const [nv] = c.split(";");
    const [k, ...rest] = nv.split("=");
    if (k && rest.length) jar[k.trim()] = rest.join("=");
  }
  return res;
};

// login first: the settings page requires a session
const loginRes = await fetch(MOCK + "api/auth/login", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ username: "pablo", password: "x" }),
});
if (loginRes.status !== 200) throw new Error("smoke login failed: " + loginRes.status);

// ---- the page under test ----------------------------------------------------
await import("/home/orca/orca/projects/n8n-viewer/frontend/settings.js");
await new Promise(r => setTimeout(r, 1200));

// ---- assertions -------------------------------------------------------------
assert.equal(byId["cfg-retention"].value, "30", "retención cargada del backend");
assert.equal(byId["cfg-title"].value, "PRUEBA", "marca cargada del backend");
const allText = [];
const walk = (n) => { if (n.textContent) allText.push(n.textContent); (n.children ?? []).forEach(walk); };
byId["users-list"].children.forEach(walk);
const allTextJoined = allText.join(" ");
assert.ok(allTextJoined.includes("pablo") && allTextJoined.includes("mama"),
  "usuarios listados: " + allTextJoined.slice(0, 120));
assert.equal(byId["users-list"].children.length, 2, "dos filas de usuario");
assert.ok(!byId["sec-users"].classList.contains("hidden"), "sección usuarios visible (admin)");
assert.ok(byId["totp-area"].children.length > 0, "sección 2FA renderizada");
// pulsar "Activar 2FA" → fetch setup → QR
const activateBtn = byId["totp-area"].children.find(
  (c) => c.listeners?.click && (c.textContent ?? "").includes("Activar 2FA"));
assert.ok(activateBtn, "botón Activar 2FA presente");
for (const fn of activateBtn.listeners.click) fn();
await new Promise(r => setTimeout(r, 500));
assert.ok(made.some(e => e.tag === "svg"), "QR construido como svg");
const qrArea = JSON.stringify(byId["totp-area"].children.map(c => c.tag ?? ""));
assert.ok(qrArea.includes("svg"), "el QR está en el área 2FA");
console.log("SMOKE OK: gate → config → marca → usuarios → 2FA (mock real)");
