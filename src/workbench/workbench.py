"""Client library for the workbench's public HTTP API (v1).

For third-party programs (and anything else) that talk to a workbench with a
workbench **API token**. It has nothing to do with joining a workbench as a
worker; that handshake lives in `WorkerServer`.

    async with WorkbenchClient("https://workbench.example", api_token="...") as wb:
        for p in await wb.participants():
            print(p.id, p.name)

Endpoints used (all require `Authorization: Bearer <api token>`):

    GET  /wb/api/v1/ping                  -> {"name", "version"}  (is the token still valid?)
    GET  /wb/api/v1/participants          -> [{"id", "name", "description", "skills"}, ...]
    POST /wb/api/v1/tasks                 start a (sub) task on another agent -> task
    GET  /wb/api/v1/tasks/{id}?wait=N     status/result of a task you started (long poll)
    GET  /wb/api/v1/tasks?scope=...       tasks you started ("started") or were given ("assigned")
    GET  /wb/api/v1/board/channels        channels you may read (and write), plus your mailbox
    GET  /wb/api/v1/board/feed?after=N    new messages across everything you may read (long poll)
    GET  /wb/api/v1/board/channels/{c}/messages   one channel (`@<worker id>` is a mailbox)
    POST /wb/api/v1/board/channels/{c}/messages   post to a channel or to a worker's mailbox
    GET  /wb/api/v1/files?prefix=         shared files you may read (path, version, size, owner)
    GET  /wb/api/v1/files/{path}          one file with content (`?version=N` an older one, `?history=true`)
    PUT  /wb/api/v1/files/{path}          create (base_version 0) or update it; a stale base_version is a 409
    POST /wb/api/v1/files/{path}/append   add text to the end of a file, atomically
    DELETE /wb/api/v1/files/{path}        delete a file you own (its history stays)
    POST /wb/api/v1/tasks/{id}/message    answer a waiting task, or add a message to a running one
    POST /wb/api/v1/tasks/{id}/cancel     cancel a task you started (and its sub tasks)

More operations will be added here, always under /wb/api/v1.
"""
from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

API_V1 = "/wb/api/v1"


class WorkbenchError(Exception):
    """The workbench was unreachable, answered with an error, or sent bad data.

    `status` is the HTTP status when the workbench answered, None when it could not
    be reached at all.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Participant(BaseModel):
    """One entry of the workbench roster. Everything here is untrusted text."""
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    description: str = ""
    skills: list[Any] = Field(default_factory=list)

    @field_validator("name", "description", mode="before")
    @classmethod
    def _truncate(cls, v: Any, info: ValidationInfo) -> Any:
        limit = 200 if info.field_name == "name" else 2000
        return v[:limit] if isinstance(v, str) else v


class BoardMessage(BaseModel):
    """A message on the board. `text` is untrusted: it was written by another participant."""
    model_config = ConfigDict(extra="ignore")

    seq: int
    time: float = 0
    channel: str
    sender: dict[str, str] = Field(default_factory=dict)   # {"kind": worker|host|api, "id", "name"}
    kind: str = "post"
    text: str = ""
    to: list[str] = Field(default_factory=list)
    refs: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def _cut(cls, v: Any) -> Any:
        return v[:20_000] if isinstance(v, str) else v


class BoardChannel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""
    kind: str = "channel"      # "channel" or "mailbox" (your own)
    can_write: bool = False


class FileInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    version: int = 0
    size: int = 0
    owner: dict[str, str] = Field(default_factory=dict)
    updated_at: float = 0


class FileContent(BaseModel):
    """One version of a shared file. `content` was written by another participant: untrusted."""
    model_config = ConfigDict(extra="ignore")

    path: str
    version: int
    latest: int = 0                    # the file's newest version
    content: str = ""
    size: int = 0
    deleted: bool = False
    author: dict[str, str] = Field(default_factory=dict)
    owner: dict[str, str] = Field(default_factory=dict)
    note: str = ""
    time: float = 0

    @field_validator("content", mode="before")
    @classmethod
    def _cut(cls, v: Any) -> Any:
        return v[:200_000] if isinstance(v, str) else v


class FileConflict(WorkbenchError):
    """The file changed since you read it; `current` is its newest version."""

    def __init__(self, message: str, current: int | None):
        super().__init__(message, 409)
        self.current = current


class PingInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    version: str = ""

    @field_validator("name", mode="before")
    @classmethod
    def _cut(cls, v: Any) -> Any:
        return v[:200] if isinstance(v, str) else v


class TaskInfo(BaseModel):
    """A task started through the workbench. `output` is untrusted text."""
    model_config = ConfigDict(extra="ignore")

    id: str
    parent_id: str | None = None
    agent_id: str = ""
    input: str = ""
    state: str = ""            # submitted, working, input_required, completed, failed, canceled, ...
    output: str = ""
    status_text: str = ""
    error: str = ""
    done: bool = False         # no run in progress: finished, or waiting (input_required) for a reply

    @field_validator("input", "output", "status_text", "error", mode="before")
    @classmethod
    def _cut(cls, v: Any) -> Any:
        return v[:100_000] if isinstance(v, str) else v


class WorkbenchClient:
    def __init__(self, base_url: str, api_token: str, *, timeout: float = 10.0,
                 http: httpx.AsyncClient | None = None):
        if not api_token:
            raise ValueError("api_token is required")
        self.base_url = base_url.rstrip("/")
        self._token = api_token
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)

    def __repr__(self) -> str:  # never leak the token into logs
        return f"WorkbenchClient({self.base_url!r})"

    async def _call(self, method: str, path: str, *, json: Any = None,
                    params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        extra = {} if timeout is None else {"timeout": timeout}
        try:
            resp = await self._http.request(method, f"{self.base_url}{API_V1}{path}", json=json,
                                            params=params, headers={"Authorization": f"Bearer {self._token}"},
                                            **extra)
        except httpx.HTTPError as e:
            raise WorkbenchError(f"workbench request {path} failed: {e}") from e
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            msg = f"workbench request {path} failed: HTTP {resp.status_code}" \
                  + (f": {detail}" if isinstance(detail, str) else "")
            if resp.status_code == 409 and "X-Current-Version" in resp.headers:
                raise FileConflict(msg, int(resp.headers["X-Current-Version"]))
            raise WorkbenchError(msg, resp.status_code)
        try:
            return resp.json()
        except ValueError as e:
            raise WorkbenchError(f"workbench request {path} failed: response is not JSON") from e

    async def ping(self) -> PingInfo:
        """Is the workbench reachable and does it still accept our token? Raises `WorkbenchError`
        (`status` None: unreachable, 401/403: token revoked, else: the workbench is unwell)."""
        try:
            return PingInfo.model_validate(await self._call("GET", "/ping", timeout=3.0))
        except ValidationError as e:
            raise WorkbenchError(f"workbench sent an invalid ping answer: {e}") from e

    async def participants(self) -> list[Participant]:
        data = await self._call("GET", "/participants")
        try:
            return [Participant.model_validate(p) for p in data]
        except (ValidationError, TypeError) as e:
            raise WorkbenchError(f"workbench sent an invalid participant list: {e}") from e

    async def _task(self, method: str, path: str, **kw: Any) -> TaskInfo:
        data = await self._call(method, path, **kw)
        try:
            return TaskInfo.model_validate(data)
        except ValidationError as e:
            raise WorkbenchError(f"workbench sent an invalid task: {e}") from e

    async def start_task(self, agent_id: str, text: str, *, parent_task_id: str | None = None,
                         context_id: str | None = None) -> TaskInfo:
        """Ask the workbench to run `text` on another agent; returns at once.

        Pass the id of the task you are working on as `parent_task_id` so that the
        workbench files the new task under it (a worker finds its own task id in
        `context.metadata["workbench"]["task_id"]`).
        """
        return await self._task("POST", "/tasks", json={
            "agent_id": agent_id, "text": text, "parent_task_id": parent_task_id,
            "context_id": context_id})

    async def get_task(self, task_id: str, *, wait: float = 0) -> TaskInfo:
        """Current state of a task you started; `wait` seconds long-polls for it to end."""
        return await self._task("GET", f"/tasks/{task_id}", params={"wait": wait} if wait else None,
                                timeout=wait + 10 if wait else None)

    async def list_tasks(self, scope: str = "started") -> list[TaskInfo]:
        """Tasks you started (`scope="started"`) or that were given to you (`"assigned"`)."""
        data = await self._call("GET", "/tasks", params={"scope": scope})
        try:
            return [TaskInfo.model_validate(t) for t in data]
        except (ValidationError, TypeError) as e:
            raise WorkbenchError(f"workbench sent an invalid task list: {e}") from e

    async def send_message(self, task_id: str, text: str) -> TaskInfo:
        """Add a message to a task you started. If it is `input_required` the task resumes. If
        it is still running, the message is queued and the agent reads it at its next step
        ("btw"); that fails with a `WorkbenchError` if the agent does not support it."""
        return await self._task("POST", f"/tasks/{task_id}/message", json={"text": text})

    async def cancel_task(self, task_id: str) -> None:
        await self._call("POST", f"/tasks/{task_id}/cancel")

    async def run_task(self, agent_id: str, text: str, *, parent_task_id: str | None = None,
                       context_id: str | None = None) -> TaskInfo:
        """`start_task`, then wait until the task has ended. Cancelling this coroutine
        (e.g. because the caller's own task was cancelled) cancels the sub task too."""
        task = await self.start_task(agent_id, text, parent_task_id=parent_task_id,
                                     context_id=context_id)
        try:
            while not task.done:
                task = await self.get_task(task.id, wait=20)
        except BaseException:
            try:
                await self.cancel_task(task.id)
            except Exception:
                pass
            raise
        return task

    # ------------------------------------------------------------------ board
    async def board_channels(self) -> list[BoardChannel]:
        """Channels you may read and your mailbox (`can_write` says whether you may post)."""
        data = await self._call("GET", "/board/channels")
        try:
            return [BoardChannel.model_validate(c) for c in data]
        except (ValidationError, TypeError) as e:
            raise WorkbenchError(f"workbench sent an invalid channel list: {e}") from e

    @staticmethod
    def _messages(data: Any) -> list[BoardMessage]:
        try:
            return [BoardMessage.model_validate(m) for m in data]
        except (ValidationError, TypeError) as e:
            raise WorkbenchError(f"workbench sent invalid board messages: {e}") from e

    async def board_post(self, channel: str, text: str, *, kind: str = "post",
                         to: list[str] | None = None, refs: list[dict[str, Any]] | None = None) -> BoardMessage:
        """Post to a channel, or to one worker's mailbox with channel `@<worker id>`.
        The sender is set by the workbench from your token."""
        data = await self._call("POST", f"/board/channels/{channel}/messages",
                                json={"text": text, "kind": kind, "to": to or [], "refs": refs or []})
        return self._messages([data])[0]

    async def board_read(self, channel: str, *, after: int = 0, wait: float = 0, limit: int = 100,
                         newest: bool = False) -> list[BoardMessage]:
        """Messages of one channel after `after` (a `seq`); `wait` seconds long-polls for new ones,
        `newest` returns the latest `limit` instead."""
        params: dict[str, Any] = {"after": after, "limit": limit, "newest": newest}
        if wait:
            params["wait"] = wait
        return self._messages(await self._call("GET", f"/board/channels/{channel}/messages", params=params,
                                               timeout=wait + 10 if wait else None))

    async def board_feed(self, *, after: int = 0, wait: float = 0, limit: int = 100) -> list[BoardMessage]:
        """New messages across every channel you may read plus your mailbox, after `after`."""
        params: dict[str, Any] = {"after": after, "limit": limit}
        if wait:
            params["wait"] = wait
        return self._messages(await self._call("GET", "/board/feed", params=params,
                                               timeout=wait + 10 if wait else None))

    # ------------------------------------------------------------------ files
    async def files_list(self, prefix: str = "") -> list[FileInfo]:
        """The shared files you may read."""
        try:
            return [FileInfo.model_validate(f) for f in await self._call("GET", "/files", params={"prefix": prefix})]
        except (ValidationError, TypeError) as e:
            raise WorkbenchError(f"workbench sent an invalid file list: {e}") from e

    async def file_read(self, path: str, *, version: int | None = None) -> FileContent:
        params = {"version": version} if version is not None else None
        try:
            return FileContent.model_validate(await self._call("GET", f"/files/{path}", params=params))
        except ValidationError as e:
            raise WorkbenchError(f"workbench sent an invalid file: {e}") from e

    async def file_history(self, path: str) -> list[dict[str, Any]]:
        return await self._call("GET", f"/files/{path}", params={"history": True})

    async def file_write(self, path: str, content: str, *, base_version: int, note: str = "") -> FileInfo:
        """Create a file (`base_version` 0) or update it. Raises `FileConflict` if someone else changed
        it since `base_version`: read it again and redo your change."""
        data = await self._call("PUT", f"/files/{path}", json={"content": content, "base_version": base_version,
                                                               "note": note})
        return FileInfo.model_validate(data)

    async def file_append(self, path: str, content: str, *, note: str = "") -> FileInfo:
        """Add text to the end of a file (created if needed). Atomic: no version needed."""
        return FileInfo.model_validate(await self._call("POST", f"/files/{path}/append",
                                                        json={"content": content, "note": note}))

    async def file_delete(self, path: str) -> None:
        """Delete a file you own. Its history stays readable to hosts."""
        await self._call("DELETE", f"/files/{path}")

    async def aclose(self) -> None:
        if self._owns_http:      # a shared client is closed by whoever created it
            await self._http.aclose()

    async def __aenter__(self) -> "WorkbenchClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()