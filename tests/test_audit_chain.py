"""Tests for the hash-chained AuditSink extension (GitHub issue #136,
docs/TAMPER-EVIDENT-AUDIT-DESIGN.md). Written before the implementation
(TDD) -- these fail against a bare `SQLiteAuditSink`/no chain support and
should pass once `HashChainedSQLiteAuditSink`/`verify_audit_db_file` exist.

Covers: genesis-hash seeding, chain verification staying green across
ordinary writes, per-session chain isolation, legacy (pre-chain) rows being
skipped rather than breaking verification, and -- the load-bearing case --
that corrupting a row via an ordinary `UPDATE`/`DELETE` (not through the
sink's own API) is detected by the verifier at the exact row it happened.
"""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

import pytest

from boxkite.audit import (
    GENESIS_HASH,
    ChainVerificationResult,
    HashChainedSQLiteAuditSink,
    NoOpAuditSink,
    SQLiteAuditSink,
    compute_row_hash,
    safe_call,
    verify_audit_db_file,
)


async def _record(sink: HashChainedSQLiteAuditSink, session_id: str, command: str) -> None:
    await sink.record_exec(
        organization_id=None,
        work_item_id=None,
        session_id=session_id,
        agent_name="agent-a",
        command=command,
        exit_code=0,
        duration_ms=1,
    )


def _raw_rows(db_path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, session_id, row_hash, prev_hash, detail FROM audit_entries ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_first_row_in_a_session_chains_from_the_genesis_constant(tmp_path):
    sink = HashChainedSQLiteAuditSink(tmp_path / "audit.db")
    await _record(sink, "session-1", "echo one")

    rows = _raw_rows(tmp_path / "audit.db")
    assert len(rows) == 1
    _id, _session_id, row_hash, prev_hash, _detail = rows[0]
    assert prev_hash == GENESIS_HASH
    assert row_hash is not None
    assert row_hash != GENESIS_HASH


@pytest.mark.asyncio
async def test_second_row_chains_from_the_first_rows_hash(tmp_path):
    sink = HashChainedSQLiteAuditSink(tmp_path / "audit.db")
    await _record(sink, "session-1", "echo one")
    await _record(sink, "session-1", "echo two")

    rows = _raw_rows(tmp_path / "audit.db")
    assert len(rows) == 2
    first_hash = rows[0][2]
    second_prev_hash = rows[1][3]
    assert second_prev_hash == first_hash


@pytest.mark.asyncio
async def test_verify_chain_ok_after_several_ordinary_writes(tmp_path):
    sink = HashChainedSQLiteAuditSink(tmp_path / "audit.db")
    for i in range(5):
        await _record(sink, "session-1", f"cmd-{i}")

    result = await sink.verify_chain(session_id="session-1")

    assert isinstance(result, ChainVerificationResult)
    assert result.ok is True
    assert result.rows_checked == 5
    assert result.first_break_at_row_id is None


@pytest.mark.asyncio
async def test_verify_chain_ok_on_a_never_written_session(tmp_path):
    sink = HashChainedSQLiteAuditSink(tmp_path / "audit.db")

    result = await sink.verify_chain(session_id="never-recorded")

    assert result.ok is True
    assert result.rows_checked == 0


@pytest.mark.asyncio
async def test_each_session_has_its_own_independent_chain(tmp_path):
    sink = HashChainedSQLiteAuditSink(tmp_path / "audit.db")
    await _record(sink, "session-a", "a1")
    await _record(sink, "session-b", "b1")
    await _record(sink, "session-a", "a2")

    rows = _raw_rows(tmp_path / "audit.db")
    session_b_row = next(r for r in rows if r[1] == "session-b")
    assert session_b_row[3] == GENESIS_HASH  # session-b's first row, own genesis

    result_a = await sink.verify_chain(session_id="session-a")
    result_b = await sink.verify_chain(session_id="session-b")
    assert result_a.ok is True and result_a.rows_checked == 2
    assert result_b.ok is True and result_b.rows_checked == 1


@pytest.mark.asyncio
async def test_verify_chain_with_no_session_id_checks_every_session(tmp_path):
    sink = HashChainedSQLiteAuditSink(tmp_path / "audit.db")
    await _record(sink, "session-a", "a1")
    await _record(sink, "session-b", "b1")
    await _record(sink, "session-b", "b2")

    result = await sink.verify_chain()

    assert result.ok is True
    assert result.rows_checked == 3


@pytest.mark.asyncio
async def test_verify_chain_detects_row_content_tampered_via_ordinary_sql_update(tmp_path):
    db_path = tmp_path / "audit.db"
    sink = HashChainedSQLiteAuditSink(db_path)
    for i in range(4):
        await _record(sink, "session-1", f"cmd-{i}")
    rows = _raw_rows(db_path)
    tampered_row_id = rows[1][0]  # second row, 1-indexed among the four

    # Corrupt it by "ordinary means" -- a raw UPDATE, not through the sink.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE audit_entries SET detail = ? WHERE id = ?",
        (json.dumps({"command": "rm -rf /", "exit_code": 0, "duration_ms": 1}), tampered_row_id),
    )
    conn.commit()
    conn.close()

    result = await sink.verify_chain(session_id="session-1")

    assert result.ok is False
    assert result.first_break_at_row_id == tampered_row_id
    assert result.rows_checked >= 1


@pytest.mark.asyncio
async def test_verify_chain_reports_ok_for_rows_before_the_tampered_one(tmp_path):
    """The break is reported at the tampered row -- rows written before it
    are unaffected, matching a real partial-tamper scenario."""
    db_path = tmp_path / "audit.db"
    sink = HashChainedSQLiteAuditSink(db_path)
    for i in range(4):
        await _record(sink, "session-1", f"cmd-{i}")
    rows = _raw_rows(db_path)
    third_row_id = rows[2][0]

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE audit_entries SET detail = ? WHERE id = ?",
        (json.dumps({"command": "tampered", "exit_code": 0, "duration_ms": 1}), third_row_id),
    )
    conn.commit()
    conn.close()

    result = await sink.verify_chain(session_id="session-1")

    assert result.ok is False
    assert result.first_break_at_row_id == third_row_id
    # The first two (untampered) rows must have checked out fine before the break.
    assert result.rows_checked == 3


@pytest.mark.asyncio
async def test_verify_chain_detects_a_deleted_row_breaking_the_chain(tmp_path):
    db_path = tmp_path / "audit.db"
    sink = HashChainedSQLiteAuditSink(db_path)
    for i in range(4):
        await _record(sink, "session-1", f"cmd-{i}")
    rows = _raw_rows(db_path)
    deleted_row_id = rows[1][0]
    row_after_deleted_id = rows[2][0]

    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM audit_entries WHERE id = ?", (deleted_row_id,))
    conn.commit()
    conn.close()

    result = await sink.verify_chain(session_id="session-1")

    assert result.ok is False
    assert result.first_break_at_row_id == row_after_deleted_id


@pytest.mark.asyncio
async def test_legacy_unhashed_rows_are_skipped_not_treated_as_breaks(tmp_path):
    db_path = tmp_path / "audit.db"
    plain_sink = SQLiteAuditSink(db_path)
    await plain_sink.record_exec(
        organization_id=None, work_item_id=None, session_id="session-1",
        agent_name=None, command="pre-chain", exit_code=0, duration_ms=1,
    )

    chained_sink = HashChainedSQLiteAuditSink(db_path)
    await _record(chained_sink, "session-1", "post-chain-1")
    await _record(chained_sink, "session-1", "post-chain-2")

    rows = _raw_rows(db_path)
    assert len(rows) == 3
    assert rows[0][3] is None  # legacy row has no prev_hash
    assert rows[1][3] == GENESIS_HASH  # first chained row starts fresh from genesis

    result = await chained_sink.verify_chain(session_id="session-1")
    assert result.ok is True
    assert result.rows_checked == 2  # only the two chained rows count


@pytest.mark.asyncio
async def test_verify_audit_db_file_matches_sink_verify_chain(tmp_path):
    db_path = tmp_path / "audit.db"
    sink = HashChainedSQLiteAuditSink(db_path)
    for i in range(3):
        await _record(sink, "session-1", f"cmd-{i}")

    live_result = await sink.verify_chain(session_id="session-1")
    file_result = verify_audit_db_file(db_path, session_id="session-1")

    assert file_result.ok == live_result.ok
    assert file_result.rows_checked == live_result.rows_checked


@pytest.mark.asyncio
async def test_verify_audit_db_file_opens_read_only_and_detects_tampering(tmp_path):
    db_path = tmp_path / "audit.db"
    sink = HashChainedSQLiteAuditSink(db_path)
    for i in range(3):
        await _record(sink, "session-1", f"cmd-{i}")
    rows = _raw_rows(db_path)
    tampered_row_id = rows[0][0]

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE audit_entries SET detail = ? WHERE id = ?",
        (json.dumps({"command": "tampered", "exit_code": 1, "duration_ms": 1}), tampered_row_id),
    )
    conn.commit()
    conn.close()

    result = verify_audit_db_file(db_path, session_id="session-1")

    assert result.ok is False
    assert result.first_break_at_row_id == tampered_row_id


def test_compute_row_hash_is_deterministic():
    fields = {"a": 1, "b": {"c": 2}}
    assert compute_row_hash(GENESIS_HASH, fields) == compute_row_hash(GENESIS_HASH, fields)
    assert compute_row_hash(GENESIS_HASH, fields) != compute_row_hash("f" * 64, fields)


@pytest.mark.asyncio
async def test_hash_chained_sink_still_implements_plain_auditsink_methods(tmp_path):
    sink = HashChainedSQLiteAuditSink(tmp_path / "audit.db")
    org_id = uuid4()

    await sink.record_file_write(
        organization_id=org_id, work_item_id=None, session_id="s1",
        agent_name=None, file_path="/tmp/x", content=b"hi",
    )
    entries = await sink.list_for_session("s1")

    assert len(entries) == 1
    assert entries[0].kind == "file_write"


@pytest.mark.asyncio
async def test_noop_and_plain_sqlite_sink_unaffected_by_new_symbols():
    sink = NoOpAuditSink()
    assert await sink.record_exec() is None
    assert not hasattr(sink, "verify_chain")

    result = await safe_call(sink, "verify_chain", session_id=None)
    assert result is None
