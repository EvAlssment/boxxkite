"""
Tests for boxkite.tools.run_tests_tool (run_tests LangChain tool).

Mirrors tests/test_bash_tool.py's/tests/test_process_tools.py's pattern:
mock SandboxManager, assert the tool calls the right manager method, and
assert the response is the structured JSON schema from
boxkite.tools.test_parsers.schema.TestRunResult -- covering a passing run,
a failing run with specific failure details parsed out, and a malformed/
unparseable-output fallback that must not crash.
"""

import json

import pytest

from boxkite.tools.run_tests_tool import create_run_tests_tool

pytestmark = pytest.mark.pr

PASSING_PYTEST_OUTPUT = """\
============================= test session starts ==============================
collected 2 items

tests/test_foo.py::test_a PASSED                                       [ 50%]
tests/test_foo.py::test_b PASSED                                       [100%]

============================== 2 passed in 0.05s ===============================
"""

FAILING_PYTEST_OUTPUT = """\
============================= test session starts ==============================
collected 2 items

tests/test_foo.py::test_a PASSED                                       [ 50%]
tests/test_foo.py::test_b FAILED                                       [100%]

=================================== FAILURES ====================================
___________________________________ test_b _____________________________________

    def test_b():
>       assert 1 == 2
E       assert 1 == 2

tests/test_foo.py:10: AssertionError
=========================== short test summary info ============================
FAILED tests/test_foo.py::test_b - AssertionError: assert 1 == 2
========================= 1 failed, 1 passed in 0.07s ===========================
"""

MALFORMED_OUTPUT = "Segmentation fault (core dumped)\n"


class _FakeSandboxManager:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.execute_calls = []
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    async def execute(self, session_id, command, timeout, secret_env=None):
        self.execute_calls.append({"session_id": session_id, "command": command, "timeout": timeout})
        return {"exit_code": self.exit_code, "stdout": self.stdout, "stderr": self.stderr}


def test_create_run_tests_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_run_tests_tool()


@pytest.mark.asyncio
async def test_run_tests_rejects_an_empty_command():
    tool = create_run_tests_tool(sandbox_manager=_FakeSandboxManager(), session_id="session-1")

    result = await tool.ainvoke({"command": "   "})

    assert result == "Error: Empty command provided"


@pytest.mark.asyncio
async def test_run_tests_parses_a_passing_pytest_run():
    manager = _FakeSandboxManager(exit_code=0, stdout=PASSING_PYTEST_OUTPUT)
    tool = create_run_tests_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"command": "pytest tests/"})
    parsed = json.loads(result)

    assert len(manager.execute_calls) == 1
    assert manager.execute_calls[0]["command"] == "pytest tests/"
    assert parsed == {
        "framework": "pytest",
        "parsed": True,
        "exit_code": 0,
        "passed": 2,
        "failed": 0,
        "errors": 0,
        "failures": [],
        "duration_seconds": 0.05,
        "raw_output": None,
    }


@pytest.mark.asyncio
async def test_run_tests_parses_a_failing_pytest_run_with_failure_details():
    manager = _FakeSandboxManager(exit_code=1, stdout=FAILING_PYTEST_OUTPUT)
    tool = create_run_tests_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"command": "pytest tests/"})
    parsed = json.loads(result)

    assert parsed["framework"] == "pytest"
    assert parsed["parsed"] is True
    assert parsed["exit_code"] == 1
    assert parsed["passed"] == 1
    assert parsed["failed"] == 1
    assert parsed["errors"] == 0
    assert parsed["duration_seconds"] == 0.07
    assert parsed["failures"] == [
        {
            "file": "tests/test_foo.py",
            "line": 10,
            "name": "test_b",
            "message": "assert 1 == 2",
        }
    ]


@pytest.mark.asyncio
async def test_run_tests_falls_back_to_raw_output_on_malformed_pytest_output():
    manager = _FakeSandboxManager(exit_code=139, stdout=MALFORMED_OUTPUT)
    tool = create_run_tests_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"command": "pytest tests/"})
    parsed = json.loads(result)

    assert parsed["parsed"] is False
    assert parsed["exit_code"] == 139
    assert parsed["passed"] == 0
    assert parsed["failed"] == 0
    assert parsed["failures"] == []
    assert parsed["duration_seconds"] is None
    assert parsed["raw_output"] == MALFORMED_OUTPUT


@pytest.mark.asyncio
async def test_run_tests_falls_back_to_raw_output_for_an_unsupported_framework():
    manager = _FakeSandboxManager(exit_code=0, stdout="PASS ok (3 tests)\n")
    tool = create_run_tests_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"command": "go test ./...", "framework": "go-test"})
    parsed = json.loads(result)

    assert parsed["framework"] == "go-test"
    assert parsed["parsed"] is False
    assert "no parser available" in parsed["note"].lower()
    assert "PASS ok" in parsed["raw_output"]


@pytest.mark.asyncio
async def test_run_tests_auto_detects_pytest_framework_from_command():
    manager = _FakeSandboxManager(exit_code=0, stdout=PASSING_PYTEST_OUTPUT)
    tool = create_run_tests_tool(sandbox_manager=manager, session_id="session-1")

    result = await tool.ainvoke({"command": "python -m pytest tests/ -v"})
    parsed = json.loads(result)

    assert parsed["framework"] == "pytest"
    assert parsed["parsed"] is True


@pytest.mark.asyncio
async def test_run_tests_blocks_a_command_not_on_the_agent_whitelist():
    manager = _FakeSandboxManager()
    tool = create_run_tests_tool(
        sandbox_manager=manager,
        session_id="session-1",
        allowed_commands=["pytest"],
    )

    result = await tool.ainvoke({"command": "rm -rf /"})

    assert len(manager.execute_calls) == 0
    assert "not" in result.lower() or "block" in result.lower() or "allow" in result.lower()


@pytest.mark.asyncio
async def test_run_tests_blocks_an_os_environ_leakage_attempt():
    # is_blocked_command's package-install blocklist is deliberately disabled
    # (see preset_packages.py's BLOCKED_COMMANDS comments -- false positives
    # on generated docs), but the os.environ leakage check is still active;
    # this is the one still-enforced case that exercises the same
    # is_blocked_command() wiring bash_tool uses.
    manager = _FakeSandboxManager()
    tool = create_run_tests_tool(sandbox_manager=manager, session_id="session-1")

    await tool.ainvoke({"command": "python3 -c 'print(dict(os.environ))' && pytest"})

    assert len(manager.execute_calls) == 0
