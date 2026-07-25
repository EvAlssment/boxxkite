"""
Tests for boxkite.tools.search_tools (ls, glob, grep LangChain tools).

Mirrors tests/test_tool_factory.py's pattern: mock SandboxManager, assert
the tool calls the right manager method with the right args, and assert
error paths return a string instead of raising.
"""

import pytest

from boxkite.tools.search_tools import (
    create_glob_tool,
    create_grep_tool,
    create_ls_tool,
    create_watch_directory_tool_spec,
)

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(self):
        self.ls_calls = []
        self.glob_calls = []
        self.grep_calls = []
        self.ls_result = []
        self.glob_result = []
        self.grep_result = {"matches": [], "error": None, "truncated": False}
        self.raise_error = None
        self.watch_calls = []
        self.watch_result = {"changes": [], "timed_out": True}

    async def watch_directory(self, session_id, path="/", timeout_seconds=10.0):
        if self.raise_error:
            raise self.raise_error
        self.watch_calls.append(
            {"session_id": session_id, "path": path, "timeout_seconds": timeout_seconds}
        )
        return self.watch_result

    async def ls(self, session_id, path="/"):
        if self.raise_error:
            raise self.raise_error
        self.ls_calls.append({"session_id": session_id, "path": path})
        return self.ls_result

    async def glob(self, session_id, pattern, path="/"):
        if self.raise_error:
            raise self.raise_error
        self.glob_calls.append({"session_id": session_id, "pattern": pattern, "path": path})
        return self.glob_result

    async def grep(self, session_id, pattern, path="/", glob=None, max_matches=500):
        if self.raise_error:
            raise self.raise_error
        self.grep_calls.append(
            {
                "session_id": session_id,
                "pattern": pattern,
                "path": path,
                "glob": glob,
                "max_matches": max_matches,
            }
        )
        return self.grep_result


def test_create_ls_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_ls_tool()


def test_create_glob_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_glob_tool()


def test_create_grep_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_grep_tool()


@pytest.mark.asyncio
async def test_ls_calls_manager_ls_with_default_path():
    manager = _FakeSandboxManager()
    manager.ls_result = [{"path": "/a.txt", "is_dir": False, "size": 10}]
    tool = create_ls_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({})

    assert manager.ls_calls == [{"session_id": "session-1", "path": "/"}]
    assert "/a.txt" in result


@pytest.mark.asyncio
async def test_ls_calls_manager_ls_with_explicit_path():
    manager = _FakeSandboxManager()
    tool = create_ls_tool(sandbox_manager=manager, session_id="session-1")

    await tool.ainvoke({"path": "reports"})

    assert manager.ls_calls == [{"session_id": "session-1", "path": "reports"}]


@pytest.mark.asyncio
async def test_ls_returns_error_string_on_exception():
    manager = _FakeSandboxManager()
    manager.raise_error = RuntimeError("sidecar unreachable")
    tool = create_ls_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"path": "/"})

    assert isinstance(result, str)
    assert "Error listing directory" in result
    assert "sidecar unreachable" in result


@pytest.mark.asyncio
async def test_glob_calls_manager_glob_with_pattern_and_path():
    manager = _FakeSandboxManager()
    manager.glob_result = [{"path": "/workspace/a.py"}]
    tool = create_glob_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"pattern": "**/*.py", "path": "/workspace"})

    assert manager.glob_calls == [
        {"session_id": "session-1", "pattern": "**/*.py", "path": "/workspace"}
    ]
    assert "a.py" in result


@pytest.mark.asyncio
async def test_glob_requires_a_pattern():
    manager = _FakeSandboxManager()
    tool = create_glob_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"pattern": "  "})

    assert result == "Error: pattern is required"
    assert manager.glob_calls == []


@pytest.mark.asyncio
async def test_glob_returns_error_string_on_exception():
    manager = _FakeSandboxManager()
    manager.raise_error = RuntimeError("sidecar unreachable")
    tool = create_glob_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"pattern": "*.py"})

    assert isinstance(result, str)
    assert "Error searching for files" in result


@pytest.mark.asyncio
async def test_glob_reports_no_matches():
    manager = _FakeSandboxManager()
    manager.glob_result = []
    tool = create_glob_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"pattern": "*.rs"})

    assert "No files matching" in result


@pytest.mark.asyncio
async def test_grep_calls_manager_grep_with_all_args():
    manager = _FakeSandboxManager()
    manager.grep_result = {
        "matches": [{"path": "/workspace/a.py", "line": 3, "text": "import pandas"}],
        "error": None,
        "truncated": False,
    }
    tool = create_grep_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke(
        {"pattern": "import pandas", "path": "/workspace", "glob": "*.py", "max_matches": 10}
    )

    assert manager.grep_calls == [
        {
            "session_id": "session-1",
            "pattern": "import pandas",
            "path": "/workspace",
            "glob": "*.py",
            "max_matches": 10,
        }
    ]
    assert "a.py:3" in result
    assert "import pandas" in result


@pytest.mark.asyncio
async def test_grep_requires_a_pattern():
    manager = _FakeSandboxManager()
    tool = create_grep_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"pattern": ""})

    assert result == "Error: pattern is required"
    assert manager.grep_calls == []


@pytest.mark.asyncio
async def test_grep_returns_error_string_on_exception():
    manager = _FakeSandboxManager()
    manager.raise_error = RuntimeError("sidecar unreachable")
    tool = create_grep_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"pattern": "TODO"})

    assert isinstance(result, str)
    assert "Error searching file contents" in result


@pytest.mark.asyncio
async def test_grep_surfaces_manager_reported_error_without_raising():
    manager = _FakeSandboxManager()
    manager.grep_result = {"matches": [], "error": "invalid regex", "truncated": False}
    tool = create_grep_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"pattern": "("})

    assert isinstance(result, str)
    assert "invalid regex" in result


@pytest.mark.asyncio
async def test_grep_reports_truncation():
    manager = _FakeSandboxManager()
    manager.grep_result = {
        "matches": [{"path": "/a.py", "line": 1, "text": "x"}],
        "error": None,
        "truncated": True,
    }
    tool = create_grep_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"pattern": "x"})

    assert "truncated" in result


def test_create_watch_directory_tool_spec_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_watch_directory_tool_spec()


@pytest.mark.asyncio
async def test_watch_directory_calls_manager_with_default_args():
    manager = _FakeSandboxManager()
    spec = create_watch_directory_tool_spec(sandbox_manager=manager, session_id="session-1")

    await spec.handler()

    assert manager.watch_calls == [
        {"session_id": "session-1", "path": "/", "timeout_seconds": 10.0}
    ]


@pytest.mark.asyncio
async def test_watch_directory_reports_changes():
    manager = _FakeSandboxManager()
    manager.watch_result = {
        "changes": [{"path": "output.txt", "event": "created"}],
        "timed_out": False,
    }
    spec = create_watch_directory_tool_spec(sandbox_manager=manager, session_id="session-1")

    result = await spec.handler(path="/workspace", timeout_seconds=5)

    assert "created" in result
    assert "output.txt" in result


@pytest.mark.asyncio
async def test_watch_directory_reports_timeout_with_no_changes():
    manager = _FakeSandboxManager()
    manager.watch_result = {"changes": [], "timed_out": True}
    spec = create_watch_directory_tool_spec(sandbox_manager=manager, session_id="session-1")

    result = await spec.handler()

    assert "No changes" in result


@pytest.mark.asyncio
async def test_watch_directory_returns_error_string_on_exception():
    manager = _FakeSandboxManager()
    manager.raise_error = RuntimeError("boom")
    spec = create_watch_directory_tool_spec(sandbox_manager=manager, session_id="session-1")

    result = await spec.handler()

    assert "Error" in result
