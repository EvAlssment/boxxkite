"""Framework-agnostic tool spec types.

A ToolSpec describes one sandbox tool — name, description, JSON-schema
parameters, and the plain async callable that implements it — without
depending on any agent framework (LangChain, LangGraph, CrewAI, AutoGen,
raw OpenAI-style function calling, or a hand-rolled callable loop).

Adapters in boxkite.tools.adapters convert a list of these into
framework-specific shapes (LangChain BaseTool objects, an OpenAI
function-calling schema, etc.) without changing anything defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class ToolImageResult:
    """Multimodal result: image bytes (base64) plus presentation metadata.

    Kept framework-agnostic — no LangChain content-block or ToolMessage
    type here. Adapters that support multimodal output (e.g. the LangChain
    adapter) convert this into their own representation.
    """

    base64_data: str
    mime_type: str
    file_path: str


@dataclass(frozen=True)
class ToolSpec:
    """Framework-agnostic description of one sandbox tool.

    Attributes:
        name: Tool name, matching the name agents call it by.
        description: Human/model-readable description of what the tool
            does, when to use it, and its arguments — the same text that
            used to live in the LangChain @tool-decorated docstring.
        parameters: JSON-schema `object` describing the tool's parameters
            (the shape an OpenAI-style function-calling `parameters` field
            expects). Adapters may use this directly, or infer an
            equivalent schema from `handler`'s own type hints.
        handler: The plain async callable implementing the tool. Call it
            directly with keyword arguments matching `parameters` — no
            framework, no decorator, no injected framework-specific
            arguments.
        returns_multimodal: True if `handler` may return a `ToolImageResult`
            in addition to `str` (currently only the `view` tool). Adapters
            that don't support multimodal output should treat this as a
            documented possibility, not sugar-coat it away.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    returns_multimodal: bool = False
