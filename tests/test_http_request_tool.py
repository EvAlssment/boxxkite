"""Tests for boxkite.tools.http_request_tool and its wiring into
create_sandbox_tools (docs/SECRETS-DESIGN.md).

Covers:
- The tool is absent by default (opt-in, like enable_git_tools).
- enable_http_request_tool=True adds exactly one new tool, "http_request".
- The tool proxies to SandboxManager.http_request with the right args.
- An unsupported method is rejected before any manager call is made.
"""

from uuid import uuid4

import pytest

from boxkite.tools import create_sandbox_tools
from boxkite.tools.http_request_tool import create_http_request_tool


class _FakeSandboxManager:
    def __init__(self):
        self.calls = []

    async def http_request(self, session_id, method, url, headers=None, body=None, timeout=15):
        self.calls.append(
            {"session_id": session_id, "method": method, "url": url, "headers": headers, "body": body, "timeout": timeout}
        )
        return {"status_code": 200, "headers": {"content-type": "text/plain"}, "body": "ok", "truncated": False}


def test_http_request_tool_absent_by_default():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
    )
    assert "http_request" not in {t.name for t in tools}


def test_http_request_tool_present_when_enabled():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
        enable_http_request_tool=True,
    )
    names = {t.name for t in tools}
    assert "http_request" in names
    assert len(tools) == 16


@pytest.mark.asyncio
async def test_http_request_tool_proxies_to_manager():
    manager = _FakeSandboxManager()
    tool = create_http_request_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke(
        {
            "method": "post",
            "url": "https://api.example.com/v1/charges",
            "headers": {"Authorization": "Bearer {{secret:prod-stripe}}"},
            "body": "amount=2000",
        }
    )

    assert manager.calls == [
        {
            "session_id": "session-1",
            "method": "POST",
            "url": "https://api.example.com/v1/charges",
            "headers": {"Authorization": "Bearer {{secret:prod-stripe}}"},
            "body": "amount=2000",
            "timeout": 15,
        }
    ]
    assert "Status: 200" in result
    assert "ok" in result


@pytest.mark.asyncio
async def test_http_request_tool_rejects_unsupported_method_without_calling_manager():
    manager = _FakeSandboxManager()
    tool = create_http_request_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke({"method": "TRACE", "url": "https://api.example.com/"})

    assert "unsupported method" in result.lower()
    assert manager.calls == []
