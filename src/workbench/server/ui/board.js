"use strict";

// The board: everything that participants say to each other, live. Hosts see every channel and
// every mailbox, which is the point: this is where you watch how the agents organise themselves.

onUnauthorized = () => { location.href = "index.html"; };
mountSettingsIcon("settings-link");
mountIcon("files-link", "files");

const MAX_KEPT = 3000;
let messages = [];            // everything loaded so far, oldest first
let lastSeq = 0;
let view = "all";             // "all" | "dms" | a channel name
let channels = [];            // [{name, description}]
let workers = new Map();      // id -> {name, has_icon}
let stick = true;             // follow the newest message while the user is at the bottom

const feed = $("feed");

function workerName(id) { return (workers.get(id) || {}).name || "(removed)"; }

function channelLabel(ch) {
  return ch.startsWith("@") ? `✉ ${workerName(ch.slice(1))}` : `#${ch}`;
}

function senderIcon(sender) {
  if (sender.kind === "worker") return agentIcon(sender.id, (workers.get(sender.id) || {}).has_icon, true);
  const wrap = el("span", "agent-icon sm");
  const svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.8",
    "stroke-linecap": "round", "stroke-linejoin": "round" });
  drawIcon(svg, sender.kind === "host" ? "person" : "code");
  wrap.append(svg);
  return wrap;
}

function visible(m) {
  if (view === "all") return true;
  if (view === "dms") return m.channel.startsWith("@");
  return m.channel === view;
}

function render1(m) {
  const row = el("div", "bmsg");
  row.dataset.seq = m.seq;
  const head = el("div", "bhead");
  const who = el("strong", "", m.sender.name || m.sender.id);
  head.append(senderIcon(m.sender), who);
  if (m.sender.kind !== "worker") head.append(el("span", "chip-kind", m.sender.kind));
  if (m.kind && m.kind !== "post") head.append(el("span", "chip-kind accent", m.kind));
  if (view === "all" || view === "dms") head.append(el("span", "bchan", channelLabel(m.channel)));
  if (m.to && m.to.length && !m.channel.startsWith("@")) {
    head.append(el("span", "bchan", `→ ${m.to.map(workerName).join(", ")}`));
  }
  const t = new Date(m.time * 1000);
  const time = el("time", "btime", t.toLocaleTimeString());
  time.title = t.toLocaleString();
  time.dateTime = t.toISOString();
  head.append(time);
  row.append(head, el("div", "btext", m.text));
  return row;
}

function renderAll() {
  feed.replaceChildren(...messages.filter(visible).map(render1));
  if (!feed.children.length) feed.append(el("p", "muted pad", "Nothing here yet."));
  feed.scrollTop = feed.scrollHeight;
  stick = true;
  const writable = view !== "all" && view !== "dms";
  $("compose").hidden = !writable;
}

function appendNew(fresh) {
  const shown = fresh.filter(visible);
  if (!shown.length) return;
  if (feed.querySelector("p.muted")) feed.replaceChildren();
  feed.append(...shown.map(render1));
  if (stick) feed.scrollTop = feed.scrollHeight;
}

feed.addEventListener("scroll", () => {
  stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
});

// ----------------------------------------------------------------- channel list
function renderChannels() {
  const nav = $("channels");
  const item = (id, label, title) => {
    const b = el("button", `chan${view === id ? " sel" : ""}`, label);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", () => { view = id; renderChannels(); renderAll(); });
    return b;
  };
  nav.replaceChildren(
    item("all", "All activity"),
    el("h3", "", "Channels"),
    ...channels.map(c => item(c.name, `# ${c.name}`, c.description)),
    el("h3", "", "Private"),
    item("dms", "✉ Mailboxes"));
}

// ------------------------------------------------------------------------ loading
async function loadMeta() {
  const [ws, chs] = await Promise.all([api("GET", "/admin/workers"), api("GET", "/admin/board/channels")]);
  workers = new Map(ws.map(w => [w.id, { name: w.name, has_icon: w.has_icon }]));
  channels = chs;
  renderChannels();
}

function keep(fresh) {
  for (const m of fresh) { messages.push(m); lastSeq = Math.max(lastSeq, m.seq); }
  if (messages.length > MAX_KEPT) messages = messages.slice(-MAX_KEPT);
}

async function follow() {
  // long poll: the server answers as soon as something is posted
  for (;;) {
    try {
      const fresh = await api("GET", `/admin/board/messages?after=${lastSeq}&wait=25&limit=200`);
      $("live").textContent = "live";
      if (fresh.length) {
        keep(fresh);
        if (fresh.some(m => !channels.some(c => c.name === m.channel) && !m.channel.startsWith("@"))) await loadMeta();
        appendNew(fresh);
      }
    } catch (e) {
      $("live").textContent = "reconnecting…";
      await new Promise(r => setTimeout(r, 2000));
    }
  }
}

$("compose").addEventListener("submit", async ev => {
  ev.preventDefault();
  const box = $("compose-text"), text = box.value.trim();
  if (!text) return;
  formError("compose-error");
  try {
    await api("POST", `/admin/board/channels/${encodeURIComponent(view)}/messages`, { text });
    box.value = "";
  } catch (e) { formError("compose-error", e.message); }
});

(async function init() {
  if (!(await resumeSession())) { location.href = "index.html"; return; }
  await loadMeta();
  keep(await api("GET", "/admin/board/messages?newest=true&limit=300"));
  renderAll();
  $("live").textContent = "live";
  setInterval(() => loadMeta().catch(() => {}), 15000);   // names, icons and new channels
  follow();
})();
