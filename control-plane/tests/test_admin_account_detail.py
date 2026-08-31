"""GET /v1/admin/accounts/{account_id} -- one account's usage + per-feature
resource-count breakdown (docs/ADMIN-ROLE-DESIGN.md).

Mirrors test_admin_metrics.py's fixture patterns: signup via the client
fixture, then flip Account.is_admin directly via the DB (there is no API
route to do this, by design -- see the design doc).
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from conftest import signup_and_get_api_key
from control_plane import db as db_module
from control_plane.models_orm import Account, AdminAccessLog


async def _make_admin(email: str) -> None:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.email == email))
        account = result.scalar_one()
        account.is_admin = True
        await db.commit()


async def _account_id(email: str) -> str:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.email == email))
        return result.scalar_one().id


async def _admin_access_log_rows() -> list[AdminAccessLog]:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(AdminAccessLog))
        return list(result.scalars().all())


async def test_non_admin_account_gets_403(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "not-an-admin-detail@example.com")
    account_id = await _account_id("not-an-admin-detail@example.com")

    resp = await client.get(
        f"/v1/admin/accounts/{account_id}", headers={"Authorization": f"Bearer {key}"}
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "admin_required"


async def test_missing_auth_gets_401_not_403(client: httpx.AsyncClient):
    resp = await client.get("/v1/admin/accounts/does-not-matter")

    assert resp.status_code == 401


async def test_unknown_account_id_gets_404(client: httpx.AsyncClient):
    admin_email = "admin-detail-404@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)

    resp = await client.get(
        "/v1/admin/accounts/does-not-exist", headers={"Authorization": f"Bearer {admin_key}"}
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "account_not_found"


async def test_admin_account_detail_returns_feature_usage(client: httpx.AsyncClient):
    admin_email = "admin-detail-usage@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)

    other_email = "other-detail-usage@example.com"
    other_key = await signup_and_get_api_key(client, other_email)
    other_id = await _account_id(other_email)

    await client.post(
        "/v1/secrets",
        json={"name": "prod-key", "value": "sk_live_abc123", "allowed_hosts": ["api.example.com"]},
        headers={"Authorization": f"Bearer {other_key}"},
    )
    await client.post(
        "/v1/memory",
        json={"content": "The user prefers TypeScript."},
        headers={"Authorization": f"Bearer {other_key}"},
    )
    create_resp = await client.post(
        "/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {other_key}"}
    )
    assert create_resp.status_code == 201, create_resp.text

    resp = await client.get(
        f"/v1/admin/accounts/{other_id}", headers={"Authorization": f"Bearer {admin_key}"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_id"] == other_id
    assert body["email"] == other_email
    assert body["concurrent_sandboxes"] == 1
    assert body["total_sandboxes_created"] == 1
    assert body["feature_usage"]["secrets"] == 1
    assert body["feature_usage"]["memory"]["total_memories"] == 1
    assert body["feature_usage"]["mcp_connections"] == 0
    assert body["feature_usage"]["webhooks"] == 0
    assert body["feature_usage"]["snapshots"] == 0
    assert body["feature_usage"]["sandbox_images"] == 0
    assert body["feature_usage"]["sandbox_volumes"] == 0


async def test_admin_account_detail_access_is_logged(client: httpx.AsyncClient):
    admin_email = "admin-detail-logged@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)
    account_id = await _account_id(admin_email)

    await client.get(
        f"/v1/admin/accounts/{account_id}", headers={"Authorization": f"Bearer {admin_key}"}
    )

    rows = await _admin_access_log_rows()
    assert len(rows) == 1
    assert rows[0].endpoint == f"/v1/admin/accounts/{account_id}"


async def test_dashboard_jwt_mirror_returns_same_shape(client: httpx.AsyncClient):
    admin_email = "admin-detail-jwt@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)
    account_id = await _account_id(admin_email)

    login_resp = await client.post(
        "/v1/auth/login", json={"email": admin_email, "password": "hunter2pass"}
    )
    assert login_resp.status_code == 200, login_resp.text
    jwt = login_resp.json()["access_token"]

    resp = await client.get(
        f"/v1/account/admin/accounts/{account_id}", headers={"Authorization": f"Bearer {jwt}"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["account_id"] == account_id
