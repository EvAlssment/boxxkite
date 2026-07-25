"""
Tests for boxkite.tools.browser_tools, in particular its
AuditSink.record_exec integration point.

Mirrors tests/test_node_interpreter_tool.py's pattern (mock SandboxManager,
assert the tool calls the right manager method, assert the optional
AuditSink is invoked/safely skipped as expected) and
tests/test_pty_tools.py's style of exercising the framework-agnostic
ToolSpec's `handler` directly rather than requiring the LangChain adapter.
"""

from uuid import uuid4

import pytest

from boxkite.tools.browser_tools import (
    create_browser_close_tool_spec,
    create_browser_exec_tool_spec,
    create_browser_navigate_tool_spec,
    create_browser_screenshot_tool_spec,
    create_browser_tool_specs,
)
from boxkite.tools.types import ToolImageResult

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(
        self,
        navigate_result=None,
        exec_result=None,
        screenshot_result=None,
        close_result=None,
    ):
        self.navigate_calls = []
        self.exec_calls = []
        self.screenshot_calls = []
        self.close_calls = []
        self.navigate_result = navigate_result or {
            "title": "Example",
            "url": "https://example.com",
            "status": 200,
            "error": None,
        }
        self.exec_result = exec_result or {"result": 42, "error": None}
        self.screenshot_result = screenshot_result or {
            "image_base64": "aGVsbG8=",
            "error": None,
        }
        self.close_result = close_result or {"status": "closed"}

    async def browser_navigate(self, session_id, url, wait_until="load", timeout_seconds=30):
        self.navigate_calls.append(
            {
                "session_id": session_id,
                "url": url,
                "wait_until": wait_until,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.navigate_result

    async def browser_exec(self, session_id, script, timeout_seconds=10):
        self.exec_calls.append(
            {"session_id": session_id, "script": script, "timeout_seconds": timeout_seconds}
        )
        return self.exec_result

    async def browser_screenshot(self, session_id, full_page=False):
        self.screenshot_calls.append({"session_id": session_id, "full_page": full_page})
        return self.screenshot_result

    async def browser_close(self, session_id):
        self.close_calls.append({"session_id": session_id})
        return self.close_result


class _RecordingAuditSink:
    def __init__(self):
        self.record_exec_calls = []

    async def record_exec(self, **kwargs):
        self.record_exec_calls.append(kwargs)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_create_browser_navigate_tool_spec_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_browser_navigate_tool_spec()


def test_create_browser_exec_tool_spec_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_browser_exec_tool_spec()


def test_create_browser_screenshot_tool_spec_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_browser_screenshot_tool_spec()


def test_create_browser_close_tool_spec_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_browser_close_tool_spec()


def test_create_browser_tool_specs_returns_all_four_in_design_doc_order():
    manager = _FakeSandboxManager()
    specs = create_browser_tool_specs(session_id="session-1", sandbox_manager=manager)
    assert [s.name for s in specs] == [
        "browser_navigate",
        "browser_exec",
        "browser_screenshot",
        "browser_close",
    ]


def test_browser_screenshot_tool_spec_is_marked_multimodal():
    manager = _FakeSandboxManager()
    spec = create_browser_screenshot_tool_spec(session_id="session-1", sandbox_manager=manager)
    assert spec.returns_multimodal is True


# ---------------------------------------------------------------------------
# browser_navigate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_browser_navigate_rejects_empty_url():
    manager = _FakeSandboxManager()
    spec = create_browser_navigate_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(url="   ")

    assert result == "Error: Empty url provided"
    assert manager.navigate_calls == []


@pytest.mark.asyncio
async def test_browser_navigate_rejects_invalid_wait_until():
    manager = _FakeSandboxManager()
    spec = create_browser_navigate_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(url="https://example.com", wait_until="instant")

    assert result.startswith("Error:")
    assert manager.navigate_calls == []


@pytest.mark.asyncio
async def test_browser_navigate_calls_manager_and_returns_summary():
    manager = _FakeSandboxManager()
    spec = create_browser_navigate_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(url="https://example.com", timeout_seconds=15)

    assert len(manager.navigate_calls) == 1
    assert manager.navigate_calls[0]["url"] == "https://example.com"
    assert manager.navigate_calls[0]["timeout_seconds"] == 15
    assert "Example" in result
    assert "https://example.com" in result
    assert "200" in result


@pytest.mark.asyncio
async def test_browser_navigate_reports_application_level_error_without_raising():
    manager = _FakeSandboxManager(
        navigate_result={"title": None, "url": None, "status": None, "error": "net::ERR_NAME_NOT_RESOLVED"}
    )
    spec = create_browser_navigate_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(url="https://does-not-exist.invalid")

    assert result.startswith("Error:")
    assert "ERR_NAME_NOT_RESOLVED" in result


@pytest.mark.asyncio
async def test_browser_navigate_mirrors_successful_call_to_audit_sink():
    manager = _FakeSandboxManager()
    sink = _RecordingAuditSink()
    org_id = uuid4()
    work_item_id = uuid4()

    spec = create_browser_navigate_tool_spec(
        session_id="session-1",
        sandbox_manager=manager,
        organization_id=org_id,
        work_item_id=work_item_id,
        agent_name="researcher",
        audit_sink=sink,
    )

    await spec.handler(url="https://example.com")

    assert len(sink.record_exec_calls) == 1
    call = sink.record_exec_calls[0]
    assert call["organization_id"] == org_id
    assert call["work_item_id"] == work_item_id
    assert call["session_id"] == "session-1"
    assert call["agent_name"] == "researcher"
    assert "https://example.com" in call["command"]
    assert call["exit_code"] == 0
    assert isinstance(call["duration_ms"], int)


@pytest.mark.asyncio
async def test_browser_navigate_mirrors_failed_call_with_nonzero_exit_code():
    manager = _FakeSandboxManager(
        navigate_result={"title": None, "url": None, "status": None, "error": "boom"}
    )
    sink = _RecordingAuditSink()
    spec = create_browser_navigate_tool_spec(
        session_id="session-1", sandbox_manager=manager, audit_sink=sink
    )

    await spec.handler(url="https://example.com")

    assert sink.record_exec_calls[0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_browser_navigate_survives_a_broken_audit_sink():
    class BrokenSink:
        async def record_exec(self, **_kwargs):
            raise RuntimeError("downstream system is down")

    manager = _FakeSandboxManager()
    spec = create_browser_navigate_tool_spec(
        session_id="session-1", sandbox_manager=manager, audit_sink=BrokenSink()
    )

    # Must not raise -- a broken AuditSink can never fail browser_navigate.
    result = await spec.handler(url="https://example.com")
    assert "Example" in result


# ---------------------------------------------------------------------------
# browser_exec
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_browser_exec_rejects_empty_script():
    manager = _FakeSandboxManager()
    spec = create_browser_exec_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(script="   ")

    assert result == "Error: Empty script provided"
    assert manager.exec_calls == []


@pytest.mark.asyncio
async def test_browser_exec_returns_the_scripts_result():
    manager = _FakeSandboxManager(exec_result={"result": 42, "error": None})
    spec = create_browser_exec_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(script="21 * 2")

    assert result == "42"
    assert manager.exec_calls[0]["script"] == "21 * 2"


@pytest.mark.asyncio
async def test_browser_exec_reports_thrown_errors_without_raising():
    manager = _FakeSandboxManager(exec_result={"result": None, "error": "boom"})
    spec = create_browser_exec_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(script="throw new Error('boom')")

    assert result == "Error: boom"


@pytest.mark.asyncio
async def test_browser_exec_mirrors_the_script_itself_to_audit_sink():
    manager = _FakeSandboxManager(exec_result={"result": 1, "error": None})
    sink = _RecordingAuditSink()
    spec = create_browser_exec_tool_spec(
        session_id="session-1", sandbox_manager=manager, audit_sink=sink
    )

    await spec.handler(script="document.title")

    assert sink.record_exec_calls[0]["command"] == "document.title"
    assert sink.record_exec_calls[0]["exit_code"] == 0


# ---------------------------------------------------------------------------
# browser_screenshot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_browser_screenshot_returns_a_tool_image_result_on_success():
    manager = _FakeSandboxManager(screenshot_result={"image_base64": "aGVsbG8=", "error": None})
    spec = create_browser_screenshot_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(full_page=True)

    assert isinstance(result, ToolImageResult)
    assert result.base64_data == "aGVsbG8="
    assert result.mime_type == "image/png"
    assert manager.screenshot_calls[0]["full_page"] is True


@pytest.mark.asyncio
async def test_browser_screenshot_returns_error_string_on_failure_not_a_corrupt_image():
    manager = _FakeSandboxManager(
        screenshot_result={
            "image_base64": None,
            "error": "Screenshot is 99999999 bytes, exceeding the 5242880-byte cap",
        }
    )
    spec = create_browser_screenshot_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler(full_page=True)

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "exceeding" in result


@pytest.mark.asyncio
async def test_browser_screenshot_audit_record_never_contains_the_image_bytes():
    """docs/BROWSER-EXEC-DESIGN.md §6: the audit trail must record enough to
    reconstruct what happened, never the payload itself."""
    manager = _FakeSandboxManager(
        screenshot_result={"image_base64": "A" * 5000, "error": None}
    )
    sink = _RecordingAuditSink()
    spec = create_browser_screenshot_tool_spec(
        session_id="session-1", sandbox_manager=manager, audit_sink=sink
    )

    await spec.handler(full_page=False)

    assert len(sink.record_exec_calls) == 1
    command_logged = sink.record_exec_calls[0]["command"]
    assert "A" * 5000 not in command_logged
    assert len(command_logged) < 200


# ---------------------------------------------------------------------------
# browser_close
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_browser_close_calls_manager_and_reports_status():
    manager = _FakeSandboxManager(close_result={"status": "closed"})
    spec = create_browser_close_tool_spec(session_id="session-1", sandbox_manager=manager)

    result = await spec.handler()

    assert "closed" in result
    assert manager.close_calls == [{"session_id": "session-1"}]
