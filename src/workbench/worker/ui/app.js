"use strict";

// ------------------------------------------------------------------ api
let csrf = "";

async function api(method, path, body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  const resp = await fetch(path, {
    method, headers, credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await resp.json(); } catch (_) { /* no body */ }
  if (resp.status === 401 && !path.endsWith("/login")) showLogin();
  if (!resp.ok) {
    const d = data && data.detail;
    const err = new Error(typeof d === "string" ? d : `HTTP ${resp.status}`);
    err.status = resp.status;
    throw err;
  }
  return data;
}

// ----------------------------------------------------------- workbench info
/** Display name of a workbench (denied requests never told us theirs). */
function workbenchName(entry) { return entry.name || "workbench"; }

/** Connection state of a joined workbench: {state: "connected" | "down" | "error", name}. */
async function checkConnection(baseUrl) {
  try {
    const r = await api("GET", `/admin/workbench/status?base_url=${encodeURIComponent(baseUrl)}`);
    return { state: r.connection, name: r.name };
  } catch (_) {
    return { state: "error", name: "" };   // our own worker server failed to answer
  }
}

// ---------------------------------------------------------------- state
const DENIED_KEY = "worker.denied";   // join requests the workbench rejected (the server forgets them)
let entries = [];                     // [{base_url, state, message}]

function loadDenied() {
  try { return JSON.parse(localStorage.getItem(DENIED_KEY)) || []; } catch (_) { return []; }
}
function saveDenied(list) {
  try { localStorage.setItem(DENIED_KEY, JSON.stringify(list)); } catch (_) { /* private mode */ }
}
function addDenied(baseUrl, message) {
  const list = loadDenied().filter(d => d.base_url !== baseUrl);
  list.push({ base_url: baseUrl, message });
  saveDenied(list);
}
function normUrl(u) { return u.trim().replace(/\/+$/, ""); }

/** The workbench URL inside a join token (only to label a denied request; the server does the real decoding). */
function tokenUrl(token) {
  try {
    const b64 = token.replace(/\s+/g, "").replace(/^wbj1\./, "").replace(/-/g, "+").replace(/_/g, "/");
    const url = JSON.parse(atob(b64 + "=".repeat((4 - b64.length % 4) % 4))).u;
    return typeof url === "string" ? normUrl(url) : "";
  } catch (_) { return ""; }
}

// ------------------------------------------------------------------ dom
const $ = id => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";

const ICONS = {   // 24x24 stroke icons
  connected: ["M20 6 9 17l-5-5"],
  down: ["M18 6 6 18", "M6 6l12 12"],
  error: ["M12 8v5", "M12 16.5v.01", "M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"],
  pending: ["M21 12a9 9 0 1 1-6.2-8.6"],
  denied: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M5.6 5.6l12.8 12.8"],
};
const TITLES = {
  connected: "Connected", down: "Workbench unreachable", error: "Connection problem",
  pending: "Join request pending", denied: "Join request denied",
};

function icon(state) {
  const svg = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
      "stroke-width": "2.2", "stroke-linecap": "round", "stroke-linejoin": "round" })) svg.setAttribute(k, v);
  for (const d of ICONS[state]) {
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", d);
    svg.appendChild(p);
  }
  return svg;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function render() {
  const list = $("wb-list");
  list.replaceChildren(...entries.map(entry => {
    const li = el("li", "wb");
    const status = el("span", `status ${entry.state}`);
    status.title = TITLES[entry.state];
    status.appendChild(icon(entry.state));
    const rm = el("button", "remove", "×");
    rm.type = "button";
    rm.title = "Disconnect and remove";
    rm.setAttribute("aria-label", "Remove workbench");
    rm.addEventListener("click", () => remove(entry));
    li.append(status, el("span", "name", workbenchName(entry)), rm, el("span", "url", entry.base_url));
    if (entry.state === "denied") li.appendChild(el("span", "note", entry.message || "Join request denied"));
    return li;
  }));
  $("wb-empty").hidden = entries.length > 0;
}

// -------------------------------------------------------------- actions
async function refresh() {
  let server;
  try { server = await api("GET", "/admin/workbench"); } catch (_) { return; }
  const next = [];
  await Promise.all(server.map(async w => {
    let state = "pending", name = w.name;
    if (w.status === "pending") {
      try {
        const r = await api("POST", `/admin/workbench/join/poll?base_url=${encodeURIComponent(w.base_url)}`);
        if (r.status === "rejected") { addDenied(w.base_url, r.message); return; }
        if (r.status === "joined") { const c = await checkConnection(w.base_url); state = c.state; name = c.name || name; }
      } catch (_) { state = "error"; }
    } else {
      const c = await checkConnection(w.base_url);
      state = c.state; name = c.name || name;
    }
    next.push({ base_url: w.base_url, state, name });
  }));
  next.sort((a, b) => a.base_url.localeCompare(b.base_url));
  const live = new Set(next.map(e => e.base_url));
  const denied = loadDenied().filter(d => !live.has(d.base_url))
    .map(d => ({ ...d, state: "denied" }));
  entries = [...next, ...denied];
  render();
}

async function remove(entry) {
  if (entry.state === "denied") {
    saveDenied(loadDenied().filter(d => d.base_url !== entry.base_url));
  } else {
    try { await api("DELETE", `/admin/workbench?base_url=${encodeURIComponent(entry.base_url)}`); }
    catch (e) { if (e.status !== 404) return; }
  }
  entries = entries.filter(e => e !== entry);
  render();
}

function formError(id, msg) { const p = $(id); p.textContent = msg || ""; p.hidden = !msg; }

// the join token is the default way to join; URL + invite code is the manual fallback
let manualMode = false;
$("join-mode").addEventListener("click", () => {
  manualMode = !manualMode;
  $("join-token-fields").hidden = manualMode;
  $("join-manual-fields").hidden = !manualMode;
  $("join-mode").textContent = manualMode ? "Use a join token instead" : "Enter URL and invite code instead";
  formError("join-error");
});

$("join-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const form = ev.target, btn = form.querySelector("button");
  const token = manualMode ? "" : form.join_token.value.trim();
  const base_url = token ? tokenUrl(token) : normUrl(form.base_url.value);
  const invite_code = form.invite_code.value.trim();
  formError("join-error");
  if (!token && !(base_url && invite_code)) {
    formError("join-error", manualMode ? "Enter the workbench URL and the invite code."
                                       : "Enter a join token.");
    return;
  }
  btn.disabled = true;
  try {
    await api("POST", "/admin/workbench/join", token ? { join_token: token } : { base_url, invite_code });
    saveDenied(loadDenied().filter(d => d.base_url !== base_url));
    form.reset();
    await refresh();
  } catch (e) {
    if (e.status === 403 && base_url) {   // the workbench answered and said no: keep it in the list
      addDenied(base_url, e.message);
      form.reset();
      await refresh();
    } else {
      formError("join-error", e.message);
    }
  } finally { btn.disabled = false; }
});

// ---------------------------------------------------------------- login
let timer = null;

function showLogin() {
  clearInterval(timer);
  $("app-view").hidden = true;
  $("login-view").hidden = false;
}

async function showApp() {
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  // a worker with extra pages (a HumanWorker has an inbox) links to them
  try { $("inbox-link").hidden = !(await api("GET", "/admin/config")).features.includes("inbox"); } catch (_) { /* no link */ }
  await refresh();
  clearInterval(timer);
  timer = setInterval(refresh, 5000);
}

$("login-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const form = ev.target;
  formError("login-error");
  try {
    const r = await api("POST", "/admin/login",
      { username: form.username.value, password: form.password.value });
    csrf = r.csrf_token;
    form.reset();
    await showApp();
  } catch (e) {
    formError("login-error", e.status === 401 ? "Wrong username or password." : e.message);
  }
});

$("logout").addEventListener("click", async () => {
  try { await api("POST", "/admin/logout"); } catch (_) { /* already logged out */ }
  csrf = "";
  showLogin();
});

(async function init() {
  try {
    const s = await api("GET", "/admin/session");   // the session cookie survives reloads
    if (s.authenticated) { csrf = s.csrf_token; return showApp(); }
  } catch (_) { /* fall through */ }
  showLogin();
})();
