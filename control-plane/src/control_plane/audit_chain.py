"""Hash-chain verifier for `exec_log_entries` -- the control-plane analog of
`boxkite.audit.HashChainedSQLiteAuditSink.verify_chain` (GitHub issue #136,
`docs/TAMPER-EVIDENT-AUDIT-DESIGN.md`). Reuses `boxkite.audit`'s shared
`ChainRow`/`verify_chain_rows`/hash formula so both audit surfaces (this
Postgres-backed table and the self-hosted `SQLiteAuditSink`) are provably
running the exact same algorithm, not two independent reimplementations
that could quietly drift apart.

The write side lives in `ExecLogEntryRepository.create` (computes and
persists `row_hash`/`prev_hash` in the same INSERT as the row); this module
is read-only verification, used by `scripts/verify_exec_log_chain.py` and
directly importable for a future admin-facing convenience endpoint (design
doc §7 -- deliberately not the primary verification artifact; an external
auditor should verify an *exported* copy of the rows, not ask the same
service that wrote them "are your own logs valid?").
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boxkite.audit import ChainRow, ChainVerificationResult, verify_chain_rows

from .models_orm import ExecLogEntry


def canonical_started_at(started_at: datetime) -> str:
    """Deterministic serialization of `started_at` for the hash chain.

    Normalizes away tzinfo before formatting so the same instant hashes
    identically regardless of round-trip: Postgres' `TIMESTAMPTZ` preserves
    tzinfo on read-back, but SQLite's `DateTime(timezone=True)` does not
    (verified directly -- a tz-aware `datetime` written to this column comes
    back naive after a commit+refresh or a fresh query). Without this
    normalization, the exact same row would hash differently at write time
    (tz-aware `started_at`, from `_utcnow()`) than at verify time (naive
    `started_at`, read back from a SQLite-backed deployment), which would
    look identical to real tampering. `docs/TAMPER-EVIDENT-AUDIT-DESIGN.md`
    §4 requires the canonical serialization to be reproducible "years later
    on a different machine" -- this is the same requirement applied to a
    same-machine, cross-backend round-trip.
    """
    if started_at.tzinfo is not None:
        started_at = started_at.astimezone(timezone.utc).replace(tzinfo=None)
    return started_at.isoformat()


def _to_chain_row(row: ExecLogEntry) -> ChainRow:
    return ChainRow(
        row_id=row.id,
        session_id=row.session_id,
        row_hash=row.row_hash,
        prev_hash=row.prev_hash,
        canonical_fields={
            "id": row.id,
            "session_id": row.session_id,
            "account_id": row.account_id,
            "source": row.source,
            "operation": row.operation,
            "detail": row.detail,
            "exit_code": row.exit_code,
            "output_truncated": row.output_truncated,
            "started_at": canonical_started_at(row.started_at),
            "duration_ms": row.duration_ms,
        },
    )


async def verify_exec_log_chain(
    db: AsyncSession, *, session_id: str | None = None
) -> ChainVerificationResult:
    """Recompute `exec_log_entries`' hash chain and compare against stored
    `row_hash`/`prev_hash`. `session_id=None` verifies every session in this
    database, each independently (docs/TAMPER-EVIDENT-AUDIT-DESIGN.md §5 --
    chain scope is per-session, never global or per-account, matching this
    table's existing cascade-delete-per-session behavior).
    """
    query = select(ExecLogEntry)
    if session_id is not None:
        query = query.where(ExecLogEntry.session_id == session_id)
    query = query.order_by(ExecLogEntry.session_id, ExecLogEntry.started_at, ExecLogEntry.id)
    result = await db.execute(query)
    rows = result.scalars().all()
    return verify_chain_rows([_to_chain_row(row) for row in rows])
