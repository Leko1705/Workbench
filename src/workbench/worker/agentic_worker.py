"""GPWorker: a general-purpose, LLM-driven worker that streams its progress."""
from __future__ import annotations

import logging

from a2a.helpers import new_data_part, new_task_from_user_message
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState

from .llm_providers import ChatMessage, LLMProvider, LLMResponse, TextDelta, ToolCall, ToolResult
from .worker_server import WorkerServer, WorkbenchConnection

log = logging.getLogger(__name__)

ANSWER = "answer"          # artifact id that carries the streamed text
MAX_TOOL_OUTPUT = 2000     # characters of tool output forwarded to the caller


class AgenticWorker(WorkerServer):
    """Agent loop: stream LLM text -> run requested tools -> repeat until done.

    What the caller sees (A2A streaming):
      * status updates (WORKING) with a data part per tool call:
          {"id", "tool", "input", "status": "running"}
          {"id", "tool", "status": "done" | "error", "output"}
      * artifact "answer": text chunks (first chunk append=False, rest append=True,
        final empty chunk last_chunk=True)
      * a final COMPLETED (or FAILED) status
    """

    def __init__(
        self,
        llm: LLMProvider,
        model: str,
        name: str | None = None,
        description: str = "",
        version: str = "0.1.0",
        *,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
        max_tool_rounds: int = 10,
        **server_kwargs,  # skills, tools, state_file, task_store, streaming
    ):
        super().__init__(name=name, description=description, version=version, **server_kwargs)
        self.llm = llm
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.max_tool_rounds = max_tool_rounds
        # conversation memory per A2A context_id (in-memory; lost on restart)
        self.histories: dict[str, list[ChatMessage]] = {}

    def _system(self) -> str | None:
        return self.system_prompt or self.agent_card.description or None

    async def run_task(self, context: RequestContext, event_queue: EventQueue, workbench: WorkbenchConnection | None) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)       # the Task must come first
        up = TaskUpdater(event_queue, task.id, task.context_id)
        await up.start_work()

        history = self.histories.setdefault(task.context_id, [])
        mark = len(history)                             # roll back to here on failure
        history.append(ChatMessage.user(context.get_user_input()))

        # --- streamed answer text -----------------------------------------
        started = False       # has the first chunk been sent? (it must use append=False)
        wrote_text = False    # any text so far? (to separate text of different rounds)

        async def emit(text: str) -> None:
            nonlocal started
            await up.add_artifact([Part(text=text)], artifact_id=ANSWER,
                                  append=started, last_chunk=False)
            started = True

        try:
            for round_no in range(self.max_tool_rounds):
                response: LLMResponse | None = None
                round_has_text = False
                async for ev in self.llm.stream(
                    self.model, history, self.active_tools(),
                    system=self._system(), max_tokens=self.max_tokens,
                ):
                    if isinstance(ev, TextDelta):
                        if not round_has_text and wrote_text:
                            await emit("\n\n")          # separate rounds visually
                        round_has_text = True
                        await emit(ev.text)
                    elif isinstance(ev, LLMResponse):
                        response = ev
                wrote_text = wrote_text or round_has_text
                if response is None:
                    raise RuntimeError("LLM stream ended without a final LLMResponse")

                history.append(response.to_message())
                if not response.tool_calls:
                    break
                results = [await self._run_tool(up, call) for call in response.tool_calls]
                history.append(ChatMessage.tool(results))
            else:
                await emit("\n\n[stopped: tool round limit reached]")

            if started:
                await up.add_artifact([Part(text="")], artifact_id=ANSWER,
                                      append=True, last_chunk=True)
            await up.complete()

        except Exception as e:
            log.exception("GPWorker task %s failed", task.id)
            del history[mark:]      # never leave a dangling tool call in the history
            await up.failed(up.new_agent_message([Part(text=f"{type(e).__name__}: {e}")]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        up = TaskUpdater(event_queue, context.task_id, context.context_id)
        self.histories.pop(context.task_id, None)
        await up.cancel()

    async def _run_tool(self, up: TaskUpdater, call: ToolCall) -> ToolResult:
        await up.update_status(TaskState.TASK_STATE_WORKING, up.new_agent_message(
            [new_data_part({"id": call.id, "tool": call.name,
                            "input": call.input, "status": "running"})]))
        try:
            out, is_error = str(await self.call_tool(call.name, **call.input)), False
        except Exception as e:      # bad args, disabled tool, tool crash, timeout ...
            log.warning("tool %s failed: %s", call.name, e)
            out, is_error = f"{type(e).__name__}: {e}", True
        await up.update_status(TaskState.TASK_STATE_WORKING, up.new_agent_message(
            [new_data_part({"id": call.id, "tool": call.name,
                            "status": "error" if is_error else "done",
                            "output": out[:MAX_TOOL_OUTPUT]})]))
        return ToolResult(call.id, out, is_error)