"""What a running task may know about the workbench it was started by.

`WorkerServer.execute` sets this for the duration of `run_task`, so tools (which only
receive their own arguments) can reach the caller's workbench.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .worker_server import WorkbenchConnection


@dataclass(frozen=True)
class TaskContext:
    workbench: "WorkbenchConnection | None"   # None for a direct (non-workbench) caller
    task_id: str | None                       # the workbench's id of this task, if it has one


_current: ContextVar[TaskContext | None] = ContextVar("workbench_task_context", default=None)


def current_task_context() -> TaskContext | None:
    return _current.get()
