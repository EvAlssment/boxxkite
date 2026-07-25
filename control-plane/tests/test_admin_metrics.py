"""GET /v1/admin/metrics -- docs/ADMIN-ROLE-DESIGN.md, closing GitHub issue
#72.

Mirrors test_rate_limit_headers.py's/test_sandbox_images.py's fixture
patterns: signup via the client fixture, then flip Account.is_admin
directly via the DB (there is no API route to do this, by design -- see
the design doc).
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from conftest import signup_and_get_api_key
from control_plane import db as db_module
from control_plane.config import settings
from control_plane.models_orm import Account, AdminAccessLog


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


async def test_non_admin_account_gets_403(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "not-an-admin@example.com")

    resp = await client.get("/v1/admin/metrics", headers={"Authorization": f"Bearer {key}"})

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "admin_required"


async def test_missing_auth_gets_401_not_403(client: httpx.AsyncClient):
    resp = await client.get("/v1/admin/metrics")

    assert resp.status_code == 401


async def test_admin_account_can_read_cluster_metrics(client: httpx.AsyncClient):
    email = "admin-user@example.com"
    key = await signup_and_get_api_key(client, email)
    await _make_admin(email)

    resp = await client.get("/v1/admin/metrics", headers={"Authorization": f"Bearer {key}"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_accounts"] >= 1
    assert body["global_concurrent_sandboxes_limit"] == settings.BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES
    assert isinstance(body["accounts"], list)


async def test_admin_metrics_sees_sandboxes_across_all_accounts(client: httpx.AsyncClient):
    admin_email = "admin-cross-account@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)

    other_key = await signup_and_get_api_key(client, "other-account@example.com")
    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {other_key}"})
    assert create_resp.status_code == 201, create_resp.text

    resp = await client.get("/v1/admin/metrics", headers={"Authorization": f"Bearer {admin_key}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["global_concurrent_sandboxes"] >= 1
    other_row = next(
        (row for row in body["accounts"] if row["email"] == "other-account@example.com"), None
    )
    assert other_row is not None
    assert other_row["concurrent_sandboxes"] == 1


async def test_admin_metrics_pagination_respects_limit(client: httpx.AsyncClient):
    admin_email = "admin-paginated@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)
    await signup_and_get_api_key(client, "extra-account-1@example.com")
    await signup_and_get_api_key(client, "extra-account-2@example.com")

    resp = await client.get(
        "/v1/admin/metrics?limit=1", headers={"Authorization": f"Bearer {admin_key}"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["accounts"]) == 1
    assert body["total_accounts"] >= 3


async def test_admin_metrics_access_is_logged(client: httpx.AsyncClient):
    admin_email = "admin-logged@example.com"
    admin_key = await signup_and_get_api_key(client, admin_email)
    await _make_admin(admin_email)

    await client.get("/v1/admin/metrics", headers={"Authorization": f"Bearer {admin_key}"})

    rows = await _admin_access_log_rows()
    assert len(rows) == 1
    assert rows[0].endpoint == "/v1/admin/metrics"


async def test_non_admin_access_attempt_is_not_logged(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "not-admin-no-log@example.com")

    await client.get("/v1/admin/metrics", headers={"Authorization": f"Bearer {key}"})

    rows = await _admin_access_log_rows()
    assert rows == []
