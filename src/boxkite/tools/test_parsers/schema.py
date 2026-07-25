"""Framework-agnostic test-result schema (see docs/issue #123).

`TestRunResult` is the one shape every framework parser (pytest today;
jest/go-test/cargo-test later, see registry.py) must produce, so
`run_tests_tool.py` never needs to know which framework actually ran.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class TestFailure:
    """One failing/erroring test case, parsed from a framework's raw output."""

    # Not a pytest test class despite the name -- pytest's own collector
    # would otherwise warn about it (it starts with "Test" and has an
    # __init__, courtesy of @dataclass) when this module is imported from
    # a test file.
    __test__ = False

    file: Optional[str]
    line: Optional[int]
    name: str
    message: str


@dataclass(frozen=True)
class TestRunResult:
    """Structured result of one test run, common across every framework.

    Attributes:
        framework: The framework whose parser produced this result (e.g.
            "pytest"), or the framework the caller asked for even when no
            parser exists for it yet.
        parsed: False means the framework's output didn't match this
            parser's expected shape (or no parser exists for `framework`
            at all) -- `passed`/`failed`/`errors`/`failures`/
            `duration_seconds` are then all unset/zeroed and `raw_output`
            carries the full combined stdout+stderr instead, per issue
            #123's explicit "fall back to raw output, not a hard failure"
            requirement.
        exit_code: The underlying command's exit code -- kept even when
            parsed, since a 0 exit code with failed>0 (unusual, but some
            wrapper scripts do this) or a nonzero exit with failed==0
            (e.g. a coverage-threshold failure) is meaningful context an
            agent would otherwise have to guess at.
        passed / failed / errors: Counts. `errors` covers setup/collection
            errors, distinct from assertion failures.
        failures: Assertion/uncaught-exception failures with file/line/name/
            message detail. Collection errors are counted in `errors` but
            not currently itemized here (see pytest_parser.py's docstring).
        duration_seconds: Wall-clock time the framework itself reported for
            the run, or None if not found/not parsed.
        raw_output: Only populated when `parsed` is False -- omitted (left
            as None) on a successful parse to avoid wasting agent context
            on output that's already been extracted into structured fields.
    """

    # See TestFailure's __test__ note above -- same pytest-collection reason.
    __test__ = False

    framework: str
    parsed: bool
    exit_code: int
    passed: int = 0
    failed: int = 0
    errors: int = 0
    failures: list[TestFailure] = field(default_factory=list)
    duration_seconds: Optional[float] = None
    raw_output: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
