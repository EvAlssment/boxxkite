"""Tests for the boxkite MCP server's tool registrations. httpx.MockTransport
stands in for the real control-plane, same pattern as sdk-python/tests/
test_client.py and test_langchain_tools.py -- no network, no real deployment
needed.

Each tool is invoked through FastMCP's own ``call_tool`` dispatch (not by
calling the wrapped function directly) so these tests exercise the exact
path a real MCP client drives: JSON args in, a CallToolResult's text content
out.
"""

from __future__ import annotations

import json

import httpx
import pytest

from boxkite_client import BoxkiteClient
from boxkite_mcp.server import ConfigurationError, _load_config, build_server


def _client_with(handler) -> BoxkiteClient:
    return BoxkiteClient(
        base_url="https://cp.example.com",
        api_key="bxk_live_test",
        transport=httpx.MockTransport(handler),
    )


def _text(result) -> str:
    content, _structured = result
    assert len(content) == 1
    return content[0].text


async def _call(server, name, arguments):
    return await server.call_tool(name, arguments)


# --- configuration ----------------------------------------------------


def test_load_config_raises_when_base_url_missing(monkeypatch):
    monkeypatch.delenv("BOXKITE_BASE_URL", raising=False)
    monkeypatch.setenv("BOXKITE_API_KEY", "bxk_live_test")
    with pytest.raises(ConfigurationError, match="BOXKITE_BASE_URL"):
        _load_config()


def test_load_config_raises_when_api_key_missing(monkeypatch):
    monkeypatch.setenv("BOXKITE_BASE_URL", "https://cp.example.com")
    monkeypatch.delenv("BOXKITE_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="BOXKITE_API_KEY"):
        _load_config()


def test_load_config_raises_when_both_missing(monkeypatch):
    monkeypatch.delenv("BOXKITE_BASE_URL", raising=False)
    monkeypatch.delenv("BOXKITE_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="BOXKITE_BASE_URL.*BOXKITE_API_KEY"):
        _load_config()


def test_load_config_returns_values_when_present(monkeypatch):
    monkeypatch.setenv("BOXKITE_BASE_URL", "https://cp.example.com")
    monkeypatch.setenv("BOXKITE_API_KEY", "bxk_live_test")
    assert _load_config() == ("https://cp.example.com", "bxk_live_test")


def test_load_config_rejects_plain_http_to_a_remote_host(monkeypatch):
    """BOXKITE_API_KEY is a full-privilege, long-lived credential -- an
    http:// BOXKITE_BASE_URL to anything other than localhost would put it
    on the wire in cleartext."""
    monkeypatch.setenv("BOXKITE_BASE_URL", "http://cp.example.com")
    monkeypatch.setenv("BOXKITE_API_KEY", "bxk_live_test")
    with pytest.raises(ConfigurationError, match="cleartext"):
        _load_config()


def test_load_config_allows_http_localhost_for_local_dev(monkeypatch):
    monkeypatch.setenv("BOXKITE_BASE_URL", "http://localhost:8090")
    monkeypatch.setenv("BOXKITE_API_KEY", "bxk_live_test")
    assert _load_config() == ("http://localhost:8090", "bxk_live_test")


# --- tool registration --------------------------------------------------


async def test_all_twenty_six_tools_are_registered():
    client = _client_with(lambda request: httpx.Response(200, json={}))
    server = build_server(client)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "create_sandbox",
        "destroy_sandbox",
        "get_sandbox",
        "list_sandboxes",
        "create_sandbox_image",
        "get_sandbox_image",
        "list_sandbox_images",
        "delete_sandbox_image",
        "create_sandbox_volume",
        "get_sandbox_volume",
        "list_sandbox_volumes",
        "delete_sandbox_volume",
        "create_mcp_connection",
        "list_mcp_connections",
        "delete_mcp_connection",
        "exec",
        "lsp_start",
        "lsp_open",
        "lsp_completion",
        "lsp_stop",
        "file_create",
        "view",
        "str_replace",
        "ls",
        "glob",
        "grep",
    }


# --- create_sandbox ------------------------------------------------------


async def test_create_sandbox_posts_label_and_returns_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/sandboxes"
        assert json.loads(request.content) == {"label": "demo"}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "create_sandbox", {"label": "demo"}))
    assert "sess-1" in result
    assert "active" in result


async def test_create_sandbox_without_label_sends_empty_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {}
        return httpx.Response(201, json={"id": "sess-2", "status": "active"})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "create_sandbox", {}))
    assert "sess-2" in result


async def test_create_sandbox_surfaces_api_error_as_text_not_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"code": "concurrent_sandbox_limit_reached", "message": "Too many sandboxes"}},
        )

    server = build_server(_client_with(handler))
    call_result = await _call(server, "create_sandbox", {})
    content, _structured = call_result
    assert "Too many sandboxes" in content[0].text
    assert "concurrent_sandbox_limit_reached" in content[0].text


async def test_create_sandbox_surfaces_connection_error_as_text():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "create_sandbox", {}))
    assert "could not reach" in result.lower()


async def test_create_sandbox_passes_image_id_when_provided():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"image_id": "img-1"}
        return httpx.Response(201, json={"id": "sess-3", "status": "active"})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "create_sandbox", {"image_id": "img-1"}))
    assert "sess-3" in result


async def test_create_sandbox_passes_volume_mounts_when_provided():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"volume_mounts": {"vol-1": "/mnt/data"}}
        return httpx.Response(201, json={"id": "sess-4", "status": "active"})

    server = build_server(_client_with(handler))
    result = _text(
        await _call(server, "create_sandbox", {"volume_mounts": {"vol-1": "/mnt/data"}})
    )
    assert "sess-4" in result


async def test_create_sandbox_passes_gpu_count_when_provided():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"gpu_count": 2}
        return httpx.Response(201, json={"id": "sess-6", "status": "active"})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "create_sandbox", {"gpu_count": 2}))
    assert "sess-6" in result


async def test_create_sandbox_passes_mcp_connection_names_when_provided():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"mcp_connection_names": ["team-slack"]}
        return httpx.Response(201, json={"id": "sess-5", "status": "active"})

    server = build_server(_client_with(handler))
    result = _text(
        await _call(server, "create_sandbox", {"mcp_connection_names": ["team-slack"]})
    )
    assert "sess-5" in result


# --- destroy_sandbox -------------------------------------------------------


async def test_destroy_sandbox_deletes_by_session_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/sandboxes/sess-1"
        return httpx.Response(204)

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "destroy_sandbox", {"session_id": "sess-1"}))
    assert "Destroyed sandbox sess-1" == result


async def test_destroy_sandbox_surfaces_not_found_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Sandbox session not found"}})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "destroy_sandbox", {"session_id": "missing"}))
    assert "not found" in result.lower()


# --- get_sandbox -------------------------------------------------------------


async def test_get_sandbox_returns_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/sandboxes/sess-1"
        return httpx.Response(200, json={"id": "sess-1", "status": "active", "label": "demo"})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "get_sandbox", {"session_id": "sess-1"}))
    assert "sess-1" in result
    assert "demo" in result


async def test_get_sandbox_surfaces_not_found_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Sandbox session not found"}})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "get_sandbox", {"session_id": "missing"}))
    assert "not found" in result.lower()


# --- list_sandboxes ---------------------------------------------------------


async def test_list_sandboxes_passes_active_only_query_param():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes"
        assert request.url.params["active_only"] == "true"
        return httpx.Response(200, json=[{"id": "sess-1", "status": "active", "label": "demo"}])

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "list_sandboxes", {"active_only": True}))
    assert "sess-1" in result
    assert "demo" in result


async def test_list_sandboxes_reports_empty_list():
    server = build_server(_client_with(lambda r: httpx.Response(200, json=[])))
    result = _text(await _call(server, "list_sandboxes", {}))
    assert "No sandboxes" in result


# --- create_sandbox_image -----------------------------------------------


async def test_create_sandbox_image_posts_fields_and_returns_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/images"
        assert json.loads(request.content) == {
            "label": "demo",
            "base": "boxkite-default",
            "python_packages": ["requests==2.31.0"],
            "apt_packages": ["curl==7.81.0-1ubuntu1.15"],
        }
        return httpx.Response(202, json={"id": "img-1", "status": "pending"})

    server = build_server(_client_with(handler))
    result = _text(
        await _call(
            server,
            "create_sandbox_image",
            {
                "label": "demo",
                "python_packages": ["requests==2.31.0"],
                "apt_packages": ["curl==7.81.0-1ubuntu1.15"],
            },
        )
    )
    assert "img-1" in result
    assert "pending" in result
    assert "get_sandbox_image" in result


async def test_create_sandbox_image_posts_npm_packages():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "base": "boxkite-node",
            "npm_packages": ["typescript==5.6.0"],
        }
        return httpx.Response(202, json={"id": "img-2", "status": "pending"})

    server = build_server(_client_with(handler))
    result = _text(
        await _call(
            server,
            "create_sandbox_image",
            {"base": "boxkite-node", "npm_packages": ["typescript==5.6.0"]},
        )
    )
    assert "img-2" in result


async def test_create_sandbox_image_surfaces_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"error": {"code": "invalid_package_pin", "message": "python_packages must be exact-version pinned"}},
        )

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "create_sandbox_image", {"python_packages": ["requests"]}))
    assert "exact-version pinned" in result


# --- get_sandbox_image ----------------------------------------------------


async def test_get_sandbox_image_returns_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/images/img-1"
        return httpx.Response(
            200,
            json={"id": "img-1", "status": "completed", "label": "demo", "digest": "sha256:abc"},
        )

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "get_sandbox_image", {"image_id": "img-1"}))
    assert "img-1" in result
    assert "completed" in result
    assert "sha256:abc" in result


async def test_get_sandbox_image_includes_failure_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "img-1", "status": "failed", "label": None, "failure_reason": "apt package not found"},
        )

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "get_sandbox_image", {"image_id": "img-1"}))
    assert "apt package not found" in result


async def test_get_sandbox_image_surfaces_not_found_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Image not found"}})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "get_sandbox_image", {"image_id": "missing"}))
    assert "not found" in result.lower()


# --- list_sandbox_images --------------------------------------------------


async def test_list_sandbox_images_returns_bulleted_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images"
        return httpx.Response(200, json=[{"id": "img-1", "status": "completed", "label": "demo"}])

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "list_sandbox_images", {}))
    assert "img-1" in result
    assert "demo" in result


async def test_list_sandbox_images_reports_empty_list():
    server = build_server(_client_with(lambda r: httpx.Response(200, json=[])))
    result = _text(await _call(server, "list_sandbox_images", {}))
    assert "No images found" in result


# --- delete_sandbox_image -------------------------------------------------


async def test_delete_sandbox_image_deletes_by_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/images/img-1"
        return httpx.Response(204)

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "delete_sandbox_image", {"image_id": "img-1"}))
    assert "Deleted sandbox image img-1" == result


async def test_delete_sandbox_image_surfaces_not_found_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Image not found"}})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "delete_sandbox_image", {"image_id": "missing"}))
    assert "not found" in result.lower()


# --- create_sandbox_volume ------------------------------------------------


async def test_create_sandbox_volume_posts_fields_and_returns_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/volumes"
        assert json.loads(request.content) == {"size_gb": 10.0, "label": "demo"}
        return httpx.Response(202, json={"id": "vol-1", "status": "queued"})

    server = build_server(_client_with(handler))
    result = _text(
        await _call(server, "create_sandbox_volume", {"label": "demo", "size_gb": 10.0})
    )
    assert "vol-1" in result
    assert "queued" in result
    assert "get_sandbox_volume" in result


async def test_create_sandbox_volume_surfaces_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"code": "volume_limit_reached", "message": "Volume limit reached"}},
        )

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "create_sandbox_volume", {"size_gb": 5.0}))
    assert "Volume limit reached" in result


# --- get_sandbox_volume -----------------------------------------------------


async def test_get_sandbox_volume_returns_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/volumes/vol-1"
        return httpx.Response(200, json={"id": "vol-1", "status": "ready", "label": "demo"})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "get_sandbox_volume", {"volume_id": "vol-1"}))
    assert "vol-1" in result
    assert "ready" in result
    assert "demo" in result


async def test_get_sandbox_volume_includes_failure_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "vol-1", "status": "failed", "label": None, "failure_reason": "quota exceeded"},
        )

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "get_sandbox_volume", {"volume_id": "vol-1"}))
    assert "quota exceeded" in result


async def test_get_sandbox_volume_surfaces_not_found_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Volume not found"}})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "get_sandbox_volume", {"volume_id": "missing"}))
    assert "not found" in result.lower()


# --- list_sandbox_volumes ----------------------------------------------------


async def test_list_sandbox_volumes_returns_bulleted_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/volumes"
        return httpx.Response(200, json=[{"id": "vol-1", "status": "ready", "label": "demo"}])

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "list_sandbox_volumes", {}))
    assert "vol-1" in result
    assert "demo" in result


async def test_list_sandbox_volumes_reports_empty_list():
    server = build_server(_client_with(lambda r: httpx.Response(200, json=[])))
    result = _text(await _call(server, "list_sandbox_volumes", {}))
    assert "No volumes found" in result


# --- delete_sandbox_volume ---------------------------------------------------


async def test_delete_sandbox_volume_deletes_by_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/volumes/vol-1"
        return httpx.Response(204)

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "delete_sandbox_volume", {"volume_id": "vol-1"}))
    assert "Deleted sandbox volume vol-1" == result


async def test_delete_sandbox_volume_surfaces_not_found_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Volume not found"}})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "delete_sandbox_volume", {"volume_id": "missing"}))
    assert "not found" in result.lower()


# --- create_mcp_connection / list_mcp_connections / delete_mcp_connection ---


async def test_create_mcp_connection_posts_label_and_catalog_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/mcp-connections"
        assert json.loads(request.content) == {"label": "team-slack", "catalog_id": "slack"}
        return httpx.Response(
            201,
            json={
                "id": "mcpconn-1",
                "label": "team-slack",
                "catalog_id": "slack",
                "host": "mcp.slack.com",
            },
        )

    server = build_server(_client_with(handler))
    result = _text(
        await _call(server, "create_mcp_connection", {"label": "team-slack", "catalog_id": "slack"})
    )
    assert "mcpconn-1" in result
    assert "mcp.slack.com" in result


async def test_create_mcp_connection_surfaces_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"error": {"code": "mcp_connection_label_taken", "message": "Label already exists"}}
        )

    server = build_server(_client_with(handler))
    result = _text(
        await _call(server, "create_mcp_connection", {"label": "team-slack", "catalog_id": "slack"})
    )
    assert "Label already exists" in result
    assert "mcp_connection_label_taken" in result


async def test_list_mcp_connections_returns_bulleted_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/mcp-connections"
        return httpx.Response(
            200,
            json=[{"id": "mcpconn-1", "label": "team-slack", "catalog_id": "slack", "host": "mcp.slack.com"}],
        )

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "list_mcp_connections", {}))
    assert "mcpconn-1" in result
    assert "team-slack" in result
    assert "mcp.slack.com" in result


async def test_list_mcp_connections_reports_empty_list():
    server = build_server(_client_with(lambda r: httpx.Response(200, json=[])))
    result = _text(await _call(server, "list_mcp_connections", {}))
    assert "No MCP connections" in result


async def test_delete_mcp_connection_deletes_by_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/mcp-connections/mcpconn-1"
        return httpx.Response(204)

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "delete_mcp_connection", {"connection_id": "mcpconn-1"}))
    assert "Deleted MCP connection mcpconn-1" == result


async def test_delete_mcp_connection_surfaces_not_found_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "MCP connection not found"}})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "delete_mcp_connection", {"connection_id": "missing"}))
    assert "not found" in result.lower()


# --- exec --------------------------------------------------------------


async def test_exec_posts_command_and_returns_stdout():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/exec"
        assert json.loads(request.content) == {"command": "echo hi"}
        return httpx.Response(200, json={"exit_code": 0, "stdout": "hi\n", "stderr": ""})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "exec", {"session_id": "sess-1", "command": "echo hi"}))
    assert result == "hi\n"


async def test_exec_passes_timeout_when_provided():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"command": "sleep 1", "timeout": 5}
        return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})

    server = build_server(_client_with(handler))
    await _call(server, "exec", {"session_id": "sess-1", "command": "sleep 1", "timeout": 5})


async def test_exec_reports_nonzero_exit_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"exit_code": 1, "stdout": "", "stderr": "boom"})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "exec", {"session_id": "sess-1", "command": "false"}))
    assert "exited 1" in result
    assert "boom" in result


# --- lsp_start / lsp_open / lsp_completion / lsp_stop --------------------


async def test_lsp_start_posts_language_and_returns_lsp_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/lsp/start"
        assert json.loads(request.content) == {"language": "python"}
        return httpx.Response(200, json={"lsp_id": "lsp-1"})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "lsp_start", {"session_id": "sess-1", "language": "python"}))
    assert "lsp-1" in result
    assert "python" in result


async def test_lsp_start_surfaces_feature_disabled_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"error": {"code": "not_found", "message": "Language server support is not enabled"}}
        )

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "lsp_start", {"session_id": "sess-1", "language": "python"}))
    assert "not enabled" in result.lower()


async def test_lsp_open_posts_path_and_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/open"
        assert json.loads(request.content) == {"path": "main.py", "content": "x = 1"}
        return httpx.Response(200, json={"status": "ok"})

    server = build_server(_client_with(handler))
    result = _text(
        await _call(
            server, "lsp_open", {"session_id": "sess-1", "lsp_id": "lsp-1", "path": "main.py", "content": "x = 1"}
        )
    )
    assert "main.py" in result
    assert "lsp-1" in result


async def test_lsp_completion_posts_position_and_formats_items():
    # Real pyright/tsserver responses send the numeric LSP CompletionItemKind
    # (2 == "method"), not a human-readable string -- this must be
    # normalized the same way src/boxkite/tools/lsp_tools.py's sibling tool
    # does, not printed verbatim as "(2)".
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/completion"
        assert json.loads(request.content) == {"path": "main.py", "line": 3, "character": 5}
        return httpx.Response(200, json={"items": [{"label": "print", "kind": 3}]})

    server = build_server(_client_with(handler))
    result = _text(
        await _call(
            server,
            "lsp_completion",
            {"session_id": "sess-1", "lsp_id": "lsp-1", "path": "main.py", "line": 3, "character": 5},
        )
    )
    assert "print" in result
    assert "(function)" in result
    assert "(3)" not in result


async def test_lsp_completion_formats_unknown_and_missing_kind():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {"label": "mystery", "kind": 999},
                    {"label": "no_kind"},
                ]
            },
        )

    server = build_server(_client_with(handler))
    result = _text(
        await _call(
            server,
            "lsp_completion",
            {"session_id": "sess-1", "lsp_id": "lsp-1", "path": "main.py", "line": 0, "character": 0},
        )
    )
    assert "mystery (unknown)" in result
    assert "no_kind" in result
    assert "no_kind (" not in result


async def test_lsp_completion_reports_no_completions_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    server = build_server(_client_with(handler))
    result = _text(
        await _call(
            server,
            "lsp_completion",
            {"session_id": "sess-1", "lsp_id": "lsp-1", "path": "main.py", "line": 0, "character": 0},
        )
    )
    assert "no completions" in result.lower()


async def test_lsp_stop_posts_to_lsp_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/stop"
        return httpx.Response(200, json={"status": "ok"})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "lsp_stop", {"session_id": "sess-1", "lsp_id": "lsp-1"}))
    assert "lsp-1" in result


# --- file_create ---------------------------------------------------------


async def test_file_create_posts_path_and_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files"
        assert json.loads(request.content) == {"path": "notes.txt", "content": "hello"}
        return httpx.Response(200, json={"path": "notes.txt", "bytes_written": 5})

    server = build_server(_client_with(handler))
    result = _text(
        await _call(server, "file_create", {"session_id": "sess-1", "path": "notes.txt", "content": "hello"})
    )
    assert "notes.txt" in result
    assert "5 bytes" in result


# --- view ----------------------------------------------------------------


async def test_view_posts_path_and_returns_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/view"
        assert json.loads(request.content) == {"path": "notes.txt"}
        return httpx.Response(200, json={"content": "hello from boxkite"})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "view", {"session_id": "sess-1", "path": "notes.txt"}))
    assert result == "hello from boxkite"


async def test_view_passes_view_range_when_provided():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"path": "notes.txt", "view_range": [1, 10]}
        return httpx.Response(200, json={"content": "partial"})

    server = build_server(_client_with(handler))
    await _call(server, "view", {"session_id": "sess-1", "path": "notes.txt", "view_range": [1, 10]})


# --- str_replace -----------------------------------------------------------


async def test_str_replace_posts_old_and_new_str():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/str-replace"
        assert json.loads(request.content) == {
            "path": "notes.txt",
            "old_str": "hello",
            "new_str": "goodbye",
            "replace_all": False,
        }
        return httpx.Response(200, json={"path": "notes.txt", "replacements": 1})

    server = build_server(_client_with(handler))
    result = _text(
        await _call(
            server,
            "str_replace",
            {"session_id": "sess-1", "path": "notes.txt", "old_str": "hello", "new_str": "goodbye"},
        )
    )
    assert "notes.txt" in result
    assert "1 replacement" in result


async def test_str_replace_passes_replace_all():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["replace_all"] is True
        return httpx.Response(200, json={"path": "notes.txt", "replacements": 3})

    server = build_server(_client_with(handler))
    await _call(
        server,
        "str_replace",
        {
            "session_id": "sess-1",
            "path": "notes.txt",
            "old_str": "a",
            "new_str": "b",
            "replace_all": True,
        },
    )


async def test_str_replace_surfaces_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": "not_unique", "message": "old_str appears more than once"}}
        )

    server = build_server(_client_with(handler))
    result = _text(
        await _call(
            server,
            "str_replace",
            {"session_id": "sess-1", "path": "notes.txt", "old_str": "a", "new_str": "b"},
        )
    )
    assert "appears more than once" in result


# --- ls --------------------------------------------------------------


async def test_ls_posts_path_and_returns_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/ls"
        assert json.loads(request.content) == {"path": "/"}
        return httpx.Response(200, json={"entries": [{"name": "notes.txt", "type": "file"}]})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "ls", {"session_id": "sess-1"}))
    assert "notes.txt" in result


async def test_ls_passes_custom_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"path": "/src"}
        return httpx.Response(200, json={"entries": []})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "ls", {"session_id": "sess-1", "path": "/src"}))
    assert "No entries" in result


async def test_ls_surfaces_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Directory not found"}})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "ls", {"session_id": "sess-1", "path": "/missing"}))
    assert "not found" in result.lower()


# --- glob --------------------------------------------------------------


async def test_glob_posts_pattern_and_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/glob"
        assert json.loads(request.content) == {"pattern": "**/*.py", "path": "/"}
        return httpx.Response(200, json={"matches": [{"path": "main.py"}]})

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "glob", {"session_id": "sess-1", "pattern": "**/*.py"}))
    assert "main.py" in result


async def test_glob_reports_no_matches():
    server = build_server(_client_with(lambda r: httpx.Response(200, json={"matches": []})))
    result = _text(await _call(server, "glob", {"session_id": "sess-1", "pattern": "*.rs"}))
    assert "No files match" in result


async def test_glob_surfaces_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    server = build_server(_client_with(handler))
    result = _text(await _call(server, "glob", {"session_id": "sess-1", "pattern": "*.py"}))
    assert "could not reach" in result.lower()


# --- grep --------------------------------------------------------------


async def test_grep_posts_pattern_path_glob_and_max_matches():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/grep"
        assert json.loads(request.content) == {
            "pattern": "TODO",
            "path": "/",
            "max_matches": 500,
            "glob": "*.py",
        }
        return httpx.Response(
            200, json={"matches": [{"path": "main.py", "line": 3}], "error": None, "truncated": False}
        )

    server = build_server(_client_with(handler))
    result = _text(
        await _call(server, "grep", {"session_id": "sess-1", "pattern": "TODO", "glob": "*.py"})
    )
    assert "main.py" in result


async def test_grep_reports_no_matches():
    server = build_server(
        _client_with(lambda r: httpx.Response(200, json={"matches": [], "error": None, "truncated": False}))
    )
    result = _text(await _call(server, "grep", {"session_id": "sess-1", "pattern": "nope"}))
    assert "No matches" in result


async def test_grep_notes_truncation():
    server = build_server(
        _client_with(
            lambda r: httpx.Response(
                200, json={"matches": [{"path": "a.py"}], "error": None, "truncated": True}
            )
        )
    )
    result = _text(await _call(server, "grep", {"session_id": "sess-1", "pattern": "x"}))
    assert "truncated" in result.lower()


async def test_grep_surfaces_sidecar_error_field():
    server = build_server(
        _client_with(
            lambda r: httpx.Response(
                200, json={"matches": [], "error": "invalid regex", "truncated": False}
            )
        )
    )
    result = _text(await _call(server, "grep", {"session_id": "sess-1", "pattern": "("}))
    assert "invalid regex" in result
