"""Tests for boxkite.tools.pty_tools (pty_exec), docs/AGENT-PTY-DESIGN.md.

Mirrors tests/test_search_tools.py's pattern: mock SandboxManager, assert
the tool calls the right manager method with the right args, and assert
error/timeout paths return a string instead of raising.

Also covers the two fixes from the #69 security review: pty_exec previously
bypassed the per-agent command whitelist entirely (bash_tool already
enforces it) and wrote no audit record at all (bash_tool already mirrors to
AuditSink.record_exec on every command) -- see test_pty_tools.py's
whitelist/audit-sink tests below, mirroring test_bash_tool.py's own.
"""

import pytest

from boxkite.tools.pty_tools import create_pty_exec_tool, create_pty_exec_tool_spec

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(self):
        self.pty_exec_calls = []
        self.result = {"output": "hello", "exit_code": 0, "timed_out": False}
        self.raise_error = None

    async def pty_exec(self, session_id, command, input_bytes=b"", timeout_seconds=30.0):
        if self.raise_error:
            raise self.raise_error
        self.pty_exec_calls.append(
            {
                "session_id": session_id,
                "command": command,
                "input_bytes": input_bytes,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.result


class _RecordingAuditSink:
    def __init__(self):
        self.record_exec_calls = []

    async def record_exec(self, **kwargs):
        self.record_exec_calls.append(kwargs)


def test_create_pty_exec_tool_spec_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_pty_exec_tool_spec()


@pytest.mark.asyncio
async def test_pty_exec_rejects_empty_command():
    manager = _FakeSandboxManager()
    spec = create_pty_exec_tool_spec(sandbox_manager=manager, session_id="s1")

    result = await spec.handler(command="   ")

    assert "Empty command" in result
    assert manager.pty_exec_calls == []


@pytest.mark.asyncio
async def test_pty_exec_calls_manager_with_encoded_input():
    manager = _FakeSandboxManager()
    spec = create_pty_exec_tool_spec(sandbox_manager=manager, session_id="s1")

    await spec.handler(command="cat", input_text="typed input", timeout_seconds=15)

    assert manager.pty_exec_calls == [
        {
            "session_id": "s1",
            "command": "cat",
            "input_bytes": b"typed input",
            "timeout_seconds": 15,
        }
    ]


@pytest.mark.asyncio
async def test_pty_exec_reports_output_and_exit_code():
    manager = _FakeSandboxManager()
    manager.result = {"output": "hello world", "exit_code": 0, "timed_out": False}
    spec = create_pty_exec_tool_spec(sandbox_manager=manager, session_id="s1")

    result = await spec.handler(command="echo hello world")

    assert "hello world" in result
    assert "exit code 0" in result


@pytest.mark.asyncio
async def test_pty_exec_reports_timeout():
    manager = _FakeSandboxManager()
    manager.result = {"output": "partial", "exit_code": None, "timed_out": True}
    spec = create_pty_exec_tool_spec(sandbox_manager=manager, session_id="s1")

    result = await spec.handler(command="sleep 999", timeout_seconds=1)

    assert "timed out" in result.lower()
    assert "partial" in result


@pytest.mark.asyncio
async def test_pty_exec_returns_error_string_on_exception():
    manager = _FakeSandboxManager()
    manager.raise_error = RuntimeError("boom")
    spec = create_pty_exec_tool_spec(sandbox_manager=manager, session_id="s1")

    result = await spec.handler(command="echo hi")

    assert "Error" in result


@pytest.mark.asyncio
async def test_create_pty_exec_tool_langchain_wrapper_round_trips():
    manager = _FakeSandboxManager()
    tool = create_pty_exec_tool(sandbox_manager=manager, session_id="s1")

    result = await tool.ainvoke({"command": "echo hi"})

    assert "hello" in result


@pytest.mark.asyncio
async def test_pty_exec_rejects_disallowed_command_under_whitelist():
    manager = _FakeSandboxManager()
    spec = create_pty_exec_tool_spec(
        sandbox_manager=manager, session_id="s1", allowed_commands=["ls", "cat"]
    )

    result = await spec.handler(command="rm -rf /")

    assert "Blocked" in result
    assert manager.pty_exec_calls == []


@pytest.mark.asyncio
async def test_pty_exec_allows_whitelisted_command():
    manager = _FakeSandboxManager()
    spec = create_pty_exec_tool_spec(
        sandbox_manager=manager, session_id="s1", allowed_commands=["ls", "cat"]
    )

    result = await spec.handler(command="ls -la")

    assert manager.pty_exec_calls != []
    assert "exit code" in result


@pytest.mark.asyncio
async def test_pty_exec_with_no_allowed_commands_is_unrestricted():
    manager = _FakeSandboxManager()
    spec = create_pty_exec_tool_spec(sandbox_manager=manager, session_id="s1")

    result = await spec.handler(command="anything goes")

    assert manager.pty_exec_calls != []
    assert "Blocked" not in result


@pytest.mark.asyncio
async def test_pty_exec_works_with_no_audit_sink():
    manager = _FakeSandboxManager()
    spec = create_pty_exec_tool_spec(sandbox_manager=manager, session_id="s1", audit_sink=None)

    result = await spec.handler(command="echo hi")

    assert "hello" in result
    assert len(manager.pty_exec_calls) == 1


@pytest.mark.asyncio
async def test_pty_exec_mirrors_successful_run_to_audit_sink():
    manager = _FakeSandboxManager()
    sink = _RecordingAuditSink()

    spec = create_pty_exec_tool_spec(
        sandbox_manager=manager,
        session_id="s1",
        agent_name="researcher",
        audit_sink=sink,
    )

    await spec.handler(command="echo hi")

    assert len(sink.record_exec_calls) == 1
    call = sink.record_exec_calls[0]
    assert call["session_id"] == "s1"
    assert call["agent_name"] == "researcher"
    assert call["command"] == "echo hi"
    assert call["exit_code"] == 0
    assert isinstance(call["duration_ms"], int)


@pytest.mark.asyncio
async def test_pty_exec_mirrors_timed_out_run_to_audit_sink():
    manager = _FakeSandboxManager()
    manager.result = {"output": "partial", "exit_code": None, "timed_out": True}
    sink = _RecordingAuditSink()

    spec = create_pty_exec_tool_spec(sandbox_manager=manager, session_id="s1", audit_sink=sink)

    await spec.handler(command="sleep 999", timeout_seconds=1)

    assert len(sink.record_exec_calls) == 1
    assert sink.record_exec_calls[0]["exit_code"] == -1


@pytest.mark.asyncio
async def test_pty_exec_accepts_a_partial_audit_sink_missing_record_exec():
    class _EmptySink:
        pass

    manager = _FakeSandboxManager()
    spec = create_pty_exec_tool_spec(sandbox_manager=manager, session_id="s1", audit_sink=_EmptySink())

    result = await spec.handler(command="echo hi")

    assert "hello" in result
