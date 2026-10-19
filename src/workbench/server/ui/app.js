"use strict";

// The task list page: agents and invites on the left, root tasks in the middle,
// a form for a new task on the right. Clicking a root task opens task.html.

function renderWorkers(workers) {
  $("workers").replaceChildren(...workers.map(w => {
    const li = el("li");
    li.append(w.status === "active" ? agentIcon(w.id, w.has_icon, true) : agentIcon(w.id, false, true));
    const name = el("span", "grow", w.name);
    const skills = (w.skills || []).map(k => k.name || k.id).join(", ");
    name.title = [w.description, skills && `Skills: ${skills}`, w.a2a_url].filter(Boolean).join("\n");
    li.append(name, el("span", `badge ${w.paused ? "paused" : w.status}`, w.paused ? "paused" : w.status));
    if (w.status === "active") {
      li.append(button(w.paused ? "Resume" : "Pause", "", () => act("PUT", `/admin/workers/${w.id}/pause`, { paused: !w.paused })));
      if (w.paused) li.append(button("Cancel work", "danger", () => {
        if (window.confirm(`Cancel everything ${w.name} is working on or has started?`)) act("POST", `/admin/workers/${w.id}/stop`);
      }));
    }
    if (w.status === "pending") {
      li.append(button("Approve", "", () => act("POST", `/admin/workers/${w.id}/approve`)),
                button("Deny", "danger", () => act("POST", `/admin/workers/${w.id}/deny`)));
    } else {
      li.append(button("×", "danger", () => act("DELETE", `/admin/workers/${w.id}`)));
    }
    return li;
  }));
  $("workers-empty").hidden = workers.length > 0;

  const sel = $("task-form").agent_id, prev = sel.value;
  const active = workers.filter(w => w.status === "active");
  sel.replaceChildren(...active.map(w => { const o = el("option", "", w.name); o.value = w.id; return o; }));
  if (active.some(w => w.id === prev)) sel.value = prev;
}

function renderInvites(invites) {
  $("invites").replaceChildren(...invites.map(i => {
    const li = el("li");
    const left = el("span", "grow", i.label || "(no label)");
    left.title = i.expires_at ? `expires ${new Date(i.expires_at * 1000).toLocaleString()}` : "";
    const meta = el("span", "sub", `${i.uses_left} use${i.uses_left === 1 ? "" : "s"}${i.auto_approve ? ", auto" : ""}`);
    li.append(left, meta, button("×", "danger", () => act("DELETE", `/admin/invites/${i.id}`)));
    return li;
  }));
}

function renderRoots(roots) {
  $("roots").replaceChildren(...roots.map(r => {
    const li = el("li");
    const a = el("a", "root");
    a.href = `task.html?id=${encodeURIComponent(r.id)}`;
    const text = el("span", "root-text");
    text.append(el("span", "topic", r.topic),
                el("span", "sub", [r.worker_name, ago(r.created_at),
                                   r.subtasks ? `${r.subtasks} sub-task${r.subtasks === 1 ? "" : "s"}` : ""]
                                   .filter(Boolean).join(" · ")));
    // a finished root whose sub-tasks still run counts as running
    text.classList.add("grow");
    a.append(agentIcon(r.worker_id, r.worker_has_icon), text,
             statusIcon(r.active > 0 && FINAL.has(r.state) ? "working" : r.state));
    a.title = r.input;
    li.append(a);
    return li;
  }));
  $("roots-empty").hidden = roots.length > 0;
}

async function act(method, path, body) {
  try { await api(method, path, body); } catch (e) { formError("task-error", e.message); }
  await refresh();
}

async function refresh() {
  try {
    const [workers, invites, roots, gov] = await Promise.all([
      api("GET", "/admin/workers"), api("GET", "/admin/invites"), api("GET", "/admin/tasks"),
      api("GET", "/admin/governance")]);
    $("paused-banner").hidden = !gov.paused;
    $("stop-all").hidden = gov.paused;
    const n = $("alert-count");
    n.textContent = gov.alerts; n.hidden = !gov.alerts;
    renderWorkers(workers);
    renderInvites(invites);
    renderRoots(roots);
  } catch (_) { /* logged out or server restarting; onUnauthorized handles 401 */ }
}

$("task-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const f = ev.target, btn = f.querySelector("button");
  formError("task-error");
  btn.disabled = true;
  try {
    await api("POST", "/admin/tasks", { agent_id: f.agent_id.value, text: f.text.value });
    f.text.value = "";
    await refresh();
  } catch (e) { formError("task-error", e.message); }
  finally { btn.disabled = false; }
});

$("stop-all").addEventListener("click", async () => {
  if (!window.confirm("Pause the whole workbench and cancel every unfinished task?")) return;
  await act("POST", "/admin/stop-all");
});
$("resume-all").addEventListener("click", () => act("POST", "/admin/pause", { paused: false }));

$("copy-token").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("invite-token").textContent);
    $("copy-token").textContent = "Copied";
  } catch (_) {   // no clipboard permission: select the text so Ctrl+C works
    const r = document.createRange();
    r.selectNodeContents($("invite-token"));
    getSelection().removeAllRanges();
    getSelection().addRange(r);
  }
});

$("invite-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const f = ev.target;
  try {
    const inv = await api("POST", "/admin/invites", { label: f.label.value, auto_approve: f.auto_approve.checked });
    $("invite-token").textContent = inv.join_token;
    $("invite-url").textContent = inv.base_url;
    $("invite-code").textContent = inv.code;
    $("copy-token").textContent = "Copy";
    $("invite-result").hidden = false;
    f.reset();
    await refresh();
  } catch (e) { formError("task-error", e.message); }
});

// ---------------------------------------------------------------- login
let timer = null;

function showLogin() {
  clearInterval(timer);
  $("app-view").hidden = true;
  $("login-view").hidden = false;
}
onUnauthorized = showLogin;
(function () {   // the shield icon goes before the alert count
  const svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
    "stroke-linecap": "round", "stroke-linejoin": "round" });
  drawIcon(svg, "shield");
  $("audit-link").prepend(svg);
})();
mountSettingsIcon("settings-link");
mountIcon("board-link", "board");
mountIcon("files-link", "files");

async function showApp() {
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  try { $("wb-name").textContent = (await api("GET", "/admin/config")).name; } catch (_) { /* keep default */ }
  await refresh();
  clearInterval(timer);
  timer = setInterval(refresh, 3000);
}

$("login-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const form = ev.target;
  formError("login-error");
  try {
    const r = await api("POST", "/admin/login", { username: form.username.value, password: form.password.value });
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
  $("invite-result").hidden = true;
  showLogin();
});

(async function init() {
  if (await resumeSession()) showApp(); else showLogin();
})();
