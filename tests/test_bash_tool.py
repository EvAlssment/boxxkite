"""
Tests for boxkite.tools.bash_tool, in particular its AuditSink.record_exec
integration point.

Mirrors tests/test_tool_factory.py's and tests/test_search_tools.py's pattern:
mock SandboxManager, assert the tool calls the right manager method, and
assert the optional AuditSink is invoked (or safely skipped) as expected.
"""

from uuid import uuid4

import pytest

from boxkite.audit import NoOpAuditSink, safe_call
from boxkite.tools.bash_tool import create_bash_tool

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(self, exit_code=0, stdout="hello", stderr=""):
        self.execute_calls = []
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    async def execute(self, session_id, command, timeout, secret_env=None):
        self.execute_calls.append(
            {"session_id": session_id, "command": command, "timeout": timeout, "secret_env": secret_env}
        )
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class _RecordingAuditSink:
    def __init__(self):
        self.record_exec_calls = []

    async def record_exec(self, **kwargs):
        self.record_exec_calls.append(kwargs)


def test_create_bash_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_bash_tool()


@pytest.mark.asyncio
async def test_bash_tool_works_with_no_audit_sink():
    # audit_sink is entirely optional — this must not raise or change behavior.
    manager = _FakeSandboxManager()
    tool = create_bash_tool(session_id="session-1", sandbox_manager=manager, audit_sink=None)

    result = await tool.ainvoke({"command": "echo hi"})

    assert result == "hello"
    assert len(manager.execute_calls) == 1


@pytest.mark.asyncio
async def test_bash_tool_mirrors_successful_exec_to_audit_sink():
    manager = _FakeSandboxManager(exit_code=0, stdout="hello", stderr="")
    sink = _RecordingAuditSink()
    org_id = uuid4()
    work_item_id = uuid4()

    tool = create_bash_tool(
        session_id="session-1",
        sandbox_manager=manager,
        organization_id=org_id,
        work_item_id=work_item_id,
        agent_name="researcher",
        audit_sink=sink,
    )

    result = await tool.ainvoke({"command": "echo hi"})

    assert result == "hello"
    assert len(sink.record_exec_calls) == 1
    call = sink.record_exec_calls[0]
    assert call["organization_id"] == org_id
    assert call["work_item_id"] == work_item_id
    assert call["session_id"] == "session-1"
    assert call["agent_name"] == "researcher"
    assert call["command"] == "echo hi"
    assert call["exit_code"] == 0
    assert isinstance(call["duration_ms"], int)
    assert call["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_bash_tool_mirrors_failed_exec_to_audit_sink_with_nonzero_exit_code():
    manager = _FakeSandboxManager(exit_code=1, stdout="", stderr="boom")
    sink = _RecordingAuditSink()

    tool = create_bash_tool(session_id="session-1", sandbox_manager=manager, audit_sink=sink)

    await tool.ainvoke({"command": "false"})

    assert len(sink.record_exec_calls) == 1
    assert sink.record_exec_calls[0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_bash_tool_accepts_a_partial_audit_sink_missing_record_exec():
    # A sink that doesn't implement record_exec must not break bash_tool —
    # safe_call treats a missing method the same as a no-op.
    class PartialSink:
        async def record_file_write(self, **_kwargs):
            return None

    manager = _FakeSandboxManager()
    tool = create_bash_tool(session_id="session-1", sandbox_manager=manager, audit_sink=PartialSink())

    result = await tool.ainvoke({"command": "echo hi"})

    assert result == "hello"


@pytest.mark.asyncio
async def test_bash_tool_survives_a_broken_audit_sink():
    class BrokenSink:
        async def record_exec(self, **_kwargs):
            raise RuntimeError("downstream system is down")

    manager = _FakeSandboxManager()
    tool = create_bash_tool(session_id="session-1", sandbox_manager=manager, audit_sink=BrokenSink())

    # Must not raise — a broken AuditSink can never fail bash_tool.
    result = await tool.ainvoke({"command": "echo hi"})

    assert result == "hello"


@pytest.mark.asyncio
async def test_bash_tool_extracts_bare_uuid_from_prefixed_session_id():
    manager = _FakeSandboxManager()
    sink = _RecordingAuditSink()
    session_uuid = uuid4()

    tool = create_bash_tool(
        session_id=f"execution:{session_uuid}",
        sandbox_manager=manager,
        audit_sink=sink,
    )

    await tool.ainvoke({"command": "echo hi"})

    assert sink.record_exec_calls[0]["session_id"] == str(session_uuid)


@pytest.mark.asyncio
async def test_noop_audit_sink_record_exec_is_a_safe_default():
    sink = NoOpAuditSink()
    assert (
        await sink.record_exec(
            organization_id=None,
            work_item_id=None,
            session_id=None,
            agent_name=None,
            command="echo hi",
            exit_code=0,
            duration_ms=1,
        )
        is None
    )


@pytest.mark.asyncio
async def test_safe_call_swallows_exceptions_from_record_exec():
    class BrokenSink:
        async def record_exec(self, **_kwargs):
            raise RuntimeError("downstream system is down")

    result = await safe_call(BrokenSink(), "record_exec", command="echo hi")
    assert result is None


@pytest.mark.asyncio
async def test_bash_tool_rejects_disallowed_command_under_whitelist():
    manager = _FakeSandboxManager()
    tool = create_bash_tool(
        session_id="session-1",
        sandbox_manager=manager,
        allowed_commands=["ls", "cat"],
    )

    result = await tool.ainvoke({"command": "rm -rf /"})

    assert "Blocked" in result
    assert "rm" in result
    assert manager.execute_calls == []


@pytest.mark.asyncio
async def test_bash_tool_allows_whitelisted_command():
    manager = _FakeSandboxManager()
    tool = create_bash_tool(
        session_id="session-1",
        sandbox_manager=manager,
        allowed_commands=["ls", "cat"],
    )

    result = await tool.ainvoke({"command": "ls -la"})

    assert result == "hello"
    assert len(manager.execute_calls) == 1


def test_bash_tool_spec_omits_secret_env_from_schema_by_default():
    from boxkite.tools.bash_tool import create_bash_tool_spec

    spec = create_bash_tool_spec(session_id="s1", sandbox_manager=_FakeSandboxManager())

    assert "secret_env" not in spec.parameters["properties"]


def test_bash_tool_spec_exposes_secret_env_when_enabled():
    from boxkite.tools.bash_tool import create_bash_tool_spec

    spec = create_bash_tool_spec(
        session_id="s1", sandbox_manager=_FakeSandboxManager(), enable_secret_env=True
    )

    assert "secret_env" in spec.parameters["properties"]


@pytest.mark.asyncio
async def test_bash_tool_spec_forwards_secret_env_when_enabled():
    from boxkite.tools.bash_tool import create_bash_tool_spec

    manager = _FakeSandboxManager()
    spec = create_bash_tool_spec(
        session_id="s1", sandbox_manager=manager, enable_secret_env=True
    )

    await spec.handler(command="echo hi", secret_env={"ANTHROPIC_API_KEY": "claude-code-key"})

    assert manager.execute_calls[0]["secret_env"] == {"ANTHROPIC_API_KEY": "claude-code-key"}


@pytest.mark.asyncio
async def test_bash_tool_spec_ignores_secret_env_when_not_enabled():
    from boxkite.tools.bash_tool import create_bash_tool_spec

    manager = _FakeSandboxManager()
    spec = create_bash_tool_spec(session_id="s1", sandbox_manager=manager)

    await spec.handler(command="echo hi", secret_env={"ANTHROPIC_API_KEY": "claude-code-key"})

    assert manager.execute_calls[0]["secret_env"] is None
