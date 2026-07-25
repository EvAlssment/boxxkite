"""Hosted, remote MCP server -- docs/HOSTED-MCP-DESIGN.md, closing GitHub
issue #85.

Builds a FRESH `hosted_mcp.build_hosted_mcp_asgi_app()` instance per test
rather than reusing `control_plane.main`'s already-mounted one -- the
underlying MCP SDK's `StreamableHTTPSessionManager.run()` can only be
entered once per instance ("Create a new instance if you need to run
again"), so sharing the process-wide singleton across tests would break on
the second test. A fresh instance still talks to the SAME test database
the `client` fixture already set up (both go through
`db_module.get_session_factory()`, which conftest.py's `client` fixture
points at a fresh per-test SQLite file), so an API key created via the
normal REST `client` is valid against this separately-built MCP app too.

`_mcp_test_client()` is a plain async context manager (not a pytest
fixture) so its enter/exit run in the SAME coroutine as the test body --
`anyio`'s task group backing `session_manager.run()` requires that; a
fixture whose teardown runs after pytest-asyncio hands control back
crosses task boundaries and raises "Attempted to exit cancel scope in a
different task than it was entered in".

The Streamable HTTP transport is session-based: `initialize`'s response
carries an `Mcp-Session-Id` header that every subsequent request in the
same logical connection must echo back, or the server 400s with "Missing
session ID" -- `_McpSession` below wraps that bookkeeping so individual
tests don't have to.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx

from conftest import signup_and_get_api_key
from control_plane import hosted_mcp
from control_plane.hosted_mcp import build_hosted_mcp_asgi_app


def _tool_text(resp: httpx.Response) -> str:
    """Extracts a tool call's plain-text result from the Streamable HTTP
    transport's `event: message\\ndata: {...}` SSE body. FastMCP mirrors a
    str-returning tool's result into BOTH `result.content[0].text` and
    `result.structuredContent.result` -- reading the raw response body
    (as some tests below still do for a single `str.split` extraction)
    sees that text twice, which breaks any assertion that counts
    occurrences rather than just checking membership."""
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: "):])
            return payload["result"]["content"][0]["text"]
    raise AssertionError(f"no SSE data line found in response: {resp.text!r}")


def _mcp_headers(api_key: str | None, session_id: str | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return headers


class _McpSession:
    """Thin wrapper handling the Streamable HTTP transport's session-ID
    handshake so tests can just call `initialize()`/`call_tool()`."""

    def __init__(self, http_client: httpx.AsyncClient, api_key: str | None):
        self._http = http_client
        self._api_key = api_key
        self._session_id: str | None = None
        self._next_id = 1

    async def initialize(self) -> httpx.Response:
        resp = await self._http.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "0.1"},
                },
            },
            headers=_mcp_headers(self._api_key),
        )
        self._next_id += 1
        self._session_id = resp.headers.get("mcp-session-id")
        return resp

    async def call_tool(self, name: str, arguments: dict) -> httpx.Response:
        resp = await self._http.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers=_mcp_headers(self._api_key, self._session_id),
        )
        self._next_id += 1
        return resp


@asynccontextmanager
async def _mcp_test_client():
    mcp, asgi_app = build_hosted_mcp_asgi_app()
    async with mcp.session_manager.run():
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


async def test_missing_bearer_token_gets_401():
    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key=None)
        resp = await session.initialize()

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"


async def test_malformed_authorization_header_gets_401():
    async with _mcp_test_client() as http_client:
        resp = await http_client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={**_mcp_headers(None), "Authorization": "NotBearer something"},
        )

    assert resp.status_code == 401


async def test_invalid_api_key_gets_401(client: httpx.AsyncClient):
    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key="bxk_live_totally_bogus")
        resp = await session.initialize()

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


async def test_valid_api_key_completes_initialize_handshake(client: httpx.AsyncClient):
    api_key = await signup_and_get_api_key(client, "mcp-init@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        resp = await session.initialize()

    assert resp.status_code == 200
    assert "boxkite" in resp.text


async def test_create_and_list_sandbox_round_trips(
    client: httpx.AsyncClient, fake_manager, monkeypatch
):
    monkeypatch.setattr(hosted_mcp, "get_manager", lambda: fake_manager)
    api_key = await signup_and_get_api_key(client, "mcp-lifecycle@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()

        create_resp = await session.call_tool("create_sandbox", {"label": "mcp-test"})
        assert create_resp.status_code == 200
        assert "Created sandbox" in create_resp.text

        list_resp = await session.call_tool("list_sandboxes", {})
        assert list_resp.status_code == 200
        assert "mcp-test" in list_resp.text


async def test_cross_account_session_id_not_found(
    client: httpx.AsyncClient, fake_manager, monkeypatch
):
    monkeypatch.setattr(hosted_mcp, "get_manager", lambda: fake_manager)
    owner_key = await signup_and_get_api_key(client, "mcp-owner@example.com")
    other_key = await signup_and_get_api_key(client, "mcp-other@example.com")

    async with _mcp_test_client() as http_client:
        owner_session = _McpSession(http_client, owner_key)
        await owner_session.initialize()
        create_resp = await owner_session.call_tool("create_sandbox", {})
        assert "Created sandbox" in create_resp.text
        session_id = create_resp.text.split("Created sandbox ")[1].split(" ")[0]

        other_session = _McpSession(http_client, other_key)
        await other_session.initialize()
        get_resp = await other_session.call_tool("get_sandbox", {"session_id": session_id})
        assert "not found" in get_resp.text


async def test_exec_tool_runs_command(client: httpx.AsyncClient, fake_manager, monkeypatch):
    monkeypatch.setattr(hosted_mcp, "get_manager", lambda: fake_manager)
    api_key = await signup_and_get_api_key(client, "mcp-exec@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()
        create_resp = await session.call_tool("create_sandbox", {})
        session_id = create_resp.text.split("Created sandbox ")[1].split(" ")[0]

        exec_resp = await session.call_tool(
            "exec", {"session_id": session_id, "command": "echo hi"}
        )
        assert exec_resp.status_code == 200


async def test_command_whitelist_blocks_disallowed_command(
    client: httpx.AsyncClient, fake_manager, monkeypatch
):
    monkeypatch.setattr(hosted_mcp, "get_manager", lambda: fake_manager)
    api_key = await signup_and_get_api_key(client, "mcp-whitelist@example.com")

    from control_plane import db as db_module
    from control_plane.models_orm import Account
    from sqlalchemy import select

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.email == "mcp-whitelist@example.com"))
        account = result.scalar_one()
        account.custom_allowed_commands = ["ls", "cat"]
        await db.commit()

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()
        create_resp = await session.call_tool("create_sandbox", {})
        session_id = create_resp.text.split("Created sandbox ")[1].split(" ")[0]

        exec_resp = await session.call_tool(
            "exec", {"session_id": session_id, "command": "rm -rf /"}
        )
        assert "not allowed" in exec_resp.text


async def test_create_sandbox_count_creates_multiple_and_returns_one_line_each(
    client: httpx.AsyncClient, fake_manager, monkeypatch
):
    monkeypatch.setattr(hosted_mcp, "get_manager", lambda: fake_manager)
    # BOXKITE_MAX_CONCURRENT_SANDBOXES defaults to 2 -- request exactly that
    # many so this test exercises the "multiple, all succeed" path without
    # tripping the cap (see the dedicated cap test below for that path).
    api_key = await signup_and_get_api_key(client, "mcp-count@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()

        create_resp = await session.call_tool("create_sandbox", {"count": 2})
        assert create_resp.status_code == 200
        assert _tool_text(create_resp).count("Created sandbox") == 2

        list_resp = await session.call_tool("list_sandboxes", {})
        assert _tool_text(list_resp).count("- ") == 2


async def test_create_sandbox_count_partial_success_when_capacity_limit_hit(
    client: httpx.AsyncClient, fake_manager, monkeypatch
):
    monkeypatch.setattr(hosted_mcp, "get_manager", lambda: fake_manager)
    # BOXKITE_MAX_CONCURRENT_SANDBOXES defaults to 2 -- requesting 3 should
    # create the first 2 then report the capacity error for the 3rd,
    # mirroring the REST route's "a later item can still fail" behavior.
    api_key = await signup_and_get_api_key(client, "mcp-count-cap@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()

        create_resp = await session.call_tool("create_sandbox", {"count": 3})
        result_text = _tool_text(create_resp)
        assert result_text.count("Created sandbox") == 2
        assert "Error creating sandbox" in result_text

        list_resp = await session.call_tool("list_sandboxes", {})
        assert _tool_text(list_resp).count("- ") == 2


async def test_create_sandbox_count_out_of_range_returns_error_without_creating(
    client: httpx.AsyncClient, fake_manager, monkeypatch
):
    monkeypatch.setattr(hosted_mcp, "get_manager", lambda: fake_manager)
    api_key = await signup_and_get_api_key(client, "mcp-count-oob@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()

        create_resp = await session.call_tool("create_sandbox", {"count": 11})
        assert "count must be between 1 and 10" in create_resp.text

        list_resp = await session.call_tool("list_sandboxes", {})
        assert "No sandboxes found." in list_resp.text


async def test_sandbox_image_tools_return_disabled_message_by_default(
    client: httpx.AsyncClient, monkeypatch
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_IMAGE_BUILDER_ENABLED", False)
    api_key = await signup_and_get_api_key(client, "mcp-image-disabled@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()

        resp = await session.call_tool("create_sandbox_image", {})
        assert "not enabled" in resp.text

        resp = await session.call_tool("list_sandbox_images", {})
        assert "not enabled" in resp.text


async def test_sandbox_image_lifecycle_round_trips(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_IMAGE_BUILDER_ENABLED", True)
    api_key = await signup_and_get_api_key(client, "mcp-image-lifecycle@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()

        create_resp = await session.call_tool(
            "create_sandbox_image", {"label": "test-image", "python_packages": ["polars==1.9.0"]}
        )
        assert "Started building image" in create_resp.text
        image_id = create_resp.text.split("Started building image ")[1].split(" ")[0]

        list_resp = await session.call_tool("list_sandbox_images", {})
        assert image_id in list_resp.text

        get_resp = await session.call_tool("get_sandbox_image", {"image_id": image_id})
        assert image_id in get_resp.text

        delete_resp = await session.call_tool("delete_sandbox_image", {"image_id": image_id})
        assert "Deleted sandbox image" in delete_resp.text

        get_after_delete_resp = await session.call_tool("get_sandbox_image", {"image_id": image_id})
        assert "not found" in get_after_delete_resp.text


async def test_sandbox_image_cross_account_id_not_found(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_IMAGE_BUILDER_ENABLED", True)
    owner_key = await signup_and_get_api_key(client, "mcp-image-owner@example.com")
    other_key = await signup_and_get_api_key(client, "mcp-image-other@example.com")

    async with _mcp_test_client() as http_client:
        owner_session = _McpSession(http_client, owner_key)
        await owner_session.initialize()
        create_resp = await owner_session.call_tool(
            "create_sandbox_image", {"python_packages": ["polars==1.9.0"]}
        )
        image_id = create_resp.text.split("Started building image ")[1].split(" ")[0]

        other_session = _McpSession(http_client, other_key)
        await other_session.initialize()
        get_resp = await other_session.call_tool("get_sandbox_image", {"image_id": image_id})
        assert "not found" in get_resp.text


async def test_sandbox_volume_tools_return_disabled_message_by_default(
    client: httpx.AsyncClient, monkeypatch
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_VOLUMES_ENABLED", False)
    api_key = await signup_and_get_api_key(client, "mcp-volume-disabled@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()

        resp = await session.call_tool("create_sandbox_volume", {})
        assert "not enabled" in resp.text

        resp = await session.call_tool("list_sandbox_volumes", {})
        assert "not enabled" in resp.text


async def test_sandbox_volume_lifecycle_round_trips(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_VOLUMES_ENABLED", True)
    api_key = await signup_and_get_api_key(client, "mcp-volume-lifecycle@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()

        create_resp = await session.call_tool(
            "create_sandbox_volume", {"label": "test-volume", "size_gb": 5}
        )
        assert "Started creating volume" in create_resp.text
        volume_id = create_resp.text.split("Started creating volume ")[1].split(" ")[0]

        list_resp = await session.call_tool("list_sandbox_volumes", {})
        assert volume_id in list_resp.text

        get_resp = await session.call_tool("get_sandbox_volume", {"volume_id": volume_id})
        assert volume_id in get_resp.text

        delete_resp = await session.call_tool("delete_sandbox_volume", {"volume_id": volume_id})
        assert "Deleted sandbox volume" in delete_resp.text

        get_after_delete_resp = await session.call_tool("get_sandbox_volume", {"volume_id": volume_id})
        assert "not found" in get_after_delete_resp.text


async def test_sandbox_volume_cross_account_id_not_found(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_VOLUMES_ENABLED", True)
    owner_key = await signup_and_get_api_key(client, "mcp-volume-owner@example.com")
    other_key = await signup_and_get_api_key(client, "mcp-volume-other@example.com")

    async with _mcp_test_client() as http_client:
        owner_session = _McpSession(http_client, owner_key)
        await owner_session.initialize()
        create_resp = await owner_session.call_tool("create_sandbox_volume", {"size_gb": 2})
        volume_id = create_resp.text.split("Started creating volume ")[1].split(" ")[0]

        other_session = _McpSession(http_client, other_key)
        await other_session.initialize()
        get_resp = await other_session.call_tool("get_sandbox_volume", {"volume_id": volume_id})
        assert "not found" in get_resp.text


async def test_create_sandbox_with_image_id_not_found_returns_error(
    client: httpx.AsyncClient, fake_manager, monkeypatch
):
    monkeypatch.setattr(hosted_mcp, "get_manager", lambda: fake_manager)
    api_key = await signup_and_get_api_key(client, "mcp-sandbox-bad-image@example.com")

    async with _mcp_test_client() as http_client:
        session = _McpSession(http_client, api_key)
        await session.initialize()

        resp = await session.call_tool("create_sandbox", {"image_id": "does-not-exist"})
        assert "Error" in resp.text and "not found" in resp.text.lower()
