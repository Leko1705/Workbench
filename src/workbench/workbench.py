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
    POST /wb/api/v1/tasks/{id}/message    answer a task that waits for input
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
            raise WorkbenchError(f"workbench request {path} failed: HTTP {resp.status_code}"
                                 + (f": {detail}" if isinstance(detail, str) else ""), resp.status_code)
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

    async def aclose(self) -> None:
        if self._owns_http:      # a shared client is closed by whoever created it
            await self._http.aclose()

    async def __aenter__(self) -> "WorkbenchClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()