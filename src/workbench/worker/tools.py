"""Tool abstraction for worker agents.

A `Tool` has a name, a description and a list of `Parameter`s. It can render
itself as a JSON schema (Anthropic or OpenAI function-calling format), so you
can hand it straight to an LLM, and it validates arguments before executing.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import shlex
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Sequence

_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


class ToolError(Exception):
    """Raised for invalid arguments or a failing tool call."""


@dataclass(frozen=True)
class Parameter:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    default: Any = None
    enum: Sequence[Any] | None = None

    def __post_init__(self) -> None:
        if self.type not in _PY_TYPES:
            raise ValueError(f"unknown parameter type {self.type!r}")

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum is not None:
            schema["enum"] = list(self.enum)
        if self.default is not None:
            schema["default"] = self.default
        return schema

    def check(self, value: Any) -> None:
        ok = isinstance(value, _PY_TYPES[self.type])
        if self.type in ("integer", "number") and isinstance(value, bool):
            ok = False
        if not ok:
            raise ToolError(
                f"parameter {self.name!r} must be {self.type}, got {type(value).__name__}"
            )
        if self.enum is not None and value not in self.enum:
            raise ToolError(f"parameter {self.name!r} must be one of {list(self.enum)}")


class Tool(ABC):
    def __init__(self, name: str, description: str, parameters: Sequence[Parameter] = ()):
        self.name = name
        self.description = description
        self.parameters = list(parameters)
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate parameter names in tool {name!r}")

    @abstractmethod
    def execute(self, *args, **kwargs: Any) -> Any:
        """Do the work. Maybe a plain method or a coroutine function."""

    async def invoke(self, **kwargs: Any) -> Any:
        """Validate `kwargs`, apply defaults, run `execute` (sync or async)."""
        known = {p.name: p for p in self.parameters}
        unknown = set(kwargs) - set(known)
        if unknown:
            raise ToolError(f"unknown parameters for {self.name!r}: {sorted(unknown)}")
        for p in self.parameters:
            if p.name in kwargs:
                p.check(kwargs[p.name])
            elif p.required and p.default is None:
                raise ToolError(f"missing required parameter {p.name!r}")
            elif p.default is not None:
                kwargs[p.name] = p.default
        result = self.execute(**kwargs)
        return await result if inspect.isawaitable(result) else result

    # ---- schema export -------------------------------------------------
    def json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {p.name: p.json_schema() for p in self.parameters},
            "required": [p.name for p in self.parameters if p.required],
        }

    def to_anthropic(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.json_schema()}

    def to_openai(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.json_schema()}}

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": self.json_schema()}
