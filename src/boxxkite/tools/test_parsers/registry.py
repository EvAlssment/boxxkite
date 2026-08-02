"""Framework-name -> parser registry, plus best-effort detection from a
command string.

This is the one place `run_tests_tool.py` needs to touch to add jest,
go test, or cargo test later: write a `parse_<x>_output(output, exit_code)
-> TestRunResult` function (see pytest_parser.py for the shape) and add one
entry to `_PARSERS` and `_DETECTION_PATTERNS` below. Nothing else in this
package -- the tool, the schema, run_tests_tool.py's handler -- changes.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from .pytest_parser import parse_pytest_output
from .schema import TestRunResult

ParserFn = Callable[[str, int], TestRunResult]

# Only pytest is implemented so far -- see module docstring for how to add
# jest/go-test/cargo-test parsers here without touching the tool itself.
_PARSERS: dict[str, ParserFn] = {
    "pytest": parse_pytest_output,
}

# Best-effort framework detection from the command string, used only when
# the caller doesn't pass an explicit `framework` argument. Order matters:
# first pattern to match wins.
_DETECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pytest", re.compile(r"\bpy\.?test\b")),
]

SUPPORTED_FRAMEWORKS: tuple[str, ...] = tuple(_PARSERS.keys())


def get_parser(framework: str) -> Optional[ParserFn]:
    """Return the parser function for `framework`, or None if unsupported."""
    return _PARSERS.get(framework.strip().lower())


def detect_framework(command: str) -> str:
    """Best-effort framework name guess from a shell command string.

    Falls back to "pytest" (the only implemented parser) rather than
    raising or returning an empty string -- run_tests_tool.py always needs
    *some* framework name to attempt, and pytest is this Python-first
    repo's own most common case (see issue #123).
    """
    for framework, pattern in _DETECTION_PATTERNS:
        if pattern.search(command):
            return framework
    return "pytest"
