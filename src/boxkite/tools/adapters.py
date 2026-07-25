"""Adapters from framework-agnostic ToolSpecs (see ./types.py) to specific
agent-framework shapes.

Every tool's actual logic lives in a plain async function with no
framework import (see bash_tool.py, file_tools.py, present_files.py,
search_tools.py, process_tools.py, git_tools.py, python_interpreter_tool.py).
This module is where framework-specific plumbing lives instead — today
that's:

- `to_langchain_tools`: LangChain `BaseTool` objects, for LangChain/LangGraph
  agents. Requires the `langchain` extra (`langchain-core`); imported
  lazily inside this function so importing `boxkite.tools` — or calling
  `create_sandbox_tool_specs()` — never requires langchain-core to be
  installed.
- `to_openai_functions`: the `{"type": "function", "function": {...}}`
  schema list OpenAI-style function-calling APIs expect. Pure stdlib —
  it only describes the shape, it never calls the `openai` package.
- `to_llamaindex_tools`: LlamaIndex `FunctionTool` objects, for LlamaIndex
  agents (`ReActAgent`, `FunctionAgent`, etc). Requires the `llamaindex`
  extra (`llama-index-core`); imported lazily for the same reason as the
  LangChain adapter above.
- `to_openai_agents_tools`: `agents.tool.FunctionTool` objects (the OpenAI
  Agents SDK's `Agent(tools=[...])` shape), for building an `Agent`/`Runner`
  loop with boxkite's tools. Requires the `openai-agents` extra; imported
  lazily for the same reason as the other adapters. NOTE this is a
  function-tool-level integration, not the deeper "native sandbox provider"
  the OpenAI Agents SDK also supports for E2B/Modal/Daytona/etc (a
  `BaseSandboxSession` implementation covering exec, workspace archive
  read/write, PTY streaming, and mount policies as one object the SDK
  drives directly) — see docs/E2B-COMPARISON.md §5 for why that deeper
  integration is intentionally not attempted yet (it depends on the
  agent-callable PTY and volume-mount gaps tracked there).

CrewAI, AutoGen, and hand-rolled agent loops don't need a bespoke adapter
here: a `ToolSpec`'s `handler` is already a plain callable, and its
`parameters` is already a JSON schema — that's the whole interface those
frameworks want.
"""

from typing import Any

from .types import ToolImageResult, ToolSpec


def to_openai_functions(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """Convert ToolSpecs into the OpenAI-style function-calling schema shape.

    Pure stdlib — no `openai` package dependency. Returns the schema only;
    callers are responsible for dispatching a model's tool call back to
    `spec.handler(**arguments)` themselves.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in specs
    ]


def to_langchain_tools(specs: list[ToolSpec]) -> list:
    """Convert ToolSpecs into LangChain `BaseTool` objects.

    Requires the `langchain` extra: `pip install boxkite-sandbox[langchain]`.
    Imports `langchain_core` lazily so nothing outside this function ever
    requires it to be installed.
    """
    return [_to_langchain_tool(spec) for spec in specs]


def _to_langchain_tool(spec: ToolSpec):
    if spec.returns_multimodal:
        return _to_multimodal_langchain_tool(spec)

    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        coroutine=spec.handler,
        name=spec.name,
        description=spec.description,
    )


def _to_multimodal_langchain_tool(spec: ToolSpec):
    """Wrap a ToolSpec whose handler may return a ToolImageResult.

    LangChain needs an injected `tool_call_id` to build a `ToolMessage`
    carrying multimodal content — that's LangChain-only plumbing with no
    equivalent in the framework-agnostic handler signature, so it's added
    here rather than in the core handler.
    """
    from typing import Annotated, Union

    from langchain_core.messages import ToolMessage
    from langchain_core.messages.content import create_image_block
    from langchain_core.tools import InjectedToolCallId, tool

    handler = spec.handler

    @tool(spec.name, description=spec.description)
    async def multimodal_tool(
        path: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        start_line: int = 1,
        end_line: int = 100,
    ) -> Union[str, ToolMessage]:
        result = await handler(path=path, start_line=start_line, end_line=end_line)

        if isinstance(result, ToolImageResult):
            return ToolMessage(
                content_blocks=[
                    create_image_block(base64=result.base64_data, mime_type=result.mime_type)
                ],
                name=spec.name,
                tool_call_id=tool_call_id,
                additional_kwargs={
                    "read_file_path": result.file_path,
                    "read_file_media_type": result.mime_type,
                },
            )

        return result

    return multimodal_tool


def to_llamaindex_tools(specs: list[ToolSpec]) -> list:
    """Convert ToolSpecs into LlamaIndex `FunctionTool` objects.

    Requires the `llamaindex` extra: `pip install boxkite-sandbox[llamaindex]`.
    Imports `llama_index.core` lazily so nothing outside this function ever
    requires it to be installed.
    """
    return [_to_llamaindex_tool(spec) for spec in specs]


def _to_llamaindex_tool(spec: ToolSpec):
    from llama_index.core.tools import FunctionTool

    fn_schema = _json_schema_to_pydantic_model(spec.name, spec.parameters)
    handler = spec.handler

    async def _async_fn(**kwargs: Any) -> str:
        result = await handler(**kwargs)
        if isinstance(result, ToolImageResult):
            # LlamaIndex's FunctionTool return channel is text-only (no
            # multimodal content-block concept like LangChain's ToolMessage)
            # -- surface the file path/mime type as text rather than silently
            # dropping the image data.
            return (
                f"[image content omitted from LlamaIndex text tool result: "
                f"{result.file_path}, {result.mime_type} -- use the "
                f"LangChain adapter (to_langchain_tools) if you need the "
                f"raw image bytes surfaced to the model]"
            )
        return result

    return FunctionTool.from_defaults(
        async_fn=_async_fn,
        name=spec.name,
        description=spec.description,
        fn_schema=fn_schema,
    )


def _json_schema_to_pydantic_model(name: str, parameters: dict[str, Any]):
    """Build a pydantic model from a ToolSpec's flat JSON-schema `parameters`.

    ToolSpec parameter schemas are always a flat `object` with `string`/
    `integer`/`number`/`boolean` properties (see types.py) -- this covers
    that shape only, not arbitrary JSON schema (nested objects, arrays,
    `$ref`, etc), which no boxkite tool currently needs.
    """
    from pydantic import Field, create_model

    _JSON_TYPE_TO_PYTHON = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }

    required = set(parameters.get("required", []))
    fields: dict[str, Any] = {}
    for prop_name, prop_schema in parameters.get("properties", {}).items():
        python_type = _JSON_TYPE_TO_PYTHON.get(prop_schema.get("type"), str)
        description = prop_schema.get("description", "")
        if prop_name in required:
            fields[prop_name] = (python_type, Field(description=description))
        else:
            default = prop_schema.get("default")
            fields[prop_name] = (python_type, Field(default=default, description=description))

    return create_model(f"{name}_schema", **fields)


def to_openai_agents_tools(specs: list[ToolSpec]) -> list:
    """Convert ToolSpecs into `agents.tool.FunctionTool` objects for the
    OpenAI Agents SDK's `Agent(tools=[...])`.

    Requires the `openai-agents` extra: `pip install boxkite-sandbox[openai-agents]`.
    Imports `agents` lazily so nothing outside this function ever requires
    it to be installed. `strict_json_schema=False` -- ToolSpec parameter
    schemas (see types.py) allow optional fields with defaults and don't
    set `additionalProperties: false`, neither of which OpenAI's *strict*
    structured-output mode permits; passing strict=True here without first
    rewriting every schema to comply would be a silent behavior change this
    module doesn't verify, so it isn't claimed.
    """
    return [_to_openai_agents_tool(spec) for spec in specs]


def _to_openai_agents_tool(spec: ToolSpec):
    import json

    from agents.tool import FunctionTool

    handler = spec.handler

    async def _on_invoke_tool(_ctx: Any, arguments_json: str) -> str:
        arguments = json.loads(arguments_json) if arguments_json else {}
        result = await handler(**arguments)
        if isinstance(result, ToolImageResult):
            # agents.tool.FunctionTool's return channel is text-only here --
            # same limitation and same reasoning as the LlamaIndex adapter's
            # handling of ToolImageResult above.
            return (
                f"[image content omitted from OpenAI Agents SDK text tool result: "
                f"{result.file_path}, {result.mime_type}]"
            )
        return result

    return FunctionTool(
        name=spec.name,
        description=spec.description,
        params_json_schema=spec.parameters,
        on_invoke_tool=_on_invoke_tool,
        strict_json_schema=False,
    )
