"""Tests for the budget_status tool (GitHub issue #75).

Unlike every other tool test in this package, there's no SandboxManager
involved at all -- this tool calls the hosted control-plane's GET
/v1/usage directly, so these tests fake httpx.AsyncClient rather than a
sandbox manager.
"""

from __future__ import annotations

import pytest

from boxxkite.tools.budget_status_tool import create_budget_status_tool_spec

pytestmark = pytest.mark.pr

USAGE_BODY = {
    "monthly_sandbox_hours_used": 12.5,
    "monthly_sandbox_hours_limit": 100.0,
    "concurrent_sandboxes": 1,
    "concurrent_sandboxes_limit": 5,
    "total_sandboxes_created": 3,
    "sandbox_ops_rate_limit_remaining": 55,
    "sandbox_ops_rate_limit": 60,
}


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeAsyncClient:
    last_url = None
    last_headers = None
    response = _FakeResponse(200, USAGE_BODY)

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, *, headers=None):
        type(self).last_url = url
        type(self).last_headers = headers
        return type(self).response


def _patch_client(monkeypatch, response: _FakeResponse | None = None):
    if response is not None:
        _FakeAsyncClient.response = response
    _FakeAsyncClient.last_url = None
    _FakeAsyncClient.last_headers = None
    monkeypatch.setattr("boxxkite.tools.budget_status_tool.httpx.AsyncClient", _FakeAsyncClient)


async def test_reports_not_available_without_a_hosted_api_key():
    spec = create_budget_status_tool_spec(hosted_api_key=None)

    result = await spec.handler()

    assert "isn't available" in result
    assert "hosted API key" in result


async def test_calls_hosted_usage_endpoint_with_the_api_key(monkeypatch):
    _patch_client(monkeypatch)
    spec = create_budget_status_tool_spec(hosted_api_key="test-key", hosted_base_url="https://api.example.com")

    result = await spec.handler()

    assert _FakeAsyncClient.last_url == "https://api.example.com/v1/usage"
    assert _FakeAsyncClient.last_headers == {"Authorization": "Bearer test-key"}
    assert "12.50 / 100.00" in result
    assert "1 / 5" in result
    assert "55 / 60" in result


async def test_strips_trailing_slash_from_base_url(monkeypatch):
    _patch_client(monkeypatch)
    spec = create_budget_status_tool_spec(hosted_api_key="test-key", hosted_base_url="https://api.example.com/")

    await spec.handler()

    assert _FakeAsyncClient.last_url == "https://api.example.com/v1/usage"


async def test_reports_http_error_status(monkeypatch):
    _patch_client(monkeypatch, response=_FakeResponse(401, {}))
    spec = create_budget_status_tool_spec(hosted_api_key="bad-key")

    result = await spec.handler()

    assert "401" in result


async def test_reports_transport_error(monkeypatch):
    class _BrokenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, *, headers=None):
            raise ConnectionError("no route to host")

    monkeypatch.setattr("boxxkite.tools.budget_status_tool.httpx.AsyncClient", _BrokenClient)
    spec = create_budget_status_tool_spec(hosted_api_key="test-key")

    result = await spec.handler()

    assert "Error checking budget status" in result
    assert "no route to host" in result
