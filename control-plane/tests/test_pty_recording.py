"""Tests for pty_recording.py -- the full-duplex asciicast-v2 recorder for
human-takeover sessions (GitHub issue #133). See that module's docstring
for the redaction-approach tradeoffs asserted on below.
"""

from __future__ import annotations

import json

import pytest

from control_plane.pty_recording import (
    TAKEOVER_RECORDING_STORAGE_PREFIX,
    PtyRecordingBuffer,
    finalize_takeover_recording,
    redact_pty_bytes,
    takeover_recording_storage_key,
)


# ── redact_pty_bytes ──────────────────────────────────────────────────────


def test_redact_pty_bytes_leaves_ordinary_text_untouched():
    assert redact_pty_bytes(b"hello world\r\n$ ls\r\n") == "hello world\r\n$ ls\r\n"


def test_redact_pty_bytes_empty_chunk_returns_empty_string():
    assert redact_pty_bytes(b"") == ""


def test_redact_pty_bytes_redacts_aws_access_key_shape():
    out = redact_pty_bytes(b"AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP\r\n")
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    assert "REDACTED_AWS_KEY" in out


def test_redact_pty_bytes_redacts_generic_api_key_assignment():
    # Deliberately NOT shaped like any real vendor's key format (e.g. Stripe's
    # `sk_live_`/`sk_test_` prefix) -- this test exercises the *generic*
    # `api_key=...` assignment pattern, not a vendor-specific one, and a
    # vendor-shaped fixture here previously tripped GitHub's push-protection
    # secret scanner on an otherwise fake value.
    out = redact_pty_bytes(b'api_key="example-plaintext-secret-1234567890abcdef"\r\n')
    assert "example-plaintext-secret-1234567890abcdef" not in out
    assert "REDACTED_KEY" in out


def test_redact_pty_bytes_redacts_github_token_shape():
    out = redact_pty_bytes(b"ghp_" + b"a" * 36 + b"\r\n")
    assert "REDACTED_GITHUB_TOKEN" in out


def test_redact_pty_bytes_tolerates_invalid_utf8_without_raising():
    # A lone continuation byte is invalid UTF-8 on its own -- must not raise.
    out = redact_pty_bytes(b"\xff\xfehello\r\n")
    assert "hello" in out


def test_redact_pty_bytes_does_not_catch_secret_split_across_chunks():
    """Disclosed limitation: redaction is per-chunk, so a value split
    across two separate reads is not reconstructed and not redacted. This
    test pins that behavior down explicitly rather than leaving it
    implicit -- a future change that "fixes" this by buffering across
    chunks should update this test deliberately, not stumble into it."""
    first_half = redact_pty_bytes(b"api_key=sk_live_abc")
    second_half = redact_pty_bytes(b"defghijklmnopqrstuvwx\r\n")
    assert "sk_live_abc" in first_half  # not recognized as a secret on its own
    assert "defghijklmnopqrstuvwx" in second_half


# ── PtyRecordingBuffer ────────────────────────────────────────────────────


def _parse_cast_lines(blob: bytes) -> list:
    lines = blob.decode("utf-8").strip("\n").split("\n")
    return [json.loads(line) for line in lines]


def test_recording_buffer_starts_empty():
    buf = PtyRecordingBuffer()
    assert buf.event_count == 0
    assert buf.truncated is False


def test_recording_buffer_serialize_header_is_valid_asciicast_v2():
    buf = PtyRecordingBuffer()
    parsed = _parse_cast_lines(buf.serialize(session_id="sess-1"))
    header = parsed[0]
    assert header["version"] == 2
    assert header["width"] == 80
    assert header["height"] == 24
    assert "sess-1" in header["title"]
    assert len(parsed) == 1  # header only, no events yet


def test_recording_buffer_records_output_and_input_in_order():
    buf = PtyRecordingBuffer()
    buf.record("o", b"$ ")
    buf.record("i", b"ls\n")
    buf.record("o", b"file.txt\n")

    assert buf.event_count == 3
    parsed = _parse_cast_lines(buf.serialize(session_id="sess-1"))
    events = parsed[1:]
    assert [e[1] for e in events] == ["o", "i", "o"]
    assert [e[2] for e in events] == ["$ ", "ls\n", "file.txt\n"]
    # Timestamps must be non-decreasing.
    times = [e[0] for e in events]
    assert times == sorted(times)


def test_recording_buffer_rejects_invalid_direction():
    buf = PtyRecordingBuffer()
    with pytest.raises(ValueError):
        buf.record("x", b"data")


def test_recording_buffer_ignores_empty_chunks():
    buf = PtyRecordingBuffer()
    buf.record("o", b"")
    buf.record("i", b"")
    assert buf.event_count == 0


def test_recording_buffer_applies_redaction_before_storing():
    buf = PtyRecordingBuffer()
    buf.record("o", b"AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP\r\n")
    parsed = _parse_cast_lines(buf.serialize(session_id="sess-1"))
    _, _, text = parsed[1]
    assert "AKIAABCDEFGHIJKLMNOP" not in text
    assert "REDACTED_AWS_KEY" in text


def test_recording_buffer_enforces_max_bytes_and_marks_truncated():
    buf = PtyRecordingBuffer(max_bytes=10)
    buf.record("o", b"0123456789")  # exactly at the cap -- fits
    assert buf.truncated is False
    buf.record("o", b"more data that pushes past the cap")
    assert buf.truncated is True
    # Once truncated, further writes are no-ops.
    events_before = buf.event_count
    buf.record("i", b"should not be recorded")
    assert buf.event_count == events_before


def test_recording_buffer_serialize_appends_truncation_notice():
    buf = PtyRecordingBuffer(max_bytes=5)
    buf.record("o", b"12345")
    buf.record("o", b"6789")  # exceeds the cap -> truncated
    parsed = _parse_cast_lines(buf.serialize(session_id="sess-1"))
    last_event = parsed[-1]
    assert last_event[1] == "o"
    assert "truncated" in last_event[2]


def test_recording_buffer_no_truncation_notice_when_under_cap():
    buf = PtyRecordingBuffer()
    buf.record("o", b"small\n")
    parsed = _parse_cast_lines(buf.serialize(session_id="sess-1"))
    assert not any("truncated" in e[2] for e in parsed[1:])


# ── takeover_recording_storage_key ────────────────────────────────────────


def test_storage_key_is_namespaced_by_account_and_session():
    buf = PtyRecordingBuffer()
    key = takeover_recording_storage_key(account_id="acct-1", session_id="sess-1", recording=buf)
    assert key.startswith(f"{TAKEOVER_RECORDING_STORAGE_PREFIX}/acct-1/sess-1/")
    assert key.endswith(".cast")


# ── finalize_takeover_recording ───────────────────────────────────────────


class _FakeUploadStorage:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.uploads: list[dict] = []

    async def upload_bytes(self, *, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        if self.fail:
            raise RuntimeError("simulated storage failure")
        self.uploads.append({"key": key, "data": data, "content_type": content_type})


async def test_finalize_returns_none_when_nothing_recorded():
    buf = PtyRecordingBuffer()
    storage = _FakeUploadStorage()
    result = await finalize_takeover_recording(buf, storage=storage, account_id="acct-1", session_id="sess-1")
    assert result is None
    assert storage.uploads == []


async def test_finalize_uploads_serialized_recording_and_returns_pointer():
    buf = PtyRecordingBuffer()
    buf.record("o", b"hello\n")
    storage = _FakeUploadStorage()

    result = await finalize_takeover_recording(buf, storage=storage, account_id="acct-1", session_id="sess-1")

    assert result is not None
    assert result["storage_key"].startswith(f"{TAKEOVER_RECORDING_STORAGE_PREFIX}/acct-1/sess-1/")
    assert result["truncated"] is False
    assert result["bytes"] > 0
    assert len(storage.uploads) == 1
    assert storage.uploads[0]["key"] == result["storage_key"]
    assert b"hello" in storage.uploads[0]["data"]


async def test_finalize_reports_truncated_flag_from_buffer():
    buf = PtyRecordingBuffer(max_bytes=2)
    buf.record("o", b"way too much data for the cap")
    storage = _FakeUploadStorage()

    result = await finalize_takeover_recording(buf, storage=storage, account_id="acct-1", session_id="sess-1")

    assert result is not None
    assert result["truncated"] is True


async def test_finalize_returns_none_and_does_not_raise_when_upload_fails():
    buf = PtyRecordingBuffer()
    buf.record("o", b"hello\n")
    storage = _FakeUploadStorage(fail=True)

    result = await finalize_takeover_recording(buf, storage=storage, account_id="acct-1", session_id="sess-1")

    assert result is None
