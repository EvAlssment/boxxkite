"""LangChain tool factory over the hosted API -- mirrors boxkite.tools'
tool names/shapes (bash_tool, file_create, view, str_replace, ls, glob,
grep) but calls the hosted control-plane instead of an embedded
SandboxManager."""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("langchain_core")

from boxkite_client import BoxkiteClient  # noqa: E402
from boxkite_client.langchain_tools import create_sandbox_tools  # noqa: E402


def _client_with(handler) -> BoxkiteClient:
    return BoxkiteClient(
        base_url="https://cp.example.com",
        api_key="bxk_live_test",
        transport=httpx.MockTransport(handler),
    )


def test_create_sandbox_tools_returns_twelve_tools():
    client = _client_with(lambda request: httpx.Response(200, json={}))
    tools = create_sandbox_tools(client, "sess-1")

    names = {t.name for t in tools}
    assert names == {
        "bash_tool",
        "file_create",
        "view",
        "str_replace",
        "ls",
        "glob",
        "grep",
        "start_process",
        "get_process_output",
        "send_process_input",
        "stop_process",
        "list_processes",
    }


def test_bash_tool_invokes_exec_and_returns_stdout():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/exec"
        return httpx.Response(200, json={"exit_code": 0, "stdout": "hello\n", "stderr": ""})

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    bash_tool = next(t for t in tools if t.name == "bash_tool")

    result = bash_tool.invoke({"command": "echo hello"})
    assert "hello" in result


def test_file_create_tool_invokes_file_create():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files"
        return httpx.Response(200, json={"path": "x.txt", "size": 5, "created": True})

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    file_create = next(t for t in tools if t.name == "file_create")

    result = file_create.invoke({"path": "x.txt", "content": "hello"})
    assert "x.txt" in result


def test_view_tool_invokes_view():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/view"
        return httpx.Response(200, json={"content": "file contents"})

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    view = next(t for t in tools if t.name == "view")

    result = view.invoke({"path": "x.txt"})
    assert "file contents" in result


def test_str_replace_tool_invokes_str_replace():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/str-replace"
        return httpx.Response(200, json={"path": "x.txt", "replaced": True, "occurrences": 3})

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    str_replace = next(t for t in tools if t.name == "str_replace")

    result = str_replace.invoke({"path": "x.txt", "old_str": "a", "new_str": "b"})
    assert "x.txt" in result
    assert "3 replacement(s)" in result


def test_ls_tool_invokes_ls():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/ls"
        return httpx.Response(200, json={"entries": [{"path": "a.txt", "is_dir": False}]})

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    ls_tool = next(t for t in tools if t.name == "ls")

    result = ls_tool.invoke({"path": "/workspace"})
    assert "a.txt" in result


def test_ls_tool_reports_empty_directory():
    client = _client_with(lambda request: httpx.Response(200, json={"entries": []}))
    tools = create_sandbox_tools(client, "sess-1")
    ls_tool = next(t for t in tools if t.name == "ls")

    result = ls_tool.invoke({"path": "/empty"})
    assert "empty" in result.lower()


def test_glob_tool_invokes_glob():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/glob"
        return httpx.Response(200, json={"matches": [{"path": "a.py"}]})

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    glob_tool = next(t for t in tools if t.name == "glob")

    result = glob_tool.invoke({"pattern": "**/*.py"})
    assert "a.py" in result


def test_grep_tool_invokes_grep():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/grep"
        return httpx.Response(200, json={"matches": [{"path": "a.py", "line": 1, "text": "import os"}], "truncated": False})

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    grep_tool = next(t for t in tools if t.name == "grep")

    result = grep_tool.invoke({"pattern": "import os"})
    assert "a.py" in result


def test_grep_tool_surfaces_reported_error():
    client = _client_with(lambda request: httpx.Response(200, json={"matches": [], "error": "invalid regex"}))
    tools = create_sandbox_tools(client, "sess-1")
    grep_tool = next(t for t in tools if t.name == "grep")

    result = grep_tool.invoke({"pattern": "("})
    assert "invalid regex" in result


def test_start_process_tool_invokes_start_process():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/processes"
        return httpx.Response(201, json={"process_id": "proc_1", "status": "running", "started_at": "now"})

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    start_process = next(t for t in tools if t.name == "start_process")

    result = start_process.invoke({"command": "sleep 5"})
    assert "proc_1" in result


def test_get_process_output_tool_invokes_get_process_output():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/processes/proc_1/output"
        return httpx.Response(
            200,
            json={"status": "exited", "stdout_chunk": "hello", "next_offset": 5, "truncated": False, "exit_code": 0},
        )

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    get_process_output = next(t for t in tools if t.name == "get_process_output")

    result = get_process_output.invoke({"process_id": "proc_1"})
    assert "hello" in result
    assert "exit_code: 0" in result


def test_stop_process_tool_invokes_stop_process():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/processes/proc_1/stop"
        return httpx.Response(200, json={"status": "stopped", "exit_code": 143})

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    stop_process = next(t for t in tools if t.name == "stop_process")

    result = stop_process.invoke({"process_id": "proc_1"})
    assert "stopped" in result


def test_list_processes_tool_reports_empty_list():
    client = _client_with(lambda request: httpx.Response(200, json={"processes": []}))
    tools = create_sandbox_tools(client, "sess-1")
    list_processes = next(t for t in tools if t.name == "list_processes")

    result = list_processes.invoke({})
    assert "no background processes" in result.lower()


def test_bash_tool_surfaces_api_error_as_string_not_exception():
    """LangChain tools should return an error string the agent can react
    to, not raise -- an uncaught exception would kill the agent loop."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Sandbox session not found"}})

    client = _client_with(handler)
    tools = create_sandbox_tools(client, "sess-1")
    bash_tool = next(t for t in tools if t.name == "bash_tool")

    result = bash_tool.invoke({"command": "echo hi"})
    assert "not found" in result.lower() or "error" in result.lower()
