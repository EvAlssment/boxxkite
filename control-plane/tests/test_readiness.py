"""Liveness (/health) and readiness (/health/ready) probe behavior."""

from __future__ import annotations

import httpx


async def test_health_is_static_liveness(client: httpx.AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_ok_when_db_reachable(client: httpx.AsyncClient):
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"


async def test_readiness_returns_503_when_db_unreachable(
    client: httpx.AsyncClient, monkeypatch
):
    class _BoomEngine:
        def connect(self):
            raise RuntimeError("simulated database outage")

    monkeypatch.setattr("control_plane.main.get_engine", lambda: _BoomEngine())
    resp = await client.get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["database"] == "unreachable"
