"""Fair-use limit enforcement: concurrent-sandbox cap, monthly usage cap,
and the background reaper's per-session wall-clock cap. No dollar amounts or
plan names should ever appear in any of these responses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from control_plane import db as db_module
from control_plane.config import settings
from control_plane.reaper import _reap_once
from control_plane.repository import SandboxSessionRepository
from conftest import FakeSandboxManager, signup_and_get_api_key


async def test_concurrent_sandbox_limit_returns_429_with_no_pricing_language(
    client: httpx.AsyncClient, monkeypatch
):
    monkeypatch.setattr(settings, "BOXKITE_MAX_CONCURRENT_SANDBOXES", 1)
    key = await signup_and_get_api_key(client, "concurrency-limit@example.com")

    first = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    assert first.status_code == 201

    second = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})

    assert second.status_code == 429
    body = second.json()
    assert body["error"]["code"] == "concurrent_sandbox_limit_reached"
    _assert_no_pricing_language(body)


async def test_destroying_a_session_frees_a_concurrency_slot(
    client: httpx.AsyncClient, monkeypatch
):
    monkeypatch.setattr(settings, "BOXKITE_MAX_CONCURRENT_SANDBOXES", 1)
    key = await signup_and_get_api_key(client, "concurrency-free@example.com")

    first = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    session_id = first.json()["id"]

    blocked = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    assert blocked.status_code == 429

    delete_resp = await client.delete(
        f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert delete_resp.status_code == 204

    second = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    assert second.status_code == 201


async def test_monthly_usage_limit_returns_429_with_no_pricing_language(
    client: httpx.AsyncClient, monkeypatch
):
    """Fast-forward usage by seeding an already-destroyed session that alone
    consumes the entire monthly cap, rather than sleeping in real time."""
    monkeypatch.setattr(settings, "BOXKITE_FREE_MONTHLY_SANDBOX_HOURS", 0.01)  # 36 seconds
    key = await signup_and_get_api_key(client, "monthly-limit@example.com")

    # Consume the whole monthly cap with one long-since-destroyed session.
    async with db_module.get_session_factory()() as db:
        from control_plane.repository import AccountRepository

        account = await AccountRepository(db).get_by_email("monthly-limit@example.com")
        sessions = SandboxSessionRepository(db)
        now = datetime.now(timezone.utc)
        row = await sessions.create(session_id="seed-session", account_id=account.id, pod_name="seed-pod")
        row.created_at = now - timedelta(hours=1)
        await sessions.mark_destroyed(session_id="seed-session", duration_seconds=3600, reason="test_seed")

    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})

    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "monthly_usage_limit_reached"
    _assert_no_pricing_language(body)


async def test_reaper_destroys_sessions_past_max_session_minutes(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    monkeypatch.setattr(settings, "BOXKITE_MAX_SESSION_MINUTES", 30)
    key = await signup_and_get_api_key(client, "reaper-user@example.com")

    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    session_id = create_resp.json()["id"]

    # Backdate the session so it looks like it was created 31 minutes ago,
    # without needing to actually sleep 31 minutes in the test.
    from sqlalchemy import select

    from control_plane.models_orm import SandboxSession

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(SandboxSession).where(SandboxSession.id == session_id))
        row = result.scalar_one()
        row.created_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        await db.commit()

    assert session_id not in fake_manager.destroyed

    await _reap_once(fake_manager)

    assert session_id in fake_manager.destroyed

    list_resp = await client.get("/v1/sandboxes", headers={"Authorization": f"Bearer {key}"})
    session_after = next(s for s in list_resp.json() if s["id"] == session_id)
    assert session_after["status"] == "destroyed"


def _assert_no_pricing_language(body: dict) -> None:
    flat = str(body).lower()
    for banned in ("$", "dollar", "price", "pricing", "plan", "tier", "subscription", "billing"):
        assert banned not in flat, f"found banned pricing/plan language {banned!r} in {body!r}"
