"""HumanWorker: a worker whose "agent" is a person.

To the workbench it is an ordinary participant. Agents (and hosts) hand it tasks, it can be woken by
board messages, it shows up in the participant list with an icon and skills, and the task tree
shows what it was asked and what it answered. Behind the A2A endpoint there is no LLM, but an
inbox that a human works through in the worker's web UI (`/ui/inbox.html`):

* **reply**: the answer completes the task
* **ask back**: the task goes to `input_required`; the requester answers with a message and the
  task resumes (the same mechanism an LLM agent uses to ask a human)
* **decline**: the task is rejected, with a reason

Messages that arrive while a task is open ("btw") are added to its thread. From the same page the
person can also take part like any agent: read and post on the board, list the participants and
hand tasks to them.

Inbox items live in memory (like the A2A task store), so they are gone after a restart.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..workbench import WorkbenchError
from .worker_server import WorkerServer

log = logging.getLogger(__name__)

MAX_ITEMS = 300


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class PostIn(TextIn):
    channel: str = Field(max_length=100)
    kind: str = Field(default="post", max_length=30)
    to: list[str] = Field(default_factory=list, max_length=10)
    workbench: str | None = None


class StartIn(TextIn):
    agent_id: str
    workbench: str | None = None


class _Item:
    def __init__(self, task_id: str, context_id: str):
        self.task_id, self.context_id = task_id, context_id
        self.state: Literal["open", "asked", "answered", "declined", "canceled"] = "open"
        self.created_at = time.time()
        self.messages: list[dict[str, Any]] = []
        self.resolution: tuple[str, str] | None = None      # (action, text) chosen by the human
        self.event = asyncio.Event()

    def add(self, who: str, text: str) -> None:
        self.messages.append({"from": who, "text": text, "time": time.time()})

    def view(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "state": self.state, "created_at": self.created_at,
                "messages": self.messages}


class HumanWorker(WorkerServer):
    supports_btw = True
    features = ("inbox",)

    def __init__(self, *args: Any, response_timeout: float | None = None, **kwargs: Any):
        """`response_timeout`: seconds after which an unanswered task is rejected (None: wait forever)."""
        super().__init__(*args, **kwargs)
        self.response_timeout = response_timeout
        self._items: dict[str, _Item] = {}

    # ---------------------------------------------------------------- the A2A side
    async def run_task(self, context: RequestContext, event_queue: EventQueue, workbench) -> None:
        fresh = context.current_task is None
        task = context.current_task or new_task_from_user_message(context.message)
        if fresh:
            await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue, task.id, task.context_id)

        item = self._items.get(task.id)
        if item is None:
            item = self._items[task.id] = _Item(task.id, task.context_id)
            if len(self._items) > MAX_ITEMS:                        # forget the oldest finished ones
                for old in [t for t, i in self._items.items() if i.state not in ("open", "asked")]:
                    self._items.pop(old, None)
                    if len(self._items) <= MAX_ITEMS:
                        break
        item.add("requester", context.get_user_input())
        item.state, item.resolution = "open", None
        item.event = asyncio.Event()
        await up.start_work(up.new_agent_message([Part(text="Waiting for a human.")]))

        deadline = None if self.response_timeout is None else time.monotonic() + self.response_timeout
        try:
            while not item.event.is_set():
                for extra in self.take_messages(task.id):           # "btw": more input while it is open
                    item.add("requester", extra)
                if deadline is not None and time.monotonic() > deadline:
                    item.resolution = ("decline", "nobody answered in time")
                    break
                try:
                    await asyncio.wait_for(item.event.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
            for extra in self.take_messages(task.id):
                item.add("requester", extra)
            self.close_inbox(task.id)
            action, text = item.resolution or ("decline", "no answer")
            if action == "reply":
                item.state = "answered"
                await up.add_artifact([Part(text=text)], artifact_id="answer", append=False, last_chunk=True)
                await up.complete()
            elif action == "ask":
                item.state = "asked"
                await up.requires_input(up.new_agent_message([Part(text=text)]))
            else:
                item.state = "declined"
                await up.reject(up.new_agent_message([Part(text=text)]))
        except asyncio.CancelledError:
            item.state = "canceled"
            raise

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        item = self._items.get(context.task_id)
        if item is not None:
            item.state = "canceled"
        await super().cancel(context, event_queue)

    # ------------------------------------------------------------- the human side
    def resolve(self, task_id: str, action: Literal["reply", "ask", "decline"], text: str) -> None:
        item = self._items.get(task_id)
        if item is None or item.state != "open":
            raise ValueError("this task is not waiting for you")
        item.add("human", text)
        item.resolution = (action, text)
        item.event.set()

    def extend_admin(self, router: APIRouter) -> None:
        def item_or_404(task_id: str) -> _Item:
            item = self._items.get(task_id)
            if item is None:
                raise HTTPException(404, "no such task")
            return item

        def resolve(task_id: str, action: Literal["reply", "ask", "decline"], text: str) -> dict:
            item_or_404(task_id)
            try:
                self.resolve(task_id, action, text)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
            return {"ok": True}

        def conn(url: str | None):
            try:
                return self.workbench_connection(url)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e

        async def wb(call):
            try:
                return await call
            except WorkbenchError as e:
                raise HTTPException(e.status or 502, str(e)) from e

        @router.get("/human/inbox")
        async def inbox():
            return sorted((i.view() for i in self._items.values()), key=lambda i: -i["created_at"])

        @router.post("/human/inbox/{task_id}/reply")
        async def reply(task_id: str, body: TextIn):
            return resolve(task_id, "reply", body.text)

        @router.post("/human/inbox/{task_id}/ask")
        async def ask(task_id: str, body: TextIn):
            return resolve(task_id, "ask", body.text)

        @router.post("/human/inbox/{task_id}/decline")
        async def decline(task_id: str, body: TextIn):
            return resolve(task_id, "decline", body.text)

        # taking part like any agent: board, participants, handing out tasks
        @router.get("/human/participants")
        async def participants(workbench: str | None = None):
            return [p.model_dump() for p in await wb(conn(workbench).participants())]

        @router.get("/human/board/channels")
        async def channels(workbench: str | None = None):
            return [c.model_dump() for c in await wb(conn(workbench).board_channels())]

        @router.get("/human/board/feed")
        async def feed(after: int = 0, wait: float = 0, workbench: str | None = None):
            return [m.model_dump() for m in await wb(conn(workbench).board_feed(after=after, wait=min(wait, 25)))]

        @router.post("/human/board")
        async def post(body: PostIn):
            m = await wb(conn(body.workbench).board_post(body.channel, body.text, kind=body.kind, to=body.to))
            return m.model_dump()

        @router.post("/human/tasks")
        async def start(body: StartIn):
            t = await wb(conn(body.workbench).start_task(body.agent_id, body.text))
            return t.model_dump()

        @router.get("/human/tasks/{task_id}")
        async def task(task_id: str, wait: float = 0, workbench: str | None = None):
            return (await wb(conn(workbench).get_task(task_id, wait=min(wait, 25)))).model_dump()
