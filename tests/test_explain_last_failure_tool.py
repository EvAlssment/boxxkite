"""The explain_last_failure tool layer (GitHub issue #77)."""

from uuid import uuid4

import pytest

from boxxkite.tools import create_sandbox_tools
from boxxkite.tools.explain_last_failure_tool import (
    _format,
    create_explain_last_failure_tool_spec,
)

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(self, result=None):
        self.result = result or {}
        self.calls = []

    async def explain_last_failure(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_explain_last_failure_is_not_registered_by_default():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="s1",
    )
    assert "explain_last_failure" not in {t.name for t in tools}


def test_explain_last_failure_is_registered_when_enabled():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="s1",
        enable_explain_last_failure=True,
    )
    assert "explain_last_failure" in {t.name for t in tools}


def test_spec_requires_a_manager_or_runtime():
    with pytest.raises(ValueError):
        create_explain_last_failure_tool_spec()


@pytest.mark.asyncio
async def test_handler_forwards_the_scan_toggle():
    manager = _FakeSandboxManager({"found": False, "notes": ["nothing"]})
    spec = create_explain_last_failure_tool_spec(sandbox_manager=manager, session_id="s1")

    await spec.handler(include_touched_files=False)

    assert manager.calls[0]["include_touched_files"] is False


@pytest.mark.asyncio
async def test_handler_returns_an_error_string_rather_than_raising():
    class _Boom:
        async def explain_last_failure(self, **kwargs):
            raise RuntimeError("sidecar unreachable")

    spec = create_explain_last_failure_tool_spec(sandbox_manager=_Boom(), session_id="s1")
    out = await spec.handler()

    assert "Error fetching last failure" in out
    assert "sidecar unreachable" in out


def test_no_failure_renders_as_a_plain_statement():
    out = _format({"found": False, "notes": ["No command has failed in this session yet."]})
    assert "No command has failed" in out


def test_a_failure_renders_command_streams_and_touched_files():
    out = _format({
        "found": True,
        "command": "pytest -q",
        "exit_code": 1,
        "stdout": "collected 3 items\n",
        "stderr": "ImportError: no module named app\n",
        "duration_seconds": 2.5,
        "touched_files": [{"path": "/w/.coverage", "size_bytes": 512}],
        "notes": ["deleted files cannot be detected this way"],
    })

    assert "exit 1" in out
    assert "2.50s" in out
    assert "pytest -q" in out
    assert "ImportError" in out
    assert "/w/.coverage (512 bytes)" in out
    assert "note: deleted files cannot be detected this way" in out


def test_empty_streams_are_labelled_rather_than_left_blank():
    """A blank gap under "stderr:" reads as a rendering bug, not as silence."""
    out = _format({
        "found": True, "command": "true", "exit_code": 1,
        "stdout": "", "stderr": "", "duration_seconds": 0.1,
    })
    assert out.count("(empty)") == 2


def test_no_touched_files_says_none_detected_not_nothing():
    out = _format({
        "found": True, "command": "x", "exit_code": 1,
        "stdout": "a", "stderr": "b", "duration_seconds": 0.1,
        "touched_files": [],
    })
    assert "none detected" in out
