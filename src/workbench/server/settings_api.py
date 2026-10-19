"""Admin endpoints of the settings pages: general settings, labels + communication, API keys."""
from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from . import labels as lbl

if TYPE_CHECKING:
    from .server import WorkbenchServer

API_KEY_PREFIX = "wbk_"


class SettingsIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    max_task_depth: int | None = Field(default=None, ge=1, le=50)
    max_active_tasks: int | None = Field(default=None, ge=1, le=10_000)
    board_rate_per_minute: int | None = Field(default=None, ge=1, le=1000)
    board_wakeups_per_minute: int | None = Field(default=None, ge=1, le=600)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("the name must not be empty")
        return v.strip() if v else v


class LabelIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("the name must not be empty")
        return v.strip()


class WorkerLabelsIn(BaseModel):
    label_ids: list[str] = Field(min_length=1, max_length=200)


class CommunicationIn(BaseModel):
    policy: Literal["open", "restricted"]
    rules: list[tuple[str, str]] = Field(default_factory=list, max_length=20_000)


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("the name must not be empty")
        return v.strip()


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def build_settings_router(server: "WorkbenchServer", require_host, startup: dict[str, Any]) -> APIRouter:
    store = server.store
    router = APIRouter(prefix="/admin", dependencies=[Depends(require_host)])

    # ---------------------------------------------------------------- general
    def general() -> dict[str, Any]:
        return {
            "name": server.name, "description": server.description, "version": server.version,
            "public_url": server.public_url,                      # fixed: workers know the workbench by it
            "max_task_depth": server.tasks.max_depth, "max_active_tasks": server.tasks.max_active,
            "board_rate_per_minute": server.board.rate_per_minute,
            "board_wakeups_per_minute": server.wakeups.per_minute,
            "startup": startup,                                    # chosen when the server was started
        }

    @router.get("/settings")
    async def get_settings():
        return general()

    @router.patch("/settings")
    async def patch_settings(body: SettingsIn):
        changes = body.model_dump(exclude_none=True)
        if changes:
            server.apply_settings(changes)
            await store.update_settings(**changes)
        return general()

    # ----------------------------------------------------------------- labels
    async def labels_view() -> dict[str, Any]:
        workers = await store.list_workers()
        out = []
        for l in sorted(await store.list_labels(), key=lambda l: l["created_at"]):
            out.append({"id": l["id"], "name": l["name"],
                        "worker_ids": [w["id"] for w in workers if l["id"] in w.get("label_ids", [])]})
        comm = await lbl.communication(store)
        return {"labels": out, "communication": {"policy": comm["policy"],
                                                 "rules": [list(r) for r in comm["rules"]]}}

    @router.get("/labels")
    async def list_labels():
        return await labels_view()

    @router.post("/labels")
    async def create_label(body: LabelIn):
        if any(l["name"].lower() == body.name.lower() for l in await store.list_labels()):
            raise HTTPException(409, "a label with this name exists")
        await lbl.create_label(store, body.name)
        return await labels_view()

    @router.patch("/labels/{label_id}")
    async def rename_label(label_id: str, body: LabelIn):
        if await store.get_label(label_id) is None:
            raise HTTPException(404, "no such label")
        if any(l["name"].lower() == body.name.lower() and l["id"] != label_id
               for l in await store.list_labels()):
            raise HTTPException(409, "a label with this name exists")
        await store.update_label(label_id, name=body.name)
        return await labels_view()

    @router.delete("/labels/{label_id}")
    async def delete_label(label_id: str):
        if await store.get_label(label_id) is None:
            raise HTTPException(404, "no such label")
        workers = await store.list_workers()
        stranded = [w["name"] for w in workers if w.get("label_ids") == [label_id]]
        if stranded:
            raise HTTPException(409, "every agent needs a label; give these another one first: "
                                     + ", ".join(stranded))
        gated = [c["name"] for c in await store.list_channels()
                 if c["read_label_ids"] == [label_id] or c["write_label_ids"] == [label_id]]
        if gated:        # removing it would turn a restricted channel into an open one
            raise HTTPException(409, "these board channels are restricted to this label only; change "
                                     "their access first: " + ", ".join(gated))
        for c in await store.list_channels():
            await store.update_channel(c["name"],
                                       read_label_ids=[x for x in c["read_label_ids"] if x != label_id],
                                       write_label_ids=[x for x in c["write_label_ids"] if x != label_id])
        for w in workers:
            if label_id in w.get("label_ids", []):
                await store.update_worker(w["id"], label_ids=[x for x in w["label_ids"] if x != label_id])
        comm = await lbl.communication(store)
        await store.update_settings(communication={"policy": comm["policy"], "rules": [
            list(r) for r in comm["rules"] if label_id not in r]})
        await store.delete_label(label_id)
        return await labels_view()

    @router.put("/workers/{worker_id}/labels")
    async def set_worker_labels(worker_id: str, body: WorkerLabelsIn):
        if await store.get_worker(worker_id) is None:
            raise HTTPException(404, "no such worker")
        ids = list(dict.fromkeys(body.label_ids))
        known = {l["id"] for l in await store.list_labels()}
        if not set(ids) <= known:
            raise HTTPException(400, "unknown label")
        await store.update_worker(worker_id, label_ids=ids)
        return await labels_view()

    @router.put("/communication")
    async def set_communication(body: CommunicationIn):
        known = {l["id"] for l in await store.list_labels()}
        rules = list(dict.fromkeys(body.rules))
        if any(a not in known or b not in known for a, b in rules):
            raise HTTPException(400, "a rule names an unknown label")
        await store.update_settings(communication={"policy": body.policy, "rules": [list(r) for r in rules]})
        return await labels_view()

    # --------------------------------------------------------------- api keys
    def key_view(k: dict) -> dict:
        return {"id": k["id"], "name": k["name"], "prefix": k["prefix"],
                "created_at": k["created_at"], "last_used_at": k["last_used_at"]}

    @router.get("/api-keys")
    async def list_api_keys():
        return [key_view(k) for k in sorted(await store.list_api_keys(), key=lambda k: k["created_at"])]

    @router.post("/api-keys")
    async def create_api_key(body: ApiKeyIn):
        key = API_KEY_PREFIX + secrets.token_urlsafe(32)
        rec = {"id": uuid.uuid4().hex, "name": body.name, "prefix": key[:len(API_KEY_PREFIX) + 4],
               "key_hash": hash_api_key(key), "created_at": time.time(), "last_used_at": None}
        await store.add_api_key(rec)
        return {**key_view(rec), "key": key, "base_url": server.public_url, "note": "shown only once"}

    @router.delete("/api-keys/{key_id}")
    async def delete_api_key(key_id: str):
        if not await store.delete_api_key(key_id):
            raise HTTPException(404, "no such key")
        return {"removed": key_id}

    return router
