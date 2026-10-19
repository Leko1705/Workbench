"""The shared files: a folder everybody can read and write, with history and permissions.

The second half of the board (see board.py): channels are for talking, files are for the things
the talk is about. Like the board it lives on the workbench so it can be governed and watched:

* the top-level folders are *workspaces*, created by hosts; read and write label lists decide who
  may use each one (empty = every label)
* nothing is ever overwritten: an edit adds a version, a delete adds a "deleted" version, and the
  whole history stays readable
* a write must say which version it is based on (`base_version`), so two agents editing the same
  file cannot silently overwrite each other; appending is atomic and needs no version
* only the file's owner (or a host) may delete it
* the author of every version is set by the server from the token
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from typing import Any

from .board import NAME_RE, Caller
from .store import Record, VersionConflict, WorkbenchStore

SHARED = "shared"
MAX_CONTENT = 200_000        # characters per version
MAX_FILES = 5000
MAX_DEPTH = 8
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")


class WorkspaceError(Exception):
    """A request that must be refused. `status` is the suggested HTTP status; `current` is set
    for version conflicts."""

    def __init__(self, message: str, status: int = 400, current: int | None = None):
        super().__init__(message)
        self.status, self.current = status, current


def clean_path(path: str) -> str:
    path = path.strip().strip("/")
    parts = path.split("/")
    if len(path) > 200 or not 2 <= len(parts) <= MAX_DEPTH or not all(SEGMENT_RE.match(p) for p in parts) \
            or any(p in (".", "..") or p.endswith(".") for p in parts):
        raise WorkspaceError("a path is <workspace>/<folders...>/<file> with names of letters, digits, "
                             "'.', '_', '-' and spaces (at most 8 levels, 200 characters)")
    if not NAME_RE.match(parts[0]):
        raise WorkspaceError("the first part of a path is a workspace name (a-z, 0-9, - and _)")
    return "/".join(parts)


class Workspace:
    def __init__(self, store: WorkbenchStore, *, writes_per_minute: int = 60):
        self.store = store
        self.writes_per_minute = writes_per_minute
        self._writes: dict[str, deque[float]] = defaultdict(deque)

    async def ensure_defaults(self) -> None:
        if not await self.store.list_workspaces():
            await self.store.add_workspace({"name": SHARED, "description": "Open to everyone.",
                                            "read_label_ids": [], "write_label_ids": [],
                                            "created_at": time.time()})

    # ----------------------------------------------------------- permissions
    @staticmethod
    def _allowed(label_ids: list[str], caller: Caller) -> bool:
        return caller.outside or not label_ids or bool(set(label_ids) & set(caller.label_ids))

    def can_read(self, space: Record, caller: Caller) -> bool:
        return self._allowed(space["read_label_ids"], caller)

    def can_write(self, space: Record, caller: Caller) -> bool:
        return self._allowed(space["write_label_ids"], caller)

    async def _space_for(self, path: str, caller: Caller, *, write: bool) -> Record:
        space = await self.store.get_workspace(path.split("/", 1)[0])
        if space is None or not self.can_read(space, caller):       # same answer for hidden spaces
            raise WorkspaceError("no such workspace or file", 404)
        if write and not self.can_write(space, caller):
            raise WorkspaceError("you may read this workspace but not write to it", 403)
        return space

    async def spaces(self, caller: Caller) -> list[dict[str, Any]]:
        return [{"name": w["name"], "description": w["description"], "can_write": self.can_write(w, caller)}
                for w in sorted(await self.store.list_workspaces(), key=lambda w: w["created_at"])
                if self.can_read(w, caller)]

    # ------------------------------------------------------------------ reads
    async def list(self, caller: Caller, *, prefix: str = "") -> list[Record]:
        readable = {w["name"] for w in await self.store.list_workspaces() if self.can_read(w, caller)}
        return [f for f in await self.store.list_files(prefix=prefix.lstrip("/")) if f["workspace"] in readable]

    async def read(self, path: str, caller: Caller, *, version: int | None = None) -> Record:
        path = clean_path(path)
        await self._space_for(path, caller, write=False)
        v = await self.store.get_version(path, version)
        head = await self.store.get_file(path)
        if v is None or head is None or (version is None and head["deleted"]):
            raise WorkspaceError("no such file", 404)
        return {**v, "owner": head["owner"], "latest": head["version"]}

    async def history(self, path: str, caller: Caller, limit: int = 100) -> list[Record]:
        path = clean_path(path)
        await self._space_for(path, caller, write=False)
        if await self.store.get_file(path) is None:
            raise WorkspaceError("no such file", 404)
        return await self.store.list_versions(path, limit)

    # ----------------------------------------------------------------- writes
    def _check_rate(self, caller: Caller) -> None:
        if caller.kind == "host":
            return
        q, now = self._writes[caller.id], time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.writes_per_minute:
            raise WorkspaceError(f"too many file writes: at most {self.writes_per_minute} per minute", 429)
        q.append(now)

    async def _add(self, path: str, caller: Caller, content: str, *, expected: int | None, note: str,
                   deleted: bool = False) -> Record:
        try:
            return await self.store.append_version(path, {
                "content": content, "size": len(content), "deleted": deleted,
                "author": caller.sender, "note": note[:200]}, expected=expected, owner=caller.sender)
        except VersionConflict as e:
            raise WorkspaceError(f"the file changed: it is at version {e.current} now. Read it again and "
                                 "apply your change to that version.", 409, current=e.current) from e

    async def write(self, path: str, caller: Caller, content: str, *, base_version: int | None,
                    note: str = "") -> Record:
        """Create a file (`base_version` 0) or add a version based on `base_version`."""
        path = clean_path(path)
        await self._space_for(path, caller, write=True)
        if len(content) > MAX_CONTENT:
            raise WorkspaceError(f"a file may hold at most {MAX_CONTENT} characters", 413)
        if base_version is None:
            raise WorkspaceError("say which version you are changing (base_version; 0 for a new file)")
        head = await self.store.get_file(path)
        if head is None and len(await self.store.list_files()) >= MAX_FILES:
            raise WorkspaceError("too many files", 429)
        self._check_rate(caller)
        return await self._add(path, caller, content, expected=base_version, note=note)

    async def append(self, path: str, caller: Caller, content: str, *, note: str = "") -> Record:
        """Add text to the end of a file (creating it), atomically: no version needed."""
        path = clean_path(path)
        await self._space_for(path, caller, write=True)
        if not content:
            raise WorkspaceError("nothing to append")
        self._check_rate(caller)
        for _ in range(5):                      # another writer may slip in between read and write
            head = await self.store.get_file(path)
            cur = await self.store.get_version(path) if head and not head["deleted"] else None
            base = (cur["content"] if cur else "")
            if len(base) + len(content) > MAX_CONTENT:
                raise WorkspaceError(f"a file may hold at most {MAX_CONTENT} characters", 413)
            try:
                return await self._add(path, caller, base + content, expected=cur["version"] if cur else 0,
                                       note=note or "append")
            except WorkspaceError as e:
                if e.status != 409:
                    raise
        raise WorkspaceError("the file is being changed too often, try again", 409)

    async def delete(self, path: str, caller: Caller, *, base_version: int | None = None) -> Record:
        path = clean_path(path)
        await self._space_for(path, caller, write=True)
        head = await self.store.get_file(path)
        if head is None or head["deleted"]:
            raise WorkspaceError("no such file", 404)
        if not caller.outside and head["owner"].get("id") != caller.id:
            raise WorkspaceError("only the owner of a file (or a host) may delete it", 403)
        self._check_rate(caller)
        return await self._add(path, caller, "", expected=base_version, note="deleted", deleted=True)
