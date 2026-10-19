"""The board: shared channels and mailboxes through which participants talk to each other.

It is the sanctioned version of what agents otherwise improvise on any shared storage they can
reach: an asynchronous place that everybody reads and writes at their own pace. What makes it
safe to run (and to watch) is that it lives on the workbench:

* the sender of a message is set by the server from the authenticated token, never claimed
* channels are created by hosts only, and label lists say who may read and write them
* a mailbox (`@<worker id>`) is private to its owner (and hosts); writing to it follows the same
  label rules as handing work to that worker
* every post is stored in order and shows up live in the host UI; posting is rate limited

Hosts and API keys are not bound by channel permissions (they are outside the network).
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from . import labels as lbl
from .store import Record, WorkbenchStore

GENERAL = "general"
MAX_TEXT = 20_000
MAX_KIND = 30
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class BoardError(Exception):
    """A board request that must be refused. `status` is the suggested HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def mailbox(worker_id: str) -> str:
    return f"@{worker_id}"


class Caller:
    """Who reads or writes: a worker (`record` is its worker record), or a host / API key."""

    def __init__(self, kind: str, id: str, name: str, record: Record | None = None):
        self.kind, self.id, self.name, self.record = kind, id, name, record

    @property
    def sender(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "name": self.name}

    @property
    def label_ids(self) -> list[str]:
        return (self.record or {}).get("label_ids", [])

    @property
    def outside(self) -> bool:      # hosts and API keys are not bound by channel permissions
        return self.kind != "worker"


class Board:
    def __init__(self, store: WorkbenchStore, *, rate_per_minute: int = 60):
        self.store = store
        self.rate_per_minute = rate_per_minute
        self._posts: dict[str, deque[float]] = defaultdict(deque)   # sender id -> recent post times
        self._cond = asyncio.Condition()
        # called (in the background) with every stored message; see wakeups.py
        self.listeners: list[Callable[[Record], Awaitable[None]]] = []
        self._background: set[asyncio.Task] = set()

    async def ensure_defaults(self) -> None:
        if not await self.store.list_channels():
            await self.store.add_channel({"name": GENERAL, "description": "Open to everyone.",
                                          "read_label_ids": [], "write_label_ids": [],
                                          "created_at": time.time()})

    # -------------------------------------------------------------- channels
    def _allowed(self, label_ids: list[str], caller: Caller) -> bool:
        return caller.outside or not label_ids or bool(set(label_ids) & set(caller.label_ids))

    def can_read(self, channel: Record, caller: Caller) -> bool:
        return self._allowed(channel["read_label_ids"], caller)

    async def _writable(self, channel: Record, caller: Caller) -> bool:
        return self._allowed(channel["write_label_ids"], caller)

    async def readable_channels(self, caller: Caller) -> list[str]:
        names = [c["name"] for c in await self.store.list_channels() if self.can_read(c, caller)]
        if caller.kind == "worker":
            names.append(mailbox(caller.id))
        return names

    async def describe(self, caller: Caller) -> list[dict[str, Any]]:
        """The channels the caller may read, with whether it may also write."""
        out = []
        for c in sorted(await self.store.list_channels(), key=lambda c: c["created_at"]):
            if self.can_read(c, caller):
                out.append({"name": c["name"], "description": c["description"], "kind": "channel",
                            "can_write": await self._writable(c, caller)})
        if caller.kind == "worker":
            out.append({"name": mailbox(caller.id), "description": "Your mailbox: messages sent to you.",
                        "kind": "mailbox", "can_write": False})
        return out

    # -------------------------------------------------------------- messages
    async def post(self, channel: str, caller: Caller, text: str, *, kind: str = "post",
                   to: list[str] | None = None, refs: list[dict] | None = None) -> Record:
        text = text.strip()
        if not text:
            raise BoardError("the message is empty")
        if len(text) > MAX_TEXT:
            raise BoardError(f"the message is longer than {MAX_TEXT} characters", 413)
        kind = (kind.strip() or "post")[:MAX_KIND]

        if channel.startswith("@"):                     # a mailbox: write to a participant
            target = await self.store.get_worker(channel[1:])
            if target is None or target["status"] != "active":
                raise BoardError("no such mailbox", 404)
            if caller.kind == "worker":
                if target["id"] == caller.id:
                    raise BoardError("that is your own mailbox")
                if not lbl.may_communicate(await lbl.communication(self.store), caller.label_ids,
                                           target.get("label_ids", [])):
                    raise BoardError("the label rules do not allow you to write to this agent", 403)
            to = [target["id"]]
        else:
            ch = await self.store.get_channel(channel)
            if ch is None or not self.can_read(ch, caller):     # same answer for hidden channels
                raise BoardError("no such channel", 404)
            if not await self._writable(ch, caller):
                raise BoardError("you may read this channel but not write to it", 403)

        recipients: list[str] = []
        for wid in dict.fromkeys(to or []):
            w = await self.store.get_worker(wid)
            if w is None or w["status"] != "active":
                raise BoardError(f"unknown recipient {wid!r}", 404)
            recipients.append(wid)
        if len(recipients) > 10:
            raise BoardError("at most 10 recipients")

        self._check_rate(caller)
        msg = await self.store.append_message({
            "channel": channel, "sender": caller.sender, "kind": kind, "text": text,
            "to": recipients, "refs": [r for r in (refs or []) if isinstance(r, dict)][:10]})
        async with self._cond:
            self._cond.notify_all()
        for listener in self.listeners:                 # never makes the post wait or fail
            t = asyncio.create_task(listener(msg))
            self._background.add(t)
            t.add_done_callback(self._background.discard)
        return msg

    def _check_rate(self, caller: Caller) -> None:
        if caller.kind == "host":
            return
        q, now = self._posts[caller.id], time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.rate_per_minute:
            raise BoardError(f"too many messages: at most {self.rate_per_minute} per minute", 429)
        q.append(now)

    async def read(self, caller: Caller, *, channel: str | None = None, after: int = 0,
                   limit: int = 100, newest: bool = False) -> list[Record]:
        """Messages the caller may read (one channel, or everything it can see)."""
        visible = await self.readable_channels(caller)
        if channel is not None:
            if channel not in visible and not (caller.outside and channel.startswith("@")):
                raise BoardError("no such channel", 404)
            wanted = [channel]
        else:
            wanted = None if caller.outside else visible    # hosts and keys see everything
        return await self.store.list_messages(after=after, channels=wanted,
                                              limit=max(1, min(limit, 500)), newest=newest)

    async def wait(self, caller: Caller, *, channel: str | None, after: int, timeout: float,
                   limit: int = 100) -> list[Record]:
        """Like `read`, but if nothing is new, wait up to `timeout` seconds for something."""
        msgs = await self.read(caller, channel=channel, after=after, limit=limit)
        if msgs or timeout <= 0:
            return msgs
        deadline = time.monotonic() + timeout
        while not msgs:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                async with self._cond:
                    await asyncio.wait_for(self._cond.wait(), left)
            except asyncio.TimeoutError:
                break
            msgs = await self.read(caller, channel=channel, after=after, limit=limit)
        return msgs
