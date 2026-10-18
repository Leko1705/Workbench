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
            "label_ids", "skills", "skills_at",     # public card skills, cached
            "invite_id", "created_at"}
  event    {"seq", "time", "type", ...}  per task, append-only; `seq` counts up from 1.
            Types: "input" (text), "state" (state), "status" (text), "tool" (id, tool, status,
            input, output), "delegated"/"child_result" (child_id, agent, topic[, state]).
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
