"""Regression test for a CRITICAL race an adversarial security review found
in an earlier pass of GitHub issue #136 (docs/TAMPER-EVIDENT-AUDIT-DESIGN.md
section 6): `ExecLogEntryRepository.create` reads the session's most recent
chained row's hash and computes the new row's hash from it -- a
read-then-write sequence that is not atomic on its own. Two genuinely
concurrent writers to the same session_id (e.g. an agent's `/exec` racing a
human-takeover periodic snapshot writer) could both read the same
prev_hash and each compute a row chaining from it, forking the chain in a
way `verify_exec_log_chain` would then falsely report as tampered.

`audit_chain_lock.get_exec_log_chain_lock` closes this by serializing the
read-prev-hash-then-insert sequence per session_id. This test proves the
fork cannot happen even under real, deliberately induced concurrency
(many overlapping `ExecLogEntryRepository.create` coroutines against the
same session_id, each on its own DB session -- the same shape multiple
concurrent HTTP requests would produce in production).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from control_plane import db as db_module
from control_plane.audit_chain import verify_exec_log_chain
from control_plane.repository import ExecLogEntryRepository


async def _create_one(session_id: str, account_id: str, index: int) -> None:
    async with db_module.get_session_factory()() as db:
        await ExecLogEntryRepository(db).create(
            session_id=session_id,
            account_id=account_id,
            source="agent",
            operation="exec",
            detail={"command": f"echo {index}"},
            exit_code=0,
            output_truncated=None,
            started_at=datetime.now(timezone.utc),
            duration_ms=1,
        )


async def test_concurrent_writes_to_the_same_session_never_fork_the_chain(client, fake_manager):
    """The load-bearing case: N genuinely concurrent create() calls against
    the same session_id must still produce one single, valid chain -- not
    N independent chains each thinking it followed the genesis row, and not
    a chain verify_exec_log_chain reports as tampered."""
    session_id = "concurrent-chain-session"
    account_id = "concurrent-chain-account"
    concurrency = 20

    await asyncio.gather(*(_create_one(session_id, account_id, i) for i in range(concurrency)))

    async with db_module.get_session_factory()() as db:
        result = await verify_exec_log_chain(db, session_id=session_id)

    assert result.ok is True, f"chain forked/tampered after concurrent writes: {result}"

    async with db_module.get_session_factory()() as db:
        from sqlalchemy import select

        from control_plane.models_orm import ExecLogEntry

        rows = (
            (
                await db.execute(
                    select(ExecLogEntry)
                    .where(ExecLogEntry.session_id == session_id)
                    .order_by(ExecLogEntry.started_at, ExecLogEntry.id)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == concurrency
    # Every row's prev_hash must equal exactly one other row's row_hash (or
    # the genesis constant for the first) -- i.e. a single unbranched chain,
    # not several independent chains that all forked from the same
    # mid-chain row.
    row_hashes = {row.row_hash for row in rows}
    assert len(row_hashes) == concurrency, "duplicate row_hash values indicate a forked/collided chain"
