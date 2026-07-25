"""Cursor adapter tests.

See boxkite_handoff/adapters/cursor.py's module docstring for the
verification record this test file exercises: Cursor's `cursor-agent` CLI
does have a documented local resume mechanism (`--resume [chatId]`,
`agent ls`, `CURSOR_API_KEY` for headless auth), but the on-disk artifact
that actually backs it could not be confirmed -- against the real shipped
binary -- to be a portable, adapter-copyable file the way Claude Code's and
Codex's JSONL transcripts are. Per docs/handoff-adapters.md's "degrade
honestly, don't fake it" rule, this adapter always raises HandoffError
rather than fabricate a session-file path/format that was never confirmed
to work.
"""

from __future__ import annotations

import pytest

from boxkite_handoff.adapters.cursor import CursorAdapter
from boxkite_handoff.core import HandoffError


def test_cursor_adapter_name_is_cursor() -> None:
    assert CursorAdapter().name == "cursor"


def test_locate_session_raises_handoff_error_with_no_session_ref() -> None:
    adapter = CursorAdapter()

    with pytest.raises(HandoffError):
        adapter.locate_session(session_ref=None)


def test_locate_session_raises_handoff_error_with_a_session_ref() -> None:
    adapter = CursorAdapter()

    with pytest.raises(HandoffError):
        adapter.locate_session(session_ref="some-chat-id")


def test_locate_session_error_message_explains_what_was_verified() -> None:
    adapter = CursorAdapter()

    with pytest.raises(HandoffError) as exc_info:
        adapter.locate_session()

    message = str(exc_info.value)
    # Cites what *is* confirmed (so this doesn't read as "we didn't try")...
    assert "--resume" in message
    assert "CURSOR_API_KEY" in message
    # ...and is explicit that full-conversation handoff isn't supported yet,
    # not silently degrading to a diff/task-summary substitute.
    assert "not" in message.lower()


def test_locate_session_does_not_return_a_located_session_on_any_input() -> None:
    """No input should coax this adapter into fabricating a LocatedSession --
    every call path must fail closed."""
    adapter = CursorAdapter()

    for ref in (None, "", "abc123", "/some/path"):
        with pytest.raises(HandoffError):
            adapter.locate_session(session_ref=ref)
