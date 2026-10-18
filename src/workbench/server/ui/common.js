"use strict";

// Shared by index.html (task list) and task.html (task tree).

// ------------------------------------------------------------------ api
let csrf = "";
let onUnauthorized = () => {};   // each page decides what a lost session means

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
  if (resp.status === 401 && !path.endsWith("/login")) onUnauthorized();
  if (!resp.ok) {
    const d = data && data.detail;
    const err = new Error(typeof d === "string" ? d : `HTTP ${resp.status}`);
    err.status = resp.status;
    throw err;
  }
  return data;
}

/** Resumes the session from the cookie. Returns false if the user has to log in. */
async function resumeSession() {
  try {
    const s = await api("GET", "/admin/session");
    if (s.authenticated) { csrf = s.csrf_token; return true; }
  } catch (_) { /* fall through */ }
  return false;
}

// ------------------------------------------------------------------ dom
const $ = id => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function svgEl(tag, attrs = {}, cls) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (cls) e.setAttribute("class", cls);
  return e;
}
function button(label, cls, onClick) {
  const b = el("button", `mini ${cls || ""}`, label);
  b.type = "button";
  b.addEventListener("click", ev => { ev.stopPropagation(); onClick(); });
  return b;
}
function formError(id, msg) { const p = $(id); p.textContent = msg || ""; p.hidden = !msg; }
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

// ---------------------------------------------------------------- icons
const FINAL = new Set(["completed", "failed", "canceled", "rejected"]);
const CIRCLE = "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z";
const ICONS = {   // 24x24 stroke icons, each a list of path data
  running: ["M21 12a9 9 0 1 1-6.2-8.6"],
  completed: ["M20 6 9 17l-5-5"],
  canceled: [CIRCLE, "M5.6 5.6l12.8 12.8"],
  failed: [CIRCLE, "M15 9l-6 6", "M9 9l6 6"],
  input: [CIRCLE, "M9.6 9.2a2.5 2.5 0 1 1 3.4 2.3c-.7.4-1 .9-1 1.6", "M12 17v.01"],
  hourglass: ["M6 2h12", "M6 22h12", "M7 2v4.5l5 5.5-5 5.5V22", "M17 2v4.5L12 12l5 5.5V22"],
  parallel: ["M5 4l9 8-9 8z", "M19 5v14"],
  settings: ["M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    "M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"],
  // an agent without an icon gets a globe (a question mark is already taken by "needs input")
  globe: [CIRCLE, "M3 12h18", "M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9s1.3-6.4 3.8-9z"],
};
const STATE_LOOK = {   // task state -> [icon, css class, label]
  submitted: ["running", "running", "starting"], working: ["running", "running", "running"],
  completed: ["completed", "completed", "completed"], canceled: ["canceled", "canceled", "canceled"],
  failed: ["failed", "failed", "failed"], rejected: ["failed", "failed", "rejected"],
  input_required: ["input", "waiting", "needs input"], auth_required: ["input", "waiting", "needs authorization"],
};
function look(state) { return STATE_LOOK[state] || ["running", "running", state]; }

/** Appends the paths of an icon to an <svg> or <g>. */
function drawIcon(parent, name) {
  for (const d of ICONS[name]) parent.appendChild(svgEl("path", { d }));
}

/** An inline status icon: spinner while the task runs, then a result icon. */
function statusIcon(state) {
  const [icon, cls, label] = look(state);
  const wrap = el("span", `status-icon ${cls}`);
  wrap.title = label;
  const svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "2.2", "stroke-linecap": "round", "stroke-linejoin": "round" });
  drawIcon(svg, icon);
  wrap.appendChild(svg);
  return wrap;
}

/** The icon of an agent (from its A2A agent card), or a globe if it has none or it fails to load. */
function agentIcon(workerId, hasIcon, small) {
  const wrap = el("span", `agent-icon${small ? " sm" : ""}`);
  const globe = () => {
    const svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
      "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round" });
    drawIcon(svg, "globe");
    wrap.replaceChildren(svg);
  };
  if (!hasIcon) { globe(); return wrap; }
  const img = el("img");
  img.alt = "";
  img.src = `/icon/${encodeURIComponent(workerId)}`;
  img.addEventListener("error", globe);
  wrap.append(img);
  return wrap;
}

/** Fills a link element with the settings (gear) icon. */
function mountSettingsIcon(id) {
  const svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" });
  drawIcon(svg, "settings");
  $(id).replaceChildren(svg);
}
