"""Tests for `boxkite audit verify` -- the self-hosted, read-only hash-chain
verifier CLI (GitHub issue #136, docs/TAMPER-EVIDENT-AUDIT-DESIGN.md §7).
Unlike `boxkite log`/`boxkite watch`, this command is local-only and never
touches hosted config -- it operates directly on a `HashChainedSQLiteAuditSink`
database file.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from typer.testing import CliRunner

from boxkite.audit import HashChainedSQLiteAuditSink
from boxkite.cli import app

runner = CliRunner()


def _write_chain(db_path, session_id: str, count: int) -> None:
    sink = HashChainedSQLiteAuditSink(db_path)

    async def _go() -> None:
        for i in range(count):
            await sink.record_exec(
                organization_id=None,
                work_item_id=None,
                session_id=session_id,
                agent_name=None,
                command=f"cmd-{i}",
                exit_code=0,
                duration_ms=1,
            )

    asyncio.run(_go())


def test_audit_verify_reports_ok_and_exits_zero(tmp_path):
    db_path = tmp_path / "audit.db"
    _write_chain(db_path, "session-1", 3)

    result = runner.invoke(app, ["audit", "verify", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "rows_checked=3" in result.output
    assert "OK" in result.output


def test_audit_verify_scoped_to_one_session(tmp_path):
    db_path = tmp_path / "audit.db"
    _write_chain(db_path, "session-a", 2)
    _write_chain(db_path, "session-b", 5)

    result = runner.invoke(app, ["audit", "verify", "--db", str(db_path), "--session", "session-a"])

    assert result.exit_code == 0
    assert "rows_checked=2" in result.output


def test_audit_verify_reports_break_location_and_exits_one(tmp_path):
    db_path = tmp_path / "audit.db"
    _write_chain(db_path, "session-1", 4)

    conn = sqlite3.connect(str(db_path))
    row_id = conn.execute("SELECT id FROM audit_entries ORDER BY id LIMIT 1 OFFSET 1").fetchone()[0]
    conn.execute(
        "UPDATE audit_entries SET detail = ? WHERE id = ?",
        (json.dumps({"command": "tampered", "exit_code": 0, "duration_ms": 1}), row_id),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["audit", "verify", "--db", str(db_path)])

    assert result.exit_code == 1
    assert f"row {row_id}" in result.output
    assert "BROKEN" in result.output


def test_audit_verify_missing_db_file_errors_cleanly(tmp_path):
    result = runner.invoke(app, ["audit", "verify", "--db", str(tmp_path / "does-not-exist.db")])

    assert result.exit_code != 0
