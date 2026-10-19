"""HTTP endpoints of the board: one router for hosts (`/admin/board/*`), one for member workers
and API keys (`/wb/api/v1/board/*`)."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .board import MAX_TEXT, NAME_RE, Board, BoardError, Caller
from .store import Record

if TYPE_CHECKING:
    from .server import WorkbenchServer

MAX_WAIT = 30.0


class PostIn(BaseModel):
    text: str = Field(max_length=MAX_TEXT + 1000)
    kind: str = Field(default="post", max_length=100)
    to: list[str] = Field(default_factory=list, max_length=50)
    refs: list[dict[str, Any]] = Field(default_factory=list, max_length=10)


class ChannelIn(BaseModel):
    name: str
    description: str = Field(default="", max_length=500)
    read_label_ids: list[str] = Field(default_factory=list, max_length=200)
    write_label_ids: list[str] = Field(default_factory=list, max_length=200)


class WakeIn(BaseModel):
    enabled: bool


class ChannelPatch(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    read_label_ids: list[str] | None = Field(default=None, max_length=200)
    write_label_ids: list[str] | None = Field(default=None, max_length=200)


def message_view(m: Record) -> dict[str, Any]:
    return {k: m[k] for k in ("seq", "time", "channel", "sender", "kind", "text", "to", "refs")}


def build_board_routers(server: "WorkbenchServer", require_host: Callable[[Request], None],
                        caller_any: Callable[[Request], Awaitable[dict]]) -> tuple[APIRouter, APIRouter]:
    board: Board = server.board
    store = server.store
    admin = APIRouter(prefix="/admin/board", dependencies=[Depends(require_host)])
    api = APIRouter(prefix="/wb/api/v1/board")

    def fail(e: BoardError) -> HTTPException:
        return HTTPException(e.status, str(e))

    async def reject(who: Caller, e: BoardError) -> HTTPException:
        """The HTTP error for a refused request; refusals of a worker feed the monitor."""
        if who.kind == "worker" and e.status in (403, 404, 429):
            await server.monitor.note(who.id, "ratelimit" if e.status == 429 else "denied")
        return fail(e)

    async def api_caller(request: Request) -> Caller:
        me = await caller_any(request)
        if me["kind"] == "worker":
            return Caller("worker", me["id"], me["record"]["name"], me["record"])
        return Caller("api", me["id"], me["record"]["name"])

    # ------------------------------------------------------------- hosts
    host = Caller("host", "host", "host")

    async def check_labels(ids: list[str]) -> None:
        known = {l["id"] for l in await store.list_labels()}
        if not set(ids) <= known:
            raise HTTPException(400, "unknown label")

    def channel_view(c: Record) -> dict[str, Any]:
        return {k: c[k] for k in ("name", "description", "read_label_ids", "write_label_ids", "created_at")}

    @admin.get("/channels")
    async def admin_channels():
        return [channel_view(c) for c in sorted(await store.list_channels(), key=lambda c: c["created_at"])]

    @admin.post("/channels")
    async def create_channel(body: ChannelIn):
        name = body.name.strip().lower()
        if not NAME_RE.match(name):
            raise HTTPException(400, "channel names are 1-40 characters: a-z, 0-9, - and _")
        if await store.get_channel(name) is not None:
            raise HTTPException(409, "a channel with this name exists")
        await check_labels(body.read_label_ids + body.write_label_ids)
        c = {"name": name, "description": body.description.strip(), "created_at": time.time(),
             "read_label_ids": list(dict.fromkeys(body.read_label_ids)),
             "write_label_ids": list(dict.fromkeys(body.write_label_ids))}
        await store.add_channel(c)
        return channel_view(c)

    @admin.patch("/channels/{name}")
    async def patch_channel(name: str, body: ChannelPatch):
        if await store.get_channel(name) is None:
            raise HTTPException(404, "no such channel")
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        await check_labels(changes.get("read_label_ids", []) + changes.get("write_label_ids", []))
        return channel_view(await store.update_channel(name, **changes))

    @admin.delete("/channels/{name}")
    async def delete_channel(name: str):
        if not await store.delete_channel(name):
            raise HTTPException(404, "no such channel")
        return {"removed": name}

    @admin.put("/workers/{worker_id}/wake")
    async def set_wake(worker_id: str, body: WakeIn):
        """Whether messages for this agent wake it (start or reach its board task)."""
        if await store.update_worker(worker_id, board_wake=body.enabled) is None:
            raise HTTPException(404, "no such worker")
        return {"board_wake": body.enabled}

    @admin.get("/messages")
    async def admin_messages(after: int = 0, channel: str | None = None, limit: int = 200,
                             newest: bool = False, wait: float = 0):
        """Everything on the board (all channels and all mailboxes), for the live view."""
        try:
            if newest:
                msgs = await board.read(host, channel=channel, after=after, limit=limit, newest=True)
            else:
                msgs = await board.wait(host, channel=channel, after=after, timeout=min(wait, MAX_WAIT),
                                        limit=limit)
        except BoardError as e:
            raise fail(e) from e
        return [message_view(m) for m in msgs]

    @admin.post("/channels/{name}/messages")
    async def host_post(name: str, body: PostIn):
        try:
            return message_view(await board.post(name, host, body.text, kind=body.kind, to=body.to,
                                                 refs=body.refs))
        except BoardError as e:
            raise fail(e) from e

    # ----------------------------------------------------- workers and keys
    @api.get("/channels")
    async def list_channels(request: Request):
        """Channels you may read (with whether you may write) and your own mailbox."""
        return await board.describe(await api_caller(request))

    @api.post("/channels/{name}/messages")
    async def post_message(name: str, body: PostIn, request: Request):
        """Post to a channel, or to a worker's mailbox (`@<worker id>`)."""
        who = await api_caller(request)
        try:
            return message_view(await board.post(name, who, body.text, kind=body.kind, to=body.to,
                                                 refs=body.refs))
        except BoardError as e:
            raise await reject(who, e) from e

    @api.get("/channels/{name}/messages")
    async def read_channel(name: str, request: Request, after: int = 0, wait: float = 0,
                           limit: int = 100, newest: bool = False):
        """Messages of one channel after `after` (a `seq`); `wait` seconds long-polls for new ones.
        With `newest`, the latest `limit` messages instead."""
        who = await api_caller(request)
        try:
            if newest:
                msgs = await board.read(who, channel=name, after=after, limit=limit, newest=True)
            else:
                msgs = await board.wait(who, channel=name, after=after, timeout=min(wait, MAX_WAIT),
                                        limit=limit)
        except BoardError as e:
            raise await reject(who, e) from e
        return [message_view(m) for m in msgs]

    @api.get("/feed")
    async def feed(request: Request, after: int = 0, wait: float = 0, limit: int = 100):
        """New messages across every channel you may read, plus your mailbox."""
        who = await api_caller(request)
        try:
            msgs = await board.wait(who, channel=None, after=after, timeout=min(wait, MAX_WAIT), limit=limit)
        except BoardError as e:
            raise await reject(who, e) from e
        return [message_view(m) for m in msgs]

    return admin, api
