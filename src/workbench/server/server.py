"""WorkbenchServer: the central server that connects workers and delegates their tasks.

Plug and play: `WorkbenchServer().run(admin_password=...)`. The workbench's name and
description are settings kept in the store (defaults: "My Workbench" and no description).

Instead of talking peer to peer, workers ask the workbench to start tasks on other
workers. The workbench relays them over A2A, so every task and sub task is seen and
recorded in a task tree that the hosts can inspect and cancel.

Three audiences, three kinds of credentials
-------------------------------------------
* hosts      username + password -> session cookie (+CSRF) or HTTP Basic, `/admin/*`, UI at `/ui/`
* workers    join once with an invite code (`/wb/worker/v1/*`), then use the token they
             received to call the workbench API (`/wb/api/v1/*`)
* the workbench calls a worker's A2A endpoint with the inbound token the worker created
  for it while joining; nobody else knows that token

Join handshake (matches `WorkerServer`): the host creates an invite code; the worker
posts it together with its endpoints and a fresh inbound token. Unless the invite
auto-approves, the join stays `pending` until a host approves or denies it; the worker
polls with a poll token and receives its workbench token exactly once.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import hashlib
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import labels as lbl
from ..extensions import BTW_EXTENSION_URI
from ..join_token import encode_join_token
from .board import Board
from .board_api import build_board_routers
from .wakeups import Wakeups
from .settings_api import API_KEY_PREFIX, build_settings_router, hash_api_key
from .store import InMemoryStore, Record, WorkbenchStore
from .tasks import FINAL_STATES, TaskError, TaskManager, is_url_ok
from ..worker.web_auth import SAFE_METHODS, LoginLimiter, SecurityHeaders, SessionStore, origin_allowed

UI_DIR = Path(__file__).parent / "ui"
DEFAULT_NAME = "My Workbench"
DEFAULT_DESCRIPTION = ""
WORKER_API = "/wb/worker/v1"     # joining and leaving (called by workers that are not members yet)
AGENT_API = "/wb/api/v1"         # called by member workers with their workbench token
CARD_SUFFIX = "/.well-known/agent-card.json"
ICON_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}
ICON_MAX = 256_000               # bytes
SKILLS_TTL = 300.0               # seconds a worker's skills are served before its card is re-read
MAX_WAIT = 30.0                  # longest long-poll of GET /tasks/{id}


# ---------------------------------------------------------------- models
class LoginIn(BaseModel):
    username: str
    password: str


class WorkerInfoIn(BaseModel):
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    version: str = Field(default="", max_length=50)
    a2a_url: str = Field(max_length=2000)
    card_url: str = Field(max_length=2000)


class JoinIn(BaseModel):
    invite_code: str = Field(max_length=200)
    worker: WorkerInfoIn
    inbound_token: str = Field(min_length=16, max_length=512)


class InviteIn(BaseModel):
    label: str = Field(default="", max_length=200)
    auto_approve: bool = False
    uses: int = Field(default=1, ge=1, le=1000)
    expires_in: float | None = Field(default=None, gt=0)       # seconds

    @field_validator("label")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class HostTaskIn(BaseModel):
    agent_id: str
    text: str = Field(max_length=100_000)
    context_id: str | None = Field(default=None, max_length=200)


class MessageIn(BaseModel):
    text: str = Field(max_length=100_000)


class AgentTaskIn(HostTaskIn):
    parent_task_id: str | None = None


# --------------------------------------------------------------- helpers
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/")


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


async def _same(w: Record) -> Record:
    return w


def _worker_view(w: Record) -> dict:        # never contains tokens
    return {**{k: w[k] for k in ("id", "name", "description", "version", "a2a_url",
                                 "status", "created_at")}, "skills": w.get("skills", []),
            "label_ids": w.get("label_ids", []), "board_wake": w.get("board_wake", True),
            "has_icon": bool(w.get("icon_url"))}


def _invite_view(i: Record) -> dict:        # never contains the code (only its hash is stored)
    return {k: i[k] for k in ("id", "label", "auto_approve", "uses_left", "expires_at", "created_at")}


_TASK_FIELDS = ("id", "topic", "root_id", "parent_id", "worker_id", "requested_by", "context_id", "state",
                "input", "output", "status_text", "error", "created_at", "updated_at")


class WorkbenchServer:
    def __init__(self, version: str = "0.1.0", *, store: WorkbenchStore | None = None):
        # name and description are settings: hosts change them in the UI and they live in the
        # store; these defaults apply until the store has values (see `apply_settings`)
        self.name, self.description, self.version = DEFAULT_NAME, DEFAULT_DESCRIPTION, version
        self.store: WorkbenchStore = store or InMemoryStore()
        self._public_url = ""
        self.tasks = TaskManager(self.store, lambda: self._public_url)
        self.board = Board(self.store)
        self.wakeups = Wakeups(self.store, self.tasks)
        self.board.listeners.append(self.wakeups.on_message)
        self._http = httpx.AsyncClient(timeout=3.0)      # reads workers' public agent cards and icons
        self._icons: dict[str, tuple[float, tuple[str, bytes] | None]] = {}
        self._allow_insecure_workers = False

    # ------------------------------------------------------------ ASGI app
    def build_app(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        admin_password: str | None = None,
        admin_user: str = "admin",
        public_url: str | None = None,
        ui: bool = True,
        ui_dir: str | os.PathLike | None = None,
        secure_cookies: bool | None = None,
        allow_insecure_workers: bool = False,
        session_idle_timeout: float = 1800,
        session_max_age: float = 12 * 3600,
        max_task_depth: int = 8,
        max_active_tasks: int = 100,
    ) -> FastAPI:
        """Build the ASGI app (use this to deploy under your own uvicorn/gunicorn).

        `public_url` is the address workers use to reach this server; it is also sent
        to workers in `metadata.workbench.url`, where it must equal the URL they joined
        with. Run behind TLS in production: the session cookie and all tokens travel
        in the clear otherwise. Plain-http workers are only accepted on localhost
        unless `allow_insecure_workers` is set.
        """
        admin_password = admin_password or os.getenv("ADMIN_PASSWORD")
        if not admin_password:
            raise ValueError("admin_password is required (or set ADMIN_PASSWORD)")
        public_url = _norm_url(public_url or f"http://{host}:{port}")
        self._public_url = public_url
        self._allow_insecure_workers = allow_insecure_workers
        self.tasks.max_depth, self.tasks.max_active = max_task_depth, max_active_tasks
        secure = public_url.startswith("https://") if secure_cookies is None else secure_cookies
        cookie_name = "__Host-wb_session" if secure else "wb_session"
        sessions = SessionStore(session_idle_timeout, session_max_age)
        login_limiter = LoginLimiter()
        join_limiter = LoginLimiter(max_failures=10)      # bad invite codes per client address
        own_hosts = {urlparse(public_url).netloc}
        store, tasks = self.store, self.tasks

        # ------------------------------------------------------ host auth
        def credentials_ok(user: str, password: str) -> bool:
            return bool(secrets.compare_digest(user.encode(), admin_user.encode())
                        & secrets.compare_digest(password.encode(), admin_password.encode()))

        def origin_ok(request: Request) -> bool:
            return origin_allowed(request.headers.get("origin"),
                                  own_hosts | {request.headers.get("host", "")})

        def authenticate(request: Request) -> dict | None:
            """Session cookie (+CSRF for unsafe methods) or an explicit Basic header.

            No `WWW-Authenticate` challenge is ever sent, so browsers never cache
            Basic credentials and can't replay them cross-site.
            """
            sess = sessions.get(request.cookies.get(cookie_name))
            if sess is not None:
                if request.method not in SAFE_METHODS:
                    if not origin_ok(request):
                        raise HTTPException(403, "cross-origin request rejected")
                    sent = request.headers.get("x-csrf-token", "")
                    if not _eq(sent, sess["csrf"]):
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

        def require_host(request: Request) -> None:
            if authenticate(request) is None:
                raise HTTPException(401, "authentication required")

        async def caller_worker(request: Request) -> Record:
            """The active worker owning the bearer token of an /wb/api/v1 request."""
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            w = await store.find_worker(token_hash=_hash(token)) \
                if scheme.lower() == "bearer" and token else None
            if w is None or w["status"] != "active":
                raise HTTPException(401, "invalid token")
            return w

        async def caller_any(request: Request) -> dict:
            """Who calls /wb/api/v1: a member worker (its workbench token) or an API key.

            Returns {"id", "kind": "worker" | "key", "record"}. Keys are for outside programs
            (WorkbenchClient): like hosts they are not bound by the label rules.
            """
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() == "bearer" and token.startswith(API_KEY_PREFIX):
                k = await store.find_api_key(hash_api_key(token))
                if k is not None:
                    if k["last_used_at"] is None or time.time() - k["last_used_at"] > 60:
                        await store.update_api_key(k["id"], last_used_at=time.time())
                    return {"id": f"key:{k['id']}", "kind": "key", "record": k}
            elif scheme.lower() == "bearer" and token:
                w = await store.find_worker(token_hash=_hash(token))
                if w is not None and w["status"] == "active":
                    return {"id": w["id"], "kind": "worker", "record": w}
            raise HTTPException(401, "invalid token")

        def client_key(request: Request) -> str:
            return request.client.host if request.client else "unknown"

        # ------------------------------------------------------ host: session
        session = APIRouter(prefix="/admin")
        app_routes = APIRouter()

        @session.post("/login")
        async def login(body: LoginIn, request: Request, response: Response):
            if not origin_ok(request):
                raise HTTPException(403, "cross-origin request rejected")
            key = client_key(request)
            wait = login_limiter.retry_after(key)
            if wait:
                raise HTTPException(429, "too many failed logins", headers={"Retry-After": str(wait)})
            if not credentials_ok(body.username, body.password):
                login_limiter.failure(key)
                raise HTTPException(401, "bad credentials")
            login_limiter.success(key)
            token, sess = sessions.create()
            response.set_cookie(cookie_name, token, httponly=True, secure=secure, samesite="strict",
                                max_age=int(session_max_age), path="/")
            return {"csrf_token": sess["csrf"], "expires_in": int(session_max_age)}

        @session.get("/session")
        async def session_info(request: Request):
            sess = sessions.get(request.cookies.get(cookie_name))
            if sess is None:
                return {"authenticated": False}
            return {"authenticated": True, "csrf_token": sess["csrf"]}

        @session.post("/logout")
        async def logout(request: Request, response: Response):
            require_host(request)
            sessions.destroy(request.cookies.get(cookie_name))
            response.delete_cookie(cookie_name, path="/")
            return {"ok": True}

        # ------------------------------------------------------ host: admin API
        admin = APIRouter(prefix="/admin", dependencies=[Depends(require_host)])

        @admin.get("/config")
        async def get_config():
            return {"name": self.name, "description": self.description, "version": self.version,
                    "public_url": public_url}

        @admin.get("/workers")
        async def list_workers():
            workers = [w for w in await store.list_workers()]
            workers = await asyncio.gather(*(self._refresh_card(w) if w["status"] == "active" else _same(w)
                                             for w in workers))
            return sorted((_worker_view(w) for w in workers), key=lambda w: w["created_at"])

        @admin.post("/workers/{worker_id}/approve")
        async def approve_worker(worker_id: str):
            w = await store.get_worker(worker_id)
            if w is None:
                raise HTTPException(404, "no such worker")
            if w["status"] != "pending":
                raise HTTPException(409, f"worker is {w['status']}, not pending")
            await self._activate(w)
            return {"status": "active"}

        @admin.post("/workers/{worker_id}/deny")
        async def deny_worker(worker_id: str):
            w = await store.get_worker(worker_id)
            if w is None:
                raise HTTPException(404, "no such worker")
            if w["status"] != "pending":
                raise HTTPException(409, f"worker is {w['status']}, not pending")
            await store.update_worker(worker_id, status="denied", inbound_token="")
            return {"status": "denied"}

        @admin.delete("/workers/{worker_id}")
        async def remove_worker(worker_id: str):
            if not await self._remove_worker(worker_id):
                raise HTTPException(404, "no such worker")
            return {"removed": worker_id}

        @admin.get("/invites")
        async def list_invites():
            return [_invite_view(i) for i in await store.list_invites()]

        @admin.post("/invites")
        async def create_invite(body: InviteIn):
            code = secrets.token_urlsafe(16)
            now = time.time()
            inv = {"id": uuid.uuid4().hex, "label": body.label, "code_hash": _hash(code),
                   "auto_approve": body.auto_approve, "uses_left": body.uses,
                   "expires_at": now + body.expires_in if body.expires_in else None,
                   "created_at": now}
            await store.add_invite(inv)
            return {**_invite_view(inv), "code": code, "base_url": public_url,
                    "join_token": encode_join_token(public_url, code), "note": "shown only once"}

        @admin.delete("/invites/{invite_id}")
        async def delete_invite(invite_id: str):
            if not await store.delete_invite(invite_id):
                raise HTTPException(404, "no such invite")
            return {"removed": invite_id}

        async def names() -> dict[str, Record]:
            workers = await store.list_workers()
            workers = await asyncio.gather(*(self._refresh_card(w) if w["status"] == "active" else _same(w)
                                             for w in workers))      # cached; keeps the icon flag current
            return {w["id"]: w for w in workers}

        def host_task_view(t: Record, workers: dict[str, Record]) -> dict:
            v = {k: t.get(k) for k in _TASK_FIELDS}
            w = workers.get(t["worker_id"])
            v["worker_name"] = w["name"] if w else "(removed)"
            v["worker_has_icon"] = bool(w and w.get("icon_url"))
            # can the agent take a message while this task runs?
            v["accepts_btw"] = bool(w and w.get("btw")) and tasks.is_running(t["id"])
            v["running"] = tasks.is_running(t["id"])
            # is the party that asked for this task blocked until it finishes?
            v["waited"] = t["state"] not in FINAL_STATES and tasks.is_waited(t["id"])
            return v

        @admin.post("/tasks")
        async def host_start_task(body: HostTaskIn):
            w = await store.get_worker(body.agent_id)
            if w is None:
                raise HTTPException(404, "no such agent")
            try:
                t = await tasks.start(w, body.text, requested_by="host", context_id=body.context_id)
            except TaskError as e:
                raise HTTPException(e.status, str(e)) from e
            return host_task_view(t, await names())

        @admin.get("/tasks")
        async def list_root_tasks():
            """All task trees (root tasks), newest first, with a summary of their sub tasks."""
            all_tasks = await store.list_tasks()
            nm, by_root = await names(), {}
            for t in all_tasks:
                by_root.setdefault(t["root_id"], []).append(t)
            out = []
            for t in all_tasks:
                if t["parent_id"] is None:
                    tree = by_root[t["id"]]
                    v = host_task_view(t, nm)
                    v["subtasks"] = len(tree) - 1
                    v["active"] = sum(1 for x in tree if x["state"] not in FINAL_STATES)
                    out.append(v)
            return sorted(out, key=lambda v: v["created_at"], reverse=True)

        @admin.get("/tasks/{task_id}")
        async def get_task_tree(task_id: str):
            """A task with all its descendants, nested under `children`."""
            t = await store.get_task(task_id)
            if t is None:
                raise HTTPException(404, "no such task")
            nm = await names()
            nodes = {x["id"]: {**host_task_view(x, nm), "children": []}
                     for x in await store.list_tasks(root_id=t["root_id"])}
            for n in nodes.values():
                n["waiting"] = False
            for n in nodes.values():
                if n["parent_id"] in nodes:
                    parent = nodes[n["parent_id"]]
                    parent["children"].append(n)
                    parent["waiting"] = parent["waiting"] or n["waited"]   # parent is blocked on a child
            return nodes[task_id]

        @admin.get("/tasks/{task_id}/events")
        async def get_task_events(task_id: str, after: int = 0):
            """The task's log (input, state changes, tool calls, delegations) after `after`,
            plus its current state and the answer streamed so far. Poll with the last `seq`."""
            t = await store.get_task(task_id)
            if t is None:
                raise HTTPException(404, "no such task")
            return {"task": host_task_view(t, await names()),
                    "events": await store.list_events(task_id, after)}

        @admin.post("/tasks/{task_id}/message")
        async def host_message_task(task_id: str, body: MessageIn):
            """Add a message to a task: answers one that waits for input, or ("btw") is
            queued for one that is still running if its agent supports that."""
            try:
                t = await tasks.send_message(task_id, body.text)
            except TaskError as e:
                raise HTTPException(e.status, str(e)) from e
            return host_task_view(t, await names())

        @admin.post("/tasks/{task_id}/cancel")
        async def host_cancel_task(task_id: str):
            try:
                return {"canceled": await tasks.cancel(task_id)}
            except TaskError as e:
                raise HTTPException(e.status, str(e)) from e

        @app_routes.get("/icon/{worker_id}", dependencies=[Depends(require_host)])
        async def worker_icon(worker_id: str):
            """A worker's agent-card icon, re-served from this origin (see `_icon`)."""
            w = await store.get_worker(worker_id)
            icon = await self._icon(await self._refresh_card(w)) if w and w["status"] == "active" else None
            if icon is None:
                raise HTTPException(404, "no icon")
            return Response(icon[1], media_type=icon[0], headers={
                "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox"})

        # ------------------------------------------------------ worker: join
        worker_api = APIRouter(prefix=WORKER_API)

        @worker_api.get("/info")
        async def info():
            return {"name": self.name, "description": self.description, "version": self.version}

        @worker_api.post("/join")
        async def join(body: JoinIn, request: Request):
            key = client_key(request)
            wait = join_limiter.retry_after(key)
            if wait:
                raise HTTPException(429, "too many invalid invite codes",
                                    headers={"Retry-After": str(wait)})
            inv = await store.find_invite(_hash(body.invite_code))
            if inv is None or inv["uses_left"] < 1 or \
                    (inv["expires_at"] is not None and inv["expires_at"] < time.time()):
                join_limiter.failure(key)
                raise HTTPException(403, "invalid or expired invite code")
            join_limiter.success(key)

            wi = body.worker
            a2a_url, card_url = _norm_url(wi.a2a_url), _norm_url(wi.card_url)
            if not (is_url_ok(a2a_url, allow_insecure=allow_insecure_workers)
                    and is_url_ok(card_url, allow_insecure=allow_insecure_workers)
                    and card_url.endswith(CARD_SUFFIX) and _origin(a2a_url) == _origin(card_url)):
                raise HTTPException(400, "worker URLs must be https (http only for localhost), "
                                         "on one origin, and card_url must end with " + CARD_SUFFIX)
            await store.update_invite(inv["id"], uses_left=inv["uses_left"] - 1)

            poll_token = secrets.token_urlsafe(32)
            w = {"id": uuid.uuid4().hex, "name": wi.name or "unnamed worker",
                 "description": wi.description, "version": wi.version,
                 "a2a_url": a2a_url, "card_url": card_url, "inbound_token": body.inbound_token,
                 "token_hash": None, "status": "pending", "request_id": uuid.uuid4().hex,
                 "poll_token_hash": _hash(poll_token), "pending_token": None, "label_ids": [],
                 "invite_id": inv["id"], "created_at": time.time()}
            await store.add_worker(w)
            if inv["auto_approve"]:
                return {"status": "accepted", "workbench_token": await self._activate(w, deliver=True)}
            return {"status": "pending", "request_id": w["request_id"], "poll_token": poll_token,
                    "message": "waiting for a host to approve"}

        @worker_api.get("/join/{request_id}")
        async def join_status(request_id: str, request: Request):
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            w = await store.find_worker(request_id=request_id)
            if w is None or scheme.lower() != "bearer" or not _eq(_hash(token), w["poll_token_hash"]):
                raise HTTPException(404, "unknown join request")
            if w["status"] == "denied":
                return {"status": "rejected", "message": "the host denied the request"}
            if w["status"] == "pending":
                return {"status": "pending"}
            if not w["pending_token"]:
                raise HTTPException(410, "the token was already delivered")
            await store.update_worker(w["id"], pending_token=None)    # hand it out exactly once
            return {"status": "accepted", "workbench_token": w["pending_token"]}

        @worker_api.post("/leave")
        async def leave(request: Request):
            w = await caller_worker(request)
            await self._remove_worker(w["id"])
            return {"ok": True}

        # ------------------------------------------------------ worker: API
        api = APIRouter(prefix=AGENT_API)

        def agent_task_view(t: Record) -> dict:
            return {"id": t["id"], "parent_id": t["parent_id"], "agent_id": t["worker_id"],
                    "input": t["input"],
                    "state": t["state"], "output": t["output"], "status_text": t["status_text"],
                    "error": t["error"],
                    "done": t["state"] in FINAL_STATES or not tasks.is_running(t["id"])}

        async def own_task(task_id: str, me: Record, *, assigned_too: bool = False) -> Record:
            """A task `me` started (or, read-only, one that was given to `me`)."""
            t = await store.get_task(task_id)
            if t is None or not (t["requested_by"] == me["id"]
                                 or (assigned_too and t["worker_id"] == me["id"])):
                raise HTTPException(404, "no such task")
            return t

        @api.get("/ping")
        async def ping(request: Request):
            """Lets a worker check that the workbench is up and still accepts its token."""
            await caller_any(request)
            return {"name": self.name, "version": self.version}

        @api.get("/participants")
        async def participants(request: Request):
            me = await caller_any(request)
            others = [w for w in await store.list_workers()
                      if w["status"] == "active" and w["id"] != me["id"]]
            if me["kind"] == "worker":              # only agents it may hand work to
                comm = await lbl.communication(store)
                mine = me["record"].get("label_ids", [])
                others = [w for w in others if lbl.may_communicate(comm, mine, w.get("label_ids", []))]
            await asyncio.gather(*(self._refresh_card(w) for w in others))
            others = [await store.get_worker(w["id"]) or w for w in others]
            return [{"id": w["id"], "name": w["name"], "description": w["description"],
                     "skills": w.get("skills", [])} for w in others]

        @api.get("/tasks")
        async def agent_list_tasks(request: Request, scope: Literal["started", "assigned"] = "started"):
            """Tasks you started, or tasks other parties gave you, newest first (at most 100)."""
            me = await caller_any(request)
            mine = [t for t in await store.list_tasks()
                    if t["requested_by" if scope == "started" else "worker_id"] == me["id"]]
            return [agent_task_view(t) for t in reversed(mine[-100:])]

        @api.post("/tasks")
        async def agent_start_task(body: AgentTaskIn, request: Request):
            """Start a (sub) task on another agent; returns immediately, poll GET /tasks/{id}."""
            me = await caller_any(request)
            target = await store.get_worker(body.agent_id)
            if target is None or target["status"] != "active":
                raise HTTPException(404, "no such agent")
            if target["id"] == me["id"]:
                raise HTTPException(400, "an agent cannot delegate to itself")
            if me["kind"] == "worker" and not lbl.may_communicate(
                    await lbl.communication(store), me["record"].get("label_ids", []),
                    target.get("label_ids", [])):
                raise HTTPException(403, f"the label rules do not allow you to give work to {target['name']!r}")
            parent = None
            if body.parent_task_id and me["kind"] == "key":
                raise HTTPException(400, "an API key has no task of its own to start sub tasks from")
            if body.parent_task_id:
                parent = await store.get_task(body.parent_task_id)
                if parent is None or parent["worker_id"] != me["id"]:
                    raise HTTPException(404, "no such parent task")
            try:
                t = await tasks.start(target, body.text, requested_by=me["id"], parent=parent,
                                      context_id=body.context_id)
            except TaskError as e:
                raise HTTPException(e.status, str(e)) from e
            return agent_task_view(t)

        @api.get("/tasks/{task_id}")
        async def agent_get_task(task_id: str, request: Request, wait: float = 0):
            """Status/result of a task you started; `wait` (seconds) long-polls until it ends."""
            me = await caller_any(request)
            t = await own_task(task_id, me, assigned_too=True)
            # a requester that polls is waiting for the result (the host UI shows that)
            poll = tasks.polling(task_id, long=wait > 0) if t["requested_by"] == me["id"] \
                else contextlib.nullcontext()
            async with poll:
                if wait > 0 and t["state"] not in FINAL_STATES:
                    await tasks.wait(task_id, min(wait, MAX_WAIT))
                    t = await own_task(task_id, me, assigned_too=True)
            return agent_task_view(t)

        @api.post("/tasks/{task_id}/message")
        async def agent_message_task(task_id: str, body: MessageIn, request: Request):
            """Add a message to a task you started: answers one that waits for input, or ("btw")
            is queued for one that is still running if its agent supports that."""
            me = await caller_any(request)
            await own_task(task_id, me)
            try:
                return agent_task_view(await tasks.send_message(task_id, body.text, requested_by=me["id"]))
            except TaskError as e:
                raise HTTPException(e.status, str(e)) from e

        @api.post("/tasks/{task_id}/cancel")
        async def agent_cancel_task(task_id: str, request: Request):
            me = await caller_any(request)
            await own_task(task_id, me)
            return {"canceled": await tasks.cancel(task_id)}

        # ------------------------------------------------------ assemble
        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            await self._load_settings()
            await self.board.ensure_defaults()
            await tasks.recover()
            yield
            await self.wakeups.close()
            await tasks.close()
            await self._http.aclose()

        app = FastAPI(title="Workbench", lifespan=lifespan)
        app.add_middleware(SecurityHeaders)
        startup = {"allow_insecure_workers": allow_insecure_workers,
                   "session_idle_timeout": session_idle_timeout, "session_max_age": session_max_age}
        settings_router = build_settings_router(self, require_host, startup)
        board_admin, board_api = build_board_routers(self, require_host, caller_any)
        for r in (session, admin, settings_router, board_admin, app_routes, worker_api, api, board_api):
            app.include_router(r)
        if ui:
            @app.get("/", include_in_schema=False)
            async def root():
                return RedirectResponse("/ui/")
            app.mount("/ui", StaticFiles(directory=ui_dir or UI_DIR, html=True), name="ui")
        return app

    # ------------------------------------------------------------ settings
    @property
    def public_url(self) -> str:
        return self._public_url

    async def _load_settings(self) -> None:
        """Sync name, description and limits with the store (runs once at startup).

        First boot (the store has no name yet): the defaults are written to the store.
        After that the store is the source of truth: whatever it holds replaces the
        defaults, so a name changed in the settings page survives restarts. This is
        async, so it cannot run in the constructor; it runs when the app starts.
        """
        stored = await self.store.get_settings()
        if "name" not in stored:
            stored = await self.store.update_settings(name=self.name, description=self.description)
        self.apply_settings({k: stored[k] for k in
                             ("name", "description", "max_task_depth", "max_active_tasks",
                              "board_rate_per_minute", "board_wakeups_per_minute") if k in stored})

    def apply_settings(self, s: dict[str, Any]) -> None:
        """Take over (saved) settings: name, description and the task limits."""
        self.name = s.get("name", self.name)
        self.description = s.get("description", self.description)
        self.tasks.max_depth = s.get("max_task_depth", self.tasks.max_depth)
        self.tasks.max_active = s.get("max_active_tasks", self.tasks.max_active)
        self.board.rate_per_minute = s.get("board_rate_per_minute", self.board.rate_per_minute)
        self.wakeups.per_minute = s.get("board_wakeups_per_minute", self.wakeups.per_minute)

    # ------------------------------------------------------------ internals
    async def _activate(self, w: Record, *, deliver: bool = False) -> str:
        """Approve a worker: mint its workbench token. Returns the token.

        Only its hash is kept; unless delivered in the join response itself, the token
        waits in `pending_token` until the worker's poll collects it.
        """
        token = secrets.token_urlsafe(32)
        label_ids = w.get("label_ids") or [(await lbl.create_label(self.store, w["name"]))["id"]]
        await self.store.update_worker(w["id"], status="active", token_hash=_hash(token),
                                       pending_token=None if deliver else token, label_ids=label_ids)
        return token

    async def _refresh_card(self, w: Record) -> Record:
        """Read skills and icon from the worker's public agent card (at most every SKILLS_TTL seconds).

        Best effort: an unreachable or odd card just leaves the previous values in place.
        Returns the (possibly updated) worker record.
        """
        if time.time() - w.get("skills_at", 0) < SKILLS_TTL:
            return w
        skills, icon_url, btw = w.get("skills", []), w.get("icon_url", ""), w.get("btw", False)
        try:
            resp = await self._http.get(w["card_url"])
            if resp.status_code == 200 and len(resp.content) <= 1_000_000:
                card = resp.json()
                icon = card.get("iconUrl") or card.get("icon_url") or ""
                icon_url = icon if isinstance(icon, str) and len(icon) <= 700_000 else ""
                exts = (card.get("capabilities") or {}).get("extensions") or []
                btw = any(isinstance(x, dict) and x.get("uri") == BTW_EXTENSION_URI for x in exts)
                raw = card.get("skills")
                if isinstance(raw, list):
                    skills = [{"id": str(s.get("id", ""))[:200], "name": str(s.get("name", ""))[:200],
                               "description": str(s.get("description", ""))[:1000],
                               "tags": [str(t)[:50] for t in s.get("tags", [])[:20]]}
                              for s in raw[:50] if isinstance(s, dict)]
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            pass
        return await self.store.update_worker(w["id"], skills=skills, icon_url=icon_url,
                                              btw=btw, skills_at=time.time()) or w

    async def _icon(self, w: Record) -> tuple[str, bytes] | None:
        """The worker's icon as (content type, bytes), fetched by the workbench itself.

        Browsers never load worker-supplied URLs: the workbench reads the image (from a
        data: URI, or from the worker's own origin only) and serves it from its own origin.
        """
        cached = self._icons.get(w["id"])
        if cached is not None and time.monotonic() - cached[0] < SKILLS_TTL:
            return cached[1]
        result = None
        url = w.get("icon_url", "")
        try:
            if url.startswith("data:"):
                m = re.fullmatch(r"data:(image/[a-z+.-]+);base64,([A-Za-z0-9+/=\s]+)", url[:700_000])
                if m:
                    result = (m.group(1), base64.b64decode(m.group(2)))
            elif is_url_ok(url, allow_insecure=self._allow_insecure_workers) \
                    and _origin(url) == _origin(w["card_url"]):          # no requests to other hosts
                resp = await self._http.get(url)                           # redirects are not followed
                if resp.status_code == 200:
                    result = (resp.headers.get("content-type", "").split(";")[0].strip().lower(), resp.content)
        except (httpx.HTTPError, ValueError):
            result = None
        if result is not None and (result[0] not in ICON_TYPES or not 0 < len(result[1]) <= ICON_MAX):
            result = None
        self._icons[w["id"]] = (time.monotonic(), result)
        return result

    async def _remove_worker(self, worker_id: str) -> bool:
        if await self.store.get_worker(worker_id) is None:
            return False
        await self.tasks.cancel_worker_tasks(worker_id)
        await self.tasks.forget_worker(worker_id)
        self._icons.pop(worker_id, None)
        await self.store.delete_worker(worker_id)
        return True

    # ------------------------------------------------------------------ run
    def run(self, host: str = "127.0.0.1", port: int = 8000, *, admin_password: str | None = None,
            admin_user: str = "admin", public_url: str | None = None, log_level: str = "info",
            **kwargs: Any) -> None:
        """Build the app and serve it (blocking).

        Keyword arguments of `build_app` (ui, secure_cookies, ...) are accepted;
        everything else is passed to uvicorn.
        """
        app_opts = {k: kwargs.pop(k) for k in
                    ("ui", "ui_dir", "secure_cookies", "allow_insecure_workers",
                     "session_idle_timeout", "session_max_age", "max_task_depth",
                     "max_active_tasks") if k in kwargs}
        app = self.build_app(host=host, port=port, admin_password=admin_password,
                             admin_user=admin_user, public_url=public_url, **app_opts)
        uvicorn.run(app, host=host, port=port, log_level=log_level, **kwargs)
