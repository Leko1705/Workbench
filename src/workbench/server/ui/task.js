"use strict";

// The task tree page: every node is a task, arrows point from the task that asked
// for work to the task that was started for it. Selecting a node opens its live log.

const rootId = new URLSearchParams(location.search).get("id");
const NODE_W = 280, NODE_H = 62, GAP_X = 110, GAP_Y = 22, PAD = 28;

let tree = null;        // latest nested task tree
let selected = null;    // id of the selected node
let busy = false;
let treeSig = "";

onUnauthorized = () => { location.href = "index.html"; };
mountSettingsIcon("settings-link");
mountIcon("board-link", "board");

// ------------------------------------------------------------------ tree
const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

/** Left-to-right layout: x by depth, leaves stacked, parents centred on their children. */
function layout(root) {
  const nodes = [];
  let slot = 0;
  (function place(n, depth) {
    n.depth = depth;
    n.children.forEach(c => place(c, depth + 1));
    n.row = n.children.length
      ? (n.children[0].row + n.children[n.children.length - 1].row) / 2
      : slot++;
    n.x = PAD + depth * (NODE_W + GAP_X);
    n.y = PAD + n.row * (NODE_H + GAP_Y);
    nodes.push(n);
  })(root, 0);
  return nodes;
}

function icon(parent, name, x, y, scale, cls, title) {
  const g = svgEl("g", { transform: `translate(${x} ${y}) scale(${scale})`, fill: "none",
    "stroke-width": "2.2", "stroke-linecap": "round", "stroke-linejoin": "round" }, cls);
  if (name === "running") {                 // the spinner turns around its own centre
    const spin = svgEl("g", {}, "spin");
    drawIcon(spin, name);
    g.appendChild(spin);
  } else {
    drawIcon(g, name);
  }
  if (title) g.appendChild(svgEl("title")).textContent = title;
  parent.appendChild(g);
}

/** The agent's card icon as an SVG image; a globe if it has none or fails to load. */
function agentImage(parent, n, x, y, size) {
  const globe = () => icon(parent, "globe", x + size * 0.1, y + size * 0.1, (size * 0.8) / 24, "agent-globe");
  if (!n.worker_has_icon) return globe();
  const img = svgEl("image", { x, y, width: size, height: size, preserveAspectRatio: "xMidYMid slice",
    href: `/icon/${encodeURIComponent(n.worker_id)}` }, "agent-img");
  img.addEventListener("error", () => { img.remove(); globe(); });
  parent.appendChild(img);
}

function drawTree() {
  const svg = $("tree");
  svg.replaceChildren();
  if (!tree) return;
  const nodes = layout(tree);
  const w = Math.max(...nodes.map(n => n.x)) + NODE_W + PAD;
  const h = Math.max(...nodes.map(n => n.y)) + NODE_H + PAD;
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);

  const defs = svgEl("defs");
  for (const kind of ["wait", "par", "done"]) {
    const m = svgEl("marker", { id: `arrow-${kind}`, viewBox: "0 0 10 10", refX: "9", refY: "5",
      markerWidth: "8", markerHeight: "8", orient: "auto-start-reverse" });
    m.appendChild(svgEl("path", { d: "M0 0L10 5L0 10z" }, `m-${kind}`));
    defs.appendChild(m);
  }
  svg.appendChild(defs);

  // arrows first, so nodes sit on top
  for (const p of nodes) {
    for (const c of p.children) {
      const x1 = p.x + NODE_W, y1 = p.y + NODE_H / 2, x2 = c.x, y2 = c.y + NODE_H / 2;
      const kind = FINAL.has(c.state) ? "done" : c.waited ? "wait" : "par";
      const mid = (x1 + x2) / 2;
      svg.appendChild(svgEl("path", { d: `M${x1} ${y1}C${mid} ${y1} ${mid} ${y2} ${x2 - 2} ${y2}`,
        "marker-end": `url(#arrow-${kind})` }, `edge ${kind}`));
      if (kind === "wait") icon(svg, "hourglass", mid - 8, (y1 + y2) / 2 - 8, 0.67, "edge-icon",
                                `${p.worker_name} is waiting for this result`);
    }
  }

  for (const n of nodes) {
    const [, cls] = look(n.state);
    const g = svgEl("g", { transform: `translate(${n.x} ${n.y})`, "data-id": n.id, tabindex: "0" },
                    `tnode${n.id === selected ? " sel" : ""}`);
    g.appendChild(svgEl("title")).textContent = `${n.topic}\n${n.worker_name} · ${look(n.state)[2]}`;
    g.appendChild(svgEl("rect", { width: NODE_W, height: NODE_H, rx: 12 }));
    agentImage(g, n, 12, 17, 28);                                  // agent icon: far left
    g.appendChild(svgEl("text", { x: 52, y: 27 }, "topic")).textContent = trunc(n.topic, 25);
    g.appendChild(svgEl("text", { x: 52, y: 47 }, "agent")).textContent = trunc(n.worker_name, 28);
    icon(g, look(n.state)[0], NODE_W - 34, 11, 0.92, `st ${cls}`);  // status: far right
    // is this agent blocked on a sub task, or carrying on while its sub tasks run?
    const active = n.children.some(c => !FINAL.has(c.state));
    if (n.waiting) icon(g, "hourglass", NODE_W - 31, 41, 0.62, "badge-icon",
                        "Waiting for a sub-task result");
    else if (active && !FINAL.has(n.state)) icon(g, "parallel", NODE_W - 31, 41, 0.62, "badge-icon",
                        "Carrying on while its sub-tasks run");
    svg.appendChild(g);
  }
}

// -------------------------------------------------------------- selection
function findNode(id, n = tree) {
  if (!n) return null;
  if (n.id === id) return n;
  for (const c of n.children) { const r = findNode(id, c); if (r) return r; }
  return null;
}

function select(id) {
  selected = id;
  drawTree();
  openPanel();
}

function deselect() {
  if (selected === null) return;
  selected = null;
  $("panel").hidden = true;
  drawTree();
}

$("tree").addEventListener("click", ev => {
  const g = ev.target.closest(".tnode");
  if (g) select(g.dataset.id); else deselect();
  ev.stopPropagation();
});
$("canvas").addEventListener("click", deselect);          // the empty area around the SVG
$("p-close").addEventListener("click", deselect);
document.addEventListener("keydown", ev => {
  if (ev.key === "Escape") deselect();
  const g = ev.target.closest && ev.target.closest(".tnode");
  if (g && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); select(g.dataset.id); }
});

// ------------------------------------------------------------ live panel
let lastSeq = 0;
let toolEls = new Map();     // tool call id -> its element, updated as the call progresses
let answerEl = null;
let panelTask = null;        // id the panel currently shows
let draft = "";              // reply text typed so far (survives refreshes)

function logBox() { return $("log"); }

function atBottom() {
  const b = logBox();
  return b.scrollHeight - b.scrollTop - b.clientHeight < 40;
}

function bubble(cls, who, text) {
  const d = el("div", `msg ${cls}`);
  d.append(el("div", "who", who), el("div", "body", text));
  return d;
}

function toolCard(e) {
  const d = el("div", "msg tool");
  const head = el("div", "who");
  head.append(statusIcon(e.status === "running" ? "working" : e.status === "error" ? "failed" : "completed"),
              el("span", "", ` ${e.tool}`));
  d.append(head);
  if (e.input && Object.keys(e.input).length) d.append(el("pre", "args", JSON.stringify(e.input, null, 1)));
  if (e.output) d.append(el("pre", "result", e.output));
  return d;
}

function addEvent(e, taskNode) {
  const box = logBox();
  let node = null;
  if (e.type === "input") {
    node = bubble("in", { host: "Task from host", api: "Task from an API client", board: "Task from the board",
      agent: "Task from an agent" }[e.from] || "Task", e.text);
  } else if (e.type === "btw") {
    node = bubble("in btw", `Message while working (${{ host: "host", api: "API client", board: "board", agent: "agent" }[e.from] || "?"})`, e.text);
  } else if (e.type === "btw_applied") {
    node = el("div", "msg state", "↳ the agent picked up the message");
  } else if (e.type === "state") {
    node = el("div", "msg state", `→ ${look(e.state)[2]}`);
  } else if (e.type === "status") {
    node = bubble("status", "Status", e.text);
  } else if (e.type === "tool") {
    const fresh = toolCard(e);
    const old = toolEls.get(e.id);
    if (old) { old.replaceWith(fresh); toolEls.set(e.id, fresh); return; }   // same call, newer status
    toolEls.set(e.id, fresh);
    node = fresh;
  } else if (e.type === "delegated" || e.type === "child_result") {
    const done = e.type === "child_result";
    node = el("div", "msg link");
    const a = el("a", "", done ? `← ${e.agent} ${look(e.state)[2]}: ${e.topic}` : `→ delegated to ${e.agent}: ${e.topic}`);
    a.href = "#";
    a.addEventListener("click", ev => { ev.preventDefault(); if (findNode(e.child_id)) select(e.child_id); });
    node.append(a);
  }
  if (node) box.insertBefore(node, answerEl);
}

function renderAnswer(t) {
  answerEl.replaceChildren();
  if (t.output) answerEl.append(el("div", "who", "Answer"), el("div", `body${FINAL.has(t.state) ? "" : " live"}`, t.output));
  if (t.error) answerEl.append(el("div", "body err", t.error));
  answerEl.hidden = !t.output && !t.error;
}

let controlsKey = "";      // which reply form is showing; only rebuilt when it changes (keeps focus)

function renderControls(t) {
  const waiting = t.state === "input_required" || t.state === "auth_required";
  const continues = t.parent_id === null && FINAL.has(t.state);
  const btw = !!t.accepts_btw && !FINAL.has(t.state);       // a message while the agent works
  const mode = waiting ? "reply" : continues ? "follow" : btw ? "btw" : "";
  const key = `${t.id}:${mode}`;
  if (key === controlsKey) return;
  controlsKey = key;
  document.querySelectorAll("#log .reply").forEach(f => f.remove());
  if (!mode) return;
  const form = el("form", "reply");
  const box = el("textarea");
  box.rows = 2;
  box.placeholder = { reply: "Reply to the agent…", follow: "Follow up in the same conversation…",
    btw: "Send a message while the agent works (it reads it at its next step)…" }[mode];
  box.value = draft;
  box.addEventListener("input", () => { draft = box.value; });
  const go = el("button", "primary small", "Send");
  go.type = "submit";
  form.append(box, go);
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const text = box.value.trim();
    if (!text) return;
    go.disabled = true;
    form.querySelectorAll(".error").forEach(p => p.remove());
    try {
      if (mode === "follow") {
        const n = await api("POST", "/admin/tasks", { agent_id: t.worker_id, text, context_id: t.context_id });
        location.href = `task.html?id=${encodeURIComponent(n.id)}`;     // a follow-up is a new task tree
        return;
      }
      await api("POST", `/admin/tasks/${t.id}/message`, { text });
    } catch (e) { form.append(el("p", "error", e.message)); go.disabled = false; return; }
    draft = ""; box.value = ""; go.disabled = false;       // btw: the form stays for the next message
    pollPanel();
  });
  logBox().append(form);
}

function fillHeader(t) {
  $("p-agent-icon").replaceChildren(agentIcon(t.worker_id, t.worker_has_icon));
  $("p-icon").replaceChildren(statusIcon(t.state));
  $("p-topic").textContent = t.topic;
  $("p-agent").textContent = `${t.worker_name} · ${look(t.state)[2]}`;
  $("p-cancel").hidden = FINAL.has(t.state);
}

function openPanel() {
  const t = findNode(selected);
  if (!t) return;
  const box = logBox();
  box.replaceChildren();
  lastSeq = 0; toolEls = new Map(); panelTask = selected; draft = ""; controlsKey = "";
  answerEl = el("div", "msg answer");
  box.append(answerEl);
  fillHeader(t);
  $("panel").hidden = false;
  pollPanel();
}

let panelBusy = false;
async function pollPanel() {
  if (selected === null || panelBusy) return;
  panelBusy = true;
  const id = selected;
  try {
    const r = await api("GET", `/admin/tasks/${id}/events?after=${lastSeq}`);
    if (id !== selected || id !== panelTask) return;           // selection changed meanwhile
    const stick = atBottom();
    for (const e of r.events) { addEvent(e, r.task); lastSeq = e.seq; }
    fillHeader(r.task);
    renderAnswer(r.task);
    renderControls(r.task);
    if (stick) logBox().scrollTop = logBox().scrollHeight;
  } catch (_) { /* transient; the next tick retries */ }
  finally { panelBusy = false; }
}

$("p-cancel").addEventListener("click", () => cancel(selected));
$("cancel-root").addEventListener("click", () => cancel(rootId));

async function cancel(id) {
  const n = findNode(id);
  const subs = n ? n.children.filter(c => !FINAL.has(c.state)).length : 0;
  if (subs && !window.confirm(`Cancel this task and its ${subs} running sub-task(s)?`)) return;
  try { await api("POST", `/admin/tasks/${id}/cancel`); } catch (_) { /* shown by the next refresh */ }
  await refresh();
}

// --------------------------------------------------------------- legend
(function legend() {
  const svg = $("legend");
  const item = (x, kind, text) => {
    svg.appendChild(svgEl("path", { d: `M${x} 12H${x + 30}`, "marker-end": `url(#lg-${kind})` }, `edge ${kind}`));
    svg.appendChild(svgEl("text", { x: x + 38, y: 16 }, "legend-text")).textContent = text;
  };
  const defs = svgEl("defs");
  for (const kind of ["wait", "par"]) {
    const m = svgEl("marker", { id: `lg-${kind}`, viewBox: "0 0 10 10", refX: "9", refY: "5",
      markerWidth: "8", markerHeight: "8", orient: "auto" });
    m.appendChild(svgEl("path", { d: "M0 0L10 5L0 10z" }, `m-${kind}`));
    defs.appendChild(m);
  }
  svg.appendChild(defs);
  item(0, "wait", "requester waits");
  item(190, "par", "requester continues meanwhile");
})();

// ---------------------------------------------------------------- refresh
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    const next = await api("GET", `/admin/tasks/${rootId}`);
    const sig = JSON.stringify(next);
    const changed = sig !== treeSig;        // redrawing restarts the spinners, so only when needed
    treeSig = sig;
    tree = next;
    $("root-title").textContent = tree.topic;
    document.title = `${tree.topic} – Workbench`;
    const running = (n) => !FINAL.has(n.state) || n.children.some(running);   // anything in the tree still busy?
    $("root-agent").replaceChildren(agentIcon(tree.worker_id, tree.worker_has_icon));
    $("root-icon").replaceChildren(statusIcon(FINAL.has(tree.state) && running(tree) ? "working" : tree.state));
    $("cancel-root").hidden = !running(tree);
    if (selected !== null && !findNode(selected)) { selected = null; $("panel").hidden = true; }
    if (changed) drawTree();
  } catch (e) {
    if (e.status === 404) $("root-title").textContent = "Task not found";
  } finally { busy = false; }
}

(async function init() {
  if (!rootId || !(await resumeSession())) { location.href = "index.html"; return; }
  await refresh();
  setInterval(refresh, 1500);
  setInterval(pollPanel, 1000);
})();
