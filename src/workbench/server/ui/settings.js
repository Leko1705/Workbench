"use strict";

// Settings: a list of sections on the left, the selected section on the right.

const SECTIONS = {
  general: { render: renderGeneral },
  labels: { render: renderLabels },
  "api-keys": { render: renderApiKeys },
};
let current = null;

onUnauthorized = () => { location.href = "index.html"; };

// ---------------------------------------------------------------- helpers
function section(title, intro) {
  const box = el("section", "ssection");
  box.append(el("h2", "", title));
  if (intro) box.append(el("p", "muted intro", intro));
  return box;
}
function field(label, input, hint) {
  const l = el("label");
  l.append(document.createTextNode(label), input);
  if (hint) l.append(el("span", "hint", hint));
  return l;
}
function input(value, attrs = {}) {
  const i = el("input");
  Object.assign(i, attrs);
  i.value = value ?? "";
  return i;
}
function notice() {                                  // a status line that fades after a success
  const p = el("p", "notice");
  p.show = (msg, bad) => {
    p.textContent = msg; p.className = `notice ${bad ? "bad" : "good"}`;
    clearTimeout(p.t);
    if (!bad) p.t = setTimeout(() => { p.textContent = ""; }, 2500);
  };
  return p;
}
function fmtTime(ts) { return ts ? `${ago(ts)} (${new Date(ts * 1000).toLocaleString()})` : "never"; }
function fmtDuration(s) { return s >= 3600 ? `${(s / 3600).toFixed(s % 3600 ? 1 : 0)} h` : `${Math.round(s / 60)} min`; }

async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; }
  catch (_) { btn.textContent = "Select and copy manually"; }
}

// ---------------------------------------------------------------- general
async function renderGeneral(root) {
  const s = await api("GET", "/admin/settings");
  const box = section("General", "Basic properties of this workbench.");

  const form = el("form", "card sform");
  const name = input(s.name, { maxLength: 200, required: true });
  const desc = el("textarea"); desc.rows = 3; desc.value = s.description; desc.maxLength = 2000;
  const depth = input(s.max_task_depth, { type: "number", min: 1, max: 50, required: true });
  const active = input(s.max_active_tasks, { type: "number", min: 1, max: 10000, required: true });
  const msg = notice();
  const save = el("button", "primary", "Save");
  save.type = "submit";
  form.append(
    field("Name", name, "Shown to workers and hosts."),
    field("Description", desc),
    field("Maximum task depth", depth, "How deeply agents may delegate to sub-tasks."),
    field("Maximum running tasks", active, "Tasks running at the same time, in total."),
    msg, save);
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    try {
      await api("PATCH", "/admin/settings", { name: name.value, description: desc.value,
        max_task_depth: +depth.value, max_active_tasks: +active.value });
      msg.show("Saved.");
    } catch (e) { msg.show(e.status === 422 ? "Please check the values." : e.message, true); }
  });

  const info = el("div", "card sform");
  info.append(el("h3", "plain", "Fixed settings"));
  const rows = [
    ["Workbench URL", s.public_url, "Workers know this workbench by this address, so it cannot be changed here."],
    ["Version", s.version],
    ["Accept plain-http workers", s.startup.allow_insecure_workers ? "yes" : "only on localhost"],
    ["Host session: idle timeout", fmtDuration(s.startup.session_idle_timeout)],
    ["Host session: maximum age", fmtDuration(s.startup.session_max_age)],
  ];
  const dl = el("dl", "facts");
  for (const [k, v, hint] of rows) {
    dl.append(el("dt", "", k), el("dd", k === "Workbench URL" ? "mono" : "", v));
    if (hint) dl.append(el("dd", "hint", hint));
  }
  info.append(dl, el("p", "muted hint", "These are chosen when the server is started (see WorkbenchServer.run)."));
  box.append(form, info);
  root.append(box);
}

// ----------------------------------------------------------------- labels
async function renderLabels(root) {
  const [view, workers] = await Promise.all([api("GET", "/admin/labels"), api("GET", "/admin/workers")]);
  const agents = workers.filter(w => w.status === "active");
  const labels = view.labels;
  const box = section("Labels", "A label is a cluster of agents, for example a role. Every agent has at least one label, " +
    "a new agent gets a label of its own. Who may hand work to whom is decided between labels.");
  const reload = async () => { await show(current, true); };

  // -- 1. the labels themselves
  const c1 = el("div", "card sform");
  c1.append(el("h3", "plain", "Labels"));
  const list = el("ul", "rows labels");
  for (const l of labels) {
    const li = el("li");
    const name = el("span", "grow", l.name);
    const meta = el("span", "sub", `${l.worker_ids.length} agent${l.worker_ids.length === 1 ? "" : "s"}`);
    li.append(name, meta,
      button("Rename", "", () => {
        const f = el("form", "inline");
        const i = input(l.name, { maxLength: 100, required: true });
        const ok = el("button", "mini", "Save"); ok.type = "submit";
        f.append(i, ok);
        f.addEventListener("submit", async ev => {
          ev.preventDefault();
          try { await api("PATCH", `/admin/labels/${l.id}`, { name: i.value }); await reload(); }
          catch (e) { m1.show(e.message, true); }
        });
        li.replaceChildren(f);
        i.focus();
      }),
      button("×", "danger", async () => {
        try { await api("DELETE", `/admin/labels/${l.id}`); await reload(); } catch (e) { m1.show(e.message, true); }
      }));
    list.append(li);
  }
  const addForm = el("form", "inline");
  const addName = input("", { placeholder: "New label", maxLength: 100, required: true });
  const addBtn = el("button", "primary small", "Add"); addBtn.type = "submit";
  const m1 = notice();
  addForm.append(addName, addBtn);
  addForm.addEventListener("submit", async ev => {
    ev.preventDefault();
    try { await api("POST", "/admin/labels", { name: addName.value }); await reload(); }
    catch (e) { m1.show(e.message, true); }
  });
  c1.append(list, addForm, m1);

  // -- 2. which labels each agent carries
  const c2 = el("div", "card sform");
  c2.append(el("h3", "plain", "Agents"), el("p", "muted hint", "Click a label to give it to an agent or take it away."));
  const m2 = notice();
  if (!agents.length) c2.append(el("p", "muted", "No agents have joined yet."));
  for (const w of agents) {
    const row = el("div", "agent-row");
    row.append(agentIcon(w.id, w.has_icon, true), el("span", "agent-name", w.name));
    const chips = el("span", "chips");
    for (const l of labels) {
      const on = w.label_ids.includes(l.id);
      const chip = el("button", `chip${on ? " on" : ""}`, l.name);
      chip.type = "button";
      chip.setAttribute("aria-pressed", on);
      chip.addEventListener("click", async () => {
        const ids = on ? w.label_ids.filter(x => x !== l.id) : [...w.label_ids, l.id];
        if (!ids.length) { m2.show(`${w.name} needs at least one label.`, true); return; }
        try { await api("PUT", `/admin/workers/${w.id}/labels`, { label_ids: ids }); await reload(); }
        catch (e) { m2.show(e.message, true); }
      });
      chips.append(chip);
    }
    row.append(chips);
    c2.append(row);
  }
  c2.append(m2);

  // -- 3. who may talk to whom
  const comm = view.communication;
  const rules = new Set(comm.rules.map(r => r.join(">")));
  const c3 = el("div", "card sform");
  c3.append(el("h3", "plain", "Communication"));
  const m3 = notice();
  const save = async policy => {
    const list = [...rules].map(r => r.split(">"));
    try { await api("PUT", "/admin/communication", { policy, rules: list }); m3.show("Saved."); }
    catch (e) { m3.show(e.message, true); }
  };
  const policy = el("div", "policy");
  for (const [value, title, text] of [
    ["open", "Open", "Every agent may hand work to every other agent."],
    ["restricted", "Restricted", "Only the label pairs ticked below may."]]) {
    const r = el("label", "radio");
    const inp = el("input"); inp.type = "radio"; inp.name = "policy"; inp.value = value;
    inp.checked = comm.policy === value;
    inp.addEventListener("change", async () => { await save(value); await reload(); });
    const t = el("span"); t.append(el("strong", "", title), el("span", "hint", text));
    r.append(inp, t);
    policy.append(r);
  }
  c3.append(policy);
  if (comm.policy === "restricted") {
    c3.append(el("p", "muted hint", "Read it row by row: agents with the label on the left may hand work to agents with the label on top."));
    const wrap = el("div", "matrix-wrap");
    const table = el("table", "matrix");
    const head = el("tr");
    head.append(el("th", "corner", "from \\ to"));
    for (const l of labels) head.append(el("th", "", l.name));
    table.append(head);
    for (const from of labels) {
      const tr = el("tr");
      tr.append(el("th", "rowhead", from.name));
      for (const to of labels) {
        const td = el("td");
        const cb = el("input"); cb.type = "checkbox";
        cb.checked = rules.has(`${from.id}>${to.id}`);
        cb.title = `${from.name} → ${to.name}`;
        cb.setAttribute("aria-label", cb.title);
        cb.addEventListener("change", () => {
          const key = `${from.id}>${to.id}`;
          if (cb.checked) rules.add(key); else rules.delete(key);
          save("restricted");
        });
        td.append(cb);
        tr.append(td);
      }
      table.append(tr);
    }
    wrap.append(table);
    c3.append(wrap);
    c3.append(el("p", "muted hint", "Hosts and API keys are not bound by these rules. Running tasks are not affected by changes."));
  }
  c3.append(m3);
  box.append(c1, c2, c3);
  root.append(box);
}

// --------------------------------------------------------------- api keys
async function renderApiKeys(root) {
  const box = section("API keys", "Programs outside the workbench use an API key with the WorkbenchClient: " +
    "they can list agents and start, watch, answer and cancel tasks. Keys are not bound by the label rules.");
  const form = el("form", "card sform");
  const name = input("", { placeholder: "Name, e.g. the program that uses it", maxLength: 100, required: true });
  const go = el("button", "primary", "Generate key"); go.type = "submit";
  const msg = notice();
  const result = el("div", "keyresult"); result.hidden = true;
  form.append(el("h3", "plain", "New key"), field("Name", name), go, msg, result);
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    try {
      const k = await api("POST", "/admin/api-keys", { name: name.value });
      result.hidden = false;
      const code = el("p", "code", k.key);
      const copy = button("Copy", "", () => copyText(k.key, copy));
      result.replaceChildren(
        el("p", "muted", "Copy the key now. It is shown only once."), code, copy,
        el("p", "muted hint", "Use it like this:"),
        el("pre", "snippet", `from workbench.workbench import WorkbenchClient\n\n` +
          `async with WorkbenchClient("${k.base_url}", "${k.key}") as wb:\n    print(await wb.participants())`));
      name.value = "";
      await refreshList();
    } catch (e) { msg.show(e.message, true); }
  });

  const list = el("div", "card sform");
  async function refreshList() {
    const items = await api("GET", "/admin/api-keys");
    const ul = el("ul", "rows keys");
    for (const k of items) {
      const li = el("li");
      const text = el("span", "grow");
      text.append(el("strong", "", k.name), el("br"),
        el("span", "sub", `${k.prefix}…  ·  created ${fmtTime(k.created_at)}  ·  last used ${fmtTime(k.last_used_at)}`));
      li.append(text, button("Revoke", "danger", async () => {
        try { await api("DELETE", `/admin/api-keys/${k.id}`); await refreshList(); } catch (e) { msg.show(e.message, true); }
      }));
      ul.append(li);
    }
    list.replaceChildren(el("h3", "plain", "Keys"), items.length ? ul : el("p", "muted", "No keys yet."));
  }
  await refreshList();
  box.append(form, list);
  root.append(box);
}

// ------------------------------------------------------------- navigation
async function show(name, keepScroll) {
  if (!SECTIONS[name]) name = "general";
  current = name;
  document.querySelectorAll("#snav a").forEach(a => a.classList.toggle("sel", a.dataset.section === name));
  const root = $("content");
  const top = root.scrollTop;
  const fresh = el("div");
  try { await SECTIONS[name].render(fresh); }
  catch (e) { fresh.append(el("p", "error", e.message)); }
  root.replaceChildren(fresh);
  if (keepScroll) root.scrollTop = top;
}

window.addEventListener("hashchange", () => show(location.hash.slice(1)));

(async function init() {
  if (!(await resumeSession())) { location.href = "index.html"; return; }
  await show(location.hash.slice(1));
})();
