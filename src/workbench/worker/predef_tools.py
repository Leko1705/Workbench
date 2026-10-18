from typing import Any, Sequence
from .context import current_task_context
from .tools import Tool, Parameter, ToolError
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .worker_server import WorkbenchConnection
import asyncio
import shlex
import os
import signal


class BashTool(Tool):
    """Run a shell command.

    WARNING: this gives the agent (and whoever can talk to it) code execution
    on the host. Run the worker in a container/VM, set `cwd`, and prefer
    `allowed_commands` where you can.

    - Default mode runs the command through `bash -c` (pipes, `&&`, ... work).
    - With `allowed_commands`, the command is split with shlex and executed
      directly, *without* a shell, and only if the program name is in the list.
      Shell operators then have no special meaning.
    """

    def __init__(
        self,
        *,
        name: str = "bash",
        cwd: str | None = None,
        timeout: int = 30,
        max_output: int = 20_000,
        allowed_commands: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        super().__init__(
            name,
            "Run a shell command and return its exit code and combined output.",
            [
                Parameter("command", "string", "The command to run."),
                Parameter("timeout", "integer",
                          f"Timeout in seconds (max {timeout}).",
                          required=False, default=timeout),
            ],
        )
        self.cwd = cwd
        self.max_timeout = timeout
        self.max_output = max_output
        self.allowed = set(allowed_commands) if allowed_commands is not None else None
        self.env = env

    async def execute(self, command: str, timeout: int) -> str:

        timeout = max(1, min(timeout, self.max_timeout))
        env = {**os.environ, **(self.env or {})}
        kwargs: dict[str, Any] = dict(
            cwd=self.cwd, env=env, start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
        if self.allowed is not None:
            try:
                argv = shlex.split(command)
            except ValueError as e:
                raise ToolError(f"cannot parse command: {e}") from e
            if not argv or argv[0] not in self.allowed:
                raise ToolError(f"command not allowed; allowed: {sorted(self.allowed)}")
            proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        else:
            proc = await asyncio.create_subprocess_exec("bash", "-c", command, **kwargs)

        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            raise ToolError(f"command timed out after {timeout}s") from None

        text = out.decode(errors="replace")
        if len(text) > self.max_output:
            text = text[: self.max_output] + f"\n[... truncated at {self.max_output} chars]"
        return f"exit_code={proc.returncode}\n{text}"


def _workbench() -> "WorkbenchConnection":
    ctx = current_task_context()
    if ctx is None or ctx.workbench is None:
        raise ToolError("this task was not started through a workbench")
    return ctx.workbench


class ListParticipantsTool(Tool):
    """Lists the other agents in the workbench this task came from."""

    def __init__(
        self,
        *,
        name: str = "list_participants",
        description: str = "List the other agents you can delegate work to: id, name, description and skills.",
    ):
        super().__init__(name=name, description=description, parameters=[])

    async def execute(self) -> str:
        participants = await _workbench().participants()
        if not participants:
            return "There are no other agents in the workbench."
        lines = []
        for p in participants:
            skills = ", ".join(str(s.get("name") or s.get("id")) if isinstance(s, dict) else str(s)
                               for s in p.skills)
            lines.append(f"- id={p.id} name={p.name!r}: {p.description}" + (f" [skills: {skills}]" if skills else ""))
        return "\n".join(lines)


class DelegateTool(Tool):
    """Hands a job to another agent through the workbench and returns its answer.

    The new task is filed as a sub task of the one the agent is working on, so the
    hosts can see it and cancelling the parent cancels it too.
    """

    def __init__(
        self,
        *,
        name: str = "delegate_task",
        description: str = ("Give a task to another agent (see list_participants for ids) and wait for its "
                            "answer. Describe the task completely: the other agent does not see this conversation."),
        max_output: int = 20_000,
    ):
        super().__init__(name=name, description=description, parameters=[
            Parameter("agent_id", "string", "Id of the agent to delegate to"),
            Parameter("text", "string", "The complete task description"),
        ])
        self.max_output = max_output

    async def execute(self, agent_id: str, text: str) -> str:
        wb = _workbench()
        task = await wb.run_task(agent_id, text, parent_task_id=current_task_context().task_id)
        out = task.output[: self.max_output]
        if task.state == "completed":
            return out
        if task.state == "input_required":
            return f"The agent needs more input: {task.status_text or out}\n(task id {task.id})"
        return f"The task ended as {task.state}. {task.error or task.status_text}\n{out}".strip()
