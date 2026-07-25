"""GET /v1/usage/rollup -- the read-only compute-time/operation-count
attribution rollup over `ExecLogEntry` rows (GitHub issue #162).

Follows test_admin_audit_log.py's pattern for seeding real `ExecLogEntry`
rows directly via `ExecLogEntryRepository.create` against a real sandbox
session id (rather than driving seven separate `/exec`-style routes just to
get controlled `started_at`/`duration_ms` values), and
test_usage_limits.py's `_assert_no_pricing_language` for the project's
hard "no billing/pricing language anywhere" rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from control_plane import db as db_module
from control_plane.repository import AccountRepository, ExecLogEntryRepository
from conftest import signup_and_get_api_key
from test_usage_limits import _assert_no_pricing_language


async def _account_id_for(email: str) -> str:
    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_email(email)
        return account.id


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _seed_entry(
    *,
    session_id: str,
    account_id: str,
    operation: str,
    started_at: datetime,
    duration_ms: int,
) -> None:
    async with db_module.get_session_factory()() as db:
        await ExecLogEntryRepository(db).create(
            session_id=session_id,
            account_id=account_id,
            source="agent",
            operation=operation,
            detail={"command": "echo hi"} if operation == "exec" else {"path": "/tmp/f"},
            exit_code=0,
            output_truncated=None,
            started_at=started_at,
            duration_ms=duration_ms,
        )


DAY_1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
DAY_2 = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


async def test_rollup_group_by_operation_sums_duration_and_counts(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "rollup-operation@example.com")
    account_id = await _account_id_for("rollup-operation@example.com")
    session_id = await _create_session(client, key)

    await _seed_entry(
        session_id=session_id, account_id=account_id, operation="exec", started_at=DAY_1, duration_ms=100
    )
    await _seed_entry(
        session_id=session_id, account_id=account_id, operation="exec", started_at=DAY_1, duration_ms=50
    )
    await _seed_entry(
        session_id=session_id, account_id=account_id, operation="file_create", started_at=DAY_1, duration_ms=20
    )

    resp = await client.get(
        "/v1/usage/rollup?group_by=operation", headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["group_by"] == "operation"
    assert body["total_duration_ms"] == 170
    assert body["total_operation_count"] == 3
    assert body["group_count"] == 2

    groups_by_key = {g["key"]: g for g in body["groups"]}
    assert groups_by_key["exec"] == {"key": "exec", "duration_ms": 150, "operation_count": 2}
    assert groups_by_key["file_create"] == {"key": "file_create", "duration_ms": 20, "operation_count": 1}
    _assert_no_pricing_language(body)


async def test_rollup_group_by_session_separates_sessions(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "rollup-session@example.com")
    account_id = await _account_id_for("rollup-session@example.com")
    session_a = await _create_session(client, key)
    session_b = await _create_session(client, key)

    await _seed_entry(
        session_id=session_a, account_id=account_id, operation="exec", started_at=DAY_1, duration_ms=100
    )
    await _seed_entry(
        session_id=session_b, account_id=account_id, operation="exec", started_at=DAY_1, duration_ms=300
    )

    resp = await client.get(
        "/v1/usage/rollup?group_by=session", headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["group_by"] == "session"
    assert body["group_count"] == 2
    groups_by_key = {g["key"]: g for g in body["groups"]}
    assert groups_by_key[session_a]["duration_ms"] == 100
    assert groups_by_key[session_b]["duration_ms"] == 300
    # Ordered by duration_ms descending.
    assert body["groups"][0]["key"] == session_b


async def test_rollup_default_group_by_is_session(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "rollup-default@example.com")
    account_id = await _account_id_for("rollup-default@example.com")
    session_id = await _create_session(client, key)
    await _seed_entry(
        session_id=session_id, account_id=account_id, operation="exec", started_at=DAY_1, duration_ms=42
    )

    resp = await client.get("/v1/usage/rollup", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["group_by"] == "session"


async def test_rollup_group_by_day_buckets_by_calendar_day(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "rollup-day@example.com")
    account_id = await _account_id_for("rollup-day@example.com")
    session_id = await _create_session(client, key)

    await _seed_entry(
        session_id=session_id, account_id=account_id, operation="exec", started_at=DAY_1, duration_ms=10
    )
    await _seed_entry(
        session_id=session_id,
        account_id=account_id,
        operation="exec",
        started_at=DAY_1 + timedelta(hours=2),
        duration_ms=15,
    )
    await _seed_entry(
        session_id=session_id, account_id=account_id, operation="exec", started_at=DAY_2, duration_ms=1000
    )

    resp = await client.get(
        "/v1/usage/rollup?group_by=day", headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["group_count"] == 2
    groups_by_key = {g["key"]: g for g in body["groups"]}
    assert groups_by_key["2026-01-01"] == {"key": "2026-01-01", "duration_ms": 25, "operation_count": 2}
    assert groups_by_key["2026-01-02"] == {"key": "2026-01-02", "duration_ms": 1000, "operation_count": 1}


async def test_rollup_time_window_filters_rows_outside_range(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "rollup-window@example.com")
    account_id = await _account_id_for("rollup-window@example.com")
    session_id = await _create_session(client, key)

    await _seed_entry(
        session_id=session_id, account_id=account_id, operation="exec", started_at=DAY_1, duration_ms=10
    )
    await _seed_entry(
        session_id=session_id, account_id=account_id, operation="exec", started_at=DAY_2, duration_ms=990
    )

    resp = await client.get(
        "/v1/usage/rollup?start=2026-01-02T00:00:00Z&end=2026-01-03T00:00:00Z",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_duration_ms"] == 990
    assert body["total_operation_count"] == 1


async def test_rollup_start_after_end_returns_422(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "rollup-bad-range@example.com")

    resp = await client.get(
        "/v1/usage/rollup?start=2026-01-02T00:00:00Z&end=2026-01-01T00:00:00Z",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_range"


async def test_rollup_scopes_to_calling_account_only(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "rollup-account-a@example.com")
    account_a_id = await _account_id_for("rollup-account-a@example.com")
    session_a = await _create_session(client, key_a)
    await _seed_entry(
        session_id=session_a, account_id=account_a_id, operation="exec", started_at=DAY_1, duration_ms=500
    )

    key_b = await signup_and_get_api_key(client, "rollup-account-b@example.com")

    resp = await client.get("/v1/usage/rollup", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_duration_ms"] == 0
    assert body["total_operation_count"] == 0
    assert body["groups"] == []

    resp_a = await client.get("/v1/usage/rollup", headers={"Authorization": f"Bearer {key_a}"})
    assert resp_a.json()["total_duration_ms"] == 500


async def test_rollup_requires_authentication(client: httpx.AsyncClient):
    resp = await client.get("/v1/usage/rollup")
    assert resp.status_code == 401
