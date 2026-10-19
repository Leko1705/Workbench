"use strict";

// The shared files: what the agents have written, with every version. Hosts can read everything,
// write new versions and delete; nothing is overwritten, so a delete can be undone by writing again.

onUnauthorized = () => { location.href = "index.html"; };
mountSettingsIcon("settings-link");

let files = [];          // heads from the server
let selected = null;     // path
let shown = null;        // what the viewer currently shows: {path, latest, version}
let editing = false;
let creating = false;

const view = $("view");
const enc = p => p.split("/").map(encodeURIComponent).join("/");
const when = ts => new Date(ts * 1000).toLocaleString();

// -------------------------------------------------------------------- list
function renderTree() {
  const tree = $("tree");
  const newBtn = el("button", "primary small", "New file");
  newBtn.type = "button";
  newBtn.addEventListener("click", () => { creating = true; editing = false; selected = null; renderTree(); renderNew(); });
  const groups = new Map();
  for (const f of files) {
    const ws = f.path.split("/", 1)[0];
    if (!groups.has(ws)) groups.set(ws, []);
    groups.get(ws).push(f);
  }
  const nodes = [newBtn];
  if (!files.length) nodes.push(el("p", "muted pad", "No files yet."));
  for (const [ws, list] of groups) {
    nodes.push(el("h3", "", ws));
    for (const f of list) {
      const b = el("button", `chan${f.path === selected ? " sel" : ""}`, f.path.slice(ws.length + 1));
      b.type = "button";
      b.title = `${f.path}\nversion ${f.version}, ${f.size} characters\nowner ${f.owner.name || "?"}, ${when(f.updated_at)}`;
      b.addEventListener("click", () => { selected = f.path; creating = false; editing = false; renderTree(); open(f.path); });
      nodes.push(b);
    }
  }
  tree.replaceChildren(...nodes);
}

// ------------------------------------------------------------------ viewer
async function open(path, version) {
  let cur, history;
  try {
    [cur, history] = await Promise.all([
      api("GET", `/admin/files/${enc(path)}${version ? `?version=${version}` : ""}`),
      api("GET", `/admin/files/${enc(path)}?history=true`)]);
  } catch (e) {
    view.replaceChildren(el("p", "error pad", e.message));
    return;
  }
  shown = { path, latest: cur.latest, version: cur.version };
  const box = el("div", "fbox");
  const head = el("div", "fhead");
  head.append(el("strong", "", path));

  const sel = el("select", "vsel");
  for (const h of history) {
    const o = el("option", "", `v${h.version}${h.deleted ? " (deleted)" : ""} · ${h.author.name} · ${when(h.time)}`);
    o.value = h.version;
    sel.append(o);
  }
  sel.value = cur.version;
  sel.addEventListener("change", () => open(path, +sel.value));
  head.append(sel);

  const actions = el("span", "factions");
  const latest = cur.version === cur.latest;
  if (latest) {
    actions.append(button("Edit", "", () => { editing = true; open(path); }),
      button("Delete", "danger", async () => {
        if (!window.confirm(`Delete ${path}? Its history stays and you can write it again.`)) return;
        try { await api("DELETE", `/admin/files/${enc(path)}`); selected = null; shown = null; view.replaceChildren(); await refresh(); }
        catch (e) { box.append(el("p", "error", e.message)); }
      }));
  } else {
    actions.append(button("Restore this version", "", async () => {
      try { await api("PUT", `/admin/files/${enc(path)}`, { content: cur.content, base_version: cur.latest, note: `restored v${cur.version}` });
            await refresh(); await open(path); }
      catch (e) { box.append(el("p", "error", e.message)); }
    }));
  }
  head.append(actions);
  box.append(head);
  box.append(el("p", "muted fmeta", `version ${cur.version} of ${cur.latest} · ${cur.size} characters · written by ${cur.author.name} ` +
    `(${cur.author.kind}) · owner ${cur.owner.name}${cur.note ? ` · “${cur.note}”` : ""}`));

  if (editing && latest) {
    const ta = el("textarea", "fedit"); ta.value = cur.content; ta.rows = 24; ta.spellcheck = false;
    const note = el("input"); note.placeholder = "Note about the change (optional)"; note.maxLength = 200;
    const save = el("button", "primary small", "Save as new version"); save.type = "button";
    const err = el("p", "error"); err.hidden = true;
    save.addEventListener("click", async () => {
      try {
        await api("PUT", `/admin/files/${enc(path)}`, { content: ta.value, base_version: cur.latest, note: note.value });
        editing = false; await refresh(); await open(path);
      } catch (e) { err.textContent = e.message; err.hidden = false; }
    });
    box.append(ta, note, save, button("Cancel", "", () => { editing = false; open(path); }), err);
  } else {
    box.append(el("pre", "fcontent", cur.deleted ? "(this version is a delete)" : cur.content));
  }
  view.replaceChildren(box);
}

function renderNew() {
  const form = el("form", "fbox card");
  const path = el("input"); path.placeholder = "shared/notes/plan.md"; path.required = true;
  const ta = el("textarea", "fedit"); ta.rows = 16; ta.spellcheck = false;
  const err = el("p", "error"); err.hidden = true;
  const go = el("button", "primary small", "Create"); go.type = "submit";
  form.append(el("h3", "plain", "New file"), el("label", "", "Path (workspace/folders/file)"), path, ta, go, err);
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    try {
      await api("PUT", `/admin/files/${enc(path.value.trim())}`, { content: ta.value, base_version: 0 });
      creating = false; selected = path.value.trim().replace(/^\/+|\/+$/g, "");
      await refresh(); await open(selected);
    } catch (e) { err.textContent = e.message; err.hidden = false; }
  });
  view.replaceChildren(form);
}

// ------------------------------------------------------------------ loading
async function refresh() {
  try { files = await api("GET", "/admin/files"); } catch (_) { return; }
  renderTree();
  // someone changed the file we are looking at: show it (unless the host is typing)
  const head = files.find(f => f.path === selected);
  if (head && shown && shown.path === selected && !editing && !creating && head.version !== shown.latest) open(selected);
  if (!head && selected && shown && !editing) { selected = null; shown = null; view.replaceChildren(); }
}

(async function init() {
  if (!(await resumeSession())) { location.href = "index.html"; return; }
  await refresh();
  if (!selected) view.replaceChildren(el("p", "muted pad", "Select a file, or create one."));
  setInterval(refresh, 3000);
})();
