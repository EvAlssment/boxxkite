"""Framework-agnostic test-output parsing (see run_tests_tool.py).

- schema.py: the common `TestRunResult`/`TestFailure` shape every framework
  parser produces.
- registry.py: framework name -> parser function, plus best-effort
  detection from a command string.
- pytest_parser.py: the only parser implemented so far. See registry.py's
  docstring for how to add jest/go-test/cargo-test later.
"""

from .registry import SUPPORTED_FRAMEWORKS, detect_framework, get_parser
from .schema import TestFailure, TestRunResult

__all__ = [
    "TestFailure",
    "TestRunResult",
    "SUPPORTED_FRAMEWORKS",
    "detect_framework",
    "get_parser",
]
