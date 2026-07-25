"""GET /v1/admin/audit-log -- docs/ADMIN-ROLE-DESIGN.md, closing GitHub
issue #140.

Mirrors test_admin_metrics.py's fixture patterns exactly (signup via the
client fixture, then flip Account.is_admin directly via the DB -- there is
no API route to do this, by design) and test_exec_log_entries.py's pattern
for generating real ExecLogEntry rows via the `/exec` route against the
`fake_manager` fixture.
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from conftest import FakeSandboxManager, signup_and_get_api_key
from control_plane import db as db_module
from control_plane.models_orm import Account, AdminAccessLog
from control_plane.repository import AccountRepository


async def _make_admin(email: str) -> None:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.email == email))
        account = result.scalar_one()
        account.is_admin = True
        await db.commit()


async def _admin_access_log_rows() -> list[AdminAccessLog]:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(AdminAccessLog))
        return list(result.scalars().all())


async def _account_id_for(email: str) -> str:
    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_email(email)
        return account.id


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


async def test_non_admin_account_gets_403(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "not-an-admin-audit@example.com")

    resp = await client.get("/v1/admin/audit-log", headers={"Authorization": f"Bearer {key}"})

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "admin_required"


async def test_missing_auth_gets_401_not_403(client: httpx.AsyncClient):
    resp = await client.get("/v1/admin/audit-log")

    assert resp.status_code == 401


async def test_admin_account_can_read_audit_log(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    email = "admin-audit-user@example.com"
    key = await signup_and_get_api_key(client, email)
    await _make_admin(email)
    session_id = await _create_session(client, key)
    await _exec(client, key, session_id, "echo hello")

    resp = await client.get("/v1/admin/audit-log", headers={"Authorization": f"Bearer {key}"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    assert isinstance(body["entries"], list)
    entry = next(e for e in body["entries"] if e["session_id"] == session_id)
    assert entry["operation"] == "exec"
    assert entry["account_id"] == await _account_id_for(email)
    # Hash-chain fields (GitHub issue #136, docs/TAMPER-EVIDENT-AUDIT-DESIGN.md)
    # must reach this cross-account export route too, not just the
    # session-scoped GET .../log -- both are real independent-verification
    # export paths per the design doc's §7.
    assert entry["row_hash"] is not None
    assert len(entry["row_hash"]) == 64
    assert entry["prev_hash"] is not None


async def test_admin_audit_log_sees_entries_across_all_accounts(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    admin_email = "admin-audit-cross-account@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)

    other_email = "other-account-audit@example.com"
    other_key = await signup_and_get_api_key(client, other_email)
    other_session_id = await _create_session(client, other_key)
    await _exec(client, other_key, other_session_id, "echo from other account")

    resp = await client.get("/v1/admin/audit-log", headers={"Authorization": f"Bearer {admin_key}"})

    assert resp.status_code == 200
    body = resp.json()
    other_account_id = await _account_id_for(other_email)
    matching = [e for e in body["entries"] if e["session_id"] == other_session_id]
    assert len(matching) == 1
    assert matching[0]["account_id"] == other_account_id
    assert matching[0]["operation"] == "exec"


async def test_admin_audit_log_filters_by_account_id(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    admin_email = "admin-audit-filter@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)

    account_a_email = "account-a-audit@example.com"
    account_a_key = await signup_and_get_api_key(client, account_a_email)
    account_a_session = await _create_session(client, account_a_key)
    await _exec(client, account_a_key, account_a_session, "echo a")

    account_b_email = "account-b-audit@example.com"
    account_b_key = await signup_and_get_api_key(client, account_b_email)
    account_b_session = await _create_session(client, account_b_key)
    await _exec(client, account_b_key, account_b_session, "echo b")

    account_a_id = await _account_id_for(account_a_email)

    resp = await client.get(
        f"/v1/admin/audit-log?account_id={account_a_id}",
        headers={"Authorization": f"Bearer {admin_key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) >= 1
    assert all(e["account_id"] == account_a_id for e in body["entries"])
    assert all(e["session_id"] != account_b_session for e in body["entries"])


async def test_admin_audit_log_pagination_respects_limit(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    admin_email = "admin-audit-paginated@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)
    session_id = await _create_session(client, admin_key)
    await _exec(client, admin_key, session_id, "echo one")
    await _exec(client, admin_key, session_id, "echo two")
    await _exec(client, admin_key, session_id, "echo three")

    resp = await client.get(
        "/v1/admin/audit-log?limit=1", headers={"Authorization": f"Bearer {admin_key}"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 1
    assert body["total"] >= 3
    assert body["limit"] == 1
    assert body["offset"] == 0


async def test_admin_audit_log_newest_first(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    admin_email = "admin-audit-order@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)
    session_id = await _create_session(client, admin_key)
    await _exec(client, admin_key, session_id, "echo first")
    await _exec(client, admin_key, session_id, "echo second")

    resp = await client.get("/v1/admin/audit-log", headers={"Authorization": f"Bearer {admin_key}"})

    assert resp.status_code == 200
    entries = resp.json()["entries"]
    session_entries = [e for e in entries if e["session_id"] == session_id]
    assert session_entries[0]["detail"]["command"] == "echo second"
    assert session_entries[1]["detail"]["command"] == "echo first"


async def test_admin_audit_log_access_is_logged(client: httpx.AsyncClient):
    admin_email = "admin-audit-logged@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)

    await client.get("/v1/admin/audit-log", headers={"Authorization": f"Bearer {admin_key}"})

    rows = await _admin_access_log_rows()
    assert len(rows) == 1
    assert rows[0].endpoint == "/v1/admin/audit-log"


async def test_non_admin_access_attempt_is_not_logged(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "not-admin-audit-no-log@example.com")

    await client.get("/v1/admin/audit-log", headers={"Authorization": f"Bearer {key}"})

    rows = await _admin_access_log_rows()
    assert rows == []
