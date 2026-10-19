"""HTTP endpoints of the shared files: `/admin/files` and `/admin/workspaces` for hosts,
`/wb/api/v1/files` for member workers and API keys."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .board import NAME_RE, Caller
from .store import Record
from .workspace import MAX_CONTENT, Workspace, WorkspaceError

if TYPE_CHECKING:
    from .server import WorkbenchServer


class WriteIn(BaseModel):
    content: str = Field(max_length=MAX_CONTENT + 1000)
    base_version: int | None = Field(default=None, ge=0)     # the version you changed; 0 = new file
    note: str = Field(default="", max_length=200)


class AppendIn(BaseModel):
    content: str = Field(max_length=MAX_CONTENT + 1000)
    note: str = Field(default="", max_length=200)


class SpaceIn(BaseModel):
    name: str
    description: str = Field(default="", max_length=500)
    read_label_ids: list[str] = Field(default_factory=list, max_length=200)
    write_label_ids: list[str] = Field(default_factory=list, max_length=200)


class SpacePatch(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    read_label_ids: list[str] | None = Field(default=None, max_length=200)
    write_label_ids: list[str] | None = Field(default=None, max_length=200)


def head_view(f: Record) -> dict[str, Any]:
    return {k: f[k] for k in ("path", "version", "size", "owner", "updated_at", "created_at")}


def version_view(v: Record) -> dict[str, Any]:
    return {k: v[k] for k in ("path", "version", "content", "size", "deleted", "author", "note", "time",
                              "owner", "latest")}


def build_workspace_routers(server: "WorkbenchServer", require_host: Callable[[Request], None],
                            caller_any: Callable[[Request], Awaitable[dict]]) -> tuple[APIRouter, APIRouter]:
    ws: Workspace = server.files
    store = server.store
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_host)])
    api = APIRouter(prefix="/wb/api/v1/files")
    host = Caller("host", "host", "host")

    def fail(e: WorkspaceError) -> HTTPException:
        # a conflict names the current version so the writer can re-read and retry
        return HTTPException(e.status, str(e), headers={"X-Current-Version": str(e.current)} if e.current is not None else None)

    async def reject(who: Caller, e: WorkspaceError) -> HTTPException:
        if who.kind == "worker" and e.status in (403, 404, 429):
            await server.monitor.note(who.id, "ratelimit" if e.status == 429 else "denied")
        return fail(e)

    async def api_caller(request: Request) -> Caller:
        me = await caller_any(request)
        if me["kind"] == "worker":
            return Caller("worker", me["id"], me["record"]["name"], me["record"])
        return Caller("api", me["id"], me["record"]["name"])

    async def check_labels(ids: list[str]) -> None:
        known = {l["id"] for l in await store.list_labels()}
        if not set(ids) <= known:
            raise HTTPException(400, "unknown label")

    def space_view(w: Record) -> dict[str, Any]:
        return {k: w[k] for k in ("name", "description", "read_label_ids", "write_label_ids", "created_at")}

    # ------------------------------------------------- hosts: workspaces
    @admin.get("/workspaces")
    async def list_spaces():
        return [space_view(w) for w in sorted(await store.list_workspaces(), key=lambda w: w["created_at"])]

    @admin.post("/workspaces")
    async def create_space(body: SpaceIn):
        name = body.name.strip().lower()
        if not NAME_RE.match(name):
            raise HTTPException(400, "workspace names are 1-40 characters: a-z, 0-9, - and _")
        if await store.get_workspace(name) is not None:
            raise HTTPException(409, "a workspace with this name exists")
        await check_labels(body.read_label_ids + body.write_label_ids)
        w = {"name": name, "description": body.description.strip(), "created_at": time.time(),
             "read_label_ids": list(dict.fromkeys(body.read_label_ids)),
             "write_label_ids": list(dict.fromkeys(body.write_label_ids))}
        await store.add_workspace(w)
        return space_view(w)

    @admin.patch("/workspaces/{name}")
    async def patch_space(name: str, body: SpacePatch):
        if await store.get_workspace(name) is None:
            raise HTTPException(404, "no such workspace")
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        await check_labels(changes.get("read_label_ids", []) + changes.get("write_label_ids", []))
        return space_view(await store.update_workspace(name, **changes))

    @admin.delete("/workspaces/{name}")
    async def delete_space(name: str):
        if not await store.delete_workspace(name):
            raise HTTPException(404, "no such workspace")
        return {"removed": name}

    # ------------------------------------------------------ hosts: files
    @admin.get("/files")
    async def admin_list(prefix: str = ""):
        return [head_view(f) for f in await ws.list(host, prefix=prefix)]

    @admin.get("/files/{path:path}")
    async def admin_read(path: str, version: int | None = None, history: bool = False):
        try:
            if history:
                return await ws.history(path, host)
            return version_view(await ws.read(path, host, version=version))
        except WorkspaceError as e:
            raise fail(e) from e

    @admin.put("/files/{path:path}")
    async def admin_write(path: str, body: WriteIn):
        try:
            return head_view(await ws.write(path, host, body.content, base_version=body.base_version,
                                            note=body.note))
        except WorkspaceError as e:
            raise fail(e) from e

    @admin.delete("/files/{path:path}")
    async def admin_delete(path: str):
        try:
            return head_view(await ws.delete(path, host))
        except WorkspaceError as e:
            raise fail(e) from e

    # ------------------------------------------------- workers and keys
    @api.get("")
    async def list_files(request: Request, prefix: str = ""):
        """Files you may read (newest state of each), optionally under a path prefix."""
        return [head_view(f) for f in await ws.list(await api_caller(request), prefix=prefix)]

    @api.get("/_workspaces")
    async def list_workspaces(request: Request):
        """The workspaces you may read, with whether you may write."""
        return await ws.spaces(await api_caller(request))

    @api.get("/{path:path}")
    async def read_file(path: str, request: Request, version: int | None = None, history: bool = False):
        who = await api_caller(request)
        try:
            if history:
                return await ws.history(path, who)
            return version_view(await ws.read(path, who, version=version))
        except WorkspaceError as e:
            raise await reject(who, e) from e

    @api.put("/{path:path}")
    async def write_file(path: str, body: WriteIn, request: Request):
        """Create (base_version 0) or update a file; a stale base_version answers 409."""
        who = await api_caller(request)
        try:
            return head_view(await ws.write(path, who, body.content, base_version=body.base_version,
                                            note=body.note))
        except WorkspaceError as e:
            raise await reject(who, e) from e

    @api.post("/{path:path}/append")
    async def append_file(path: str, body: AppendIn, request: Request):
        """Add text to the end of a file (created if needed), atomically."""
        who = await api_caller(request)
        try:
            return head_view(await ws.append(path, who, body.content, note=body.note))
        except WorkspaceError as e:
            raise await reject(who, e) from e

    @api.delete("/{path:path}")
    async def delete_file(path: str, request: Request):
        """Delete a file you own (its history stays)."""
        who = await api_caller(request)
        try:
            return head_view(await ws.delete(path, who))
        except WorkspaceError as e:
            raise await reject(who, e) from e

    return admin, api
