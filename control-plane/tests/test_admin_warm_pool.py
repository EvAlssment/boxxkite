import httpx
from sqlalchemy import select

from conftest import signup, signup_and_get_api_key
from control_plane import db as db_module
from control_plane.models_orm import Account
from control_plane.routers import admin as admin_router


async def _make_admin(email: str) -> None:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.email == email))
        result.scalar_one().is_admin = True
        await db.commit()


class _FakeWarmPool:
    async def get_status(self) -> dict:
        return {
            "utilization_window_seconds": 100,
            "utilization_by_size": {
                "small": {
                    "target": 3,
                    "actual_warm_count": 2,
                    "claims_last_window": 4,
                    "cold_fallthroughs_last_window": 1,
                },
                "medium": {
                    "target": 0,
                    "actual_warm_count": 0,
                    "claims_last_window": 0,
                    "cold_fallthroughs_last_window": 0,
                },
                "large": {
                    "target": 0,
                    "actual_warm_count": 0,
                    "claims_last_window": 0,
                    "cold_fallthroughs_last_window": 0,
                },
            },
        }


async def test_non_admin_cannot_read_warm_pool_utilization(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "warm-pool-not-admin@example.com")

    response = await client.get(
        "/v1/admin/warm-pool", headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "admin_required"


async def test_admin_can_read_warm_pool_utilization(
    client: httpx.AsyncClient, monkeypatch
):
    email = "warm-pool-admin@example.com"
    key = await signup_and_get_api_key(client, email)
    await _make_admin(email)

    async def _get_warm_pool():
        return _FakeWarmPool()

    monkeypatch.setattr(admin_router, "get_warm_pool", _get_warm_pool)

    response = await client.get(
        "/v1/admin/warm-pool", headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "available": True,
        "unavailable_reason": None,
        "window_seconds": 100,
        "sizes": [
            {
                "size": "small",
                "target": 3,
                "actual_warm_count": 2,
                "claims_last_window": 4,
                "cold_fallthroughs_last_window": 1,
            },
            {
                "size": "medium",
                "target": 0,
                "actual_warm_count": 0,
                "claims_last_window": 0,
                "cold_fallthroughs_last_window": 0,
            },
            {
                "size": "large",
                "target": 0,
                "actual_warm_count": 0,
                "claims_last_window": 0,
                "cold_fallthroughs_last_window": 0,
            },
        ],
    }


async def test_warm_pool_route_reports_unavailable_without_inventing_counts(
    client: httpx.AsyncClient, monkeypatch
):
    email = "warm-pool-unavailable-admin@example.com"
    key = await signup_and_get_api_key(client, email)
    await _make_admin(email)
    monkeypatch.setattr(admin_router, "get_warm_pool", lambda: _none_async())

    response = await client.get(
        "/v1/admin/warm-pool", headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "unavailable_reason": "not_configured",
        "window_seconds": None,
        "sizes": [],
    }


async def test_dashboard_admin_mirror_uses_the_same_warm_pool_report(
    client: httpx.AsyncClient, monkeypatch
):
    email = "warm-pool-dashboard-admin@example.com"
    signup_response = await signup(client, email)
    await _make_admin(email)

    async def _get_warm_pool():
        return _FakeWarmPool()

    monkeypatch.setattr(admin_router, "get_warm_pool", _get_warm_pool)

    response = await client.get(
        "/v1/account/admin/warm-pool",
        headers={"Authorization": f"Bearer {signup_response['access_token']}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["sizes"][0]["claims_last_window"] == 4


async def _none_async():
    return None
