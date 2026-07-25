"""
Tests for boxkite.tools.test_parsers.pytest_parser (parse_pytest_output).

Covers the three cases run_tests_tool.py relies on: a clean passing run, a
failing run with specific failure detail extracted, and malformed/
unrecognized output raising ValueError so the tool layer can fall back to
raw output instead of crashing.
"""

import pytest

from boxkite.tools.test_parsers.pytest_parser import parse_pytest_output
from boxkite.tools.test_parsers.schema import TestFailure, TestRunResult

pytestmark = pytest.mark.pr

PASSING_OUTPUT = """\
============================= test session starts ==============================
platform linux -- Python 3.11.9, pytest-8.2.0, pluggy-1.5.0
rootdir: /workspace
collected 2 items

tests/test_foo.py::test_a PASSED                                       [ 50%]
tests/test_foo.py::test_b PASSED                                       [100%]

============================== 2 passed in 0.05s ===============================
"""

FAILING_OUTPUT = """\
============================= test session starts ==============================
platform linux -- Python 3.11.9, pytest-8.2.0, pluggy-1.5.0
rootdir: /workspace
collected 3 items

tests/test_foo.py::test_a PASSED                                       [ 33%]
tests/test_foo.py::test_b FAILED                                       [ 66%]
tests/test_foo.py::test_c PASSED                                       [100%]

=================================== FAILURES ====================================
___________________________________ test_b _____________________________________

    def test_b():
>       assert 1 == 2
E       assert 1 == 2

tests/test_foo.py:10: AssertionError
=========================== short test summary info ============================
FAILED tests/test_foo.py::test_b - AssertionError: assert 1 == 2
========================= 1 failed, 2 passed in 0.07s ===========================
"""

MULTI_FAILURE_OUTPUT = """\
============================= test session starts ==============================
collected 2 items

tests/test_bar.py::test_x FAILED                                       [ 50%]
tests/test_bar.py::test_y FAILED                                       [100%]

=================================== FAILURES ====================================
___________________________________ test_x _____________________________________

    def test_x():
>       raise ValueError("boom")
E       ValueError: boom

tests/test_bar.py:4: ValueError
___________________________________ test_y _____________________________________

    def test_y():
>       assert False
E       assert False

tests/test_bar.py:8: AssertionError
=========================== short test summary info ============================
FAILED tests/test_bar.py::test_x - ValueError: boom
FAILED tests/test_bar.py::test_y - AssertionError
=========================== 2 failed in 0.03s ====================================
"""

MALFORMED_OUTPUT = "Segmentation fault (core dumped)\n"

EMPTY_OUTPUT = ""

NO_TESTS_RAN_OUTPUT = """\
============================= test session starts ==============================
collected 0 items

============================ no tests ran in 0.00s ==============================
"""


def test_parses_a_clean_passing_run():
    result = parse_pytest_output(PASSING_OUTPUT, exit_code=0)

    assert result == TestRunResult(
        framework="pytest",
        parsed=True,
        exit_code=0,
        passed=2,
        failed=0,
        errors=0,
        failures=[],
        duration_seconds=0.05,
    )


def test_parses_a_failing_run_with_specific_failure_details():
    result = parse_pytest_output(FAILING_OUTPUT, exit_code=1)

    assert result.framework == "pytest"
    assert result.parsed is True
    assert result.exit_code == 1
    assert result.passed == 2
    assert result.failed == 1
    assert result.errors == 0
    assert result.duration_seconds == 0.07
    assert result.failures == [
        TestFailure(
            file="tests/test_foo.py",
            line=10,
            name="test_b",
            message="assert 1 == 2",
        )
    ]


def test_parses_multiple_failures_in_one_run():
    result = parse_pytest_output(MULTI_FAILURE_OUTPUT, exit_code=1)

    assert result.failed == 2
    assert [f.name for f in result.failures] == ["test_x", "test_y"]
    assert result.failures[0] == TestFailure(
        file="tests/test_bar.py", line=4, name="test_x", message="ValueError: boom"
    )
    assert result.failures[1] == TestFailure(
        file="tests/test_bar.py", line=8, name="test_y", message="assert False"
    )


def test_no_tests_ran_is_parsed_as_zero_counts_not_an_error():
    result = parse_pytest_output(NO_TESTS_RAN_OUTPUT, exit_code=0)

    assert result.parsed is True
    assert (result.passed, result.failed, result.errors) == (0, 0, 0)
    assert result.duration_seconds == 0.0


def test_malformed_output_raises_value_error_for_the_tool_layer_to_catch():
    with pytest.raises(ValueError):
        parse_pytest_output(MALFORMED_OUTPUT, exit_code=139)


def test_empty_output_raises_value_error():
    with pytest.raises(ValueError):
        parse_pytest_output(EMPTY_OUTPUT, exit_code=1)
