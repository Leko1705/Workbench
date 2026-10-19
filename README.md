# Workbench

A small framework for running **networks of AI agents** that cooperate through one central server, using the [A2A (Agent2Agent) protocol](https://a2a-protocol.org).

It has two parts:

- **Workers** (`WorkerServer`, `AgenticWorker`): single agents. Each one is an A2A server with a web UI. An `AgenticWorker` is an LLM agent loop with tools, and you can subclass `WorkerServer` to write any other kind of agent.
- **The workbench** (`WorkbenchServer`): the central server the workers join. Agents never talk to each other directly. When one agent wants help from another, it asks the workbench, which relays the task over A2A. That way every task and sub-task is recorded in a **task tree** that people can inspect, follow live, and cancel.

You can think of the workbench as a company: workers are employees, labels are roles, and the workbench is the office that routes work between them.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- For the demo worker: an API key for an LLM provider (Anthropic by default; OpenAI and OpenAI-compatible servers are supported as well)

## Set up

```bash
git clone https://github.com/Leko1705/Workbench.git
cd Workbench
uv sync
```

### 1. Start a workbench

```bash
uv run python src/run_workbench.py
```

It serves on <http://127.0.0.1:8000>. Log in at `/ui/` with user `admin` and the password set in `src/run_workbench.py` (`1234` in the demo). The password can also come from the `ADMIN_PASSWORD` environment variable.

```python
from workbench.server import WorkbenchServer

WorkbenchServer().run(host="127.0.0.1", port=8000, admin_password="change-me")
```

If the workbench is reachable from other machines, also pass `public_url="https://your.host"`. Workers know the workbench by this address, and it must be the same address they join with. Run it behind TLS in production.

### 2. Start a worker

Edit `src/server.py`: set a real `model` (it ships with the placeholder `"xxx"`), then:

```bash
export ANTHROPIC_API_KEY=...
uv run python src/server.py
```

The worker serves on <http://127.0.0.1:9999>, with its UI at `/ui/` (user `admin`, password from `src/server.py`).

To write your own agent, subclass `WorkerServer` and implement `run_task`, or configure an `AgenticWorker` with your own tools and skills:

```python
from workbench.worker import *

agent = AgenticWorker(
    name="Researcher",
    llm=AnthropicProvider(),
    model="<model id>",
    description="Finds and summarises information.",
    tools=[ListParticipantsTool(), DelegateTool()],
)
agent.run(host="127.0.0.1", port=9999, admin_password="change-me")
```

### 3. Connect the worker to the workbench

1. In the workbench UI, create an **invite** (sidebar → Invites). It gives you a **join token** (`wbj1.…`).
2. In the worker UI, paste the token into "Join token" and press **Connect**. (You can also enter the workbench URL and invite code separately.)
3. Unless the invite was set to approve automatically, approve the worker in the workbench's agent list.

The worker now appears in the workbench, and you can start tasks on it.

### 4. Start tasks

Pick an agent in the workbench's "New task" form. The task shows up in the list, and clicking it opens the task tree. If the agent has the `DelegateTool`, it can hand parts of the work to other agents, and those sub-tasks appear in the tree as they are created.

## Features

### Workers

- A2A JSON-RPC endpoint with bearer-token authentication and a public agent card, including an optional agent icon
- `AgenticWorker`: streaming LLM loop with tool calls, a round limit and per-conversation memory, and it takes messages that arrive while it works
- LLM providers: Anthropic and OpenAI (and OpenAI-compatible servers such as vLLM or Ollama)
- Tools: your own (`Tool` subclasses), `BashTool` (timeout and command allowlist), `ListParticipantsTool`, `DelegateTool`
- Admin web UI: log in with a username and password, see which workbenches the worker is connected to (with live connection status), join one with a token, and disconnect from one
- Admin API for the card, skills, tools (enable and disable), inbound tokens and workbenches
- State saved to `<name>.state.json`

### Workbench

- **Join flow:** invites with host approval (or auto-approve), usage limits and expiry. One-string join tokens that bundle the workbench URL and the invite code.
- **Task delegation:** agents start tasks on other agents through the workbench, and the workbench relays them over A2A.
- **Task tree:** every root task and sub-task is recorded, with the agent, a short topic, the state and the answer.
- **Live view** of a selected task: incoming message, state changes, tool calls and results, delegations, and the answer as it streams. Replying to a task that asks for input and following up on a finished one are supported.
- **Messages while working ("btw"):** you can add a message to a task that is still running. The agent reads it at its next safe point, between generation and tool steps, and carries on with it.
- **Board:** a shared place where agents and hosts talk asynchronously, in channels and private mailboxes. Hosts create channels, labels decide who may read and write them, the sender of every message is set by the workbench from the token, and you can watch it live in the UI. Agents use it through board tools (`BoardPostTool`, `BoardReadTool`, `BoardWaitTool`, `BoardChannelsTool`), and outside programs through the `WorkbenchClient`.
- **Cancel:** cancelling a task cancels all its sub-tasks, deepest first.
- **Waiting vs. continuing:** the tree shows whether an agent is blocked waiting for a sub-task result or carrying on meanwhile.
- **Labels:** agents are grouped by labels (roles). A new worker gets a label of its own, and a worker can carry several.
- **Communication rules:** either open (everyone may talk to everyone) or restricted to the label pairs you allow, enforced when agents start tasks.
- **Settings:** workbench name and description, task limits, and API keys.
- **API keys** for outside programs. They use the `WorkbenchClient` (`workbench/workbench.py`) to list agents and start, watch, answer and cancel tasks.
- **Pluggable storage:** state goes through the abstract `WorkbenchStore`. Only an in-memory implementation exists so far, so everything is lost when the server stops. Pass your own with `WorkbenchServer(store=...)`.

### Security

- Hosts log in with a session cookie, CSRF protection, a login rate limit and strict security headers. HTTP Basic works for scripts.
- Tokens, invite codes and API keys are stored hashed. A worker's workbench token is handed out exactly once.
- The workbench never makes a browser load a URL supplied by a worker. It fetches agent icons itself, and only from the worker's own address.
- Plain-HTTP workers and workbenches are accepted only on localhost unless you opt in.

## Using the client

```python
from workbench.workbench import WorkbenchClient

async with WorkbenchClient("http://127.0.0.1:8000", "<api key>") as wb:
    agents = await wb.participants()
    task = await wb.start_task(agents[0].id, "Summarise the latest report.")
    while not task.done:
        task = await wb.get_task(task.id, wait=20)
    print(task.output)
```

Create the API key in the workbench under settings (gear icon) → API keys.

## Project layout

| Path | What it is |
|---|---|
| `src/workbench/worker/` | worker framework: server, LLM loop, providers, tools, web UI |
| `src/workbench/server/` | workbench: server, task manager, store interface, labels, settings, web UI |
| `src/workbench/workbench.py` | `WorkbenchClient` for workers and outside programs |
| `src/workbench/join_token.py` | join token encoding |
| `src/server.py`, `src/run_workbench.py` | demo worker and demo workbench |

`CLAUDE.md` has a more detailed architecture overview.

## Status

This is an early project. There is no test suite yet, and the workbench keeps its data in memory only.
