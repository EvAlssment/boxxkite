"""The workspace_diff tool layer (GitHub issue #71).

Two things matter here beyond "it calls the manager": that the tool stays
opt-in (every tool added since the flag-new-surface-off-by-default convention
does), and that its rendered output never lets a truncated or capped result
read as "nothing else changed".
"""

from uuid import uuid4

import pytest

from boxxkite.tools import create_sandbox_tools
from boxxkite.tools.workspace_diff_tool import (
    _format,
    create_workspace_diff_tool_spec,
)

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(self, result=None):
        self.result = result or {}
        self.calls = []

    async def workspace_diff(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


# ── registration ──────────────────────────────────────────────────────────


def test_workspace_diff_is_not_registered_by_default():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="s1",
    )
    assert "workspace_diff" not in {t.name for t in tools}


def test_workspace_diff_is_registered_when_enabled():
    tools = create_sandbox_tools(
        sandbox_manager=_FakeSandboxManager(),
        organization_id=uuid4(),
        work_item_id=uuid4(),
        session_id="s1",
        enable_workspace_diff=True,
    )
    assert "workspace_diff" in {t.name for t in tools}


def test_spec_requires_a_manager_or_runtime():
    with pytest.raises(ValueError):
        create_workspace_diff_tool_spec()


# ── handler wiring ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_forwards_its_arguments_to_the_manager():
    manager = _FakeSandboxManager({"checkpoint": "wsdiff-1", "baseline": True, "files_scanned": 3})
    spec = create_workspace_diff_tool_spec(sandbox_manager=manager, session_id="s1")

    await spec.handler(path="/src", checkpoint="wsdiff-0", exclude=["logs"], include_diffs=False)

    call = manager.calls[0]
    assert call["path"] == "/src"
    assert call["checkpoint"] == "wsdiff-0"
    assert call["exclude"] == ["logs"]
    assert call["include_diffs"] is False


@pytest.mark.asyncio
async def test_handler_returns_an_error_string_rather_than_raising():
    class _Boom:
        async def workspace_diff(self, **kwargs):
            raise RuntimeError("sidecar unreachable")

    spec = create_workspace_diff_tool_spec(sandbox_manager=_Boom(), session_id="s1")
    out = await spec.handler()

    assert "Error diffing workspace" in out
    assert "sidecar unreachable" in out


# ── rendering ─────────────────────────────────────────────────────────────


def test_baseline_is_rendered_as_a_baseline_not_as_no_changes():
    """"No changes" and "I have never looked before" are different facts and
    an agent that confuses them will skip work it needed to do."""
    out = _format(
        {"checkpoint": "wsdiff-1", "baseline": True, "files_scanned": 12, "notes": []}, "/"
    )
    assert "Baseline established" in out
    assert "12 files" in out
    assert "wsdiff-1" in out


def test_no_changes_is_rendered_plainly():
    out = _format({"checkpoint": "wsdiff-2", "baseline": False, "changes": []}, "/")
    assert "No changes" in out


def test_changes_are_rendered_with_counts_and_diffs():
    out = _format(
        {
            "checkpoint": "wsdiff-3",
            "changes": [
                {"path": "/w/a.py", "change": "modified", "size_delta_bytes": 12,
                 "diff": "--- a\n+++ b\n-old\n+new\n"},
                {"path": "/w/b.py", "change": "added", "size_delta_bytes": 40},
                {"path": "/w/c.py", "change": "removed", "size_delta_bytes": -7},
            ],
        },
        "/",
    )
    assert "1 added, 1 modified, 1 removed" in out
    assert "+new" in out
    assert "(-7 bytes)" in out


def test_a_truncated_scan_is_announced_loudly():
    """A trimmed list that looks complete is the one wrong answer this tool
    must never give."""
    out = _format(
        {
            "checkpoint": "wsdiff-4",
            "changes": [{"path": "/w/a.py", "change": "added", "size_delta_bytes": 1}],
            "truncated": True,
            "notes": ["stopped after 20000 files; results are partial"],
        },
        "/",
    )
    assert "WARNING" in out
    assert "may be incomplete" in out
    assert "partial" in out


def test_a_missing_diff_states_its_reason():
    out = _format(
        {
            "checkpoint": "wsdiff-5",
            "changes": [{
                "path": "/w/img.png", "change": "modified",
                "size_delta_bytes": 100, "binary": True,
                "diff_omitted_reason": "binary file",
            }],
        },
        "/",
    )
    assert "no diff: binary file" in out
