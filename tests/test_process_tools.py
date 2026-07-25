"""
Tests for boxkite.tools.process_tools (start_process, get_process_output,
send_process_input, stop_process, list_processes LangChain tools).

Mirrors tests/test_search_tools.py's pattern: mock SandboxManager, assert
the tool calls the right manager method with the right args, and assert
error paths return a string instead of raising.
"""

import pytest

from boxkite.tools.process_tools import (
    create_get_process_output_tool,
    create_list_processes_tool,
    create_send_process_input_tool,
    create_start_process_tool,
    create_stop_process_tool,
)

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(self):
        self.start_process_calls = []
        self.get_process_output_calls = []
        self.send_process_input_calls = []
        self.stop_process_calls = []
        self.list_processes_calls = []

        self.start_process_result = {
            "process_id": "proc_abc",
            "status": "running",
            "started_at": "2026-07-11T00:00:00",
        }
        self.get_process_output_result = {
            "status": "running",
            "stdout_chunk": "",
            "next_offset": 0,
            "truncated": False,
            "exit_code": None,
        }
        self.send_process_input_result = {"bytes_written": 0}
        self.stop_process_result = {"status": "stopped", "exit_code": 143}
        self.list_processes_result = []
        self.raise_error = None

    async def start_process(self, session_id, command, description=None, max_runtime_seconds=3600):
        if self.raise_error:
            raise self.raise_error
        self.start_process_calls.append(
            {
                "session_id": session_id,
                "command": command,
                "description": description,
                "max_runtime_seconds": max_runtime_seconds,
            }
        )
        return self.start_process_result

    async def get_process_output(self, session_id, process_id, since_offset=0):
        if self.raise_error:
            raise self.raise_error
        self.get_process_output_calls.append(
            {"session_id": session_id, "process_id": process_id, "since_offset": since_offset}
        )
        return self.get_process_output_result

    async def send_process_input(self, session_id, process_id, data):
        if self.raise_error:
            raise self.raise_error
        self.send_process_input_calls.append(
            {"session_id": session_id, "process_id": process_id, "data": data}
        )
        return self.send_process_input_result

    async def stop_process(self, session_id, process_id):
        if self.raise_error:
            raise self.raise_error
        self.stop_process_calls.append({"session_id": session_id, "process_id": process_id})
        return self.stop_process_result

    async def list_processes(self, session_id):
        if self.raise_error:
            raise self.raise_error
        self.list_processes_calls.append({"session_id": session_id})
        return self.list_processes_result


def test_create_start_process_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_start_process_tool()


def test_create_get_process_output_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_get_process_output_tool()


def test_create_send_process_input_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_send_process_input_tool()


def test_create_stop_process_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_stop_process_tool()


def test_create_list_processes_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_list_processes_tool()


@pytest.mark.asyncio
async def test_start_process_tool_calls_manager_with_expected_args():
    manager = _FakeSandboxManager()
    tool = create_start_process_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke(
        {"command": "npm run dev", "description": "dev server", "max_runtime_seconds": 1800}
    )

    assert manager.start_process_calls == [
        {
            "session_id": "session-1",
            "command": "npm run dev",
            "description": "dev server",
            "max_runtime_seconds": 1800,
        }
    ]
    assert "proc_abc" in result
    assert "running" in result


@pytest.mark.asyncio
async def test_start_process_tool_rejects_empty_command():
    manager = _FakeSandboxManager()
    tool = create_start_process_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"command": "  "})

    assert "Error" in result
    assert manager.start_process_calls == []


@pytest.mark.asyncio
async def test_start_process_tool_returns_error_string_on_exception():
    manager = _FakeSandboxManager()
    manager.raise_error = RuntimeError("sidecar unreachable")
    tool = create_start_process_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"command": "sleep 5"})

    assert "Error" in result
    assert "sidecar unreachable" in result


@pytest.mark.asyncio
async def test_start_process_tool_rejects_disallowed_command_under_whitelist():
    # SECURITY REGRESSION GUARD: an agent restricted via allowed_commands
    # must not be able to bypass that restriction by using start_process
    # instead of bash_tool -- same enforcement, same rejection.
    manager = _FakeSandboxManager()
    tool = create_start_process_tool(
        sandbox_manager=manager,
        session_id="session-1",
        allowed_commands=["npm", "node"],
    )

    result = await tool.ainvoke({"command": "rm -rf /"})

    assert "Blocked" in result
    assert "rm" in result
    assert manager.start_process_calls == []


@pytest.mark.asyncio
async def test_start_process_tool_allows_whitelisted_command():
    manager = _FakeSandboxManager()
    tool = create_start_process_tool(
        sandbox_manager=manager,
        session_id="session-1",
        allowed_commands=["npm", "node"],
    )

    result = await tool.ainvoke({"command": "npm run dev"})

    assert "proc_abc" in result
    assert len(manager.start_process_calls) == 1
    assert manager.start_process_calls[0]["command"] == "npm run dev"


@pytest.mark.asyncio
async def test_get_process_output_tool_calls_manager_with_expected_args():
    manager = _FakeSandboxManager()
    manager.get_process_output_result = {
        "status": "exited",
        "stdout_chunk": "hello\n",
        "next_offset": 6,
        "truncated": False,
        "exit_code": 0,
    }
    tool = create_get_process_output_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"process_id": "proc_abc", "since_offset": 3})

    assert manager.get_process_output_calls == [
        {"session_id": "session-1", "process_id": "proc_abc", "since_offset": 3}
    ]
    assert "exited" in result
    assert "exit_code: 0" in result
    assert "hello" in result


@pytest.mark.asyncio
async def test_get_process_output_tool_flags_truncation():
    manager = _FakeSandboxManager()
    manager.get_process_output_result = {
        "status": "running",
        "stdout_chunk": "tail-only",
        "next_offset": 100,
        "truncated": True,
        "exit_code": None,
    }
    tool = create_get_process_output_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"process_id": "proc_abc"})

    assert "truncated" in result.lower()


@pytest.mark.asyncio
async def test_get_process_output_tool_returns_error_string_on_exception():
    manager = _FakeSandboxManager()
    manager.raise_error = ValueError("Process not found: proc_missing")
    tool = create_get_process_output_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"process_id": "proc_missing"})

    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_send_process_input_tool_calls_manager_with_expected_args():
    manager = _FakeSandboxManager()
    manager.send_process_input_result = {"bytes_written": 2}
    tool = create_send_process_input_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"process_id": "proc_abc", "data": "y\n"})

    assert manager.send_process_input_calls == [
        {"session_id": "session-1", "process_id": "proc_abc", "data": "y\n"}
    ]
    assert "2 bytes" in result


@pytest.mark.asyncio
async def test_send_process_input_tool_rejects_empty_data():
    manager = _FakeSandboxManager()
    tool = create_send_process_input_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"process_id": "proc_abc", "data": ""})

    assert "Error" in result
    assert manager.send_process_input_calls == []


@pytest.mark.asyncio
async def test_stop_process_tool_calls_manager_with_expected_args():
    manager = _FakeSandboxManager()
    manager.stop_process_result = {"status": "stopped", "exit_code": 143}
    tool = create_stop_process_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"process_id": "proc_abc"})

    assert manager.stop_process_calls == [{"session_id": "session-1", "process_id": "proc_abc"}]
    assert "stopped" in result
    assert "143" in result


@pytest.mark.asyncio
async def test_stop_process_tool_returns_error_string_on_exception():
    manager = _FakeSandboxManager()
    manager.raise_error = ValueError("Process not found: proc_missing")
    tool = create_stop_process_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"process_id": "proc_missing"})

    assert "Error" in result


@pytest.mark.asyncio
async def test_list_processes_tool_calls_manager_and_formats_output():
    manager = _FakeSandboxManager()
    manager.list_processes_result = [
        {
            "process_id": "proc_abc",
            "command": "npm run dev",
            "description": "dev server",
            "status": "running",
            "exit_code": None,
        }
    ]
    tool = create_list_processes_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({})

    assert manager.list_processes_calls == [{"session_id": "session-1"}]
    assert "proc_abc" in result
    assert "dev server" in result
    assert "npm run dev" in result


@pytest.mark.asyncio
async def test_list_processes_tool_reports_empty_list():
    manager = _FakeSandboxManager()
    manager.list_processes_result = []
    tool = create_list_processes_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({})

    assert "no background processes" in result.lower()


@pytest.mark.asyncio
async def test_list_processes_tool_returns_error_string_on_exception():
    manager = _FakeSandboxManager()
    manager.raise_error = RuntimeError("sidecar unreachable")
    tool = create_list_processes_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({})

    assert "Error" in result
