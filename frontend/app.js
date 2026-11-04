// n8n execution viewer. Execution data comes from webhooks and APIs, so it is
// always rendered with textContent, never as HTML.
import { el, setChildren } from "./dom.js";
import { buildDiagram, routeEdge } from "./diagram.js";

// Your n8n web UI base URL, used for the "open in n8n" links.
// Set to e.g. "https://n8n.example.com"; leave empty to hide those links.
const N8N_URL = "";
// Deployed under a subpath (/n8n-viewer/): resolve the API relative to this
// script so it works at the site root too.
const API_BASE = import.meta.url.slice(0, import.meta.url.lastIndexOf("/") + 1);
const TREE_AUTO_OPEN_DEPTH = 1;
const TREE_CHUNK = 100;
const STATUS_LABEL = {
  success: "Correcta", error: "Error", crashed: "Caída", canceled: "Cancelada",
  waiting: "En espera", running: "En curso", new: "Nueva", unknown: "Desconocido",
};

const $ = (id) => document.getElementById(id);
const state = {
  workflows: [], workflow: "", filters: {}, nextBefore: null, shown: 0,
  exec: null, node: null, run: 0, runs: {}, execData: null,
  view: readPref("n8nViewerView", "list"),
};

function readPref(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writePref(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Preferences are optional.
  }
}

// ---------- helpers ----------

async function api(path) {
  const res = await fetch(API_BASE + path.slice(1));
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);
  return body;
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

function fmtMs(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  const m = Math.floor(ms / 60000);
  return `${m}m ${String(Math.round((ms % 60000) / 1000)).padStart(2, "0")}s`;
}

function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 100 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
}

const shortType = (t) => (t ? t.split(".").pop() : "");
const pill = (cls, text) => el("span", { class: `pill ${cls}`, text });
const statusPill = (s) => pill(s ?? "unknown", STATUS_LABEL[s] ?? s ?? STATUS_LABEL.unknown);
const loading = (text = "Cargando…") => el("div", { class: "loading" }, el("span", { class: "spinner" }), text);

function showError(container, err) {
  setChildren(container, el("div", { class: "error-box", text: `Error: ${err.message}` }));
}

// ---------- theme  ----------

$("theme-toggle").addEventListener("click", () => {
  const dark = document.body.classList.toggle("theme-dark");
  writePref("n8nViewerTheme", dark ? "dark" : "light");
});

// ---------- space: collapsible workflows column and focus mode ----------

function setWorkflowsCollapsed(collapsed) {
  document.querySelector(".layout").classList.toggle("wf-collapsed", collapsed);
}

function setFocus(on) {
  document.body.classList.toggle("focus-mode", on);
  $("focus-label").textContent = on ? "Reducir" : "Ampliar";
  if (state.view === "diagram") requestAnimationFrame(() => diagram.fit());
}

$("wf-collapse").addEventListener("click", () => setWorkflowsCollapsed(true));
$("wf-expand").addEventListener("click", () => setWorkflowsCollapsed(false));
$("focus-toggle").addEventListener("click", () => setFocus(!document.body.classList.contains("focus-mode")));
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  // S12: while the settings dialog is open it owns Escape (it closes itself and
  // gives the focus back), so Esc there must not also leave focus mode.
  if (!$("settings-overlay").hidden) return;
  if (document.body.classList.contains("focus-mode")) setFocus(false);
});

// ---------- routing (hash keeps deep links and the back button working) ----------

// Filter fields that travel in the hash too, so a shared link keeps them (S14).
const FILTER_KEYS = ["q", "status", "from", "to"];

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const filters = {};
  for (const key of FILTER_KEYS) {
    const v = p.get(key);
    if (v) filters[key] = v;
  }
  return {
    workflow: p.get("wf") ?? "", exec: p.get("exec"), node: p.get("node"),
    run: Number(p.get("run") ?? 0), filters,
  };
}

function writeHash() {
  const p = new URLSearchParams();
  if (state.workflow) p.set("wf", state.workflow);
  // S14: the search/status filters (and the date range) are part of the link.
  for (const key of FILTER_KEYS) {
    const v = state.filters[key];
    if (v) p.set(key, v);
  }
  if (state.exec) p.set("exec", state.exec);
  if (state.node) p.set("node", state.node);
  if (state.node && state.run) p.set("run", state.run);
  const next = `#${p}`;
  if (location.hash !== next) history.pushState(null, "", next);
}

function sameFilters(a, b) {
  return FILTER_KEYS.every((key) => (a[key] ?? "") === (b[key] ?? ""));
}

// The form is the visible source of truth: a deep link has to fill it in.
function fillFilters(filters) {
  const form = $("filters");
  for (const key of FILTER_KEYS) {
    const field = form.elements.namedItem(key);
    if (field && "value" in field) field.value = filters[key] ?? "";
  }
}

// ---------- JSON tree (children are built lazily, on first open) ----------

function jsonTree(value, key = null, depth = 0) {
  const label = key == null ? null : el("span", { class: "j-key", text: `${key}: ` });
  if (value === null || typeof value !== "object") {
    const type = value === null ? "null" : typeof value;
    const text = typeof value === "string" ? JSON.stringify(value) : String(value);
    return el("div", { class: "j-row" }, label, el("span", { class: `j-${type}`, text }));
  }

  const entries = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
  const open = Array.isArray(value) ? "[" : "{";
  const close = Array.isArray(value) ? "]" : "}";
  if (entries.length === 0) {
    return el("div", { class: "j-row" }, label, el("span", { class: "j-punct", text: `${open}${close}` }));
  }

  const summary = el("summary", {}, label,
    el("span", { class: "j-punct", text: `${open} ${entries.length} ${Array.isArray(value) ? "elementos" : "campos"} ${close}` }));
  const details = el("details", { class: "j-node" }, summary);
  const body = el("div", { class: "j-children" });
  details.append(body);

  let rendered = 0;
  const renderMore = () => {
    body.querySelector(":scope > .j-more")?.remove();
    const end = Math.min(rendered + TREE_CHUNK, entries.length);
    for (; rendered < end; rendered++) {
      const [k, v] = entries[rendered];
      body.append(jsonTree(v, k, depth + 1));
    }
    if (rendered < entries.length) {
      body.append(el("button", { class: "j-more", onclick: renderMore,
        text: `Mostrar ${Math.min(TREE_CHUNK, entries.length - rendered)} más (quedan ${entries.length - rendered})` }));
    }
  };
  details.addEventListener("toggle", () => {
    if (details.open && rendered === 0) renderMore();
  });
  if (depth <= TREE_AUTO_OPEN_DEPTH) details.open = true;
  return details;
}

function jsonText(value) {
  const out = JSON.stringify(value, null, 2);
  return out === undefined ? String(value) : out;
}

// S13: navigator.clipboard only exists on secure origins, so from
// http://192.168.x.x every copy failed. Select a hidden textarea and let the
// legacy command do the work.
function copyWithSelection(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  // Fixed position keeps the page from scrolling while the text is selected.
  area.style.position = "fixed";
  area.style.top = "0";
  area.style.left = "-9999px";
  area.style.opacity = "0";
  document.body.append(area);
  let ok = false;
  try {
    area.focus({ preventScroll: true });
    area.select();
    area.setSelectionRange(0, area.value.length);
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Permission denied or blocked: fall through to the selection copy.
    }
  }
  return copyWithSelection(text);
}

function copyButton(getValue) {
  const btn = el("button", { class: "copy", type: "button", text: "Copiar JSON" });
  btn.addEventListener("click", async () => {
    const ok = await copyText(jsonText(getValue()));
    btn.textContent = ok ? "Copiado ✓" : "No se pudo copiar";
    setTimeout(() => (btn.textContent = "Copiar JSON"), 1500);
  });
  return btn;
}

// File names for downloads: keep the execution id, drop what a filesystem (or a
// node name with spaces and accents) would turn into a mess.
function jsonFilename(...parts) {
  const slug = parts
    .map((p) => String(p ?? "")
      .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
      .replace(/[^\w.-]+/g, "_")
      .replace(/^_+|_+$/g, ""))
    .filter(Boolean)
    .join("-");
  return `${slug || "ejecucion"}.json`;
}

function downloadButton(getValue, filename) {
  const btn = el("button", { class: "copy", type: "button", text: "Descargar JSON" });
  btn.addEventListener("click", () => {
    const blob = new Blob([jsonText(getValue())], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: filename });
    document.body.append(a);
    a.click();
    a.remove();
    // Let the browser start the download before the URL goes away.
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  });
  return btn;
}

// Items of n8n are { json, binary?, pairedItem? }: show json, name the binaries.
function itemsView(items) {
  if (!Array.isArray(items)) return el("div", {}, jsonTree(items));
  if (items.length === 0) return el("div", { class: "muted", text: "Sin items." });
  const list = el("div", { class: "items" });
  let rendered = 0;
  const renderMore = () => {
    list.querySelector(":scope > .j-more")?.remove();
    const end = Math.min(rendered + TREE_CHUNK, items.length);
    for (; rendered < end; rendered++) {
      const it = items[rendered];
      const files = it?.binary ? Object.entries(it.binary).map(([k, b]) => `${k}: ${b?.fileName ?? b?.mimeType ?? "binario"}`) : [];
      list.append(el("div", { class: "item" },
        el("div", { class: "item-head", text: `Item ${rendered}` }),
        files.length ? el("div", { class: "muted", text: `Binarios: ${files.join(", ")}` }) : null,
        jsonTree(it?.json ?? it, null, 1)));
    }
    if (rendered < items.length) {
      list.append(el("button", { class: "j-more", onclick: renderMore, text: `Mostrar más items (quedan ${items.length - rendered})` }));
    }
  };
  renderMore();
  return list;
}

// ---------- stats and workflows ----------

// ---------- sync: fetch a fresh copy and merge it into the history ----------

const SYNC_STEP = {
  trigger: "Lanzando el flujo de n8n…", wait: "Esperando la copia de n8n…",
  fetch: "Trayendo copia de n8n…", import: "Integrando en el histórico…",
};
let lastStats = null;

function renderSync(s) {
  const btn = $("sync-btn");
  btn.disabled = s.running;
  btn.classList.toggle("running", s.running);
  $("sync-label").textContent = s.running ? SYNC_STEP[s.step] ?? "Actualizando…" : "Actualizar ahora";
  $("live-dot").className = `live-dot${s.running ? " running" : s.last && !s.last.ok ? " failed" : ""}`;
  btn.title = s.fetchConfigured
    ? "Ejecutar el flujo de copia de n8n e integrar la copia nueva en el histórico"
    : "No hay origen configurado: solo reintegra la última copia descargada";
  if (s.running) return;
  if (s.last && !s.last.ok) {
    $("live-txt").textContent = `Falló la actualización (${s.last.step === "import" ? "al integrarla" : "al traer la copia"}): ${s.last.error}`;
  } else if (lastStats) {
    renderLive(lastStats, s.last);
  }
}

function renderLive(stats, lastRun) {
  const range = `Datos del ${fmtDate(stats.firstStartedAt).slice(0, 10)} al ${fmtDate(stats.lastStartedAt).slice(0, 10)}`;
  const r = lastRun?.ok ? lastRun.result : null;
  const when = lastRun?.finishedAt ? fmtDate(lastRun.finishedAt.replace("T", " ").replace("Z", "")) : "";
  $("live-txt").textContent = r?.skipped
    ? `${range} · última comprobación ${when}: sin copias nuevas`
    : r
    ? `${range} · última actualización ${fmtDate(lastRun.finishedAt.replace("T", " ").replace("Z", ""))}: +${nf.format(r.added)} nuevas, ${nf.format(r.updated)} actualizadas`
    : range;
}

let watching = false;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Follows a running sync until it ends; then refreshes figures and lists in place.
async function watchSync({ refresh }) {
  if (watching) return;
  watching = true;
  let s = null;
  try {
    for (;;) {
      s = await api("/api/sync");
      renderSync(s);
      if (!s.running) break;
      refresh = true;
      await sleep(2000);
    }
    if (refresh && s.last?.ok) {
      await Promise.all([loadStats(), loadWorkflows()]);
      await loadExecutions(true);
      renderSync(s);
    }
  } catch {
    // Status is informative; the viewer keeps working without it.
  } finally {
    watching = false;
  }
}

$("sync-btn").addEventListener("click", async () => {
  try {
    const res = await fetch(API_BASE + "api/sync", { method: "POST", headers: { "x-viewer-action": "sync" } });
    if (!res.ok && res.status !== 409) throw new Error((await res.json().catch(() => ({}))).error ?? `HTTP ${res.status}`);
  } catch (e) {
    $("live-txt").textContent = `No se pudo iniciar la actualización: ${e.message}`;
    return;
  }
  watchSync({ refresh: true });
});

async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("st-total").textContent = nf.format(s.executions);
    $("st-workflows").textContent = nf.format(s.workflows);
    $("st-errors").textContent = nf.format((s.byStatus.error ?? 0) + (s.byStatus.crashed ?? 0));
    $("st-lost").textContent = nf.format(s.deleted);
    $("st-lost-lbl").textContent = s.importIssues
      ? `Borradas en n8n · ${nf.format(s.importIssues)} filas irrecuperables`
      : "Borradas en n8n";
    renderDisk(s.storage);
    lastStats = s;
    renderLive(s, null);
  } catch (e) {
    $("live-txt").textContent = `Error: ${e.message}`;
  }
}

// S7: current disk use of the history, with the estimate in the tooltip.
function renderDisk(st) {
  const val = $("st-disk");
  if (!st || !val) return;
  val.textContent = `${st.percentOfDisk}%`;
  val.title = [
    `Histórico: ${fmtBytes(st.dbBytes)}`,
    `Crecimiento estimado: ~${fmtBytes(st.estBytesPerDay)}/día`,
    `Proyección a 30 días: ~${fmtBytes(st.projectedBytes30d)} (${st.projectedPercentOfDisk30d}%)`,
    st.retentionDays > 0 ? `Retención: ${st.retentionDays} días` : "Retención: ilimitada",
    st.totalDiskBytes ? `Disco: ${fmtBytes(st.freeDiskBytes)} libres` : "",
  ].filter(Boolean).join(" · ");
  $("st-disk-lbl").textContent = st.retentionDays > 0
    ? `Disco (ret. ${st.retentionDays} d)`
    : "Disco (histórico)";
}

function renderWorkflows() {
  const filter = $("wf-filter").value.trim().toLowerCase();
  const visible = state.workflows
    .filter((w) => !filter || String(w.name).toLowerCase().includes(filter) || w.id.toLowerCase().includes(filter))
    .sort((a, b) => b.total - a.total || String(a.name).localeCompare(String(b.name)));
  $("wf-count").textContent = `${visible.length} de ${state.workflows.length}`;
  setChildren($("wf-list"), ...visible.map((w) => el("li", {},
    el("button", {
      type: "button",
      class: `wf-item${w.id === state.workflow ? " selected" : ""}${w.total === 0 ? " idle" : ""}`,
      onclick: () => selectWorkflow(w.id),
      title: w.id,
    },
    el("span", { class: "wf-name", text: w.name }),
    el("span", { class: "wf-meta" },
      w.active ? pill("on", "activo") : null,
      w.deleted ? pill("del", "borrado") : null,
      w.errors ? pill("err", nf.format(w.errors)) : null,
      el("span", { class: "count", text: nf.format(w.total) }))))));
  $("wf-all").classList.toggle("selected", !state.workflow);
}

async function loadWorkflows() {
  setChildren($("wf-list"), loading());
  try {
    state.workflows = await api("/api/workflows");
    renderWorkflows();
  } catch (e) {
    showError($("wf-list"), e);
  }
}

function selectWorkflow(id) {
  state.workflow = id;
  state.exec = null;
  state.node = null;
  writeHash();
  renderWorkflows();
  loadExecutions(true);
  renderDetailEmpty();
}

// ---------- executions ----------

function execQuery() {
  const p = new URLSearchParams();
  if (state.workflow) p.set("workflow", state.workflow);
  for (const [k, v] of Object.entries(state.filters)) if (v) p.set(k, v);
  if (state.nextBefore) p.set("before", state.nextBefore);
  return p;
}

// S10: only the newest load may touch the list. A slow response that lands
// after a newer one ("Cargar más" then a filter, or two quick filter submits)
// is dropped instead of appending stale rows over the fresh list.
let execLoadSeq = 0;

async function loadExecutions(reset) {
  const seq = ++execLoadSeq;
  const list = $("exec-list");
  if (reset) {
    state.nextBefore = null;
    state.shown = 0;
    setChildren(list, el("li", {}, loading()));
    $("exec-count").textContent = "";
  }
  const wf = state.workflows.find((w) => w.id === state.workflow);
  $("exec-title").textContent = wf ? wf.name : "Todas las ejecuciones";
  try {
    const page = await api(`/api/executions?${execQuery()}`);
    if (seq !== execLoadSeq) return;
    if (reset) setChildren(list);
    if (reset && page.items.length === 0) {
      list.append(el("li", { class: "empty", text: "Sin ejecuciones para estos filtros." }));
    }
    for (const e of page.items) list.append(execRow(e));
    state.shown += page.items.length;
    state.nextBefore = page.nextBefore;
    $("exec-more").hidden = !page.nextBefore;
    $("exec-count").textContent = `${nf.format(state.shown)}${page.nextBefore ? "+" : ""} mostradas`;
    markSelectedExecution();
  } catch (e) {
    if (seq !== execLoadSeq) return;
    if (!reset) {
      // S11: a failed "Cargar más" must not throw away what is already loaded;
      // keep the rows and warn at the bottom (one notice, not one per attempt).
      for (const old of list.querySelectorAll(":scope > .exec-error")) old.remove();
      list.append(el("li", { class: "exec-error" },
        el("div", { class: "error-box", text: `Error: ${e.message}` })));
      // Scroll the list itself (never the page) so the notice is visible.
      list.scrollTop = list.scrollHeight;
      return;
    }
    showError(list, e);
  }
}

function execRow(e) {
  return el("li", {},
    el("button", { type: "button", class: "exec-item", "data-id": e.id, onclick: () => selectExecution(e.id) },
      el("div", { class: "exec-line" },
        statusPill(e.status),
        el("span", { class: "exec-id", text: `#${e.id}` }),
        el("span", { class: "exec-date", text: fmtDate(e.startedAt) }),
        el("span", { class: "exec-dur", text: fmtMs(e.durationMs) })),
      state.workflow ? null : el("div", { class: "exec-wf", text: e.workflowName ?? e.workflowId }),
      e.errorMessage ? el("div", { class: "exec-err", text: `${e.errorNode ? `${e.errorNode}: ` : ""}${e.errorMessage}` }) : null,
      e.deletedAt ? el("div", { class: "exec-note", text: `Borrada en n8n el ${fmtDate(e.deletedAt)}` }) : null,
      e.dataSize == null ? el("div", { class: "exec-note", text: "Sin datos recuperables" }) : null));
}

function markSelectedExecution() {
  for (const b of document.querySelectorAll(".exec-item")) {
    b.classList.toggle("selected", b.dataset.id === String(state.exec));
  }
}

// ---------- execution detail ----------

function renderDetailEmpty() {
  $("detail").hidden = true;
  $("detail-empty").hidden = false;
}

function nodePanelEmpty() {
  return el("div", { class: "empty" }, el("p", { text: "Selecciona una caja para ver su entrada y su salida." }));
}

async function selectExecution(id, node = null, run = 0) {
  state.exec = String(id);
  state.node = node;
  state.run = run;
  state.execData = null;
  writeHash();
  markSelectedExecution();
  setWorkflowsCollapsed(true);
  $("detail-empty").hidden = true;
  $("detail").hidden = false;
  setChildren($("exec-head"), loading());
  setChildren($("node-list"));
  setChildren($("node-panel"), nodePanelEmpty());
  try {
    const e = await api(`/api/executions/${encodeURIComponent(id)}`);
    if (state.exec !== String(id)) return;
    renderExecution(e);
    if (node) selectNode(node, run);
  } catch (err) {
    showError($("exec-head"), err);
  }
}

function renderExecution(e) {
  state.execData = e;
  state.runs = Object.fromEntries(e.nodes.map((n) => [n.name, n.runs.length]));
  const facts = [
    ["Inicio", fmtDate(e.startedAt)],
    ["Fin", fmtDate(e.stoppedAt)],
    ["Duración", fmtMs(e.durationMs)],
    ["Modo", e.mode ?? "—"],
    ["Tamaño", fmtBytes(e.dataSize)],
    ["Última caja", e.lastNodeExecuted ?? "—"],
  ];
  setChildren($("exec-head"), 
    el("div", { class: "exec-title" },
      statusPill(e.status),
      el("strong", { text: `#${e.id}` }),
      el("span", { class: "wf", text: e.workflowName ?? e.workflowId }),
      N8N_URL ? el("a", { href: `${N8N_URL}/workflow/${encodeURIComponent(e.workflowId)}/executions/${e.id}`,
        target: "_blank", rel: "noopener", text: "Abrir en n8n ↗" }) : null),
    el("dl", { class: "facts" }, facts.map(([k, v]) => [el("dt", { text: k }), el("dd", { text: v, title: v })])),
    e.error ? el("div", { class: "error-box", text: `${e.error.node ? `${e.error.node}: ` : ""}${e.error.message}` }) : null,
    e.deletedAt ? el("div", { class: "warn-box", text: `n8n marcó esta ejecución como borrada el ${fmtDate(e.deletedAt)}.` }) : null,
    e.dataMissing ? el("div", { class: "warn-box", text: "Los datos de esta ejecución no se pudieron recuperar de la copia de la base de datos." }) : null,
    e.parseError ? el("div", { class: "warn-box", text: `Datos dañados en la copia: ${e.parseError}` }) : null);

  const executed = e.nodes.filter((n) => n.executed);
  const skipped = e.nodes.filter((n) => !n.executed);
  setChildren($("node-list"), 
    ...executed.map((n, i) => nodeRow(n, i + 1)),
    skipped.length
      ? el("li", { class: "skipped" },
          el("details", {},
            el("summary", { text: `No ejecutadas (${skipped.length})` }),
            el("ul", {}, skipped.map((n) => el("li", {},
              el("span", { text: n.name }), n.type ? pill("type", shortType(n.type)) : null)))))
      : null);
  applyView();
}

function nodeRow(n, position) {
  const last = n.runs[n.runs.length - 1] ?? {};
  const status = n.runs.some((r) => r.status === "error") ? "error" : last.status;
  const out = n.runs.reduce((sum, r) => sum + (r.itemsOut ?? []).reduce((a, b) => a + b, 0), 0);
  const totalTime = n.runs.reduce((sum, r) => sum + (r.executionTime ?? 0), 0);
  const failed = n.runs.find((r) => r.error);
  return el("li", {},
    el("button", { type: "button", class: "node-item", "data-node": n.name, onclick: () => selectNode(n.name, 0) },
      el("span", { class: `pos ${status ?? ""}`, text: position }),
      el("span", { class: "node-name", text: n.name, title: n.name }),
      el("span", { class: "node-meta" },
        n.type ? pill("type", shortType(n.type)) : null,
        `${nf.format(out)} items · ${fmtMs(totalTime)}${n.runs.length > 1 ? ` · ${n.runs.length} runs` : ""}`)),
    failed ? el("div", { class: "node-err", text: failed.error, title: failed.error }) : null);
}

function markSelectedNode() {
  for (const b of document.querySelectorAll(".node-item, .dg-node")) {
    b.classList.toggle("selected", b.dataset.node === state.node);
  }
  $("detail-body").classList.toggle("no-node", !state.node);
}

// ---------- view switch: list or n8n-style diagram ----------

function applyView() {
  const isDiagram = state.view === "diagram";
  $("view-list").classList.toggle("selected", !isDiagram);
  $("view-diagram").classList.toggle("selected", isDiagram);
  $("detail-body").classList.toggle("diagram", isDiagram);
  $("canvas").hidden = !isDiagram;
  if (isDiagram && state.execData) diagram.render(state.execData);
  markSelectedNode();
}

function setView(view) {
  state.view = view;
  writePref("n8nViewerView", view);
  applyView();
}

$("view-list").addEventListener("click", () => setView("list"));
$("view-diagram").addEventListener("click", () => setView("diagram"));

const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) if (c) node.append(c);
  return node;
}

// Short glyph for a node type: "httpRequest" -> "HR", "if" -> "IF".
function glyph(type) {
  const t = shortType(type).replace(/Trigger$/, "").replace(/Tool$/, "");
  const caps = t.match(/[A-Z]/g) ?? [];
  const letters = caps.length ? (t[0] ?? "?") + caps.join("") : t.slice(0, 2);
  return (letters || "?").slice(0, 3).toUpperCase();
}

const ellipsis = (s, max) => (s.length > max ? `${s.slice(0, max - 1)}…` : s);

// Pan and zoom move the SVG viewBox; the drawing itself is never rebuilt.
const diagram = (() => {
  let svgRoot = null;
  let bounds = null;
  let vb = null;
  let model = null;
  // Positions moved by the user, per workflow, kept while the page is open.
  const moved = new Map();
  let nodeEls = new Map();
  let edgeEls = [];

  const size = () => {
    const c = $("canvas");
    return [Math.max(c.clientWidth, 1), Math.max(c.clientHeight, 1)];
  };
  const apply = () => svgRoot?.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  const scale = () => vb.w / size()[0];

  function fit() {
    if (!svgRoot || !bounds) return;
    const [cw, ch] = size();
    // Never zoom in past 1:1 so small workflows keep a natural size.
    const k = Math.max(bounds.w / cw, bounds.h / ch, 1);
    const w = cw * k;
    const h = ch * k;
    vb = { x: bounds.x - (w - bounds.w) / 2, y: bounds.y - (h - bounds.h) / 2, w, h };
    apply();
  }

  function zoom(factor, px = null, py = null) {
    if (!vb) return;
    const [cw, ch] = size();
    const fx = (px ?? cw / 2) / cw;
    const fy = (py ?? ch / 2) / ch;
    const w = Math.min(Math.max(vb.w * factor, 200), 40000);
    const h = vb.h * (w / vb.w);
    vb = { x: vb.x + fx * (vb.w - w), y: vb.y + fy * (vb.h - h), w, h };
    apply();
  }

  function edgeLabel(x) {
    if (!x.active || x.items == null) return null;
    const text = `${nf.format(x.items)} item${x.items === 1 ? "" : "s"}`;
    const w = 14 + text.length * 6.4;
    return svg("g", { class: "dg-edge-label", transform: `translate(${x.mid[0]} ${x.mid[1]})` },
      svg("rect", { x: -w / 2, y: -10, width: w, height: 20, rx: 10 }),
      svg("text", { x: 0, y: 0, text }));
  }

  function drawEdge(x) {
    const main = x.type === "main";
    const cls = `dg-edge${x.active ? " active" : ""}${main ? "" : " sub"}`;
    return svg("g", { class: "dg-edge-g" },
      svg("path", { class: "dg-edge-hit", d: x.path }),
      svg("path", { class: cls, d: x.path, "marker-end": main ? `url(#dg-arrow${x.active ? "-on" : ""})` : null }),
      edgeLabel(x));
  }

  function updateEdge(i) {
    const x = edgeEls[i].edge;
    const g = edgeEls[i].el;
    g.querySelectorAll("path").forEach((p) => p.setAttribute("d", x.path));
    g.querySelector(".dg-edge-label")?.setAttribute("transform", `translate(${x.mid[0]} ${x.mid[1]})`);
  }

  function drawNode(n) {
    const badge = n.status === "error" ? "!" : n.status === "success" ? "\u2713" : null;
    const sub = [shortType(n.type), n.runs > 1 ? `${n.runs} runs` : null].filter(Boolean).join(" \u00b7 ");
    const outs = Math.max(1, ...edgeEls.filter((e) => e.edge.from === n.name && e.edge.type === "main").map((e) => e.edge.outCount));
    const ins = model.edges.some((e) => e.to === n.name && e.type === "main")
      ? Math.max(1, ...model.edges.filter((e) => e.to === n.name && e.type === "main").map((e) => e.inCount))
      : 0;
    const handles = [
      ...Array.from({ length: ins }, (_, i) => svg("circle", { class: "dg-handle", cx: 0, cy: (n.h * (i + 1)) / (ins + 1), r: 5 })),
      ...Array.from({ length: outs }, (_, i) => svg("circle", { class: "dg-handle", cx: n.w, cy: (n.h * (i + 1)) / (outs + 1), r: 5 })),
    ];
    return svg("g", { class: `dg-node ${n.status}`, "data-node": n.name, transform: `translate(${n.x} ${n.y})` },
      svg("title", { text: `${n.name}${n.error ? `\n${n.error}` : ""}${n.executed ? "" : "\n(no se ejecut\u00f3)"}\nArrastra para mover` }),
      svg("rect", { class: "dg-box", width: n.w, height: n.h, rx: 16 }),
      svg("text", { class: "dg-glyph", x: n.w / 2, y: n.h / 2 - 6, text: glyph(n.type) }),
      n.executed ? svg("text", { class: "dg-count", x: n.w / 2, y: n.h / 2 + 20, text: `${nf.format(n.itemsOut)} items` }) : null,
      handles,
      badge ? svg("circle", { class: "dg-badge", cx: n.w - 6, cy: 6, r: 11 }) : null,
      badge ? svg("text", { class: "dg-badge-txt", x: n.w - 6, y: 6, text: badge }) : null,
      svg("text", { class: "dg-label", x: n.w / 2, y: n.h + 20, text: ellipsis(n.name, 22) }),
      svg("text", { class: "dg-sub", x: n.w / 2, y: n.h + 35, text: ellipsis(sub, 26) }));
  }

  function render(e) {
    const key = e.workflowId ?? "";
    if (!moved.has(key)) moved.set(key, {});
    model = buildDiagram(e, moved.get(key));
    model.key = key;
    bounds = model.bounds;

    const defs = svg("defs", {},
      ["", "-on"].map((s) => svg("marker", { id: `dg-arrow${s}`, viewBox: "0 0 10 10", refX: 9, refY: 5,
        markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      svg("path", { class: `dg-arrow${s}`, d: "M 0 0 L 10 5 L 0 10 z" }))));
    edgeEls = model.edges.map((edge) => ({ edge, el: drawEdge(edge) }));
    nodeEls = new Map(model.nodes.map((n) => [n.name, { node: n, el: drawNode(n) }]));

    const root = svg("svg", { class: "dg", xmlns: SVG_NS }, defs,
      svg("g", {}, edgeEls.map((x) => x.el)),
      svg("g", {}, [...nodeEls.values()].map((x) => x.el)));
    svgRoot?.remove();
    svgRoot = root;
    $("canvas").prepend(root);
    if (!model.nodes.length) {
      root.append(svg("text", { class: "dg-sub", x: bounds.x + bounds.w / 2, y: bounds.y + bounds.h / 2,
        text: "Esta ejecuci\u00f3n no guarda la posici\u00f3n de las cajas." }));
    }
    markSelectedNode();
    requestAnimationFrame(fit);
  }

  function moveNode(name, x, y) {
    const entry = nodeEls.get(name);
    entry.node.x = x;
    entry.node.y = y;
    entry.el.setAttribute("transform", `translate(${x} ${y})`);
    moved.get(model.key)[name] = [x, y];
    edgeEls.forEach((item, i) => {
      if (item.edge.from !== name && item.edge.to !== name) return;
      item.edge = routeEdge(item.edge, nodeEls.get(item.edge.from).node, nodeEls.get(item.edge.to).node);
      updateEdge(i);
    });
  }

  function resetPositions() {
    if (!model || !state.execData) return;
    moved.set(model.key, {});
    render(state.execData);
  }

  // Drag a node to move it (its arrows follow); drag the background to pan;
  // wheel zooms around the pointer. A press without movement is a click.
  const canvas = $("canvas");
  let drag = null;
  canvas.addEventListener("pointerdown", (ev) => {
    if (!vb || ev.button !== 0 || ev.target.closest(".canvas-tools, .canvas-legend")) return;
    const g = ev.target.closest(".dg-node");
    const base = { px: ev.clientX, py: ev.clientY, moved: false };
    if (g) {
      const n = nodeEls.get(g.dataset.node).node;
      drag = { ...base, kind: "node", name: n.name, x: n.x, y: n.y };
      g.parentNode.append(g);
    } else {
      drag = { ...base, kind: "pan", vb: { ...vb } };
    }
    canvas.setPointerCapture(ev.pointerId);
    canvas.classList.add("dragging");
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.px;
    const dy = ev.clientY - drag.py;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    drag.moved = true;
    const k = scale();
    if (drag.kind === "node") {
      // Snap to n8n's 20px grid.
      moveNode(drag.name, Math.round((drag.x + dx * k) / 20) * 20, Math.round((drag.y + dy * k) / 20) * 20);
    } else {
      vb = { ...vb, x: drag.vb.x - dx * k, y: drag.vb.y - dy * k };
      apply();
    }
  });
  const endDrag = () => {
    if (drag?.kind === "node" && !drag.moved) {
      const n = nodeEls.get(drag.name)?.node;
      if (n?.executed) selectNode(n.name, 0);
    }
    drag = null;
    canvas.classList.remove("dragging");
  };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", () => {
    drag = null;
    canvas.classList.remove("dragging");
  });
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = canvas.getBoundingClientRect();
    zoom(ev.deltaY > 0 ? 1.15 : 1 / 1.15, ev.clientX - r.left, ev.clientY - r.top);
  }, { passive: false });
  $("zoom-in").addEventListener("click", () => zoom(1 / 1.25));
  $("zoom-out").addEventListener("click", () => zoom(1.25));
  $("zoom-fit").addEventListener("click", fit);
  $("zoom-reset").addEventListener("click", resetPositions);
  // Keep the drawing undistorted when the panel changes size.
  new ResizeObserver(() => {
    if (!vb) return;
    const [cw, ch] = size();
    vb = { ...vb, h: vb.w * (ch / cw) };
    apply();
  }).observe(canvas);

  return { render, fit };
})();

// ---------- node input / output ----------

async function selectNode(name, run) {
  state.node = name;
  state.run = run;
  writeHash();
  markSelectedNode();
  const panel = $("node-panel");
  setChildren(panel, loading());
  const execId = state.exec;
  try {
    const io = await api(`/api/executions/${encodeURIComponent(execId)}/nodes/${encodeURIComponent(name)}?run=${run}`);
    if (state.exec !== execId || state.node !== name) return;
    renderNode(io);
  } catch (e) {
    showError(panel, e);
  }
}

const countInput = (input) => input.reduce((s, i) => s + (Array.isArray(i.items) ? i.items.length : 0), 0);
const countOutput = (output) => Object.values(output ?? {})
  .reduce((s, branches) => s + (branches ?? []).reduce((a, b) => a + (b ?? []).length, 0), 0);

function renderNode(io) {
  const runsCount = state.runs[io.node] ?? 1;
  const tabs = [
    ["Entrada", countInput(io.input), () => inputView(io.input), io.input, "entrada"],
    ["Salida", countOutput(io.output), () => outputView(io.output), io.output, "salida"],
    ["Parámetros", null, () => jsonTree(io.parameters ?? {}, null, 0), io.parameters, "parametros"],
  ];
  if (io.error) tabs.push(["Error", null, () => jsonTree(io.error, null, 0), io.error, "error"]);

  const body = el("div", { class: "tab-body" });
  const bar = el("div", { class: "tabs" });
  const show = (i) => {
    bar.querySelectorAll(".tab").forEach((t, j) => t.classList.toggle("selected", i === j));
    const [, , render, raw, slug] = tabs[i];
    // S14: copy and download share the same JSON; both are named after the execution.
    setChildren(body,
      el("div", { class: "tab-tools" },
        copyButton(() => raw),
        downloadButton(() => raw, jsonFilename("ejecucion", state.exec, io.node, slug, `run${io.run}`))),
      render());
  };
  tabs.forEach(([label, n], i) => bar.append(el("button", { type: "button", class: "tab", onclick: () => show(i) },
    label, n == null ? null : el("span", { class: "n", text: nf.format(n) }))));

  setChildren($("node-panel"), 
    el("div", { class: "node-head" },
      statusPill(io.status),
      el("strong", { text: io.node }),
      io.type ? pill("type", shortType(io.type)) : null,
      el("span", { class: "muted", text: `${fmtMs(io.executionTime)}${io.startTime ? ` · ${dateFmt.format(new Date(io.startTime)).replace(",", "")}` : ""}` }),
      runsCount > 1
        ? el("span", { class: "runs" }, Array.from({ length: runsCount }, (_, r) =>
            el("button", { type: "button", class: `run${r === io.run ? " selected" : ""}`, text: `run ${r}`, onclick: () => selectNode(io.node, r) })))
        : null),
    bar,
    body);
  show(io.error ? tabs.length - 1 : 0);
}

function inputView(input) {
  if (!input.length) return el("div", { class: "muted", text: "Sin entrada: es un disparador o no recibió datos de otra caja." });
  return el("div", {}, input.map((i) => {
    if (i.from === "inputOverride") {
      return el("section", { class: "io-block" },
        el("h4", { text: "Entrada registrada por n8n" }),
        outputView(i.items));
    }
    const items = Array.isArray(i.items) ? i.items : [];
    return el("section", { class: "io-block" },
      el("h4", {},
        `Entrada ${i.inputIndex} ←`,
        el("button", { type: "button", class: "link", text: i.from.node, onclick: () => selectNode(i.from.node, i.from.run) }),
        `· salida ${i.from.output}${i.from.run ? ` · run ${i.from.run}` : ""} · ${nf.format(items.length)} items`),
      itemsView(items));
  }));
}

function outputView(output) {
  const types = Object.entries(output ?? {});
  if (!types.length) return el("div", { class: "muted", text: "Sin salida registrada." });
  return el("div", {}, types.map(([type, branches]) =>
    (branches ?? []).map((items, b) => el("section", { class: "io-block" },
      el("h4", { text: `${type === "main" ? "Salida" : type} ${b} · ${nf.format((items ?? []).length)} items` }),
      itemsView(items ?? [])))));
}

// ---------- wiring ----------

$("wf-filter").addEventListener("input", renderWorkflows);
$("wf-all").addEventListener("click", () => selectWorkflow(""));
$("exec-more").addEventListener("click", () => loadExecutions(false));
$("filters").addEventListener("submit", (ev) => {
  ev.preventDefault();
  state.filters = Object.fromEntries(new FormData(ev.target));
  writeHash();
  loadExecutions(true);
});
$("f-clear").addEventListener("click", () => {
  $("filters").reset();
  state.filters = {};
  writeHash();
  loadExecutions(true);
});

async function applyHash() {
  const h = readHash();
  const workflowChanged = h.workflow !== state.workflow;
  const filtersChanged = !sameFilters(h.filters, state.filters);
  state.workflow = h.workflow;
  state.filters = h.filters;
  fillFilters(h.filters);
  renderWorkflows();
  if (workflowChanged || filtersChanged || !$("exec-list").children.length) await loadExecutions(true);
  if (!h.exec) return renderDetailEmpty();
  if (h.exec !== state.exec) return selectExecution(h.exec, h.node, h.run);
  if (h.node && (h.node !== state.node || h.run !== state.run)) return selectNode(h.node, h.run);
}

// S8/S9: branding. /api/config (the settings UI) wins; the optional static
// brand.json next to index.html is the fallback.
async function applyBrand() {
  let brand = null;
  try {
    const cfg = await api("/api/config");
    if (cfg && cfg.brand) brand = cfg.brand;
  } catch { /* no config (old backend) or API down: try the static file */ }
  if (!brand) {
    try {
      const r = await fetch("brand.json", { cache: "no-cache" });
      if (r.ok) brand = await r.json();
    } catch { /* no brand: keep defaults */ }
  }
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
    document.title = [brand.title, brand.subtitle].filter(Boolean).join(" · ");
  }
}

// ---------- S9: settings (retention + brand) ----------

const RETENTION_CUSTOM = "custom";

function setSettingsMsg(text, isErr) {
  const msg = $("settings-msg");
  msg.textContent = text || "";
  msg.classList.toggle("err", !!isErr);
}

const FOCUSABLE_SELECTOR = "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])";

// S12: aria-modal tells assistive tech that the page behind is inert, so focus
// has to move into the dialog, stay there while it is open, and come back to
// the button that opened it.
async function openSettings() {
  $("settings-overlay").hidden = false;
  setSettingsMsg("");
  $("settings-save").disabled = true;
  $("cfg-retention").focus({ preventScroll: true });
  try {
    const cfg = await api("/api/config");
    const days = cfg.retentionDays ?? 0;
    const sel = $("cfg-retention");
    if ([...sel.options].some((o) => o.value === String(days))) sel.value = String(days);
    else { sel.value = RETENTION_CUSTOM; $("cfg-retention-custom").value = days; }
    $("cfg-retention-custom").hidden = sel.value !== RETENTION_CUSTOM;
    const b = cfg.brand || {};
    $("cfg-title").value = b.title || "";
    $("cfg-subtitle").value = b.subtitle || "";
    // S9: remember what the server has; this session starts with no logo change.
    storedLogo = b.logo || null;
    pendingLogo = null;
    logoCleared = false;
    $("cfg-logo-file").value = "";
    showLogoPreview(storedLogo);
  } catch (e) {
    setSettingsMsg(`No se pudieron cargar: ${e.message}`, true);
    return;
  }
  $("settings-save").disabled = false;
}

function closeSettings() {
  $("settings-overlay").hidden = true;
  $("settings-btn").focus({ preventScroll: true });
}

// Tab circulates inside the dialog instead of reaching the page behind it.
function trapFocus(ev) {
  const modal = document.querySelector("#settings-overlay .modal");
  if (!modal) return;
  const items = [...modal.querySelectorAll(FOCUSABLE_SELECTOR)]
    .filter((n) => !n.disabled && n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const current = document.activeElement;
  const outside = !modal.contains(current);
  if (ev.shiftKey ? current === first || outside : current === last || outside) {
    ev.preventDefault();
    (ev.shiftKey ? last : first).focus({ preventScroll: true });
  }
}

function showLogoPreview(dataUrl) {
  const img = $("cfg-logo-preview");
  const clear = $("cfg-logo-clear");
  if (dataUrl) { img.src = dataUrl; img.hidden = false; clear.hidden = false; }
  else { img.hidden = true; img.removeAttribute("src"); clear.hidden = true; }
}

let storedLogo = null; // logo the server has right now (captured when the dialog opens)
let pendingLogo = null; // dataURL chosen in this session, not yet saved
let logoCleared = false; // the user pressed "Quitar" in this session

async function saveSettings() {
  const sel = $("cfg-retention");
  let days;
  if (sel.value === RETENTION_CUSTOM) {
    days = parseInt($("cfg-retention-custom").value, 10);
    if (!Number.isInteger(days) || days < 1 || days > 36500) {
      return setSettingsMsg("Días personalizados: entero entre 1 y 36500.", true);
    }
  } else {
    days = parseInt(sel.value, 10);
  }
  const brand = {
    title: $("cfg-title").value.trim(),
    subtitle: $("cfg-subtitle").value.trim(),
  };
  // S9: only touch the logo when the user changed it. Sending null for "nothing
  // picked in this session" made the backend delete the stored logo.
  if (pendingLogo) brand.logo = pendingLogo;
  else if (logoCleared) brand.logo = null; // "Quitar" is the explicit clear
  else if (storedLogo) brand.logo = storedLogo; // unchanged: keep what is stored
  // Nothing stored and nothing picked: omit the field and let the server decide.
  const payload = { retentionDays: days, brand };
  $("settings-save").disabled = true;
  setSettingsMsg("");
  try {
    const res = await fetch(API_BASE + "api/config", {
      method: "POST",
      headers: { "x-viewer-action": "config", "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);
    storedLogo = brand.logo ?? null;
    pendingLogo = null;
    logoCleared = false;
    await applyBrand();
    await loadStats(); // refresh the disk card label (retention)
    setSettingsMsg("Guardado.");
    setTimeout(closeSettings, 400);
  } catch (e) {
    setSettingsMsg(`Error al guardar: ${e.message}`, true);
  } finally {
    $("settings-save").disabled = false;
  }
}

function initSettings() {
  $("settings-btn").addEventListener("click", openSettings);
  $("settings-close").addEventListener("click", closeSettings);
  $("settings-save").addEventListener("click", saveSettings);
  $("settings-overlay").addEventListener("click", (e) => {
    if (e.target.id === "settings-overlay") closeSettings();
  });
  document.addEventListener("keydown", (e) => {
    // S12: while the dialog is open it owns the keyboard. The focus-mode Escape
    // handler above skips the key when this overlay is visible.
    if ($("settings-overlay").hidden) return;
    if (e.key === "Escape") return closeSettings();
    if (e.key === "Tab") return trapFocus(e);
  });
  $("cfg-retention").addEventListener("change", (e) => {
    $("cfg-retention-custom").hidden = e.target.value !== RETENTION_CUSTOM;
  });
  $("cfg-logo-file").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
      setSettingsMsg("El logo pesa más de 2 MB; elegí uno más chico.", true);
      e.target.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      pendingLogo = reader.result;
      logoCleared = false;
      showLogoPreview(pendingLogo);
      setSettingsMsg("");
    };
    reader.readAsDataURL(file);
  });
  $("cfg-logo-clear").addEventListener("click", () => {
    pendingLogo = null;
    logoCleared = true;
    $("cfg-logo-file").value = "";
    showLogoPreview(null);
  });
}

window.addEventListener("popstate", applyHash);

(async () => {
  initSettings();
  await Promise.all([loadStats(), loadWorkflows()]);
  await applyBrand();
  await applyHash();
  // Show the last sync, and follow one that is already running (e.g. the scheduled one).
  watchSync({ refresh: false });
})();
