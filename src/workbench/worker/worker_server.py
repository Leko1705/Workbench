"""WorkerServer: a plug-and-play A2A worker agent.

Subclass it, implement `run_task()`, call `.run(...)`. You get:

- an A2A JSON-RPC endpoint at /a2a/, protected by bearer tokens
- a public Agent Card at /.well-known/agent-card.json (always current)
- an admin API at /admin/*  (browser session cookie + CSRF token, or explicit HTTP Basic)
- a web UI at /ui/ that manages everything above (card, skills, tools, tokens,
  and joining / leaving workbenches)

Workbench model
---------------
* Each inbound token can be *bound* to one workbench. The workbench of a request
  is derived from the authenticated token; `metadata.workbench.url` is only
  cross-checked, never trusted.
* A worker joins a workbench from the UI (workbench URL + invite code). Both
  sides exchange tokens: the worker sends a fresh inbound token (so the workbench
  can call it) and receives the token it uses to call the workbench. No shared
  password is involved and either side can revoke independently.
* `run_task` receives a `WorkbenchConnection`: this worker's authenticated link to
  the workbench of the caller. It exposes what a task may do (e.g. list the
  participants) and never exposes the token.

Worker <-> workbench protocol (implemented by the workbench server). This is
separate from the public API v1 that third parties use through `WorkbenchClient`.

    GET  /wb/worker/v1/info                public  -> {"name", "description", "version"}
    POST /wb/worker/v1/join                public  {"invite_code", "worker": {...}, "inbound_token"}
                                                   -> {"status": "accepted"|"pending"|"rejected", ...}
                                                   (any 4xx counts as "rejected")
    GET  /wb/worker/v1/join/{request_id}   Bearer <poll_token>  -> same shape as join
    POST /wb/worker/v1/leave               Bearer <worker token>
    GET  /wb/api/v1/participants           Bearer <worker token>  (public API, via WorkbenchClient)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
from abc import abstractmethod
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator
from starlette.applications import Starlette
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJSON
from starlette.routing import Route
from starlette.datastructures import Headers

from a2a.helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentInterface, AgentSkill
from a2a.utils.errors import InvalidParamsError, InvalidRequestError

from .context import TaskContext, _current
from .tools import Tool, ToolError
from .web_auth import SAFE_METHODS, LoginLimiter, SecurityHeaders, SessionStore, origin_allowed
from ..extensions import BTW_EXTENSION_URI, BTW_PATH
from ..join_token import decode_join_token
from ..workbench import BoardChannel, BoardMessage, FileContent, FileInfo, Participant, PingInfo, TaskInfo, WorkbenchClient, WorkbenchError

UI_DIR = Path(__file__).parent / "ui"
WORKER_API = "/wb/worker/v1"


# ---------------------------------------------------------------- models
class Skill(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class CardUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    version: str | None = None
    icon_url: str | None = Field(default=None, max_length=350_000)

    @field_validator("icon_url")
    @classmethod
    def _icon_scheme(cls, v: str | None) -> str | None:
        if v and not v.startswith(("https://", "http://", "data:image/")):
            raise ValueError("icon_url must be an http(s) URL or a data:image/... URI")
        return v


class ToolToggle(BaseModel):
    enabled: bool


class WorkbenchIn(BaseModel):
    base_url: str
    token: str  # the token THIS worker uses when calling that workbench


class JoinIn(BaseModel):
    """Either `base_url` + `invite_code`, or a `join_token` that contains both."""
    base_url: str | None = None
    invite_code: str | None = None
    join_token: str | None = None

    @model_validator(mode="after")
    def _one_way(self) -> "JoinIn":
        if self.join_token:
            if self.base_url or self.invite_code:
                raise ValueError("give either a join_token or base_url + invite_code, not both")
            try:
                self.base_url, self.invite_code = decode_join_token(self.join_token)
            except ValueError as e:
                raise ValueError("the join token is invalid") from e
        elif not (self.base_url and self.invite_code):
            raise ValueError("give a join_token, or base_url and invite_code")
        return self


class LoginIn(BaseModel):
    username: str
    password: str


class WorkbenchInfo(BaseModel):
    """What a workbench says about itself before we send it any credentials."""
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    description: str = ""
    version: str = ""

    @field_validator("name", "description", mode="before")
    @classmethod
    def _truncate(cls, v: Any, info: ValidationInfo) -> Any:
        limit = 200 if info.field_name == "name" else 2000
        return v[:limit] if isinstance(v, str) else v


class JoinResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["accepted", "pending", "rejected"]
    workbench_token: str | None = None   # accepted: what this worker uses to call the workbench
    request_id: str | None = None        # pending
    poll_token: str | None = None        # pending
    message: str = ""


class WorkbenchConnection:
    """This worker's authenticated link to one workbench; handed to `run_task`.

    It offers what a task may do with the workbench. The token is kept out of reach
    on purpose, so a tool or prompt helper cannot leak it. Joining and leaving are
    the server's business and are not exposed here.
    """

    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/")
        self._api = WorkbenchClient(self.base_url, token, http=http)   # shared client, not owned

    def __repr__(self) -> str:
        return f"WorkbenchConnection({self.base_url!r})"

    async def participants(self) -> list[Participant]:
        """The other agents currently in this workbench."""
        return await self._api.participants()

    async def start_task(self, agent_id: str, text: str, *, parent_task_id: str | None = None,
                         context_id: str | None = None) -> TaskInfo:
        """Start a task on another agent through the workbench (see `WorkbenchClient`)."""
        return await self._api.start_task(agent_id, text, parent_task_id=parent_task_id,
                                          context_id=context_id)

    async def get_task(self, task_id: str, *, wait: float = 0) -> TaskInfo:
        return await self._api.get_task(task_id, wait=wait)

    async def list_tasks(self, scope: str = "started") -> list[TaskInfo]:
        return await self._api.list_tasks(scope)

    async def send_message(self, task_id: str, text: str) -> TaskInfo:
        """Add a message to a task you started (answers a waiting task, or reaches a running one)."""
        return await self._api.send_message(task_id, text)

    async def board_channels(self) -> list[BoardChannel]:
        return await self._api.board_channels()

    async def board_post(self, channel: str, text: str, *, kind: str = "post",
                         to: list[str] | None = None) -> BoardMessage:
        """Post to a board channel, or to a worker's mailbox (`@<worker id>`)."""
        return await self._api.board_post(channel, text, kind=kind, to=to)

    async def board_read(self, channel: str, *, after: int = 0, wait: float = 0, limit: int = 100,
                         newest: bool = False) -> list[BoardMessage]:
        return await self._api.board_read(channel, after=after, wait=wait, limit=limit, newest=newest)

    async def board_feed(self, *, after: int = 0, wait: float = 0, limit: int = 100) -> list[BoardMessage]:
        return await self._api.board_feed(after=after, wait=wait, limit=limit)

    async def files_list(self, prefix: str = "") -> list[FileInfo]:
        return await self._api.files_list(prefix)

    async def file_read(self, path: str, *, version: int | None = None) -> FileContent:
        return await self._api.file_read(path, version=version)

    async def file_write(self, path: str, content: str, *, base_version: int, note: str = "") -> FileInfo:
        return await self._api.file_write(path, content, base_version=base_version, note=note)

    async def file_append(self, path: str, content: str, *, note: str = "") -> FileInfo:
        return await self._api.file_append(path, content, note=note)

    async def file_delete(self, path: str) -> None:
        await self._api.file_delete(path)

    async def ping(self) -> PingInfo:
        """Reachability and token check; raises `WorkbenchError` (see `WorkbenchClient.ping`)."""
        return await self._api.ping()

    async def cancel_task(self, task_id: str) -> None:
        await self._api.cancel_task(task_id)

    async def run_task(self, agent_id: str, text: str, *, parent_task_id: str | None = None,
                       context_id: str | None = None) -> TaskInfo:
        """Start a task on another agent and wait for its result."""
        return await self._api.run_task(agent_id, text, parent_task_id=parent_task_id,
                                        context_id=context_id)


# ------------------------------------------------------------ helpers
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _norm_url(url: Any) -> str:
    return url.strip().rstrip("/") if isinstance(url, str) else ""


_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _is_loopback(url: str) -> bool:
    return (urlparse(url).hostname or "") in _LOOPBACK


def _check_workbench_url(url: str, allow_insecure: bool) -> None:
    """Raises ValueError if we must not send credentials to this URL."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("base_url must be an http(s) URL")
    if p.username or p.password or p.query or p.fragment:
        raise ValueError("base_url must not contain credentials, a query or a fragment")
    if p.scheme == "http" and not _is_loopback(url) and not allow_insecure:
        raise ValueError("workbench URLs must use https (plain http only for localhost)")


def _active(rec: dict | None) -> dict | None:
    return rec if rec is not None and rec.get("active", True) else None


class _Running:
    """A task whose `run_task` is executing right now, and the messages queued for it."""

    def __init__(self, owner: str):
        self.owner = owner            # hash of the bearer token that started the task
        self.inbox: list[str] = []
        self.open = True              # False once the task no longer reads its inbox


class _BearerAuth:
    """Pure-ASGI bearer-token check (safe for SSE streaming)."""

    def __init__(self, app, verify):
        self.app, self.verify = app, verify

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        scheme, _, token = Headers(scope=scope).get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and self.verify(token):
            return await self.app(scope, receive, send)
        resp = JSONResponse({"error": "invalid or missing bearer token"}, 401,
                            headers={"WWW-Authenticate": "Bearer"})
        await resp(scope, receive, send)
        return None


class WorkerServer(AgentExecutor):
    """Base class for worker agents.

    "btw" support: a subclass whose `run_task` reads `take_messages()` between its steps (see
    `AgenticWorker`) sets `supports_btw = True`. The caller can then add a message to a task
    that is still running; it waits in an inbox until the task takes it up.

    Agent-specific configuration goes into the constructor, server-specific
    configuration into `run()` / `build_app()`.

    State precedence: values saved by the admin API (in `state_file`) win over
    the constructor arguments after a restart. Delete the file to reset.
    The state file also holds the outbound workbench tokens in plain text, so
    it is written with mode 0600.
    """

    supports_btw: bool = False

    def __init__(
        self,
        name: str | None = None,
        description: str = "",
        version: str = "0.1.0",
        *,
        skills: Iterable[Skill] = (),
        icon_url: str = "",
        tools: Iterable[Tool] = (),
        state_file: str | os.PathLike | None = None,
        task_store: TaskStore | None = None,
        streaming: bool = True,
        http_client: httpx.AsyncClient | None = None,
    ):
        super().__init__()
        name = name or type(self).__name__
        # one HTTP client for all outbound workbench calls (inject your own for proxies/mTLS/tests)
        self._http = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_http = http_client is None

        self.tools: dict[str, Tool] = {}
        for t in tools:
            if t.name in self.tools:
                raise ValueError(f"duplicate tool name {t.name!r}")
            self.tools[t.name] = t

        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"
        self.state_file = Path(state_file or f"{slug}.state.json")
        self._state: dict[str, Any] = {
            "name": name,
            "description": description,
            "version": version,
            "icon_url": icon_url,       # http(s) URL on this server, or a data:image/... URI
            "skills": {s.id: s.model_dump() for s in skills},
            "disabled_tools": [],
            # sha256(inbound token) -> {"label": str, "workbench": url | None, "active": bool}
            "tokens": {},
            # workbench url -> token this worker uses to call it
            "workbenches": {},
            # workbench url -> {"request_id", "poll_token", "token_hash"} (join awaiting approval)
            "pending_joins": {},
            # workbench url -> the name it reported (for display only)
            "workbench_names": {},
        }
        if self.state_file.exists():
            self._state.update(json.loads(self.state_file.read_text()))
        # older state files stored `hash -> label`
        self._state["tokens"] = {
            h: v if isinstance(v, dict) else {"label": v, "workbench": None}
            for h, v in self._state["tokens"].items()}
        self._wb_connections: dict[str, WorkbenchConnection] = {
            _norm_url(u): WorkbenchConnection(u, t, self._http)
            for u, t in self._state["workbenches"].items()}

        self._running: dict[str, _Running] = {}     # a2a task id -> running task (see take_messages)
        self._task_store = task_store or InMemoryTaskStore()
        # The live card. The SDK handler keeps a reference to this very object,
        # so mutating it in place changes what the running server uses.
        self.agent_card = AgentCard(
            capabilities=AgentCapabilities(streaming=streaming, push_notifications=False),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
        )
        self._apply_state_to_card()
        self._public_url = ""
        self._sessions = SessionStore()
        self._limiter = LoginLimiter()

    # ----------------------------------------------- to implement / override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Resolve the caller's workbench from its token, then call `run_task`."""
        rec = self._caller_token_record(context)
        if rec is None:  # the middleware makes this unreachable; fail closed anyway
            raise InvalidRequestError("unauthenticated request")

        wb_meta = context.metadata.get("workbench")
        claimed = _norm_url(wb_meta.get("url")) if isinstance(wb_meta, dict) else ""
        bound = rec.get("workbench")

        if bound is None:
            if claimed:
                raise InvalidParamsError("this token is not bound to a workbench")
            workbench = None            # direct (non-workbench) caller
        else:
            if claimed and claimed != bound:
                raise InvalidParamsError("workbench in metadata does not match the caller's token")
            workbench = self._wb_connections.get(bound)
            if workbench is None:
                raise InvalidRequestError(f"workbench {bound} is not registered on this worker")
        task_id = wb_meta.get("task_id") if isinstance(wb_meta, dict) else None
        reset = _current.set(TaskContext(workbench, task_id if isinstance(task_id, str) else None))
        running = _Running(self._caller_token_hash(context))
        if context.task_id:
            self._running[context.task_id] = running
        try:
            await self.run_task(context, event_queue, workbench)
        finally:
            running.open = False
            if context.task_id and self._running.get(context.task_id) is running:
                del self._running[context.task_id]
            _current.reset(reset)

    @abstractmethod
    async def run_task(self, context: RequestContext, event_queue: EventQueue,
                       workbench: WorkbenchConnection | None) -> None:
        """Handle one A2A request.

        `workbench` is the workbench the caller's token is bound to, or None for a
        direct caller. `self.agent_card` and tools are always current.
        """

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Default: record the cancellation. The SDK interrupts run_task() itself.

        Override for cleanup, but still end with a CANCELED status.
        """
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    # ----------------------------------------------------- btw (messages while working)
    def take_messages(self, task_id: str) -> list[str]:
        """Messages that arrived for a running task since the last call (and forget them).

        Call it between the steps of `run_task`, never in the middle of one. It is not a
        coroutine on purpose: with no `await` between taking the messages and calling
        `close_inbox`, nothing can slip in unseen.
        """
        r = self._running.get(task_id)
        if r is None or not r.inbox:
            return []
        msgs, r.inbox = r.inbox, []
        return msgs

    def close_inbox(self, task_id: str) -> None:
        """The task is about to finish: messages that arrive later are refused (409)."""
        r = self._running.get(task_id)
        if r is not None:
            r.open = False

    def _queue_message(self, task_id: str, text: str, owner: str) -> tuple[int, str]:
        """(HTTP status, detail) of an attempt to queue a message for a running task."""
        if not self.supports_btw:
            return 501, "this agent cannot take messages while it works"
        r = self._running.get(task_id)
        if r is None or not secrets.compare_digest(r.owner.encode(), owner.encode()):
            return 404, "no such running task"          # same answer for foreign tasks
        if not r.open:
            return 409, "the task is finishing and no longer reads messages"
        if sum(len(m) for m in r.inbox) + len(text) > 200_000:
            return 429, "too many queued messages"
        r.inbox.append(text)
        return 200, "queued"

    # ----------------------------------------------------- helpers for subclasses
    async def reply_text(self, event_queue: EventQueue, text: str) -> None:
        await event_queue.enqueue_event(new_text_message(text))

    def active_tools(self) -> list[Tool]:
        off = set(self._state["disabled_tools"])
        return [t for t in self.tools.values() if t.name not in off]

    def tool_schemas(self, fmt: Literal["anthropic", "openai"] = "anthropic") -> list[dict]:
        """Schemas of the currently enabled tools, ready for an LLM call."""
        return [t.to_anthropic() if fmt == "anthropic" else t.to_openai()
                for t in self.active_tools()]

    async def call_tool(self, name: str, **kwargs: Any) -> Any:
        tool = {t.name: t for t in self.active_tools()}.get(name)
        if tool is None:
            raise ToolError(f"unknown or disabled tool {name!r}")
        return await tool.invoke(**kwargs)

    # ------------------------------------------------------------- internals
    def _save(self) -> None:
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._state, f, indent=2)
        os.replace(tmp, self.state_file)

    def _apply_state_to_card(self) -> None:
        s, c = self._state, self.agent_card
        c.name, c.description, c.version = s["name"], s["description"], s["version"]
        c.icon_url = s["icon_url"]
        del c.capabilities.extensions[:]
        if self.supports_btw:
            c.capabilities.extensions.append(AgentExtension(
                uri=BTW_EXTENSION_URI, required=False,
                description="Takes messages that arrive while a task runs and reads them at its next step."))
        del c.skills[:]
        for sk in s["skills"].values():
            c.skills.append(AgentSkill(id=sk["id"], name=sk["name"],
                                       description=sk["description"],
                                       tags=sk["tags"], examples=sk["examples"]))

    def _config(self) -> dict[str, Any]:
        off = set(self._state["disabled_tools"])
        return {
            "card": MessageToDict(self.agent_card),
            "tools": [{**t.describe(), "enabled": t.name not in off}
                      for t in self.tools.values()],
            "tokens": sorted(({"label": r["label"], "workbench": r["workbench"],
                               "active": r.get("active", True)}
                              for r in self._state["tokens"].values()),
                             key=lambda r: r["label"]),
            "workbenches": sorted(self._wb_connections),   # URLs only, never tokens
            "pending_joins": sorted(self._state["pending_joins"]),
        }

    def _token_record(self, token: str) -> dict | None:
        return _active(self._state["tokens"].get(_hash_token(token)))

    def _verify_token(self, token: str) -> bool:
        return self._token_record(token) is not None

    @staticmethod
    def _caller_token_hash(context: RequestContext) -> str:
        auth = context.call_context.state.get("headers", {}).get("authorization", "")
        return _hash_token(auth.partition(" ")[2])

    def _caller_token_record(self, context: RequestContext) -> dict | None:
        """The token record of the request's Authorization header (set by the SDK)."""
        auth = context.call_context.state.get("headers", {}).get("authorization", "")
        scheme, _, token = auth.partition(" ")
        return self._token_record(token) if scheme.lower() == "bearer" else None

    # ------------------------------------------------ workbench handshake (worker side)
    async def _wb_call(self, method: str, url: str, path: str, *, bearer: str | None = None,
                       json: Any = None, allow_client_error: bool = False,
                       expect_json: bool = True) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        try:
            resp = await self._http.request(method, f"{url}{WORKER_API}{path}",
                                            json=json, headers=headers)
        except httpx.HTTPError as e:
            raise WorkbenchError(f"workbench request {path} failed: {e}") from e
        if resp.status_code >= 500 or (resp.status_code >= 400 and not allow_client_error):
            raise WorkbenchError(f"workbench request {path} failed: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            data = None
        if expect_json and resp.status_code < 400 and data is None:
            raise WorkbenchError(f"workbench request {path} failed: response is not JSON")
        return resp.status_code, data

    async def _wb_info(self, url: str) -> WorkbenchInfo:
        _, data = await self._wb_call("GET", url, "/info")
        try:
            return WorkbenchInfo.model_validate(data)
        except ValidationError as e:
            raise WorkbenchError(f"workbench sent invalid info: {e}") from e

    @staticmethod
    def _parse_join(status: int, data: Any, *, initial: bool) -> JoinResponse:
        if status >= 400:
            msg = str(data.get("detail") or data.get("message") or "") if isinstance(data, dict) else ""
            return JoinResponse(status="rejected", message=msg or f"HTTP {status}")
        try:
            resp = JoinResponse.model_validate(data)
        except ValidationError as e:
            raise WorkbenchError(f"workbench sent an invalid join response: {e}") from e
        if resp.status == "accepted" and not resp.workbench_token:
            raise WorkbenchError("workbench accepted the join but sent no token")
        if initial and resp.status == "pending" and not (resp.request_id and resp.poll_token):
            raise WorkbenchError("workbench sent an incomplete pending response")
        return resp

    async def _wb_request_join(self, url: str, invite_code: str, worker: dict,
                               inbound_token: str) -> JoinResponse:
        """Ask to join. `inbound_token` is what the workbench must use to call this worker."""
        status, data = await self._wb_call(
            "POST", url, "/join", allow_client_error=True,
            json={"invite_code": invite_code, "worker": worker, "inbound_token": inbound_token})
        return self._parse_join(status, data, initial=True)

    async def _wb_poll_join(self, url: str, request_id: str, poll_token: str) -> JoinResponse:
        status, data = await self._wb_call("GET", url, f"/join/{request_id}",
                                           bearer=poll_token, allow_client_error=True)
        return self._parse_join(status, data, initial=False)

    async def _wb_leave(self, url: str, token: str) -> None:
        await self._wb_call("POST", url, "/leave", bearer=token, expect_json=False)

    def _finalize_join(self, url: str, token_hash: str, workbench_token: str) -> None:
        """Join accepted: store the outbound token, activate the inbound one."""
        st = self._state
        st["workbenches"][url] = workbench_token
        self._wb_connections[url] = WorkbenchConnection(url, workbench_token, self._http)
        if token_hash in st["tokens"]:
            st["tokens"][token_hash]["active"] = True
        st["pending_joins"].pop(url, None)
        self._save()

    # ------------------------------------------------------------- ASGI app
    def build_app(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 9999,
        admin_password: str | None = None,
        admin_user: str = "admin",
        public_url: str | None = None,
        ui: bool = True,
        ui_dir: str | os.PathLike | None = None,
        secure_cookies: bool | None = None,
        allow_insecure_workbenches: bool = False,
        session_idle_timeout: float = 1800,
        session_max_age: float = 12 * 3600,
    ) -> FastAPI:
        """Build the ASGI app (use this to deploy under your own uvicorn/gunicorn).

        `secure_cookies` defaults to True when `public_url` is https. Run behind
        TLS in production: the session cookie and all tokens travel in the clear
        otherwise.
        """
        admin_password = admin_password or os.getenv("ADMIN_PASSWORD")
        if not admin_password:
            raise ValueError("admin_password is required (or set ADMIN_PASSWORD)")
        public_url = (public_url or f"http://{host}:{port}").rstrip("/")
        self._public_url = public_url
        secure = public_url.startswith("https://") if secure_cookies is None else secure_cookies
        cookie_name = "__Host-wk_session" if secure else "wk_session"
        self._sessions = SessionStore(session_idle_timeout, session_max_age)
        self._limiter = LoginLimiter()
        sessions, limiter = self._sessions, self._limiter
        own_hosts = {urlparse(public_url).netloc}

        del self.agent_card.supported_interfaces[:]
        self.agent_card.supported_interfaces.append(AgentInterface(
            protocol_binding="JSONRPC", protocol_version="1.0", url=f"{public_url}/a2a/"))

        handler = DefaultRequestHandler(agent_executor=self, task_store=self._task_store,
                                        agent_card=self.agent_card)
        async def btw(request: StarletteRequest):
            """Queue a message for a running task (the "btw" extension, see extensions.py)."""
            auth = request.headers.get("authorization", "")
            try:
                body = await request.json()
                task_id, text = body["task_id"], body["text"]
                if not (isinstance(task_id, str) and isinstance(text, str) and text.strip()
                        and len(text) <= 100_000):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return StarletteJSON({"detail": "expected {task_id, text}"}, 400)
            status, detail = self._queue_message(task_id, text, _hash_token(auth.partition(" ")[2]))
            return StarletteJSON({"detail": detail, "queued": status == 200}, status)

        a2a_app = Starlette(routes=[*create_jsonrpc_routes(handler, rpc_url="/"),
                                    Route(BTW_PATH, btw, methods=["POST"])])
        a2a_app.add_middleware(_BearerAuth, verify=self._verify_token)

        # ------------------------------------------------------ admin auth
        def credentials_ok(user: str, password: str) -> bool:
            return bool(secrets.compare_digest(user.encode(), admin_user.encode())
                        & secrets.compare_digest(password.encode(), admin_password.encode()))

        def origin_ok(request: Request) -> bool:
            return origin_allowed(request.headers.get("origin"),
                                  own_hosts | {request.headers.get("host", "")})

        def authenticate(request: Request, *, csrf: bool = True) -> dict | None:
            """Session cookie (+CSRF for unsafe methods) or an explicit Basic header.

            No `WWW-Authenticate` challenge is ever sent, so browsers never cache
            Basic credentials and can't replay them cross-site.
            """
            sess = sessions.get(request.cookies.get(cookie_name))
            if sess is not None:
                if csrf and request.method not in SAFE_METHODS:
                    if not origin_ok(request):
                        raise HTTPException(403, "cross-origin request rejected")
                    sent = request.headers.get("x-csrf-token", "")
                    if not secrets.compare_digest(sent.encode(), sess["csrf"].encode()):
                        raise HTTPException(403, "missing or invalid CSRF token")
                return sess
            scheme, _, cred = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() == "basic":
                try:
                    user, _, pw = base64.b64decode(cred).decode().partition(":")
                except Exception:
                    return None
                if credentials_ok(user, pw):
                    return {"basic": True}
            return None

        def require_admin(request: Request) -> None:
            if authenticate(request) is None:
                raise HTTPException(401, "authentication required")

        def valid_wb_url(raw: str) -> str:
            url = _norm_url(raw)
            try:
                _check_workbench_url(url, allow_insecure_workbenches)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            return url

        session = APIRouter(prefix="/admin")

        @session.post("/login")
        async def login(body: LoginIn, request: Request, response: Response):
            if not origin_ok(request):
                raise HTTPException(403, "cross-origin request rejected")
            key = request.client.host if request.client else "unknown"
            wait = limiter.retry_after(key)
            if wait:
                raise HTTPException(429, "too many failed logins",
                                    headers={"Retry-After": str(wait)})
            if not credentials_ok(body.username, body.password):
                limiter.failure(key)
                raise HTTPException(401, "bad credentials")
            limiter.success(key)
            token, sess = sessions.create()
            response.set_cookie(cookie_name, token, httponly=True, secure=secure,
                                samesite="strict", max_age=int(session_max_age), path="/")
            return {"csrf_token": sess["csrf"], "expires_in": int(session_max_age)}

        @session.get("/session")
        async def session_info(request: Request):
            sess = sessions.get(request.cookies.get(cookie_name))
            if sess is None:
                return {"authenticated": False}
            return {"authenticated": True, "csrf_token": sess["csrf"]}

        @session.post("/logout")
        async def logout(request: Request, response: Response):
            if authenticate(request) is None:
                raise HTTPException(401, "authentication required")
            sessions.destroy(request.cookies.get(cookie_name))
            response.delete_cookie(cookie_name, path="/")
            return {"ok": True}

        admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
        st = self._state

        @admin.get("/config")
        async def get_config():
            return self._config()

        @admin.patch("/card")
        async def patch_card(update: CardUpdate):
            st.update(update.model_dump(exclude_none=True))
            self._apply_state_to_card(); self._save()
            return self._config()["card"]

        @admin.put("/skills/{skill_id}")
        async def put_skill(skill_id: str, skill: Skill):
            if skill.id != skill_id:
                raise HTTPException(400, "id in path and body differ")
            st["skills"][skill_id] = skill.model_dump()
            self._apply_state_to_card(); self._save()
            return self._config()["card"]

        @admin.delete("/skills/{skill_id}")
        async def delete_skill(skill_id: str):
            if st["skills"].pop(skill_id, None) is None:
                raise HTTPException(404, "no such skill")
            self._apply_state_to_card(); self._save()
            return self._config()["card"]

        @admin.patch("/tools/{tool_name}")
        async def toggle_tool(tool_name: str, body: ToolToggle):
            if tool_name not in self.tools:
                raise HTTPException(404, "no such tool")
            off = set(st["disabled_tools"])
            (off.discard if body.enabled else off.add)(tool_name)
            st["disabled_tools"] = sorted(off)
            self._save()
            return self._config()["tools"]

        @admin.post("/tokens")
        async def create_token(label: str, workbench: str | None = None):
            """Create an inbound token, optionally bound to a joined workbench."""
            bound = _norm_url(workbench) or None
            if bound is not None and bound not in self._wb_connections:
                raise HTTPException(404, "no such workbench; join it first")
            token = secrets.token_urlsafe(32)
            st["tokens"][_hash_token(token)] = {"label": label, "workbench": bound, "active": True}
            self._save()
            return {"label": label, "workbench": bound, "token": token,
                    "note": "shown only once"}

        @admin.delete("/tokens")
        async def revoke_token(label: str):
            hashes = [h for h, r in st["tokens"].items() if r["label"] == label]
            if not hashes:
                raise HTTPException(404, "no such token label")
            for h in hashes:
                del st["tokens"][h]
            self._save()
            return {"revoked": label}

        # ------------------------------------------------------ workbenches
        @admin.get("/workbench")
        async def list_workbenches():
            def entry(u: str, status: str) -> dict:
                return {"base_url": u, "status": status, "name": st["workbench_names"].get(u, "")}
            return ([entry(u, "joined") for u in sorted(self._wb_connections)]
                    + [entry(u, "pending") for u in sorted(st["pending_joins"])])

        @admin.get("/workbench/status")
        async def workbench_status(base_url: str):
            """Is a joined workbench reachable, and does it still accept our token?

            `connection` is "connected", "down" (unreachable) or "error" (it answered
            with an error, e.g. it dropped us).
            """
            url = _norm_url(base_url)
            wb = self._wb_connections.get(url)
            if wb is None:
                raise HTTPException(404, "not joined to this workbench")
            try:
                info = await wb.ping()
            except WorkbenchError as e:
                return {"connection": "down" if e.status is None else "error", "message": str(e),
                        "name": st["workbench_names"].get(url, "")}
            if info.name and st["workbench_names"].get(url) != info.name:
                st["workbench_names"][url] = info.name
                self._save()
            return {"connection": "connected", "name": info.name}

        @admin.post("/workbench")
        async def put_workbench(body: WorkbenchIn):
            """Register a workbench with a token you already have (scripting/dev).

            This only configures the outbound direction; use /workbench/join so the
            workbench also gets a token for calling this worker.
            """
            url = valid_wb_url(body.base_url)
            self._wb_connections[url] = WorkbenchConnection(url, body.token, self._http)
            st["workbenches"][url] = body.token
            self._save()
            return {"registered": url}

        @admin.get("/workbench/preview")
        async def preview_workbench(base_url: str):
            """Ask a workbench who it is, so the admin can confirm before sending an invite code."""
            url = valid_wb_url(base_url)
            try:
                info = await self._wb_info(url)
            except WorkbenchError as e:
                raise HTTPException(502, str(e)) from e
            return {"base_url": url, "info": info.model_dump()}

        @admin.post("/workbench/join")
        async def join_workbench(body: JoinIn):
            url = valid_wb_url(body.base_url)
            if url in self._wb_connections:
                raise HTTPException(409, "already joined; leave it first")
            if url in st["pending_joins"]:
                raise HTTPException(409, "a join request is already pending; leave it first")
            if _is_loopback(self._public_url) and not _is_loopback(url):
                raise HTTPException(
                    400, f"this worker's public_url ({self._public_url}) is a loopback address "
                         "the workbench cannot reach; start the server with a real public_url")

            inbound = secrets.token_urlsafe(32)      # what the workbench will use to call us
            worker = {"name": self.agent_card.name, "description": self.agent_card.description,
                      "version": self.agent_card.version,
                      "a2a_url": f"{self._public_url}/a2a/",
                      "card_url": f"{self._public_url}/.well-known/agent-card.json"}
            try:
                resp = await self._wb_request_join(url, body.invite_code, worker, inbound)
            except WorkbenchError as e:
                raise HTTPException(502, str(e)) from e

            if resp.status == "rejected":
                raise HTTPException(403, resp.message or "join rejected")
            try:
                st["workbench_names"][url] = (await self._wb_info(url)).name
            except WorkbenchError:
                pass                      # the name is only for display
            ih = _hash_token(inbound)
            st["tokens"][ih] = {"label": f"workbench:{url}", "workbench": url, "active": False}
            if resp.status == "accepted":
                self._finalize_join(url, ih, resp.workbench_token)
                return {"status": "joined"}
            st["pending_joins"][url] = {"request_id": resp.request_id,
                                        "poll_token": resp.poll_token, "token_hash": ih}
            self._save()
            return {"status": "pending", "message": resp.message}

        @admin.post("/workbench/join/poll")
        async def poll_join(base_url: str):
            """Check a pending join; finalizes it as soon as the workbench approves."""
            url = _norm_url(base_url)
            if url in self._wb_connections:
                return {"status": "joined"}
            pj = st["pending_joins"].get(url)
            if pj is None:
                raise HTTPException(404, "no pending join for this workbench")
            try:
                resp = await self._wb_poll_join(url, pj["request_id"], pj["poll_token"])
            except WorkbenchError as e:
                raise HTTPException(502, str(e)) from e
            if url not in st["pending_joins"]:          # finished by a concurrent poll
                return {"status": "joined" if url in self._wb_connections else "rejected"}
            if resp.status == "accepted":
                self._finalize_join(url, pj["token_hash"], resp.workbench_token)
                return {"status": "joined"}
            if resp.status == "rejected":
                st["tokens"].pop(pj["token_hash"], None)
                st["pending_joins"].pop(url, None)
                self._save()
                return {"status": "rejected", "message": resp.message}
            return {"status": "pending"}

        @admin.delete("/workbench")
        async def leave_workbench(base_url: str):
            """Leave a workbench (or cancel a pending join): tell it, forget its token,
            revoke every inbound token bound to it."""
            url = _norm_url(base_url)
            wb = self._wb_connections.pop(url, None)
            pending = st["pending_joins"].pop(url, None)
            token = st["workbenches"].pop(url, None)
            st["workbench_names"].pop(url, None)
            if wb is None and pending is None:
                raise HTTPException(404, "no such workbench")
            notified = False
            if token is not None:
                try:
                    await self._wb_leave(url, token)
                    notified = True
                except WorkbenchError:
                    pass            # best effort; the local credentials are dropped regardless
            revoked = [h for h, r in st["tokens"].items() if r["workbench"] == url]
            for h in revoked:
                del st["tokens"][h]
            self._save()
            return {"removed": url, "revoked_tokens": len(revoked), "notified": notified}

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            yield
            if self._owns_http:
                await self._http.aclose()

        app = FastAPI(title=self._state["name"], lifespan=lifespan)
        app.add_middleware(SecurityHeaders)

        @app.get("/.well-known/agent-card.json")
        async def public_card():
            return JSONResponse(MessageToDict(self.agent_card))

        app.include_router(session)
        app.include_router(admin)
        app.mount("/a2a", a2a_app)
        if ui:
            @app.get("/", include_in_schema=False)
            async def root():
                return RedirectResponse("/ui/")
            app.mount("/ui", StaticFiles(directory=ui_dir or UI_DIR, html=True), name="ui")
        return app

    # ------------------------------------------------------------------ run
    def run(
        self,
        host: str = "127.0.0.1",
        port: int = 9999,
        *,
        admin_password: str | None = None,
        admin_user: str = "admin",
        public_url: str | None = None,
        log_level: str = "info",
        **kwargs: Any,
    ) -> None:
        """Build the app and serve it (blocking).

        Keyword arguments of `build_app` (ui, secure_cookies, ...) are accepted;
        everything else is passed to uvicorn.
        """
        app_opts = {k: kwargs.pop(k) for k in
                    ("ui", "ui_dir", "secure_cookies", "allow_insecure_workbenches",
                     "session_idle_timeout", "session_max_age") if k in kwargs}
        app = self.build_app(host=host, port=port, admin_password=admin_password,
                             admin_user=admin_user, public_url=public_url, **app_opts)
        uvicorn.run(app, host=host, port=port, log_level=log_level, **kwargs)
