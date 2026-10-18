"""Provider-neutral LLM layer.

The agent loop only ever sees the neutral types below; each provider converts
to and from its own wire format.

    stream() yields:  TextDelta, TextDelta, ... , LLMResponse   (final item, always)
    request() returns: LLMResponse

`LLMResponse.tool_calls` is what makes the tool loop provider-independent.
"""
from __future__ import annotations

import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Literal

from .tools import Tool

log = logging.getLogger(__name__)


# ------------------------------------------------------------ neutral types
@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class ChatMessage:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)       # role == "assistant"
    tool_results: list[ToolResult] = field(default_factory=list)   # role == "tool"

    @classmethod
    def user(cls, text: str) -> "ChatMessage":
        return cls("user", text=text)

    @classmethod
    def tool(cls, results: list[ToolResult]) -> "ChatMessage":
        return cls("tool", tool_results=list(results))


@dataclass
class TextDelta:
    text: str


@dataclass
class LLMResponse:
    """One complete assistant turn."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""

    def to_message(self) -> ChatMessage:
        return ChatMessage("assistant", text=self.text, tool_calls=list(self.tool_calls))


StreamEvent = TextDelta | LLMResponse


# ------------------------------------------------------------------- base
class LLMProvider(ABC):
    def __init__(self, name: str, streaming: bool = False):
        self.name = name
        self.streaming = streaming  # informational: does stream() really stream?

    @abstractmethod
    async def request(
        self,
        model: str,
        messages: list[ChatMessage],
        tools: Iterable[Tool] = (),
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> LLMResponse:
        ...

    async def stream(
        self,
        model: str,
        messages: list[ChatMessage],
        tools: Iterable[Tool] = (),
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Default: fall back to `request()` and emit it as one delta + final.

        Override for real token streaming. Contract: yield any number of
        `TextDelta`, then exactly one `LLMResponse` as the last item.
        """
        resp = await self.request(model, messages, tools, system=system,
                                  max_tokens=max_tokens, **kwargs)
        if resp.text:
            yield TextDelta(resp.text)
        yield resp


# -------------------------------------------------------------- Anthropic
def _to_anthropic_messages(messages: list[ChatMessage]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            blocks: list[dict] = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                       for c in m.tool_calls]
            out.append({"role": "assistant",
                        "content": blocks or [{"type": "text", "text": "(no output)"}]})
        else:  # tool results travel in a user message
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r.tool_call_id,
                 "content": r.content, "is_error": r.is_error}
                for r in m.tool_results]})
    return out


def _parse_anthropic(msg: Any) -> LLMResponse:
    return LLMResponse(
        text="".join(b.text for b in msg.content if b.type == "text"),
        tool_calls=[ToolCall(b.id, b.name, dict(b.input))
                    for b in msg.content if b.type == "tool_use"],
        stop_reason=msg.stop_reason or "",
    )


class AnthropicProvider(LLMProvider):
    """Needs `uv add anthropic` (or the `anthropic` extra)."""

    def __init__(self, client: Any = None, **client_kwargs: Any):
        super().__init__("anthropic", streaming=True)
        if client is None:
            try:
                import anthropic
            except ImportError as e:
                raise ImportError("AnthropicProvider needs the `anthropic` package") from e
            client = anthropic.AsyncAnthropic(**client_kwargs)  # reads ANTHROPIC_API_KEY
        self.client = client

    @staticmethod
    def _params(model, messages, tools, system, max_tokens, kwargs) -> dict[str, Any]:
        p: dict[str, Any] = dict(model=model, max_tokens=max_tokens,
                                 messages=_to_anthropic_messages(messages), **kwargs)
        if system:
            p["system"] = system
        schemas = [t.to_anthropic() for t in tools]
        if schemas:                       # the API rejects an empty tools list
            p["tools"] = schemas
        return p

    async def request(self, model, messages, tools=(), *, system=None,
                      max_tokens=1024, **kwargs) -> LLMResponse:
        msg = await self.client.messages.create(
            **self._params(model, messages, tools, system, max_tokens, kwargs))
        return _parse_anthropic(msg)

    async def stream(self, model, messages, tools=(), *, system=None,
                     max_tokens=1024, **kwargs) -> AsyncIterator[StreamEvent]:
        params = self._params(model, messages, tools, system, max_tokens, kwargs)
        async with self.client.messages.stream(**params) as s:
            async for text in s.text_stream:
                yield TextDelta(text)
            final = await s.get_final_message()
        yield _parse_anthropic(final)


# ---------------------------------------------------- OpenAI-compatible
def _to_openai_messages(messages: list[ChatMessage], system: str | None) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.input)}}
                    for c in m.tool_calls]
            out.append(msg)
        else:  # one "tool" message per result
            out += [{"role": "tool", "tool_call_id": r.tool_call_id,
                     "content": f"ERROR: {r.content}" if r.is_error else r.content}
                    for r in m.tool_results]
    return out


def _loads_args(raw: str, tool: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        log.warning("model sent invalid JSON arguments for tool %r: %r", tool, raw)
        return {}  # tool validation will report missing parameters back to the model


class OpenAIProvider(LLMProvider):
    """Chat Completions API: OpenAI and OpenAI-compatible servers (vLLM, Ollama, ...).

    Needs `uv add openai`. Pass `base_url=`/`api_key=` for compatible servers.
    Set `max_tokens_param="max_completion_tokens"` for newer OpenAI models.
    """

    def __init__(self, client: Any = None, *, max_tokens_param: str = "max_tokens",
                 **client_kwargs: Any):
        super().__init__("openai", streaming=True)
        if client is None:
            try:
                import openai
            except ImportError as e:
                raise ImportError("OpenAIProvider needs the `openai` package") from e
            client = openai.AsyncOpenAI(**client_kwargs)  # reads OPENAI_API_KEY
        self.client = client
        self.max_tokens_param = max_tokens_param

    def _params(self, model, messages, tools, system, max_tokens, kwargs) -> dict[str, Any]:
        p: dict[str, Any] = dict(model=model, messages=_to_openai_messages(messages, system),
                                 **{self.max_tokens_param: max_tokens}, **kwargs)
        schemas = [t.to_openai() for t in tools]
        if schemas:
            p["tools"] = schemas
        return p

    async def request(self, model, messages, tools=(), *, system=None,
                      max_tokens=1024, **kwargs) -> LLMResponse:
        r = await self.client.chat.completions.create(
            **self._params(model, messages, tools, system, max_tokens, kwargs))
        ch = r.choices[0]
        calls = [ToolCall(tc.id, tc.function.name, _loads_args(tc.function.arguments, tc.function.name))
                 for tc in (ch.message.tool_calls or [])]
        return LLMResponse(ch.message.content or "", calls, ch.finish_reason or "")

    async def stream(self, model, messages, tools=(), *, system=None,
                     max_tokens=1024, **kwargs) -> AsyncIterator[StreamEvent]:
        params = self._params(model, messages, tools, system, max_tokens, kwargs)
        resp = await self.client.chat.completions.create(stream=True, **params)

        text_parts: list[str] = []
        slots: dict[int, dict[str, str]] = {}   # tool calls arrive in fragments, by index
        finish = ""
        async for chunk in resp:
            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            d = ch.delta
            if d.content:
                text_parts.append(d.content)
                yield TextDelta(d.content)
            for tc in d.tool_calls or []:
                slot = slots.setdefault(tc.index if tc.index is not None else 0,
                                        {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    slot["name"] += tc.function.name or ""
                    slot["args"] += tc.function.arguments or ""
            if ch.finish_reason:
                finish = ch.finish_reason

        calls = [ToolCall(s["id"] or f"call_{uuid.uuid4().hex[:12]}", s["name"],
                          _loads_args(s["args"], s["name"]))
                 for _, s in sorted(slots.items())]
        yield LLMResponse("".join(text_parts), calls, finish)