// Settings page (T11): retention, branding, users and own 2FA.
// Extracted from the old settings modal in app.js — this page loads its own
// gate: without a session the visitor is sent to the login on index.html.
import { $, el, setChildren } from "./dom.js";

const RETENTION_CUSTOM = "custom";
let me = null;              // {user:{name,role,totp_enabled}, auth}
let pendingLogo = null;     // dataURL chosen in this session, not yet saved
let logoCleared = false;    // the user pressed "Quitar" in this session

async function api(path) {
  const res = await fetch(path);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.error ?? `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

function authPost(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  }).then(async (res) => ({ status: res.status, data: await res.json().catch(() => ({})) }));
}

function authSend(method, path, body) {
  return fetch(path, {
    method,
    headers: body !== undefined ? { "content-type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  }).then(async (res) => ({ status: res.status, data: await res.json().catch(() => ({})) }));
}

const nf = new Intl.NumberFormat("es-ES");
const dateFmt = new Intl.DateTimeFormat("es-ES", {
  timeZone: "Europe/Madrid", day: "2-digit", month: "2-digit", year: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
});

function fmtDate(s) {
  if (!s) return "—";
  const d = new Date(`${String(s).replace(" ", "T")}Z`);
  return Number.isNaN(d.getTime()) ? String(s) : dateFmt.format(d).replace(",", "");
}

function friendly(err) {
  if (err && err.status === 403) return "Necesitas permisos de administrador.";
  return err?.message ?? String(err);
}

function authMsg(text, isErr) {
  const node = $("users-msg");
  node.textContent = text;
  node.classList.toggle("is-error", !!isErr);
}

function setMsg(text, isErr) {
  const node = $("settings-msg");
  node.textContent = text;
  node.classList.toggle("is-error", !!isErr);
}

// ---------------------------------------------------------------- gate

async function gate() {
  try {
    me = await api("api/auth/me");
  } catch (e) {
    if (e.status === 401) {
      try { sessionStorage.setItem("returnTo", "settings.html"); } catch { /* optional */ }
      location.href = "index.html";
      return false;
    }
    throw e;
  }
  return true;
}

// ---------------------------------------------------------------- retention + brand

function paintBrand(brand) {
  if (!brand) return;
  const title = $("brand-title"), sub = $("brand-sub");
  if (brand.title && title) title.textContent = brand.title;
  if (brand.subtitle && sub) sub.textContent = brand.subtitle;
  if (brand.logo) {
    const svg = document.getElementById("brand-logo");
    if (svg) {
      const img = document.createElement("img");
      img.className = "logo";
      img.src = brand.logo;
      img.alt = "";
      svg.replaceWith(img);
    }
  }
  if (brand.favicon) {
    let link = document.querySelector('link[rel="icon"]');
    if (!link) { link = document.createElement("link"); link.rel = "icon"; document.head.appendChild(link); }
    link.href = brand.favicon;
  }
  if (brand.title || brand.subtitle) {
    document.title = ["Ajustes", brand.title].filter(Boolean).join(" · ");
  }
}

function fillForm(cfg) {
  paintBrand(cfg.brand);
  const sel = $("cfg-retention");
  const days = cfg.retentionDays ?? 0;
  const known = ["0", "30", "90", "180", "365", "730"];
  if (known.includes(String(days))) sel.value = String(days);
  else { sel.value = RETENTION_CUSTOM; $("cfg-retention-custom").value = days; }
  $("cfg-retention-custom").classList.toggle("hidden", sel.value !== RETENTION_CUSTOM);
  const brand = cfg.brand ?? {};
  $("cfg-title").value = brand.title ?? "";
  $("cfg-subtitle").value = brand.subtitle ?? "";
  showLogoPreview(brand.logo ?? null);
  pendingLogo = null;
  logoCleared = false;
}

function showLogoPreview(dataUrl) {
  const img = $("cfg-logo-preview");
  const clear = $("cfg-logo-clear");
  if (dataUrl) { img.src = dataUrl; img.hidden = false; clear.hidden = false; }
  else { img.hidden = true; img.removeAttribute("src"); clear.hidden = true; }
}

async function save() {
  const sel = $("cfg-retention");
  let days;
  if (sel.value === RETENTION_CUSTOM) {
    days = parseInt($("cfg-retention-custom").value, 10);
    if (!Number.isInteger(days) || days < 1 || days > 36500) {
      return setMsg("Días personalizados: entero entre 1 y 36500.", true);
    }
  } else {
    days = parseInt(sel.value, 10);
  }
  const brand = {
    title: $("cfg-title").value.trim(),
    subtitle: $("cfg-subtitle").value.trim(),
  };
  // null clears the stored logo; omit the field to keep it untouched
  if (pendingLogo !== null) brand.logo = pendingLogo;
  else if (logoCleared) brand.logo = null;
  $("settings-save").disabled = true;
  setMsg("");
  try {
    const res = await fetch("api/config", {
      method: "POST",
      headers: { "x-viewer-action": "config", "content-type": "application/json" },
      body: JSON.stringify({ retentionDays: days, brand }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);
    pendingLogo = null;
    logoCleared = false;
    setMsg("Guardado.");
  } catch (e) {
    setMsg(e.status === 403 ? "Necesitas permisos de administrador." : `Error al guardar: ${e.message}`, true);
  } finally {
    $("settings-save").disabled = false;
  }
}

// ---------------------------------------------------------------- users

async function isAdmin() {
  return me?.user?.role === "admin";
}

async function renderUsers() {
  const box = $("users-list");
  setChildren(box);
  let users;
  try {
    users = await api("api/users");
  } catch (e) {
    if (e.status === 401) return backToLogin();
    authMsg(friendly(e), true);
    return;
  }
  for (const u of users) {
    const row = el("div", { class: "user-row" },
      el("span", { class: "user-name", text: u.name }),
      el("span", { class: "user-role", text: u.role === "admin" ? "admin" : "viewer" }),
      u.totp_enabled ? el("span", { class: "user-totp", text: "2FA" }) : null,
      u.disabled ? el("span", { class: "user-off", text: "desactivado" }) : null,
      el("span", { class: "user-last", text: u.last_login ? fmtDate(u.last_login) : "—" }),
    );
    const acts = el("div", { class: "user-acts" });
    const act = (label, fn, confirmMsg) => {
      const b = el("button", { class: "btn btn-sm", text: label });
      b.addEventListener("click", async () => {
        if (confirmMsg && !confirm(confirmMsg)) return;
        b.disabled = true;
        try {
          await fn();
          authMsg("");
          await renderUsers();
        } catch (e) {
          if (e.status === 401) return backToLogin();
          authMsg(friendly(e), true);
          b.disabled = false;
        }
      });
      return b;
    };
    const patch = (payload) =>
      authSend("PATCH", `api/users/${encodeURIComponent(u.name)}`, payload)
        .then((r) => { if (r.status !== 200) throw new Error(r.data.error ?? `HTTP ${r.status}`); });
    if (u.role === "admin") {
      acts.append(act("Pasar a viewer", () => patch({ role: "viewer" }),
        "¿Quitar permisos de admin a este usuario?"));
    } else {
      acts.append(act("Pasar a admin", () => patch({ role: "admin" }),
        "¿Dar permisos de admin a este usuario?"));
    }
    acts.append(act(u.disabled ? "Activar" : "Desactivar", () => patch({ disabled: !u.disabled })));
    acts.append(act("Eliminar", () =>
      authSend("DELETE", `api/users/${encodeURIComponent(u.name)}`).then((r) => {
        if (r.status !== 200) throw new Error(r.data.error ?? `HTTP ${r.status}`);
      }), `¿Eliminar la cuenta ${u.name}? Esta acción no se puede deshacer.`));
    row.append(acts);
    box.append(row);
  }
}

// ---------------------------------------------------------------- own 2FA

function renderTotpSection() {
  const area = $("totp-area");
  setChildren(area);
  const enabled = !!(me?.user?.totp_enabled);
  if (enabled) {
    const input = el("input", { class: "totp-input", inputmode: "numeric",
      maxlength: "6", placeholder: "000000", autocomplete: "one-time-code" });
    const btn = el("button", { class: "btn btn-sm", text: "Desactivar 2FA" });
    btn.addEventListener("click", async () => {
      if (!confirm("¿Desactivar la verificación en dos pasos de tu cuenta?")) return;
      btn.disabled = true;
      try {
        const r = await authPost("api/auth/totp/disable", { code: input.value.trim() });
        if (r.status !== 200) throw new Error(r.data.error ?? `HTTP ${r.status}`);
        me.user.totp_enabled = false;
        renderTotpSection();
      } catch (e) {
        authMsg(e.status === 401 ? "Código incorrecto." : friendly(e), true);
        btn.disabled = false;
      }
    });
    area.append(el("p", { class: "hint", text: "Tu cuenta usa verificación en dos pasos." }),
      el("div", { class: "actions" }, input, btn));
    return;
  }
  const btn = el("button", { class: "btn btn-sm", text: "Activar 2FA" });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const setup = await api("api/auth/totp/setup");
      renderTotpSetup(area, setup);
    } catch (e) {
      if (e.status === 401) return backToLogin();
      authMsg(friendly(e), true);
      btn.disabled = false;
    }
  });
  area.append(el("p", { class: "hint",
    text: "Añade una segunda capa: un código de 6 dígitos de tu móvil en cada inicio de sesión." }), btn);
}

function renderTotpSetup(area, setup) {
  setChildren(area);
  // QR: every module becomes an SVG attribute — no string HTML, no sinks
  const qr = qrcodegen.QrCode.encodeText(setup.otpauth_uri, qrcodegen.QrCode.Ecc.MEDIUM);
  const n = qr.size;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${n} ${n}`);
  svg.setAttribute("class", "totp-qr");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Código QR para tu app de autenticación");
  const bg = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  bg.setAttribute("width", n); bg.setAttribute("height", n); bg.setAttribute("fill", "#fff");
  svg.append(bg);
  const fg = document.createElementNS("http://www.w3.org/2000/svg", "g");
  fg.setAttribute("fill", "#111");
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      if (!qr.getModule(x, y)) continue;
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      r.setAttribute("x", x); r.setAttribute("y", y);
      r.setAttribute("width", 1); r.setAttribute("height", 1);
      fg.append(r);
    }
  }
  svg.append(fg);
  const input = el("input", { class: "totp-input", inputmode: "numeric", maxlength: "6",
    placeholder: "000000", autocomplete: "one-time-code" });
  const btn = el("button", { class: "btn btn-primary btn-sm", text: "Activar" });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const r = await authPost("api/auth/totp/enable", { code: input.value.trim() });
      if (r.status !== 200) throw new Error(r.data.error ?? `HTTP ${r.status}`);
      me.user.totp_enabled = true;
      renderTotpSection();
    } catch (e) {
      authMsg(e.status === 401 ? "Código incorrecto, inténtalo de nuevo." : friendly(e), true);
      btn.disabled = false;
    }
  });
  area.append(svg,
    el("p", { class: "hint", text: "Escanea con Google Authenticator, Aegis o similar, o pega la clave a mano:" }),
    el("code", { class: "totp-secret", text: setup.secret }),
    el("div", { class: "actions" }, input, btn));
}

// ---------------------------------------------------------------- wire

function backToLogin() {
  try { sessionStorage.setItem("returnTo", "settings.html"); } catch { /* optional */ }
  location.href = "index.html";
}

function wire() {
  $("theme-toggle").addEventListener("click", () => {
    const dark = document.body.classList.toggle("theme-dark");
    try { localStorage.setItem("n8nViewerTheme", dark ? "dark" : "light"); } catch { /* optional */ }
  });
  $("settings-save").addEventListener("click", save);
  $("cfg-retention").addEventListener("change", (e) => {
    $("cfg-retention-custom").classList.toggle("hidden", e.target.value !== RETENTION_CUSTOM);
  });
  $("cfg-logo-file").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
      setMsg("El logo pesa más de 2 MB; elegí uno más chico.", true);
      e.target.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      pendingLogo = reader.result;
      logoCleared = false;
      showLogoPreview(pendingLogo);
      setMsg("");
    };
    reader.readAsDataURL(file);
  });
  $("cfg-logo-clear").addEventListener("click", () => {
    pendingLogo = null;
    logoCleared = true;
    $("cfg-logo-file").value = "";
    showLogoPreview(null);
  });
  $("user-new-add").addEventListener("click", async () => {
    const username = $("user-new-name").value.trim();
    const password = $("user-new-pass").value;
    const role = $("user-new-role").value;
    try {
      const r = await authSend("POST", "api/users", { username, password, role });
      if (r.status !== 200) throw new Error(r.data.error ?? `HTTP ${r.status}`);
      $("user-new-name").value = ""; $("user-new-pass").value = "";
      authMsg("");
      await renderUsers();
    } catch (e) {
      if (e.status === 401) return backToLogin();
      authMsg(friendly(e), true);
    }
  });
}

(async () => {
  wire();
  if (!(await gate())) return;
  const admin = await isAdmin();
  $("sec-users").classList.toggle("hidden", !admin);
  $("sec-totp").classList.toggle("hidden", !admin);
  fillForm(await api("api/config"));
  if (admin) {
    await renderUsers();
    renderTotpSection();
  }
})();
