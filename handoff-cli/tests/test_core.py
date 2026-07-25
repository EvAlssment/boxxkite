from __future__ import annotations

from pathlib import Path

import pytest

from boxkite_handoff.core import HandoffError, most_recent_by_mtime, validate_identifier


@pytest.mark.parametrize(
    "value",
    [
        "abc123",
        "ses_1",
        "rollout-2026-07-20T10-00-00-abcd1234-ab12-cd34-ef56-0123456789ab",
        "a.b_c-d",
    ],
)
def test_validate_identifier_accepts_plain_identifiers(value: str) -> None:
    assert validate_identifier(value, what="session id") == value


@pytest.mark.parametrize(
    "value",
    [
        "x'; touch pwned #",
        "x`touch pwned`",
        "x$(touch pwned)",
        "x && touch pwned",
        "x; touch pwned",
        "x | touch pwned",
        "has space",
        "quote'inside",
        "abc123\n",
        "abc123\nrm -rf ~",
    ],
)
def test_validate_identifier_rejects_anything_with_shell_metacharacters(value: str) -> None:
    """This is the regression test for a real command-injection finding:
    orchestrator.py types resume_command into the takeover shell unquoted,
    so any locally-discovered identifier that reaches it must be rejected
    here first if it isn't a plain, safe token. Includes a trailing-newline
    case specifically -- `re.match` with a `$`-anchored pattern would wrongly
    accept a string ending in exactly one newline; `fullmatch` must not."""
    with pytest.raises(HandoffError):
        validate_identifier(value, what="session id")


def test_most_recent_by_mtime_picks_the_newest_file(tmp_path: Path) -> None:
    import os
    import time

    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    old.write_text("old")
    time.sleep(0.01)
    new.write_text("new")
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))

    assert most_recent_by_mtime([old, new]) == new
