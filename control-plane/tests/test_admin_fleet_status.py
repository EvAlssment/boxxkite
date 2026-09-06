"""GET /v1/admin/fleet/status -- current runtime operational snapshot."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from conftest import signup_and_get_api_key
from control_plane import db as db_module
from control_plane.config import settings
from control_plane.deps import get_warm_pool_status_source
from control_plane.main import app
from control_plane.models_orm import Account, AdminAccessLog


async def _make_admin(email: str) -> None:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.email == email))
        result.scalar_one().is_admin = True
        await db.commit()


async def _admin_access_log_rows() -> list[AdminAccessLog]:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(AdminAccessLog))
        return list(result.scalars().all())


class FakeWarmPool:
    async def get_status(self) -> dict:
        return {
            "target_sizes": {"small": 2, "medium": 1, "large": 0},
            "warm_by_size": {"small": 1, "medium": 1, "large": 0},
            "total_pods": 4,
            "max_size": 8,
            "adaptive_warm_pool_enabled": True,
            "claim_rate_window_seconds": 300,
            "claim_rate_per_second_by_size": {
                "small": 0.01,
                "medium": 0.0,
                "large": 0.02,
            },
        }


async def test_non_admin_account_gets_403(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "not-an-admin-fleet@example.com")

    resp = await client.get(
        "/v1/admin/fleet/status", headers={"Authorization": f"Bearer {key}"}
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "admin_required"


async def test_missing_auth_gets_401_not_403(client: httpx.AsyncClient):
    resp = await client.get("/v1/admin/fleet/status")

    assert resp.status_code == 401


async def test_admin_fleet_status_aggregates_real_warm_pool_data(
    client: httpx.AsyncClient, monkeypatch
):
    email = "admin-fleet-status@example.com"
    key = await signup_and_get_api_key(client, email)
    await _make_admin(email)
    monkeypatch.setenv("RUNTIME_MODE", "k8s")
    monkeypatch.setattr(settings, "BOXXKITE_CLUSTER_ID", "primary")
    app.dependency_overrides[get_warm_pool_status_source] = lambda: FakeWarmPool()

    resp = await client.get(
        "/v1/admin/fleet/status", headers={"Authorization": f"Bearer {key}"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["clusters"]) == 1
    cluster = body["clusters"][0]
    assert cluster["cluster_id"] == "primary"
    assert cluster["runtime"] == "k8s"
    assert cluster["warm_pool"] == {
        "supported": True,
        "target_by_size": {"small": 2, "medium": 1, "large": 0},
        "actual_by_size": {"small": 1, "medium": 1, "large": 0},
        "total_active": 4,
        "max_size": 8,
        "adaptive_enabled": True,
        "reason": None,
    }
    assert cluster["recent_claim_rate"] == {
        "supported": True,
        "window_seconds": 300,
        "per_second_by_size": {"small": 0.01, "medium": 0.0, "large": 0.02},
        "reason": None,
    }
    for field in ("cold_fallthroughs", "pending_pods", "node_pressure", "placement"):
        assert cluster[field]["supported"] is False
        assert cluster[field]["value"] is None
        assert cluster[field]["reason"]

    rows = await _admin_access_log_rows()
    assert len(rows) == 1
    assert rows[0].endpoint == "/v1/admin/fleet/status"


async def test_admin_fleet_status_reports_unavailable_warm_pool(
    client: httpx.AsyncClient, monkeypatch
):
    email = "admin-fleet-unavailable@example.com"
    key = await signup_and_get_api_key(client, email)
    await _make_admin(email)
    monkeypatch.setenv("RUNTIME_MODE", "k8s")

    class BrokenWarmPool:
        async def get_status(self) -> dict:
            raise RuntimeError("test-only status failure")

    app.dependency_overrides[get_warm_pool_status_source] = lambda: BrokenWarmPool()

    resp = await client.get(
        "/v1/admin/fleet/status", headers={"Authorization": f"Bearer {key}"}
    )

    assert resp.status_code == 200
    cluster = resp.json()["clusters"][0]
    assert cluster["warm_pool"]["supported"] is False
    assert cluster["warm_pool"]["reason"] == (
        "The warm-pool status source could not be read."
    )
    assert cluster["recent_claim_rate"]["supported"] is False
