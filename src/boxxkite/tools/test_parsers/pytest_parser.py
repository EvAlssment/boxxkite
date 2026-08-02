"""Parses pytest's default-format terminal output into a TestRunResult.

Only the default (non-plugin) terminal reporter is handled -- no
`--tb=short`/`--tb=line` variants, no `pytest-json-report`/junit-xml output.
Narrowing to one output shape for the first framework keeps the regex
surface honest; widening it (or adding a `--tb` mode) is new parser work,
not a schema change (see schema.py's TestRunResult, which this returns).

Collection/setup errors (pytest's separate "ERRORS" section) are counted
into `TestRunResult.errors` via the summary line, but -- unlike FAILURES --
are not itemized into `failures` in this first pass; their traceback shape
differs enough from a normal test failure's that itemizing them well is
follow-on work, not something to fake with the same regex.

Raises ValueError on anything that doesn't look like pytest's terminal
output at all -- run_tests_tool.py catches that and falls back to raw
output (schema.py's TestRunResult.parsed=False), never a hard crash.
"""

from __future__ import annotations

import re

from .schema import TestFailure, TestRunResult

# A pytest section/summary divider: a run of "=" signs, a body, another run
# of "=" signs, e.g. "=== 1 failed, 2 passed in 0.05s ===" or
# "=================== FAILURES ===================".
_SECTION_HEADER_RE = re.compile(r"^=+\s*(?P<body>.+?)\s*=+\s*$", re.MULTILINE)

# One count fragment from a pytest summary line, e.g. "2 passed" / "1 failed".
_COUNT_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<label>passed|failed|error|errors|skipped|xfailed|xpassed)"
)

# Trailing duration on a summary line, e.g. "... in 0.05s".
_DURATION_RE = re.compile(r"\bin\s+(?P<seconds>[\d.]+)\s*s\b")

# A per-test header inside the FAILURES section, e.g.
# "___________________________ test_b ____________________________".
_FAILURE_HEADER_RE = re.compile(r"^_{3,}\s*(?P<name>.+?)\s*_{3,}$", re.MULTILINE)

# The "file:line: message" location pytest prints for a failure/traceback,
# e.g. "tests/test_foo.py:10: AssertionError".
_LOCATION_RE = re.compile(
    r"^(?P<file>[^\s:]+\.py):(?P<line>\d+):\s*(?P<message>.*)$", re.MULTILINE
)

# pytest's "E   <detail>" traceback lines (the assertion detail itself).
_ASSERTION_LINE_RE = re.compile(r"^E(?:[ \t]+(?P<message>.*))?$", re.MULTILINE)


def parse_pytest_output(output: str, exit_code: int = 0) -> TestRunResult:
    """Parse pytest's default terminal output.

    Args:
        output: Combined stdout+stderr from a pytest invocation.
        exit_code: The command's exit code, carried through unchanged.

    Returns:
        A parsed TestRunResult (`parsed=True`).

    Raises:
        ValueError: `output` doesn't contain a recognizable pytest summary
            line -- callers should catch this and fall back to raw output.
    """
    headers = list(_SECTION_HEADER_RE.finditer(output))
    if not headers:
        raise ValueError("no pytest section markers found in output")

    summary_body = headers[-1].group("body").strip()
    counts = _extract_counts(summary_body)
    if not counts and "no tests ran" not in summary_body.lower():
        raise ValueError(f"unrecognized pytest summary line: {summary_body!r}")

    duration_match = _DURATION_RE.search(summary_body)
    duration_seconds = float(duration_match.group("seconds")) if duration_match else None

    failures_section = _extract_section(output, headers, "FAILURES")
    failures = _parse_failure_blocks(failures_section) if failures_section else []

    return TestRunResult(
        framework="pytest",
        parsed=True,
        exit_code=exit_code,
        passed=counts.get("passed", 0),
        failed=counts.get("failed", 0),
        errors=counts.get("error", 0) + counts.get("errors", 0),
        failures=failures,
        duration_seconds=duration_seconds,
    )


def _extract_counts(summary_body: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for count_str, label in _COUNT_RE.findall(summary_body):
        counts[label] = counts.get(label, 0) + int(count_str)
    return counts


def _extract_section(output: str, headers: list[re.Match], name: str) -> str | None:
    for index, header in enumerate(headers):
        if header.group("body").strip().upper() == name.upper():
            start = header.end()
            end = headers[index + 1].start() if index + 1 < len(headers) else len(output)
            return output[start:end]
    return None


def _parse_failure_blocks(section_text: str) -> list[TestFailure]:
    block_headers = list(_FAILURE_HEADER_RE.finditer(section_text))
    failures = []
    for index, header in enumerate(block_headers):
        name = header.group("name").strip()
        start = header.end()
        end = block_headers[index + 1].start() if index + 1 < len(block_headers) else len(section_text)
        failures.append(_parse_one_failure(name, section_text[start:end]))
    return failures


def _parse_one_failure(name: str, block: str) -> TestFailure:
    location_matches = list(_LOCATION_RE.finditer(block))
    file_path = location_matches[-1].group("file") if location_matches else None
    line_no = int(location_matches[-1].group("line")) if location_matches else None

    assertion_lines = [
        (match.group("message") or "").rstrip()
        for match in _ASSERTION_LINE_RE.finditer(block)
    ]

    if assertion_lines:
        message = "\n".join(assertion_lines).strip()
    elif location_matches:
        message = location_matches[-1].group("message").strip()
    else:
        stripped = block.strip()
        message = stripped.splitlines()[-1] if stripped else "(no failure detail captured)"

    return TestFailure(file=file_path, line=line_no, name=name, message=message or "(empty)")
