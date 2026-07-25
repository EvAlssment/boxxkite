"""
Tests for boxkite.tools.factory.create_sandbox_tools and the AuditSink hook.

The system this package was extracted from also had a test suite covering
a per-agent "enable_sandbox" toggle, but that test exercised it through an
internal agent-orchestration factory (subagent propagation, a global feature
flag, etc.) — that orchestration layer is explicitly out of scope for this
extraction (see README "What's not included"). The part of that test
coverage that *is* portable — that the sandbox tool factory assembles the
right tool set, that it's gateable by the caller (simply: call
create_sandbox_tools() or don't), and that the optional AuditSink
integration point degrades safely — is covered here directly against
boxkite's own factory.py instead.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from boxkite.audit import NoOpAuditSink, safe_call
from boxkite.tools import create_sandbox_tools

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    """Minimal stand-in — tool *creation* never calls manager methods eagerly."""


def test_create_sandbox_tools_returns_the_full_tool_set():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
    )

    tool_names = {t.name for t in tools}
    assert tool_names == {
        "bash_tool",
        "python_interpreter",
        "file_create",
        "view",
        "str_replace",
        "present_files",
        "ls",
        "glob",
        "grep",
        "start_process",
        "get_process_output",
        "send_process_input",
        "stop_process",
        "list_processes",
        "watch_directory",
    }
    assert len(tools) == 15


def test_create_sandbox_tools_run_tests_is_off_by_default():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
    )

    assert "run_tests" not in {t.name for t in tools}


def test_create_sandbox_tools_with_run_tests_enabled_includes_run_tests():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
        enable_run_tests=True,
    )

    tool_names = {t.name for t in tools}
    assert "run_tests" in tool_names
    assert len(tools) == 16


def test_create_sandbox_tools_with_git_tools_enabled_includes_git_tool_set():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
        enable_git_tools=True,
    )

    tool_names = {t.name for t in tools}
    assert {
        "git_clone",
        "git_status",
        "git_add",
        "git_commit",
        "git_push",
        "git_pull",
        "git_branch",
        "git_checkout",
    }.issubset(tool_names)
    assert len(tools) == 23


def test_create_sandbox_tools_with_agent_pty_enabled_includes_pty_exec():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
        enable_agent_pty=True,
    )

    tool_names = {t.name for t in tools}
    assert "pty_exec" in tool_names
    assert len(tools) == 16


def test_create_sandbox_tools_omits_pty_exec_by_default():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
    )

    assert "pty_exec" not in {t.name for t in tools}


def test_create_sandbox_tools_with_node_interpreter_enabled_includes_node_interpreter():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
        enable_node_interpreter=True,
    )

    tool_names = {t.name for t in tools}
    assert "node_interpreter" in tool_names
    assert len(tools) == 16


def test_create_sandbox_tools_omits_node_interpreter_by_default():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
    )

    assert "node_interpreter" not in {t.name for t in tools}


def test_create_sandbox_tools_with_browser_tool_enabled_includes_all_four_browser_tools():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
        enable_browser_tool=True,
    )

    tool_names = {t.name for t in tools}
    assert {
        "browser_navigate",
        "browser_exec",
        "browser_screenshot",
        "browser_close",
    }.issubset(tool_names)
    assert len(tools) == 19


def test_create_sandbox_tools_omits_browser_tools_by_default():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
    )

    tool_names = {t.name for t in tools}
    assert not {
        "browser_navigate",
        "browser_exec",
        "browser_screenshot",
        "browser_close",
    } & tool_names


def test_create_sandbox_tools_with_lsp_tools_enabled_includes_all_three_lsp_tools():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
        enable_lsp_tools=True,
    )

    tool_names = {t.name for t in tools}
    assert {"lsp_start", "lsp_completion", "lsp_stop"}.issubset(tool_names)
    assert len(tools) == 18


def test_create_sandbox_tools_omits_lsp_tools_by_default():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="session-1",
    )

    tool_names = {t.name for t in tools}
    assert not {"lsp_start", "lsp_completion", "lsp_stop"} & tool_names


def test_create_sandbox_tools_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_sandbox_tools()


def test_create_sandbox_tools_works_with_no_audit_sink():
    # audit_sink is entirely optional — this must not raise or behave
    # differently from omitting it.
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        audit_sink=None,
    )
    assert len(tools) == 15


def test_create_sandbox_tools_accepts_a_partial_audit_sink():
    # A sink that only implements a subset of AuditSink must still be
    # accepted at tool-creation time (methods are looked up lazily via
    # safe_call, not required by an isinstance check).
    class PartialSink:
        async def record_file_write(self, **_kwargs):
            return None

    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        audit_sink=PartialSink(),
    )
    assert len(tools) == 15


@pytest.mark.asyncio
async def test_noop_audit_sink_methods_are_all_safe_defaults():
    sink = NoOpAuditSink()
    assert await sink.record_file_write(organization_id=None, work_item_id=None) is None
    assert await sink.record_file_registered(organization_id=None, work_item_id=None) is None
    assert await sink.get_download_url(organization_id=None, work_item_id=None) is None


@pytest.mark.asyncio
async def test_safe_call_swallows_exceptions_from_a_broken_sink():
    class BrokenSink:
        async def record_file_write(self, **_kwargs):
            raise RuntimeError("downstream system is down")

    # Must not raise — a broken AuditSink can never fail a sandbox tool call.
    result = await safe_call(BrokenSink(), "record_file_write", file_path="x")
    assert result is None


@pytest.mark.asyncio
async def test_safe_call_returns_none_for_missing_method():
    result = await safe_call(SimpleNamespace(), "record_file_write", file_path="x")
    assert result is None


@pytest.mark.asyncio
async def test_safe_call_returns_none_for_no_sink():
    result = await safe_call(None, "record_file_write", file_path="x")
    assert result is None
