"""Hash-chain coverage for exec_log_entries (GitHub issue #136,
docs/TAMPER-EVIDENT-AUDIT-DESIGN.md) -- the control-plane analog of
tests/test_audit_chain.py in the root package. Covers: every write getting
a row_hash/prev_hash chained from the previous row in the same session
(genesis-seeded for the first row), per-session chain isolation, and --
the load-bearing case -- that corrupting a row via an ordinary UPDATE (not
through the repository) is detected by verify_exec_log_chain at the exact
row it happened.

Follows test_exec_log_entries.py's fixtures (`client`, `fake_manager`,
`signup_and_get_api_key`) and its pattern for querying rows directly via
`db_module.get_session_factory()`.
"""

from __future__ import annotations

import httpx
from sqlalchemy import select, update

from boxkite.audit import GENESIS_HASH
from control_plane import db as db_module
from control_plane.audit_chain import verify_exec_log_chain
from control_plane.models_orm import ExecLogEntry

from conftest import FakeSandboxManager, signup_and_get_api_key


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _exec(client: httpx.AsyncClient, api_key: str, session_id: str, command: str) -> None:
    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": command},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200, resp.text


async def _entries_for_session(session_id: str) -> list[ExecLogEntry]:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(
            select(ExecLogEntry).where(ExecLogEntry.session_id == session_id).order_by(ExecLogEntry.started_at)
        )
        return list(result.scalars().all())


async def test_first_exec_log_entry_chains_from_genesis(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "chain-genesis@example.com")
    session_id = await _create_session(client, key)

    await _exec(client, key, session_id, "echo one")

    entries = await _entries_for_session(session_id)
    assert len(entries) == 1
    assert entries[0].prev_hash == GENESIS_HASH
    assert entries[0].row_hash is not None
    assert entries[0].row_hash != GENESIS_HASH


async def test_second_exec_log_entry_chains_from_the_first(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "chain-second@example.com")
    session_id = await _create_session(client, key)

    await _exec(client, key, session_id, "echo one")
    await _exec(client, key, session_id, "echo two")

    entries = await _entries_for_session(session_id)
    assert len(entries) == 2
    assert entries[1].prev_hash == entries[0].row_hash


async def test_chains_are_scoped_independently_per_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "chain-scope@example.com")
    session_a = await _create_session(client, key)
    session_b = await _create_session(client, key)

    await _exec(client, key, session_a, "a1")
    await _exec(client, key, session_b, "b1")
    await _exec(client, key, session_a, "a2")

    entries_b = await _entries_for_session(session_b)
    assert entries_b[0].prev_hash == GENESIS_HASH

    async with db_module.get_session_factory()() as db:
        result_a = await verify_exec_log_chain(db, session_id=session_a)
        result_b = await verify_exec_log_chain(db, session_id=session_b)

    assert result_a.ok is True and result_a.rows_checked == 2
    assert result_b.ok is True and result_b.rows_checked == 1


async def test_verify_exec_log_chain_ok_after_several_writes(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "chain-ok@example.com")
    session_id = await _create_session(client, key)
    for i in range(4):
        await _exec(client, key, session_id, f"cmd-{i}")

    async with db_module.get_session_factory()() as db:
        result = await verify_exec_log_chain(db, session_id=session_id)

    assert result.ok is True
    assert result.rows_checked == 4
    assert result.first_break_at_row_id is None


async def test_verify_exec_log_chain_detects_row_tampered_via_ordinary_sql_update(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "chain-tamper@example.com")
    session_id = await _create_session(client, key)
    for i in range(4):
        await _exec(client, key, session_id, f"cmd-{i}")

    entries = await _entries_for_session(session_id)
    tampered_id = entries[1].id

    # Corrupt it by ordinary means -- a raw UPDATE, never through
    # ExecLogEntryRepository, exactly the "operator error or bug issues a
    # raw UPDATE" scenario this feature exists to catch.
    async with db_module.get_session_factory()() as db:
        await db.execute(
            update(ExecLogEntry).where(ExecLogEntry.id == tampered_id).values(detail={"command": "rm -rf /"})
        )
        await db.commit()

    async with db_module.get_session_factory()() as db:
        result = await verify_exec_log_chain(db, session_id=session_id)

    assert result.ok is False
    assert result.first_break_at_row_id == tampered_id


async def test_verify_exec_log_chain_reports_rows_before_tamper_as_fine(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "chain-tamper-loc@example.com")
    session_id = await _create_session(client, key)
    for i in range(4):
        await _exec(client, key, session_id, f"cmd-{i}")

    entries = await _entries_for_session(session_id)
    tampered_id = entries[2].id

    async with db_module.get_session_factory()() as db:
        await db.execute(
            update(ExecLogEntry).where(ExecLogEntry.id == tampered_id).values(detail={"command": "tampered"})
        )
        await db.commit()

    async with db_module.get_session_factory()() as db:
        result = await verify_exec_log_chain(db, session_id=session_id)

    assert result.ok is False
    assert result.first_break_at_row_id == tampered_id
    assert result.rows_checked == 3


async def test_verify_exec_log_chain_with_no_session_id_checks_every_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "chain-all@example.com")
    session_a = await _create_session(client, key)
    session_b = await _create_session(client, key)
    await _exec(client, key, session_a, "a1")
    await _exec(client, key, session_b, "b1")
    await _exec(client, key, session_b, "b2")

    async with db_module.get_session_factory()() as db:
        result = await verify_exec_log_chain(db)

    assert result.ok is True
    assert result.rows_checked >= 3
