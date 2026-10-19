"use strict";

// The inbox of a HumanWorker: tasks that agents handed to this person, plus the board and a way
// to hand out work, so the person takes part in the network like any agent.

let csrf = "";
async function api(method, path, body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  const resp = await fetch(path, { method, headers, credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await resp.json(); } catch (_) { /* no body */ }
  if (resp.status === 401) location.href = "index.html";
  if (!resp.ok) {
    const d = data && data.detail;
    const e = new Error(typeof d === "string" ? d : `HTTP ${resp.status}`);
    e.status = resp.status;
    throw e;
  }
  return data;
}
const $ = id => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const when = ts => new Date(ts * 1000).toLocaleTimeString();
function showError(id, msg) { const p = $(id); p.textContent = msg || ""; p.hidden = !msg; }

// -------------------------------------------------------------------- tabs
let tab = "inbox";
for (const b of document.querySelectorAll("#tabs button")) {
  b.addEventListener("click", () => {
    tab = b.dataset.tab;
    for (const x of document.querySelectorAll("#tabs button")) x.classList.toggle("sel", x === b);
    for (const t of ["inbox", "board", "delegate"]) $(`tab-${t}`).hidden = t !== tab;
    if (tab === "board") loadBoard();
    if (tab === "delegate") loadParticipants();
  });
}

// ------------------------------------------------------------------- inbox
let items = [];
let selected = null;
let drafts = {};            // task id -> text typed into the reply box (survives refreshes)
const STATE = { open: ["needs you", "open"], asked: ["waiting for the answer to your question", "asked"],
  answered: ["answered", "done"], declined: ["declined", "done"], canceled: ["canceled", "done"] };

function renderList() {
  $("ilist").replaceChildren(...(items.length ? items : []).map(i => {
    const b = el("button", `iitem ${STATE[i.state][1]}${i.task_id === selected ? " sel" : ""}`);
    const first = i.messages.length ? i.messages[0].text.split("\n")[0] : "(empty)";
    b.append(el("span", "ititle", first), el("span", "isub", `${STATE[i.state][0]} · ${when(i.created_at)}`));
    b.addEventListener("click", () => { selected = i.task_id; renderList(); renderThread(true); });
    return b;
  }));
  if (!items.length) $("ilist").append(el("p", "muted pad", "No tasks yet. Agents can hand you work through the workbench."));
}

let threadKey = "";
function renderThread(force) {
  const i = items.find(x => x.task_id === selected);
  const box = $("ithread");
  if (!i) { box.replaceChildren(el("p", "muted pad", "Nothing selected.")); threadKey = ""; return; }
  const key = `${i.task_id}:${i.messages.length}:${i.state}`;
  if (!force && key === threadKey) return;
  threadKey = key;
  const typing = document.activeElement && document.activeElement.closest("#ithread textarea");
  const prev = typing ? document.activeElement.value : null;

  const thread = el("div", "thread");
  for (const m of i.messages) {
    const d = el("div", `tmsg ${m.from}`);
    d.append(el("div", "who", `${m.from === "human" ? "You" : "Requester"} · ${when(m.time)}`), el("div", "body", m.text));
    thread.append(d);
  }
  box.replaceChildren(el("h2", "", STATE[i.state][0]), thread);
  if (i.state === "open") {
    const ta = el("textarea"); ta.rows = 4; ta.placeholder = "Your answer…";
    ta.value = prev !== null ? prev : (drafts[i.task_id] || "");
    ta.addEventListener("input", () => { drafts[i.task_id] = ta.value; });
    const err = el("p", "error"); err.hidden = true;
    const act = (path, label, cls) => {
      const b = el("button", cls, label); b.type = "button";
      b.addEventListener("click", async () => {
        if (!ta.value.trim()) { err.textContent = "Write something first."; err.hidden = false; return; }
        try { await api("POST", `/admin/human/inbox/${i.task_id}/${path}`, { text: ta.value }); delete drafts[i.task_id]; await load(); }
        catch (e) { err.textContent = e.message; err.hidden = false; }
      });
      return b;
    };
    const row = el("div", "iactions");
    row.append(act("reply", "Answer and finish", "primary"), act("ask", "Ask back", ""), act("decline", "Decline", "danger"));
    box.append(ta, row, err);
    if (typing) ta.focus();
  }
}

async function load() {
  try { items = await api("GET", "/admin/human/inbox"); } catch (_) { return; }
  if (!selected && items.length) selected = items[0].task_id;
  renderList();
  renderThread(false);
}

// ------------------------------------------------------------------- board
let msgs = [], lastSeq = 0, channel = null, following = false, channels = [];

function channelName(c) { return c.kind === "mailbox" ? "✉ my mailbox" : `# ${c.name}`; }

function renderFeed() {
  const shown = msgs.filter(m => !channel || m.channel === channel);
  $("feed").replaceChildren(...shown.map(m => {
    const d = el("div", "bmsg");
    d.append(el("div", "bhead", `${m.sender.name} (${m.sender.kind}) · ${m.channel.startsWith("@") ? "mailbox" : "#" + m.channel}` +
      `${m.kind !== "post" ? " · " + m.kind : ""} · ${when(m.time)}`), el("div", "btext", m.text));
    return d;
  }));
  $("feed").scrollTop = $("feed").scrollHeight;
}

async function loadBoard() {
  try {
    channels = await api("GET", "/admin/human/board/channels");
    const list = $("chlist");
    const mk = (id, label) => { const b = el("button", `iitem${channel === id ? " sel" : ""}`, label); b.type = "button";
      b.addEventListener("click", () => { channel = id; loadBoard(); renderFeed(); }); return b; };
    list.replaceChildren(mk(null, "Everything"), ...channels.map(c => mk(c.name, channelName(c))));
    $("post-channel").replaceChildren(...channels.filter(c => c.can_write).map(c => { const o = el("option", "", `# ${c.name}`); o.value = c.name; return o; }));
    showError("post-error");
    if (!following) { following = true; follow(); }
    renderFeed();
  } catch (e) { showError("post-error", e.message); }
}

async function follow() {
  for (;;) {
    try {
      const fresh = await api("GET", `/admin/human/board/feed?after=${lastSeq}&wait=20`);
      if (fresh.length) { msgs.push(...fresh); lastSeq = fresh[fresh.length - 1].seq; if (tab === "board") renderFeed(); }
    } catch (_) { await new Promise(r => setTimeout(r, 3000)); }
  }
}

$("post-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const f = ev.target;
  showError("post-error");
  try { await api("POST", "/admin/human/board", { channel: f.channel.value, text: f.text.value }); f.text.value = ""; }
  catch (e) { showError("post-error", e.message); }
});

// ---------------------------------------------------------------- delegate
let started = [];
async function loadParticipants() {
  try {
    const ps = await api("GET", "/admin/human/participants");
    const sel = $("agent-select"), prev = sel.value;
    sel.replaceChildren(...ps.map(p => { const o = el("option", "", p.name); o.value = p.id; return o; }));
    if (ps.some(p => p.id === prev)) sel.value = prev;
    showError("start-error");
  } catch (e) { showError("start-error", e.message); }
}

$("start-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const f = ev.target;
  showError("start-error");
  try {
    const t = await api("POST", "/admin/human/tasks", { agent_id: f.agent_id.value, text: f.text.value });
    started.unshift({ id: t.id, agent: f.agent_id.selectedOptions[0].textContent, text: f.text.value, state: t.state, output: "" });
    f.text.value = "";
    renderStarted();
  } catch (e) { showError("start-error", e.message); }
});

function renderStarted() {
  $("started").replaceChildren(...started.map(t => {
    const d = el("div", "card started");
    d.append(el("strong", "", `${t.agent}: ${t.text.split("\n")[0].slice(0, 80)}`),
             el("div", "muted", t.state), ...(t.output ? [el("pre", "out", t.output)] : []));
    return d;
  }));
}

async function pollStarted() {
  for (const t of started) {
    if (["completed", "failed", "canceled", "rejected"].includes(t.state)) continue;
    try { const r = await api("GET", `/admin/human/tasks/${t.id}`); t.state = r.state; t.output = r.output || r.error || ""; } catch (_) { /* later */ }
  }
  if (tab === "delegate") renderStarted();
}

// -------------------------------------------------------------------- start
(async function init() {
  try {
    const s = await api("GET", "/admin/session");
    if (!s.authenticated) { location.href = "index.html"; return; }
    csrf = s.csrf_token;
  } catch (_) { location.href = "index.html"; return; }
  await load();
  setInterval(load, 2000);
  setInterval(pollStarted, 3000);
})();
