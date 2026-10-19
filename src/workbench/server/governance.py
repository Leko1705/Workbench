"""Governance: keeping a network of agents under oversight.

The workbench is the one place every agent has to go through, so this is where control lives:

* **Pause and stop.** A host can pause one agent, a label, or the whole workbench. A paused agent is
  refused by every workbench API (board, files, tasks), gets no new tasks and no wake-ups. "Stop"
  additionally cancels its work. This is enforced here, not requested from the agent: the reports
  on agents that ignored pause requests are the reason. The workbench controls what passes through
  it; a worker with its own shell or network access can still do things elsewhere, so tools like
  `BashTool` need sandboxing on the worker.
* **Limits** (rates, concurrent tasks, tasks started per hour) are enforced where the actions
  happen; see the settings.
* **Audit log.** Who did what: joins, task starts, board posts, file writes, every change a host
  makes. Entries carry no message or file content, only that it happened and where.
* **Alerts.** A small monitor watches for patterns that deserve a look: two agents answering each
  other in a loop, an agent that keeps hitting its limits, an agent that keeps asking for things it
  may not see. Optionally it pauses the agents of critical alerts (loops).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from .store import Record, WorkbenchStore

log = logging.getLogger(__name__)

LOOP_WINDOW = 120.0        # seconds
LOOP_MESSAGES = 10         # direct messages each way within the window
TRIP_WINDOW = 300.0
RATELIMIT_HITS = 5
DENIED_HITS = 15
ALERT_COOLDOWN = 600.0     # the same alert is not raised again within this time


def actor(kind: str, id: str, name: str) -> dict[str, str]:
    return {"kind": kind, "id": id, "name": name}


class Audit:
    def __init__(self, store: WorkbenchStore):
        self.store = store

    async def record(self, who: dict[str, str], action: str, summary: str) -> None:
        try:
            await self.store.append_audit({"actor": who, "action": action, "summary": summary[:300]})
        except Exception:                                   # the log must never break the action
            log.exception("could not write the audit log")


class Monitor:
    """Raises alerts. `pause` is called with the agent ids to pause when auto-pause is on."""

    def __init__(self, store: WorkbenchStore, audit: Audit,
                 pause: Callable[[list[str], str], Awaitable[None]]):
        self.store, self.audit, self._pause = store, audit, pause
        self.auto_pause = False
        self._dms: dict[tuple[str, str], deque[float]] = defaultdict(deque)      # (from, to) -> times
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)     # (agent, kind) -> times
        self._raised: dict[tuple, float] = {}

    @staticmethod
    def _count(q: deque[float], window: float) -> int:
        now = time.monotonic()
        while q and now - q[0] > window:
            q.popleft()
        return len(q)

    async def on_message(self, msg: Record) -> None:
        """Board listener: spot two agents answering each other without end."""
        if not msg["channel"].startswith("@") or msg["sender"]["kind"] != "worker":
            return
        a, b = msg["sender"]["id"], msg["channel"][1:]
        self._dms[(a, b)].append(time.monotonic())
        if self._count(self._dms[(a, b)], LOOP_WINDOW) >= LOOP_MESSAGES \
                and self._count(self._dms[(b, a)], LOOP_WINDOW) >= LOOP_MESSAGES:
            await self.raise_alert("loop", [a, b], "critical",
                                   f"{LOOP_MESSAGES * 2}+ direct messages between two agents in "
                                   f"{int(LOOP_WINDOW)} seconds: they may be answering each other in a loop")

    async def note(self, agent_id: str, kind: str) -> None:
        """An agent hit a rate limit ("ratelimit") or asked for something it may not see ("denied")."""
        q = self._hits[(agent_id, kind)]
        q.append(time.monotonic())
        n = self._count(q, TRIP_WINDOW)
        if kind == "ratelimit" and n >= RATELIMIT_HITS:
            await self.raise_alert("limits", [agent_id], "warning",
                                   f"an agent hit a rate limit {n} times in {int(TRIP_WINDOW // 60)} minutes")
        elif kind == "denied" and n >= DENIED_HITS:
            await self.raise_alert("probing", [agent_id], "warning",
                                   f"an agent was refused {n} times in {int(TRIP_WINDOW // 60)} minutes "
                                   "(channels, files or mailboxes that do not exist or that it may not use)")

    async def raise_alert(self, kind: str, agent_ids: list[str], severity: str, text: str) -> None:
        key = (kind, tuple(sorted(agent_ids)))
        now = time.monotonic()
        if now - self._raised.get(key, -ALERT_COOLDOWN) < ALERT_COOLDOWN:
            return
        self._raised[key] = now
        names = []
        for wid in agent_ids:
            w = await self.store.get_worker(wid)
            names.append(w["name"] if w else wid)
        full = f"{', '.join(names)}: {text}"
        await self.store.add_alert({"id": uuid.uuid4().hex, "time": time.time(), "severity": severity,
                                    "kind": kind, "agent_ids": agent_ids, "text": full, "acknowledged": False})
        await self.audit.record(actor("system", "monitor", "monitor"), f"alert.{kind}", full)
        if self.auto_pause and severity == "critical":
            await self._pause(agent_ids, f"alert: {kind}")


class AdminAudit:
    """ASGI middleware: every successful change a host makes through /admin is written to the audit log."""

    def __init__(self, app, audit: Audit):
        self.app, self.audit = app, audit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS") \
                or not scope["path"].startswith("/admin"):
            return await self.app(scope, receive, send)
        method, path = scope["method"], scope["path"]

        async def wrapper(message):
            if message["type"] == "http.response.start" and message["status"] < 400:
                await self.audit.record(actor("host", "host", "host"), f"admin.{method.lower()}", f"{method} {path}")
            await send(message)

        return await self.app(scope, receive, wrapper)
