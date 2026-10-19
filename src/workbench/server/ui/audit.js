"use strict";

// Alerts raised by the monitor, the audit log of everything that happened, and a note on what the
// workbench can and cannot control.

onUnauthorized = () => { location.href = "index.html"; };

let tab = "alerts";
let timer = null;
const when = ts => new Date(ts * 1000).toLocaleString();

async function renderAlerts() {
  const all = $("show-all") ? $("show-all").checked : false;
  const alerts = await api("GET", `/admin/alerts${all ? "?all=true" : ""}`);
  const box = el("div", "ssection");
  const toggle = el("label", "check");
  const cb = el("input"); cb.type = "checkbox"; cb.id = "show-all"; cb.checked = all;
  cb.addEventListener("change", show);
  toggle.append(cb, document.createTextNode(" Show acknowledged alerts too"));
  box.append(el("p", "muted intro", "The monitor watches for loops between agents, agents that keep hitting their limits, " +
    "and agents that keep asking for things they may not use. Pause the agents involved from the agent list on the tasks page."), toggle);
  if (!alerts.length) box.append(el("p", "muted", "No alerts."));
  for (const a of alerts) {
    const card = el("div", `card alert ${a.severity}${a.acknowledged ? " ack" : ""}`);
    card.append(el("strong", "", `${a.severity === "critical" ? "Critical" : "Warning"} · ${a.kind}`),
      el("p", "", a.text), el("span", "muted", when(a.time)));
    if (!a.acknowledged) card.append(button("Acknowledge", "", async () => { await api("POST", `/admin/alerts/${a.id}/ack`); show(); }));
    box.append(card);
  }
  return box;
}

let logFilter = { actor: "", action: "" };
async function renderLog() {
  const q = new URLSearchParams({ newest: "true", limit: "300" });
  if (logFilter.actor) q.set("actor_filter", logFilter.actor);
  if (logFilter.action) q.set("action", logFilter.action);
  const entries = (await api("GET", `/admin/audit?${q}`)).reverse();
  const box = el("div", "ssection");
  const form = el("form", "inline");
  const actor = el("input"); actor.placeholder = "Actor (name or id)"; actor.value = logFilter.actor;
  const action = el("input"); action.placeholder = "Action prefix, e.g. task. or board."; action.value = logFilter.action;
  const go = el("button", "primary small", "Filter"); go.type = "submit";
  form.append(actor, action, go);
  form.addEventListener("submit", ev => { ev.preventDefault(); logFilter = { actor: actor.value.trim(), action: action.value.trim() }; show(); });
  box.append(form);
  const table = el("table", "matrix auditlog");
  const head = el("tr");
  for (const h of ["Time", "Who", "Action", "What"]) head.append(el("th", "", h));
  table.append(head);
  for (const e of entries) {
    const tr = el("tr");
    tr.append(el("td", "", when(e.time)), el("td", "", `${e.actor.name} (${e.actor.kind})`), el("td", "", e.action), el("td", "left", e.summary));
    table.append(tr);
  }
  const wrap = el("div", "matrix-wrap"); wrap.append(table);
  box.append(wrap);
  if (!entries.length) box.append(el("p", "muted", "Nothing logged yet."));
  box.append(el("p", "muted hint", "Entries say that something happened, never what was written: messages and file contents are not copied here."));
  return box;
}

function renderAbout() {
  const box = el("div", "ssection");
  box.append(el("p", "muted intro", "Everything agents do to each other goes through this workbench, so this is where control is enforced."));
  const card = el("div", "card sform");
  const items = [
    ["Pause and stop", "A paused agent is refused by every workbench API (board, files, tasks), gets no new tasks and no wake-ups. " +
      "\"Cancel work\" also cancels what it runs or started. \"Emergency stop\" does this for everyone. It is enforced here, not requested from the agent."],
    ["Limits", "Board posts, file writes, wake-ups, tasks per agent and tasks started per hour are limited (General settings)."],
    ["Audit log", "Joins, task starts, board posts, file writes and every change a host makes."],
    ["Alerts", "Loops between agents, repeated limit hits, repeated refused requests. Optionally the agents of critical alerts are paused automatically (General settings)."],
    ["What it cannot do", "The workbench controls what passes through it. A worker that has its own shell or network access can still act elsewhere, " +
      "so tools like BashTool need sandboxing on the worker, and agents need the least access that works."],
  ];
  const dl = el("dl", "facts");
  for (const [k, v] of items) dl.append(el("dt", "", k), el("dd", "", v));
  card.append(dl);
  box.append(card);
  return box;
}

async function show() {
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("sel", b.dataset.tab === tab));
  let node;
  try { node = tab === "alerts" ? await renderAlerts() : tab === "log" ? await renderLog() : renderAbout(); }
  catch (e) { node = el("p", "error", e.message); }
  const typing = document.activeElement && document.activeElement.closest("#content input");
  if (!typing) $("content").replaceChildren(node);
  try {
    const g = await api("GET", "/admin/governance");
    const n = $("alert-count"); n.textContent = g.alerts; n.hidden = !g.alerts;
  } catch (_) { /* ignore */ }
}

for (const b of document.querySelectorAll("#tabs button")) {
  b.addEventListener("click", () => { tab = b.dataset.tab; show(); });
}

(async function init() {
  if (!(await resumeSession())) { location.href = "index.html"; return; }
  await show();
  timer = setInterval(() => { if (tab !== "about") show(); }, 4000);
})();
