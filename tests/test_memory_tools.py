from __future__ import annotations

import json

import pytest

from boxxkite.tools.memory_tools import create_memory_tool_specs


class _FakeMemoryClient:
    async def remember(self, **memory):
        return {"operation": "remember", **memory}

    async def ingest(self, **document):
        return {"operation": "ingest", **document}

    async def recall(self, **query):
        return {"operation": "recall", **query}

    async def profile(self, **query):
        return {"operation": "profile", **query}

    async def forget(self, memory_id):
        return None


@pytest.mark.asyncio
async def test_memory_tools_are_opt_in_and_call_owned_client():
    assert create_memory_tool_specs(hosted_api_key=None, hosted_base_url="https://example.test") == []
    specs = create_memory_tool_specs(
        hosted_api_key=None,
        hosted_base_url="https://example.test",
        client=_FakeMemoryClient(),
    )
    assert [spec.name for spec in specs] == [
        "remember",
        "ingest_memory",
        "recall",
        "memory_profile",
        "forget_memory",
    ]
    result = await specs[0].handler(content="Use dark mode", kind="preference")
    assert json.loads(result)["operation"] == "remember"


@pytest.mark.asyncio
async def test_factory_keeps_default_tool_count_and_adds_memory_only_when_enabled():
    from boxxkite.tools.factory import create_sandbox_tool_specs

    class _Sandbox:
        pass

    default = create_sandbox_tool_specs(sandbox_manager=_Sandbox())
    assert len(default) == 17
    memory = create_sandbox_tool_specs(
        sandbox_manager=_Sandbox(),
        hosted_api_key="key",
        hosted_base_url="https://example.test",
        enable_memory_tools=True,
    )
    assert {spec.name for spec in memory} >= {
        "remember",
        "ingest_memory",
        "recall",
        "memory_profile",
        "forget_memory",
    }
