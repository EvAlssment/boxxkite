"""
Tests for boxkite.tools.python_interpreter_tool, in particular its
AuditSink.record_exec integration point.

Mirrors tests/test_bash_tool.py's pattern: mock SandboxManager, assert the
tool calls the right manager method, and assert the optional AuditSink is
invoked (or safely skipped) as expected.
"""

from uuid import uuid4

import pytest

from boxkite.tools.python_interpreter_tool import create_python_interpreter_tool

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(self, stdout="", result=None, error=None, truncated=False):
        self.interpreter_exec_calls = []
        self.stdout = stdout
        self.result = result
        self.error = error
        self.truncated = truncated

    async def interpreter_exec(self, session_id, code, timeout):
        self.interpreter_exec_calls.append(
            {"session_id": session_id, "code": code, "timeout": timeout}
        )
        return {
            "stdout": self.stdout,
            "result": self.result,
            "error": self.error,
            "truncated": self.truncated,
        }


class _RecordingAuditSink:
    def __init__(self):
        self.record_exec_calls = []

    async def record_exec(self, **kwargs):
        self.record_exec_calls.append(kwargs)


def test_create_python_interpreter_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_python_interpreter_tool()


@pytest.mark.asyncio
async def test_python_interpreter_tool_works_with_no_audit_sink():
    manager = _FakeSandboxManager(stdout="", result="42", error=None)
    tool = create_python_interpreter_tool(
        session_id="session-1", sandbox_manager=manager, audit_sink=None
    )

    result = await tool.ainvoke({"code": "40 + 2"})

    assert result == "42"
    assert len(manager.interpreter_exec_calls) == 1
    assert manager.interpreter_exec_calls[0]["code"] == "40 + 2"


@pytest.mark.asyncio
async def test_python_interpreter_tool_rejects_empty_code():
    manager = _FakeSandboxManager()
    tool = create_python_interpreter_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke({"code": "   "})

    assert result == "Error: Empty code provided"
    assert manager.interpreter_exec_calls == []


@pytest.mark.asyncio
async def test_python_interpreter_tool_combines_stdout_and_result():
    manager = _FakeSandboxManager(stdout="hi\n", result="10", error=None)
    tool = create_python_interpreter_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke({"code": "print('hi'); 10"})

    assert result == "hi\n\n10"


@pytest.mark.asyncio
async def test_python_interpreter_tool_reports_no_output_when_nothing_returned():
    manager = _FakeSandboxManager(stdout="", result=None, error=None)
    tool = create_python_interpreter_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke({"code": "x = 1"})

    assert result == "(no output)"


@pytest.mark.asyncio
async def test_python_interpreter_tool_surfaces_errors():
    manager = _FakeSandboxManager(
        stdout="", result=None, error="ZeroDivisionError: division by zero"
    )
    tool = create_python_interpreter_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke({"code": "1 / 0"})

    assert result.startswith("Error:")
    assert "ZeroDivisionError" in result


@pytest.mark.asyncio
async def test_python_interpreter_tool_flags_truncated_output():
    manager = _FakeSandboxManager(stdout="a" * 10, result=None, error=None, truncated=True)
    tool = create_python_interpreter_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke({"code": "print('a' * 100000)"})

    assert "[output truncated]" in result


@pytest.mark.asyncio
async def test_python_interpreter_tool_mirrors_successful_exec_to_audit_sink():
    manager = _FakeSandboxManager(stdout="", result="42", error=None)
    sink = _RecordingAuditSink()
    org_id = uuid4()
    work_item_id = uuid4()

    tool = create_python_interpreter_tool(
        session_id="session-1",
        sandbox_manager=manager,
        organization_id=org_id,
        work_item_id=work_item_id,
        agent_name="researcher",
        audit_sink=sink,
    )

    await tool.ainvoke({"code": "40 + 2"})

    assert len(sink.record_exec_calls) == 1
    call = sink.record_exec_calls[0]
    assert call["organization_id"] == org_id
    assert call["work_item_id"] == work_item_id
    assert call["session_id"] == "session-1"
    assert call["agent_name"] == "researcher"
    assert call["command"] == "40 + 2"
    assert call["exit_code"] == 0
    assert isinstance(call["duration_ms"], int)


@pytest.mark.asyncio
async def test_python_interpreter_tool_mirrors_failed_exec_with_nonzero_exit_code():
    manager = _FakeSandboxManager(stdout="", result=None, error="NameError: name 'y' is not defined")
    sink = _RecordingAuditSink()

    tool = create_python_interpreter_tool(
        session_id="session-1", sandbox_manager=manager, audit_sink=sink
    )

    await tool.ainvoke({"code": "y"})

    assert len(sink.record_exec_calls) == 1
    assert sink.record_exec_calls[0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_python_interpreter_tool_survives_a_broken_audit_sink():
    class BrokenSink:
        async def record_exec(self, **_kwargs):
            raise RuntimeError("downstream system is down")

    manager = _FakeSandboxManager(stdout="", result="1", error=None)
    tool = create_python_interpreter_tool(
        session_id="session-1", sandbox_manager=manager, audit_sink=BrokenSink()
    )

    # Must not raise -- a broken AuditSink can never fail python_interpreter.
    result = await tool.ainvoke({"code": "1"})

    assert result == "1"
