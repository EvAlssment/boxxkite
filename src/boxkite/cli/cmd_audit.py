"""`boxkite audit verify` -- read-only hash-chain verifier for a
`HashChainedSQLiteAuditSink` database file (GitHub issue #136,
docs/TAMPER-EVIDENT-AUDIT-DESIGN.md §7).

Local-only, unlike `boxkite log`/`boxkite watch`: this operates directly on
a SQLite file an operator points it at (their own `boxkite_audit.db`, or a
copy exported off-host), never against a hosted control-plane. Exit code
0/1 makes this scriptable in a compliance pipeline (nightly cron, CI job
against a nightly export, etc.).
"""

from __future__ import annotations

from pathlib import Path

import typer

from boxkite.audit import verify_audit_db_file

from .errors import CliError


def verify(
    db: Path = typer.Option(
        ...,
        "--db",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to a HashChainedSQLiteAuditSink database file (e.g. boxkite_audit.db).",
    ),
    session: str = typer.Option(
        None,
        "--session",
        help="Scope verification to one session_id. Omit to verify every session in the file.",
    ),
) -> None:
    """Recompute the hash chain over a SQLite audit-log file and report
    whether it is intact, or exactly where it first breaks.

    Opens the file read-only -- this command can never itself modify the
    audit log, even if pointed at a live, in-use database file.
    """
    try:
        result = verify_audit_db_file(db, session_id=session)
    except Exception as exc:  # noqa: BLE001 - surface any failure as a clean CLI error
        raise CliError(f"Could not verify {db}: {exc}") from exc

    typer.echo(f"rows_checked={result.rows_checked}")
    if result.ok:
        typer.echo(f"OK: {result.detail}")
        return
    typer.echo(f"BROKEN: {result.detail}")
    raise typer.Exit(code=1)
