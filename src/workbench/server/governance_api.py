"""Admin endpoints for pausing, stopping, the audit log and alerts."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .governance import actor

if TYPE_CHECKING:
    from .server import WorkbenchServer


class PauseIn(BaseModel):
    paused: bool


def build_governance_router(server: "WorkbenchServer", require_host: Callable[[Request], None]) -> APIRouter:
    store, tasks, audit = server.store, server.tasks, server.audit
    router = APIRouter(prefix="/admin", dependencies=[Depends(require_host)])
    system = actor("system", "governance", "governance")

    @router.get("/governance")
    async def status():
        return {"paused": server.paused, "alerts": len(await store.list_alerts()),
                "auto_pause_on_alert": server.monitor.auto_pause}

    @router.post("/pause")
    async def pause_all(body: PauseIn):
        """Pause (or resume) the whole workbench: no agent call is accepted, no task is relayed."""
        await server.set_paused(body.paused)
        return {"paused": server.paused}

    @router.post("/stop-all")
    async def stop_all():
        """Emergency stop: pause everything and cancel every unfinished task."""
        await server.stop_everything()
        return {"paused": True}

    @router.put("/workers/{worker_id}/pause")
    async def pause_worker(worker_id: str, body: PauseIn):
        if await store.update_worker(worker_id, paused=body.paused) is None:
            raise HTTPException(404, "no such worker")
        return {"paused": body.paused}

    @router.post("/workers/{worker_id}/stop")
    async def stop_worker(worker_id: str):
        """Pause an agent and cancel everything it is working on or has started."""
        if await store.get_worker(worker_id) is None:
            raise HTTPException(404, "no such worker")
        await server.stop_agent(worker_id)
        return {"paused": True}

    @router.put("/labels/{label_id}/pause")
    async def pause_label(label_id: str, body: PauseIn):
        if await store.get_label(label_id) is None:
            raise HTTPException(404, "no such label")
        n = 0
        for w in await store.list_workers():
            if label_id in w.get("label_ids", []):
                await store.update_worker(w["id"], paused=body.paused)
                n += 1
        return {"paused": body.paused, "agents": n}

    @router.get("/audit")
    async def list_audit(after: int = 0, limit: int = 200, newest: bool = False,
                         actor_filter: str | None = None, action: str | None = None):
        return await store.list_audit(after=after, limit=max(1, min(limit, 1000)), newest=newest,
                                      actor=actor_filter, action=action)

    @router.get("/alerts")
    async def list_alerts(all: bool = False):
        return await store.list_alerts(include_acknowledged=all)

    @router.post("/alerts/{alert_id}/ack")
    async def ack(alert_id: str):
        if not await store.acknowledge_alert(alert_id):
            raise HTTPException(404, "no such alert")
        return {"acknowledged": True}

    return router
