"""Rate-limit response headers -- callers should be able to back off
intelligently instead of parsing 429 bodies to guess when to retry.
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def test_successful_request_includes_rate_limit_headers(client: httpx.AsyncClient):
    # Signup rather than a failed login: a raised ApiError discards whatever
    # headers were set on the injected Response, since FastAPI's exception
    # handler builds its own response -- only a normal return carries them.
    resp = await client.post(
        "/v1/auth/signup", json={"email": "rl-headers@example.com", "password": "correcthorse123"}
    )
    assert resp.status_code == 201
    assert "X-RateLimit-Limit" in resp.headers
    assert "X-RateLimit-Remaining" in resp.headers
    assert int(resp.headers["X-RateLimit-Remaining"]) < int(resp.headers["X-RateLimit-Limit"])


async def test_remaining_decreases_across_requests(client: httpx.AsyncClient):
    # Same IP-keyed bucket for signup regardless of email, so two distinct
    # successful signups still share one decrementing counter.
    first = await client.post(
        "/v1/auth/signup", json={"email": "rl-first@example.com", "password": "correcthorse123"}
    )
    second = await client.post(
        "/v1/auth/signup", json={"email": "rl-second@example.com", "password": "correcthorse123"}
    )

    remaining_first = int(first.headers["X-RateLimit-Remaining"])
    remaining_second = int(second.headers["X-RateLimit-Remaining"])
    assert remaining_second == remaining_first - 1


async def test_429_response_includes_retry_after_and_zero_remaining(client: httpx.AsyncClient):
    for _ in range(10):
        await client.post("/v1/auth/login", json={"email": "flood@example.com", "password": "x"})

    resp = await client.post("/v1/auth/login", json={"email": "flood@example.com", "password": "x"})
    assert resp.status_code == 429
    assert resp.headers["X-RateLimit-Remaining"] == "0"
    assert "Retry-After" in resp.headers


async def test_sandbox_exec_route_includes_rate_limit_headers(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    api_key = await signup_and_get_api_key(client, "rl-account@example.com")
    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    session_id = create_resp.json()["id"]

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert "X-RateLimit-Limit" in resp.headers
    assert resp.headers["X-RateLimit-Limit"] == "120"


async def test_sandbox_create_route_includes_lifecycle_rate_limit_headers(client: httpx.AsyncClient):
    api_key = await signup_and_get_api_key(client, "rl-lifecycle-create@example.com")

    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201
    assert resp.headers["X-RateLimit-Limit"] == "20"


async def test_sandbox_create_route_is_rate_limited_separately_from_exec(
    client: httpx.AsyncClient, monkeypatch
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_SANDBOX_LIFECYCLE_RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(settings, "BOXKITE_MAX_CONCURRENT_SANDBOXES", 100)
    monkeypatch.setattr(settings, "BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES", 100)

    api_key = await signup_and_get_api_key(client, "rl-lifecycle-flood@example.com")

    for _ in range(2):
        resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 201

    blocked = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["error"]["code"] == "rate_limited"


async def test_sandbox_delete_route_is_rate_limited(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_SANDBOX_LIFECYCLE_RATE_LIMIT_PER_MINUTE", 3)
    monkeypatch.setattr(settings, "BOXKITE_MAX_CONCURRENT_SANDBOXES", 100)
    monkeypatch.setattr(settings, "BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES", 100)

    api_key = await signup_and_get_api_key(client, "rl-lifecycle-delete@example.com")

    # 1 create + 2 deletes below == 3 lifecycle calls, right at the limit.
    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    session_id = create_resp.json()["id"]

    first_delete = await client.delete(
        f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert first_delete.status_code == 204

    # Deleting an already-destroyed session still counts against the
    # lifecycle bucket -- the rate limit check runs before the 404.
    second_delete = await client.delete(
        f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert second_delete.status_code == 404

    third_delete = await client.delete(
        f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert third_delete.status_code == 429
