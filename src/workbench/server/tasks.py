"""Task delegation and the task tree of the workbench.

Every task the workbench delegates to a worker is a node of a tree:

  * a *root* task is started by a host (admin API) and has no parent,
  * a *sub task* is started by a worker through the workbench API and names the
    task it works on as `parent_id` (the worker learns its own task id from the
    `metadata.workbench.task_id` of the A2A request it received).

All A2A traffic goes through this class, so the workbench sees every hop. Cancelling a
task cancels its whole subtree, deepest tasks first, the requested task last.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import logging
import time
import uuid
from collections import defaultdict
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.helpers import get_artifact_text, get_message_text
from google.protobuf.json_format import MessageToDict
from a2a.types import (CancelTaskRequest, Message, Part, Role, SendMessageRequest, StreamResponse,
                       TaskState)

from ..extensions import BTW_PATH
from .store import Record, WorkbenchStore

log = logging.getLogger(__name__)

FINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})
_STATE_NAMES = {
    TaskState.TASK_STATE_SUBMITTED: "submitted",
    TaskState.TASK_STATE_WORKING: "working",
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_CANCELED: "canceled",
    TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
    TaskState.TASK_STATE_REJECTED: "rejected",
    TaskState.TASK_STATE_AUTH_REQUIRED: "auth_required",
}
MAX_TEXT = 100_000        # characters kept of a task's input / output
FLUSH_INTERVAL = 0.5      # seconds between store writes while output streams in


WAIT_GRACE = 3.0          # seconds after a requester's last poll that it still counts as waiting
TOPIC_LEN = 60


def make_topic(text: str) -> str:
    """A short title for a task: its first line or sentence, shortened.

    The topic is a plain string in the task record, so a smarter summary (e.g. an LLM)
    can replace this function through `TaskManager.topic_fn`.
    """
    line = " ".join(text.split("\n", 1)[0].split())
    m = re.match(r"(.{12,}?[.!?])(\s|$)", line)
    if m:
        line = m.group(1)
    return line if len(line) <= TOPIC_LEN else line[:TOPIC_LEN - 1].rstrip() + "…"


def origin(requested_by: str | None) -> str:
    """Who asked, for the event log: host, api (an API key), board (a board message) or agent."""
    if requested_by in (None, "host"):
        return "host"
    return "api" if requested_by.startswith("key:") else "board" if requested_by == "board" else "agent"


class TaskError(Exception):
    """A task request that must be refused. `status` is the suggested HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class TaskManager:
    def __init__(self, store: WorkbenchStore, public_url: Callable[[], str], *,
                 max_depth: int = 8, max_active: int = 100, cancel_timeout: float = 5.0,
                 http_factory: Callable[[dict[str, str]], httpx.AsyncClient] | None = None):
        self.store = store
        self._public_url = public_url
        self.max_depth, self.max_active, self.cancel_timeout = max_depth, max_active, cancel_timeout
        self._http_factory = http_factory or (lambda headers: httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(10.0, read=None)))   # streams may idle
        self._clients: dict[str, tuple[Client, httpx.AsyncClient]] = {}
        self._client_lock = asyncio.Lock()
        self._runners: dict[str, asyncio.Task] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._canceled: set[str] = set()
        self._notified: set[str] = set()        # finished children already reported to their parent
        self._polling: dict[str, int] = {}      # task id -> requester polls currently in flight
        self._last_poll: dict[str, float] = {}  # task id -> when a requester last polled it
        self.topic_fn: Callable[[str], str] = make_topic

    # ------------------------------------------------------------- lifecycle
    async def recover(self) -> None:
        """After a restart nothing runs any more: close tasks a persistent store left open."""
        for t in await self.store.list_tasks():
            if t["state"] not in FINAL_STATES:
                await self.store.update_task(t["id"], state="failed", updated_at=time.time(),
                                             error="the workbench restarted while the task was running")

    async def close(self) -> None:
        for r in list(self._runners.values()):
            r.cancel()
        await asyncio.gather(*self._runners.values(), return_exceptions=True)
        for _, http in self._clients.values():
            await http.aclose()
        self._clients.clear()

    # ----------------------------------------------------------------- start
    async def start(self, worker: Record, text: str, *, requested_by: str,
                    parent: Record | None = None, context_id: str | None = None) -> Record:
        """Create a task for `worker` and begin running it in the background.

        `requested_by` is the id of the worker that asked for it, or "host".
        """
        if worker["status"] != "active":
            raise TaskError("that agent is not active in this workbench", 409)
        if not text.strip():
            raise TaskError("text must not be empty")
        depth = 0
        if parent is not None:
            if parent["state"] in FINAL_STATES:
                raise TaskError("the parent task has already finished", 409)
            depth = parent["depth"] + 1
            if depth > self.max_depth:
                raise TaskError(f"task tree too deep (max {self.max_depth})", 429)
        if len(self._runners) >= self.max_active:
            raise TaskError("too many running tasks", 429)

        now, tid = time.time(), uuid.uuid4().hex
        rec: Record = {
            "id": tid, "topic": self.topic_fn(text) or "(no topic)", "root_id": parent["root_id"] if parent else tid,
            "parent_id": parent["id"] if parent else None, "depth": depth,
            "worker_id": worker["id"], "requested_by": requested_by, "context_id": context_id or uuid.uuid4().hex,
            "a2a_task_id": None, "state": "submitted", "input": text[:MAX_TEXT], "output": "",
            "status_text": "", "error": "", "created_at": now, "updated_at": now,
        }
        await self.store.add_task(rec)
        await self.store.append_event(tid, {"type": "input", "text": rec["input"],
                                            "from": origin(requested_by)})
        if parent is not None:
            await self.store.append_event(parent["id"], {
                "type": "delegated", "child_id": tid, "agent": worker["name"], "topic": rec["topic"]})
        self._done[tid] = asyncio.Event()
        self._runners[tid] = asyncio.create_task(self._run(rec, worker), name=f"task-{tid}")
        return rec

    def is_running(self, task_id: str) -> bool:
        return task_id in self._runners

    def is_waited(self, task_id: str) -> bool:
        """Is the requester of this task actively waiting for its result right now?

        True while a long poll of the task is in flight, and for a moment after the
        last poll (a waiting agent immediately polls again).
        """
        return self._polling.get(task_id, 0) > 0 \
            or time.monotonic() - self._last_poll.get(task_id, float("-inf")) < WAIT_GRACE

    @contextlib.asynccontextmanager
    async def polling(self, task_id: str, long: bool):
        """Wrap a requester's status request; `long` polls count as actively waiting."""
        if long:
            self._polling[task_id] = self._polling.get(task_id, 0) + 1
        self._last_poll[task_id] = time.monotonic()
        try:
            yield
        finally:
            if long:
                self._polling[task_id] -= 1
                if not self._polling[task_id]:
                    del self._polling[task_id]
            self._last_poll[task_id] = time.monotonic()

    async def wait(self, task_id: str, timeout: float) -> None:
        """Return when the task's run ended (any state) or after `timeout` seconds."""
        ev = self._done.get(task_id)
        if ev is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), timeout)

    # ------------------------------------------------------------------ run
    async def send_message(self, task_id: str, text: str, *, requested_by: str | None = None,
                           source: str | None = None) -> Record:
        """Add a message to an unfinished task.

        * waiting for input (`input_required`/`auth_required`): the task resumes with it
        * still running ("btw"): the message is queued at the worker, which reads it at its
          next safe point (between generation and tool steps), if the agent supports it
        * finished: refused; start a new task with the same `context_id` to continue

        With `requested_by` the task must be one that worker started.
        """
        rec = await self.store.get_task(task_id)
        if rec is None or (requested_by is not None and rec["requested_by"] != requested_by):
            raise TaskError("no such task", 404)
        if not text.strip():
            raise TaskError("text must not be empty")
        if rec["state"] in FINAL_STATES:
            raise TaskError("the task has finished; start a new task with the same context_id", 409)
        worker = await self.store.get_worker(rec["worker_id"])
        if task_id in self._runners:
            return await self._send_btw(rec, worker, text[:MAX_TEXT], requested_by, source)
        if worker is None or worker["status"] != "active" or not rec["a2a_task_id"]:
            raise TaskError("the task cannot be continued", 409)
        rec = await self.store.update_task(task_id, state="working", status_text="",
                                           updated_at=time.time())
        await self.store.append_event(task_id, {
            "type": "input", "text": text[:MAX_TEXT], "from": source or origin(requested_by)})
        self._done[task_id] = asyncio.Event()
        self._runners[task_id] = asyncio.create_task(self._run(rec, worker, followup=text[:MAX_TEXT]),
                                                     name=f"task-{task_id}")
        return rec

    async def _send_btw(self, rec: Record, worker: Record | None, text: str,
                        requested_by: str | None, source: str | None = None) -> Record:
        """Queue a message for a task that is running (see `BTW_EXTENSION_URI`)."""
        if worker is None or worker["status"] != "active":
            raise TaskError("the agent is no longer part of this workbench", 409)
        if not worker.get("btw"):
            raise TaskError("this agent cannot take messages while it works; wait until it is done "
                            "or cancel the task", 409)
        if not rec["a2a_task_id"]:
            raise TaskError("the task has only just started; try again in a moment", 409)
        await self._client_for(worker)                  # makes sure the authenticated HTTP client exists
        http = self._clients[worker["id"]][1]
        try:
            resp = await http.post(worker["a2a_url"].rstrip("/") + BTW_PATH,
                                   json={"task_id": rec["a2a_task_id"], "text": text}, timeout=10.0)
        except httpx.HTTPError as e:
            raise TaskError(f"the agent could not be reached: {type(e).__name__}", 502) from e
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = None
            raise TaskError(detail if isinstance(detail, str) else f"the agent refused (HTTP {resp.status_code})",
                            409 if resp.status_code in (404, 409, 501) else 502)
        await self.store.append_event(rec["id"], {
            "type": "btw", "text": text,
            "from": source or origin(requested_by)})
        return await self.store.update_task(rec["id"], updated_at=time.time()) or rec

    async def _run(self, rec: Record, worker: Record, followup: str | None = None) -> None:
        tid = rec["id"]
        # artifact id -> text, in arrival order; a resumed task keeps what it produced before
        outputs: dict[str, str] = {"_earlier": rec["output"]} if followup and rec["output"] else {}
        state, last_flush, dirty = ("working" if followup else "submitted"), 0.0, False

        async def flush(**extra: Any) -> None:
            nonlocal last_flush, dirty
            if tid in self._canceled:
                return
            await self.store.update_task(
                tid, state=state, output="\n".join(outputs.values())[:MAX_TEXT],
                updated_at=time.time(), **extra)
            last_flush, dirty = time.monotonic(), False

        try:
            client = await self._client_for(worker)
            msg = Message(message_id=uuid.uuid4().hex, context_id=rec["context_id"],
                          task_id=rec["a2a_task_id"] if followup else None,
                          role=Role.ROLE_USER, parts=[Part(text=followup or rec["input"])])
            req = SendMessageRequest(message=msg)
            req.metadata.update({"workbench": {
                "url": self._public_url(), "task_id": tid,
                "parent_task_id": rec["parent_id"], "root_task_id": rec["root_id"]}})
            async for ev in client.send_message(req):
                extra, events = self._apply_event(ev, outputs)
                changed = extra.get("state", state) != state
                if changed:
                    events.append({"type": "state", "state": extra["state"]})
                for e in events:
                    if tid not in self._canceled:
                        await self.store.append_event(tid, e)
                if "state" in extra:
                    state = extra.pop("state")
                if "a2a_task_id" in extra:
                    await self.store.update_task(tid, a2a_task_id=extra.pop("a2a_task_id"))
                dirty = True
                if extra or changed or time.monotonic() - last_flush >= FLUSH_INTERVAL \
                        or state in FINAL_STATES:
                    await flush(**extra)
            if state in ("submitted", "working"):
                state = "failed"
                await flush(error="the worker's response ended before the task finished")
            elif dirty:
                await flush()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("task %s failed: %s", tid, e)
            state = "failed"
            await flush(error=f"{type(e).__name__}: {e}"[:2000])
        finally:
            self._runners.pop(tid, None)
            if state in FINAL_STATES and tid not in self._canceled:
                await self._report_to_parent(tid)
            ev = self._done.get(tid)
            if ev is not None:
                ev.set()

    async def _report_to_parent(self, tid: str) -> None:
        """Log in the parent's event list that a sub task ended (once)."""
        rec = await self.store.get_task(tid)
        if rec is None or not rec["parent_id"] or tid in self._notified:
            return
        self._notified.add(tid)
        worker = await self.store.get_worker(rec["worker_id"])
        await self.store.append_event(rec["parent_id"], {
            "type": "child_result", "child_id": tid, "state": rec["state"], "topic": rec["topic"],
            "agent": worker["name"] if worker else "(removed)"})

    @staticmethod
    def _apply_event(ev: StreamResponse, outputs: dict[str, str]) -> tuple[dict[str, Any], list[dict]]:
        """Fold one A2A event into `outputs`.

        Returns the task fields that changed and the log events it contains: status
        messages and the tool calls that workers report as data parts.
        """
        out: dict[str, Any] = {}
        events: list[dict] = []
        if ev.HasField("task"):
            out["a2a_task_id"] = ev.task.id
            out["state"] = _STATE_NAMES.get(ev.task.status.state, "working")
            for a in ev.task.artifacts:
                outputs[a.artifact_id] = get_artifact_text(a, "")
        elif ev.HasField("status_update"):
            su = ev.status_update
            out["a2a_task_id"] = su.task_id
            out["state"] = _STATE_NAMES.get(su.status.state, "working")
            if su.status.HasField("message"):
                text = get_message_text(su.status.message)
                if text:
                    out["status_text"] = text[:2000]
                    events.append({"type": "status", "text": text[:2000]})
                for part in su.status.message.parts:
                    if part.HasField("data"):
                        d = MessageToDict(part.data)
                        if isinstance(d, dict) and "btw_applied" in d:      # the worker read queued messages
                            events.append({"type": "btw_applied"})
                        elif isinstance(d, dict) and isinstance(d.get("tool"), str):
                            events.append({"type": "tool", "id": str(d.get("id", ""))[:200],
                                           "tool": d["tool"][:200], "status": str(d.get("status", ""))[:20],
                                           "input": d.get("input"), "output": str(d.get("output", ""))[:4000]})
        elif ev.HasField("artifact_update"):
            au = ev.artifact_update
            text = get_artifact_text(au.artifact, "")
            aid = au.artifact.artifact_id
            outputs[aid] = outputs.get(aid, "") + text if au.append else text
        elif ev.HasField("message"):          # a plain reply without a task
            outputs["reply"] = get_message_text(ev.message)
            out["state"] = "completed"
        return out, events

    # --------------------------------------------------------------- cancel
    async def cancel(self, task_id: str) -> list[str]:
        """Cancel a task and all its descendants: children first, `task_id` last.

        Returns the ids that were still running and are now canceled, in that order.
        """
        rec = await self.store.get_task(task_id)
        if rec is None:
            raise TaskError("no such task", 404)
        children: dict[str, list[str]] = defaultdict(list)
        for t in await self.store.list_tasks(root_id=rec["root_id"]):
            if t["parent_id"]:
                children[t["parent_id"]].append(t["id"])
        order: list[str] = []

        def post_order(tid: str) -> None:
            for c in children[tid]:
                post_order(c)
            order.append(tid)
        post_order(task_id)

        canceled = []
        for tid in order:
            if await self._cancel_one(tid):
                canceled.append(tid)
        return canceled

    async def _cancel_one(self, tid: str) -> bool:
        rec = await self.store.get_task(tid)
        if rec is None or rec["state"] in FINAL_STATES:
            return False
        self._canceled.add(tid)             # the runner must not overwrite the result any more
        if rec["a2a_task_id"]:
            worker = await self.store.get_worker(rec["worker_id"])
            if worker is not None:
                try:
                    client = await self._client_for(worker)
                    await asyncio.wait_for(client.cancel_task(CancelTaskRequest(id=rec["a2a_task_id"])),
                                           self.cancel_timeout)
                except Exception as e:      # the worker may be gone; still cancel on our side
                    log.info("worker did not confirm cancel of %s: %s", tid, e)
        runner = self._runners.get(tid)
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        await self.store.update_task(tid, state="canceled", updated_at=time.time(),
                                     status_text="canceled")
        await self.store.append_event(tid, {"type": "state", "state": "canceled"})
        await self._report_to_parent(tid)
        ev = self._done.get(tid)
        if ev is not None:
            ev.set()
        return True

    async def cancel_worker_tasks(self, worker_id: str) -> None:
        """Cancel everything a worker is working on (it left or was removed)."""
        for t in await self.store.list_tasks(worker_id=worker_id):
            if t["state"] not in FINAL_STATES:
                await self.cancel(t["id"])

    # -------------------------------------------------------------- clients
    async def _client_for(self, worker: Record) -> Client:
        async with self._client_lock:
            cached = self._clients.get(worker["id"])
            if cached is not None:
                return cached[0]
            http = self._http_factory({"Authorization": f"Bearer {worker['inbound_token']}"})
            try:
                base = worker["card_url"].rsplit("/.well-known/", 1)[0]
                client = await ClientFactory(ClientConfig(streaming=True, httpx_client=http)) \
                    .create_from_url(base)
            except BaseException:
                await http.aclose()
                raise
            self._clients[worker["id"]] = (client, http)
            return client

    async def forget_worker(self, worker_id: str) -> None:
        cached = self._clients.pop(worker_id, None)
        if cached is not None:
            await cached[1].aclose()


def is_url_ok(url: str, *, allow_insecure: bool) -> bool:
    """Is `url` something the workbench may send a worker's token to?"""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password \
            or p.query or p.fragment:
        return False
    return p.scheme == "https" or allow_insecure or p.hostname in ("localhost", "127.0.0.1", "::1")
