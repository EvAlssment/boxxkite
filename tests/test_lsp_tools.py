"""
Tests for boxkite.tools.lsp_tools -- the lsp_start/lsp_completion/lsp_stop
ToolSpecs and their raw-LSP-item -> agent-readable-item translation.

Mirrors tests/test_node_interpreter_tool.py's pattern: mock SandboxManager,
assert each tool calls the right manager method(s) with the right
arguments, and assert the optional AuditSink is invoked (or safely
skipped) as expected for lsp_completion (the one "exec-like" tool in this
set -- lsp_start/lsp_stop don't audit, same as start_process/stop_process).
"""

from uuid import uuid4

import pytest

from boxkite.tools.lsp_tools import (
    _simplify_completion_items,
    create_lsp_completion_tool_spec,
    create_lsp_start_tool_spec,
    create_lsp_stop_tool_spec,
    create_lsp_tool_specs,
)

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(self, view_content="", completion_items=None, lsp_id="lsp_abc123"):
        self.lsp_start_calls = []
        self.lsp_open_calls = []
        self.lsp_completion_calls = []
        self.lsp_stop_calls = []
        self.view_calls = []
        self.view_content = view_content
        self.completion_items = completion_items if completion_items is not None else []
        self.lsp_id = lsp_id

    async def lsp_start(self, session_id, language):
        self.lsp_start_calls.append({"session_id": session_id, "language": language})
        return {"lsp_id": self.lsp_id}

    async def view(self, session_id, path):
        self.view_calls.append({"session_id": session_id, "path": path})
        return {"content": self.view_content}

    async def lsp_open(self, session_id, lsp_id, path, content):
        self.lsp_open_calls.append(
            {"session_id": session_id, "lsp_id": lsp_id, "path": path, "content": content}
        )
        return {"status": "opened"}

    async def lsp_completion(self, session_id, lsp_id, path, line, character):
        self.lsp_completion_calls.append(
            {
                "session_id": session_id,
                "lsp_id": lsp_id,
                "path": path,
                "line": line,
                "character": character,
            }
        )
        return {"items": self.completion_items}

    async def lsp_stop(self, session_id, lsp_id):
        self.lsp_stop_calls.append({"session_id": session_id, "lsp_id": lsp_id})
        return {"status": "stopped"}


class _RecordingAuditSink:
    def __init__(self):
        self.record_exec_calls = []

    async def record_exec(self, **kwargs):
        self.record_exec_calls.append(kwargs)


# ============================================================================
# _simplify_completion_items -- raw LSP CompletionItem payloads are
# permissive by spec (only `label` is required); every optional field must
# have an explicit fallback.
# ============================================================================


def test_simplify_completion_items_translates_full_item():
    items = _simplify_completion_items(
        [{"label": "path", "kind": 6, "detail": "module", "insertText": "path"}]
    )
    assert items == [{"label": "path", "kind": "variable", "detail": "module", "insertText": "path"}]


def test_simplify_completion_items_handles_item_with_only_label():
    """Optional fields absent -- kind falls back to 'unknown', detail to
    None, insertText to the label itself (per the LSP spec's own fallback
    rule)."""
    items = _simplify_completion_items([{"label": "bare_item"}])
    assert items == [
        {"label": "bare_item", "kind": "unknown", "detail": None, "insertText": "bare_item"}
    ]


def test_simplify_completion_items_handles_unknown_kind_code():
    items = _simplify_completion_items([{"label": "x", "kind": 9999}])
    assert items[0]["kind"] == "unknown"


def test_simplify_completion_items_skips_non_dict_entries():
    items = _simplify_completion_items([{"label": "a"}, "not-a-dict", None])
    assert len(items) == 1
    assert items[0]["label"] == "a"


def test_simplify_completion_items_handles_empty_list():
    assert _simplify_completion_items([]) == []


# ============================================================================
# lsp_start
# ============================================================================


def test_create_lsp_start_tool_spec_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_lsp_start_tool_spec()


async def test_lsp_start_tool_calls_manager_and_returns_lsp_id():
    manager = _FakeSandboxManager(lsp_id="lsp_xyz")
    spec = create_lsp_start_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(language="python")

    assert "lsp_xyz" in result
    assert manager.lsp_start_calls == [{"session_id": "session-1", "language": "python"}]


async def test_lsp_start_tool_rejects_unsupported_language():
    manager = _FakeSandboxManager()
    spec = create_lsp_start_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(language="ruby")

    assert result.startswith("Error")
    assert manager.lsp_start_calls == []


async def test_lsp_start_tool_surfaces_manager_errors():
    class _FailingManager(_FakeSandboxManager):
        async def lsp_start(self, session_id, language):
            raise RuntimeError("sidecar unreachable")

    spec = create_lsp_start_tool_spec(session_id="session-1", sandbox_manager=_FailingManager())

    result = await spec.handler(language="python")

    assert result.startswith("Error starting LSP server")
    assert "sidecar unreachable" in result


# ============================================================================
# lsp_completion
# ============================================================================


def test_create_lsp_completion_tool_spec_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_lsp_completion_tool_spec()


async def test_lsp_completion_tool_reads_file_opens_it_then_requests_completion():
    manager = _FakeSandboxManager(
        view_content="import os\nos.pat",
        completion_items=[{"label": "path", "kind": 6, "insertText": "path"}],
    )
    spec = create_lsp_completion_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(lsp_id="lsp_abc123", path="probe.py", line=1, character=6)

    assert manager.view_calls == [{"session_id": "session-1", "path": "probe.py"}]
    assert manager.lsp_open_calls == [
        {
            "session_id": "session-1",
            "lsp_id": "lsp_abc123",
            "path": "probe.py",
            "content": "import os\nos.pat",
        }
    ]
    assert manager.lsp_completion_calls == [
        {
            "session_id": "session-1",
            "lsp_id": "lsp_abc123",
            "path": "probe.py",
            "line": 1,
            "character": 6,
        }
    ]
    assert "path" in result
    assert "variable" in result


async def test_lsp_completion_tool_reports_no_completions():
    manager = _FakeSandboxManager(view_content="x = 1", completion_items=[])
    spec = create_lsp_completion_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(lsp_id="lsp_abc123", path="a.py", line=0, character=1)

    assert result == "(no completions)"


async def test_lsp_completion_tool_rejects_empty_lsp_id():
    manager = _FakeSandboxManager()
    spec = create_lsp_completion_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(lsp_id="", path="a.py", line=0, character=0)

    assert result.startswith("Error")
    assert manager.view_calls == []


async def test_lsp_completion_tool_surfaces_errors():
    class _FailingManager(_FakeSandboxManager):
        async def view(self, session_id, path):
            raise RuntimeError("file not found")

    spec = create_lsp_completion_tool_spec(
        session_id="session-1", sandbox_manager=_FailingManager()
    )

    result = await spec.handler(lsp_id="lsp_abc123", path="missing.py", line=0, character=0)

    assert result.startswith("Error getting completions")
    assert "file not found" in result


async def test_lsp_completion_tool_mirrors_successful_call_to_audit_sink():
    manager = _FakeSandboxManager(
        view_content="x", completion_items=[{"label": "path", "kind": 6}]
    )
    sink = _RecordingAuditSink()
    org_id = uuid4()
    work_item_id = uuid4()

    spec = create_lsp_completion_tool_spec(
        session_id="session-1",
        sandbox_manager=manager,
        organization_id=org_id,
        work_item_id=work_item_id,
        agent_name="researcher",
        audit_sink=sink,
    )

    await spec.handler(lsp_id="lsp_abc123", path="a.py", line=1, character=6)

    assert len(sink.record_exec_calls) == 1
    call = sink.record_exec_calls[0]
    assert call["organization_id"] == org_id
    assert call["work_item_id"] == work_item_id
    assert call["session_id"] == "session-1"
    assert call["agent_name"] == "researcher"
    assert "a.py:1:6" in call["command"]
    assert call["exit_code"] == 0
    assert isinstance(call["duration_ms"], int)


async def test_lsp_completion_tool_mirrors_failed_call_with_nonzero_exit_code():
    class _FailingManager(_FakeSandboxManager):
        async def view(self, session_id, path):
            raise RuntimeError("boom")

    sink = _RecordingAuditSink()
    spec = create_lsp_completion_tool_spec(
        session_id="session-1", sandbox_manager=_FailingManager(), audit_sink=sink
    )

    await spec.handler(lsp_id="lsp_abc123", path="a.py", line=0, character=0)

    assert len(sink.record_exec_calls) == 1
    assert sink.record_exec_calls[0]["exit_code"] == 1


async def test_lsp_completion_tool_survives_a_broken_audit_sink():
    class BrokenSink:
        async def record_exec(self, **_kwargs):
            raise RuntimeError("downstream system is down")

    manager = _FakeSandboxManager(view_content="x", completion_items=[])
    spec = create_lsp_completion_tool_spec(
        session_id="session-1", sandbox_manager=manager, audit_sink=BrokenSink()
    )

    # Must not raise -- a broken AuditSink can never fail lsp_completion.
    result = await spec.handler(lsp_id="lsp_abc123", path="a.py", line=0, character=0)

    assert result == "(no completions)"


# ============================================================================
# lsp_stop
# ============================================================================


def test_create_lsp_stop_tool_spec_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_lsp_stop_tool_spec()


async def test_lsp_stop_tool_calls_manager():
    manager = _FakeSandboxManager()
    spec = create_lsp_stop_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(lsp_id="lsp_abc123")

    assert "lsp_abc123" in result
    assert manager.lsp_stop_calls == [{"session_id": "session-1", "lsp_id": "lsp_abc123"}]


async def test_lsp_stop_tool_rejects_empty_lsp_id():
    manager = _FakeSandboxManager()
    spec = create_lsp_stop_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(lsp_id="")

    assert result.startswith("Error")
    assert manager.lsp_stop_calls == []


# ============================================================================
# create_lsp_tool_specs bundle
# ============================================================================


def test_create_lsp_tool_specs_returns_all_three_in_order():
    manager = _FakeSandboxManager()
    specs = create_lsp_tool_specs(session_id="session-1", sandbox_manager=manager)

    assert [s.name for s in specs] == ["lsp_start", "lsp_completion", "lsp_stop"]
