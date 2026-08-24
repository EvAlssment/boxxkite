"""Opt-in agent tools backed by Boxkite's owned MemoryBase."""

from __future__ import annotations

import json
from typing import Any

from ..memory_client import AsyncMemoryClient, MemoryClientError
from .types import ToolSpec


def _json_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def create_memory_tool_specs(
    *,
    hosted_api_key: str | None,
    hosted_base_url: str,
    default_scope: str = "default",
    client: AsyncMemoryClient | None = None,
) -> list[ToolSpec]:
    """Build the five opt-in memory tools for the Boxkite control-plane."""
    if not hosted_api_key and client is None:
        return []
    memory_client = client or AsyncMemoryClient(base_url=hosted_base_url, api_key=hosted_api_key or "")

    async def remember(
        content: str,
        kind: str = "fact",
        scope: str = default_scope,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        try:
            return _json_result(
                await memory_client.remember(
                    content=content,
                    kind=kind,
                    scope=scope,
                    metadata=metadata or {},
                )
            )
        except MemoryClientError as exc:
            return f"Memory error: {exc}"

    async def ingest_memory(
        content: str,
        scope: str = default_scope,
        source_session_id: str | None = None,
    ) -> str:
        try:
            return _json_result(
                await memory_client.ingest(
                    content=content,
                    scope=scope,
                    source_session_id=source_session_id,
                )
            )
        except MemoryClientError as exc:
            return f"Memory error: {exc}"

    async def recall(query: str, scope: str = default_scope, limit: int = 10) -> str:
        try:
            return _json_result(await memory_client.recall(query=query, scope=scope, limit=limit))
        except MemoryClientError as exc:
            return f"Memory error: {exc}"

    async def memory_profile(scope: str = default_scope, limit: int = 20) -> str:
        try:
            return _json_result(await memory_client.profile(scope=scope, limit=limit))
        except MemoryClientError as exc:
            return f"Memory error: {exc}"

    async def forget_memory(memory_id: str) -> str:
        try:
            await memory_client.forget(memory_id)
            return _json_result({"status": "forgotten", "id": memory_id})
        except MemoryClientError as exc:
            return f"Memory error: {exc}"

    return [
        ToolSpec(
            name="remember",
            description="Store one durable fact, preference, goal, instruction, note, or summary in Boxkite MemoryBase.",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "enum": ["fact", "preference", "goal", "instruction", "note", "summary"]},
                    "scope": {"type": "string", "default": default_scope},
                    "metadata": {"type": "object"},
                },
                "required": ["content"],
            },
            handler=remember,
        ),
        ToolSpec(
            name="ingest_memory",
            description="Ingest a conversation or document into bounded atomic memories with source provenance.",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "scope": {"type": "string", "default": default_scope},
                    "source_session_id": {"type": ["string", "null"]},
                },
                "required": ["content"],
            },
            handler=ingest_memory,
        ),
        ToolSpec(
            name="recall",
            description="Search Boxkite MemoryBase for relevant durable context.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "scope": {"type": "string", "default": default_scope},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "required": ["query"],
            },
            handler=recall,
        ),
        ToolSpec(
            name="memory_profile",
            description="Return stable profile facts and recent dynamic context from Boxkite MemoryBase.",
            parameters={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "default": default_scope},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "required": [],
            },
            handler=memory_profile,
        ),
        ToolSpec(
            name="forget_memory",
            description="Permanently delete one Boxkite memory by id.",
            parameters={
                "type": "object",
                "properties": {"memory_id": {"type": "string", "minLength": 1}},
                "required": ["memory_id"],
            },
            handler=forget_memory,
        ),
    ]
