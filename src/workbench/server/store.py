"""Storage abstraction of the workbench server.

Records are plain JSON-compatible dicts so that a file, SQL or key/value backend can
store them without a mapping layer. All methods are async so a backend may do I/O.
Implement `WorkbenchStore` and hand an instance to `WorkbenchServer(store=...)` to
change where the state lives; `InMemoryStore` is the default (state is lost on restart).

Record shapes (the server only relies on the fields named here):

  worker   {"id", "name", "description", "version", "a2a_url", "card_url",
            "inbound_token",      # secret: what the workbench sends when it calls the worker
            "token_hash",         # sha256 of the token the worker uses to call the workbench
            "status",             # "pending" | "active" | "denied"
            "request_id", "poll_token_hash", "pending_token",  # join handshake, see server
            "label_ids", "skills", "btw", "skills_at",     # public card skills, cached
            "invite_id", "created_at"}
  event    {"seq", "time", "type", ...}  per task, append-only; `seq` counts up from 1.
            Types: "input" (text), "btw" (text, queued while running), "btw_applied", "state" (state), "status" (text), "tool" (id, tool, status,
            input, output), "delegated"/"child_result" (child_id, agent, topic[, state]).
  channel  {"name", "description", "read_label_ids", "write_label_ids", "created_at"}
            Board channels. Empty label lists mean "every label". Mailboxes ("@<worker id>") are
            not stored; they exist implicitly for every worker.
  message  {"seq", "time", "channel", "sender": {"kind": "worker"|"host"|"api", "id", "name"},
            "kind", "text", "to": [worker ids], "refs": [{"type", "id"}]}
            `seq` counts up over all channels together, so one cursor can follow everything.
  workspace {"name", "description", "read_label_ids", "write_label_ids", "created_at"}
            The top-level folders of the shared files; empty label lists mean "every label".
  file     {"path", "workspace", "version", "deleted", "owner": {kind, id, name}, "size",
            "created_at", "updated_at"}   head of a file; `version` is the latest one
  file version {"path", "version", "content", "size", "deleted", "author": {kind, id, name},
            "note", "time"}   append-only: an edit or a delete adds a version, nothing is overwritten
  label    {"id", "name", "created_at"}   a cluster of workers; workers carry `label_ids` (at least one)
  api_key  {"id", "name", "prefix", "key_hash", "created_at", "last_used_at"}
  settings {"name", "description", "max_task_depth", "max_active_tasks",
            "communication": {"policy": "open" | "restricted", "rules": [[from_label_id, to_label_id], ...]}}
            (only the keys that were ever changed; the server falls back to its start-up values)
  invite   {"id", "label", "code_hash", "auto_approve", "uses_left", "expires_at", "created_at"}
  task     {"id", "topic", "root_id", "parent_id", "depth", "worker_id", "requested_by", "context_id", "a2a_task_id",
            "state", "input", "output", "status_text", "error", "created_at", "updated_at"}
"""
from __future__ import annotations

import copy
import time
from abc import ABC, abstractmethod
from typing import Any

Record = dict[str, Any]


class VersionConflict(Exception):
    """A file changed since the writer last read it. `current` is its latest version."""

    def __init__(self, current: int):
        super().__init__(f"the file is at version {current}")
        self.current = current


class WorkbenchStore(ABC):
    # ---------------------------------------------------------------- workers
    @abstractmethod
    async def add_worker(self, worker: Record) -> None: ...

    @abstractmethod
    async def get_worker(self, worker_id: str) -> Record | None: ...

    @abstractmethod
    async def find_worker(self, **match: Any) -> Record | None:
        """First worker whose fields equal `match` (e.g. token_hash=..., request_id=...)."""

    @abstractmethod
    async def list_workers(self) -> list[Record]: ...

    @abstractmethod
    async def update_worker(self, worker_id: str, **fields: Any) -> Record | None: ...

    @abstractmethod
    async def delete_worker(self, worker_id: str) -> bool: ...

    # ---------------------------------------------------------------- invites
    @abstractmethod
    async def add_invite(self, invite: Record) -> None: ...

    @abstractmethod
    async def get_invite(self, invite_id: str) -> Record | None: ...

    @abstractmethod
    async def find_invite(self, code_hash: str) -> Record | None: ...

    @abstractmethod
    async def list_invites(self) -> list[Record]: ...

    @abstractmethod
    async def update_invite(self, invite_id: str, **fields: Any) -> Record | None: ...

    @abstractmethod
    async def delete_invite(self, invite_id: str) -> bool: ...

    # ------------------------------------------------------------------ tasks
    @abstractmethod
    async def add_task(self, task: Record) -> None: ...

    @abstractmethod
    async def get_task(self, task_id: str) -> Record | None: ...

    @abstractmethod
    async def update_task(self, task_id: str, **fields: Any) -> Record | None: ...

    @abstractmethod
    async def list_tasks(self, *, root_id: str | None = None, worker_id: str | None = None,
                         roots_only: bool = False) -> list[Record]:
        """Tasks ordered by creation. `root_id` selects one whole tree (root included)."""


    # ----------------------------------------------------------------- labels
    @abstractmethod
    async def add_label(self, label: Record) -> None: ...

    @abstractmethod
    async def get_label(self, label_id: str) -> Record | None: ...

    @abstractmethod
    async def list_labels(self) -> list[Record]: ...

    @abstractmethod
    async def update_label(self, label_id: str, **fields: Any) -> Record | None: ...

    @abstractmethod
    async def delete_label(self, label_id: str) -> bool: ...

    # --------------------------------------------------------------- api keys
    @abstractmethod
    async def add_api_key(self, key: Record) -> None: ...

    @abstractmethod
    async def find_api_key(self, key_hash: str) -> Record | None: ...

    @abstractmethod
    async def list_api_keys(self) -> list[Record]: ...

    @abstractmethod
    async def update_api_key(self, key_id: str, **fields: Any) -> Record | None: ...

    @abstractmethod
    async def delete_api_key(self, key_id: str) -> bool: ...

    # --------------------------------------------------------------- settings
    @abstractmethod
    async def get_settings(self) -> Record:
        """The stored settings (empty if nothing was ever saved)."""

    @abstractmethod
    async def update_settings(self, **fields: Any) -> Record:
        """Merge top-level keys into the settings; returns the result."""

    # --------------------------------------------------------- board channels
    @abstractmethod
    async def add_channel(self, channel: Record) -> None: ...

    @abstractmethod
    async def get_channel(self, name: str) -> Record | None: ...

    @abstractmethod
    async def list_channels(self) -> list[Record]: ...

    @abstractmethod
    async def update_channel(self, name: str, **fields: Any) -> Record | None: ...

    @abstractmethod
    async def delete_channel(self, name: str) -> bool:
        """Delete the channel and its messages."""

    # --------------------------------------------------------- board messages
    @abstractmethod
    async def append_message(self, message: Record) -> Record:
        """Store a message; the store assigns `seq` and `time`. Returns the stored record."""

    @abstractmethod
    async def list_messages(self, *, after: int = 0, channels: list[str] | None = None,
                            limit: int = 200, newest: bool = False) -> list[Record]:
        """Messages with `seq` > `after` (oldest first), optionally only from `channels`.
        With `newest` the last `limit` matching messages instead of the first ones."""

    # ------------------------------------------------------------ file spaces
    @abstractmethod
    async def add_workspace(self, space: Record) -> None: ...

    @abstractmethod
    async def get_workspace(self, name: str) -> Record | None: ...

    @abstractmethod
    async def list_workspaces(self) -> list[Record]: ...

    @abstractmethod
    async def update_workspace(self, name: str, **fields: Any) -> Record | None: ...

    @abstractmethod
    async def delete_workspace(self, name: str) -> bool:
        """Delete the workspace with all its files and their history."""

    # ------------------------------------------------------------------ files
    @abstractmethod
    async def get_file(self, path: str) -> Record | None:
        """The file's head (including deleted ones), or None if it never existed."""

    @abstractmethod
    async def list_files(self, *, workspace: str | None = None, prefix: str = "",
                         include_deleted: bool = False) -> list[Record]:
        """File heads sorted by path."""

    @abstractmethod
    async def get_version(self, path: str, version: int | None = None) -> Record | None:
        """One version of a file with its content (the latest if `version` is None)."""

    @abstractmethod
    async def list_versions(self, path: str, limit: int = 100) -> list[Record]:
        """Versions without their content, newest first."""

    @abstractmethod
    async def append_version(self, path: str, version: Record, *, expected: int | None,
                             owner: dict[str, str]) -> Record:
        """Atomically add the next version of a file and return its new head.

        `expected` is the version the writer based its change on: 0 means "the file must not
        exist (or be deleted)", None skips the check. Raises `VersionConflict` otherwise.
        `owner` becomes the file's owner when it is created (or re-created after a delete).
        """

    # ----------------------------------------------------------------- events
    @abstractmethod
    async def append_event(self, task_id: str, event: Record) -> None:
        """Append to a task's event log; the store assigns `seq` and `time`."""

    @abstractmethod
    async def list_events(self, task_id: str, after: int = 0, limit: int = 500) -> list[Record]:
        """Events with `seq` > `after`, oldest first."""


class InMemoryStore(WorkbenchStore):
    def __init__(self) -> None:
        self._workers: dict[str, Record] = {}
        self._invites: dict[str, Record] = {}
        self._tasks: dict[str, Record] = {}
        self._workspaces: dict[str, Record] = {}
        self._files: dict[str, Record] = {}
        self._versions: dict[str, list[Record]] = {}
        self._channels: dict[str, Record] = {}
        self._messages: list[Record] = []
        self._message_seq = 0
        self._labels: dict[str, Record] = {}
        self._api_keys: dict[str, Record] = {}
        self._settings: Record = {}
        self._events: dict[str, list[Record]] = {}
        self._seq: dict[str, int] = {}

    @staticmethod
    def _out(rec: Record | None) -> Record | None:
        return copy.deepcopy(rec) if rec is not None else None

    # workers
    async def add_worker(self, worker):
        self._workers[worker["id"]] = copy.deepcopy(worker)

    async def get_worker(self, worker_id):
        return self._out(self._workers.get(worker_id))

    async def find_worker(self, **match):
        for w in self._workers.values():
            if all(w.get(k) == v for k, v in match.items()):
                return self._out(w)
        return None

    async def list_workers(self):
        return [self._out(w) for w in self._workers.values()]

    async def update_worker(self, worker_id, **fields):
        w = self._workers.get(worker_id)
        if w is not None:
            w.update(copy.deepcopy(fields))
        return self._out(w)

    async def delete_worker(self, worker_id):
        return self._workers.pop(worker_id, None) is not None

    # invites
    async def add_invite(self, invite):
        self._invites[invite["id"]] = copy.deepcopy(invite)

    async def get_invite(self, invite_id):
        return self._out(self._invites.get(invite_id))

    async def find_invite(self, code_hash):
        return next((self._out(i) for i in self._invites.values()
                     if i["code_hash"] == code_hash), None)

    async def list_invites(self):
        return [self._out(i) for i in self._invites.values()]

    async def update_invite(self, invite_id, **fields):
        i = self._invites.get(invite_id)
        if i is not None:
            i.update(copy.deepcopy(fields))
        return self._out(i)

    async def delete_invite(self, invite_id):
        return self._invites.pop(invite_id, None) is not None

    # tasks
    async def add_task(self, task):
        self._tasks[task["id"]] = copy.deepcopy(task)

    async def get_task(self, task_id):
        return self._out(self._tasks.get(task_id))

    async def update_task(self, task_id, **fields):
        t = self._tasks.get(task_id)
        if t is not None:
            t.update(copy.deepcopy(fields))
        return self._out(t)

    async def list_tasks(self, *, root_id=None, worker_id=None, roots_only=False):
        return [self._out(t) for t in self._tasks.values()          # dicts keep insertion order
                if (root_id is None or t["root_id"] == root_id)
                and (worker_id is None or t["worker_id"] == worker_id)
                and (not roots_only or t["parent_id"] is None)]

    # labels
    async def add_label(self, label):
        self._labels[label["id"]] = copy.deepcopy(label)

    async def get_label(self, label_id):
        return self._out(self._labels.get(label_id))

    async def list_labels(self):
        return [self._out(l) for l in self._labels.values()]

    async def update_label(self, label_id, **fields):
        l = self._labels.get(label_id)
        if l is not None:
            l.update(copy.deepcopy(fields))
        return self._out(l)

    async def delete_label(self, label_id):
        return self._labels.pop(label_id, None) is not None

    # api keys
    async def add_api_key(self, key):
        self._api_keys[key["id"]] = copy.deepcopy(key)

    async def find_api_key(self, key_hash):
        return next((self._out(k) for k in self._api_keys.values() if k["key_hash"] == key_hash), None)

    async def list_api_keys(self):
        return [self._out(k) for k in self._api_keys.values()]

    async def update_api_key(self, key_id, **fields):
        k = self._api_keys.get(key_id)
        if k is not None:
            k.update(copy.deepcopy(fields))
        return self._out(k)

    async def delete_api_key(self, key_id):
        return self._api_keys.pop(key_id, None) is not None

    # settings
    async def get_settings(self):
        return copy.deepcopy(self._settings)

    async def update_settings(self, **fields):
        self._settings.update(copy.deepcopy(fields))
        return copy.deepcopy(self._settings)

    # file spaces and files
    MAX_VERSIONS = 200          # kept per file; the oldest are dropped

    async def add_workspace(self, space):
        self._workspaces[space["name"]] = copy.deepcopy(space)

    async def get_workspace(self, name):
        return self._out(self._workspaces.get(name))

    async def list_workspaces(self):
        return [self._out(w) for w in self._workspaces.values()]

    async def update_workspace(self, name, **fields):
        w = self._workspaces.get(name)
        if w is not None:
            w.update(copy.deepcopy(fields))
        return self._out(w)

    async def delete_workspace(self, name):
        existed = self._workspaces.pop(name, None) is not None
        for path in [p for p, f in self._files.items() if f["workspace"] == name]:
            del self._files[path]
            self._versions.pop(path, None)
        return existed

    async def get_file(self, path):
        return self._out(self._files.get(path))

    async def list_files(self, *, workspace=None, prefix="", include_deleted=False):
        return [self._out(f) for p, f in sorted(self._files.items())
                if (workspace is None or f["workspace"] == workspace) and p.startswith(prefix)
                and (include_deleted or not f["deleted"])]

    async def get_version(self, path, version=None):
        versions = self._versions.get(path, [])
        for v in reversed(versions):
            if version is None or v["version"] == version:
                return self._out(v)
        return None

    async def list_versions(self, path, limit=100):
        return [{k: v for k, v in rec.items() if k != "content"}
                for rec in reversed(self._versions.get(path, [])[-limit:])]

    async def append_version(self, path, version, *, expected, owner):
        head = self._files.get(path)
        current = head["version"] if head and not head["deleted"] else 0
        if expected is not None and expected != current:
            raise VersionConflict(head["version"] if head else 0)
        now = time.time()
        number = (head["version"] if head else 0) + 1
        rec = {**copy.deepcopy(version), "path": path, "version": number, "time": now}
        self._versions.setdefault(path, []).append(rec)
        if len(self._versions[path]) > self.MAX_VERSIONS:
            del self._versions[path][0]
        fresh = head is None or head["deleted"]
        self._files[path] = {
            "path": path, "workspace": path.split("/", 1)[0], "version": number,
            "deleted": bool(rec["deleted"]), "size": rec["size"],
            "owner": copy.deepcopy(owner) if fresh else head["owner"],
            "created_at": now if head is None else head["created_at"], "updated_at": now}
        return copy.deepcopy(self._files[path])

    # board
    MAX_MESSAGES = 20_000      # in total; the oldest are dropped

    async def add_channel(self, channel):
        self._channels[channel["name"]] = copy.deepcopy(channel)

    async def get_channel(self, name):
        return self._out(self._channels.get(name))

    async def list_channels(self):
        return [self._out(c) for c in self._channels.values()]

    async def update_channel(self, name, **fields):
        c = self._channels.get(name)
        if c is not None:
            c.update(copy.deepcopy(fields))
        return self._out(c)

    async def delete_channel(self, name):
        existed = self._channels.pop(name, None) is not None
        self._messages = [m for m in self._messages if m["channel"] != name]
        return existed

    async def append_message(self, message):
        self._message_seq += 1
        rec = {**copy.deepcopy(message), "seq": self._message_seq, "time": time.time()}
        self._messages.append(rec)
        if len(self._messages) > self.MAX_MESSAGES:
            del self._messages[0]
        return copy.deepcopy(rec)

    async def list_messages(self, *, after=0, channels=None, limit=200, newest=False):
        wanted = None if channels is None else set(channels)
        found = [m for m in self._messages
                 if m["seq"] > after and (wanted is None or m["channel"] in wanted)]
        found = found[-limit:] if newest else found[:limit]
        return [copy.deepcopy(m) for m in found]

    # events
    MAX_EVENTS = 2000          # per task; the oldest are dropped

    async def append_event(self, task_id, event):
        seq = self._seq[task_id] = self._seq.get(task_id, 0) + 1
        log = self._events.setdefault(task_id, [])
        log.append({**copy.deepcopy(event), "seq": seq, "time": time.time()})
        if len(log) > self.MAX_EVENTS:
            del log[0]

    async def list_events(self, task_id, after=0, limit=500):
        return [copy.deepcopy(e) for e in self._events.get(task_id, []) if e["seq"] > after][:limit]
