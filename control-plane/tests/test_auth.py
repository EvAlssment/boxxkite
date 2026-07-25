"""Signup/login happy path, plus the basic negative cases."""

from __future__ import annotations

import httpx

from conftest import signup


async def test_signup_creates_account_and_returns_session_token(client: httpx.AsyncClient):
    body = await signup(client, "new-user@example.com")

    assert body["account"]["email"] == "new-user@example.com"
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] > 0
    # No password hash, no billing/plan concept anywhere in the response.
    assert "password" not in body["account"]
    assert "plan" not in body and "price" not in body


async def test_signup_rejects_duplicate_email(client: httpx.AsyncClient):
    await signup(client, "dupe@example.com")

    resp = await client.post(
        "/v1/auth/signup", json={"email": "dupe@example.com", "password": "anotherpass1"}
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "email_taken"


async def test_signup_rejects_duplicate_email_case_insensitively(client: httpx.AsyncClient):
    await signup(client, "CaseFold@example.com")

    resp = await client.post(
        "/v1/auth/signup", json={"email": "casefold@example.com", "password": "anotherpass1"}
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "email_taken"


async def test_signup_normalizes_email_to_lowercase(client: httpx.AsyncClient):
    body = await signup(client, "Mixed-Case@Example.com")

    assert body["account"]["email"] == "mixed-case@example.com"


async def test_login_succeeds_with_different_case_than_signup(client: httpx.AsyncClient):
    await signup(client, "CaseLogin@example.com", password="correct-horse-1")

    resp = await client.post(
        "/v1/auth/login", json={"email": "caselogin@example.com", "password": "correct-horse-1"}
    )

    assert resp.status_code == 200


async def test_signup_rejects_short_password(client: httpx.AsyncClient):
    resp = await client.post("/v1/auth/signup", json={"email": "short@example.com", "password": "abc"})

    assert resp.status_code == 422


async def test_login_succeeds_with_correct_credentials(client: httpx.AsyncClient):
    await signup(client, "login-user@example.com", password="correct-horse-1")

    resp = await client.post(
        "/v1/auth/login", json={"email": "login-user@example.com", "password": "correct-horse-1"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["account"]["email"] == "login-user@example.com"
    assert body["access_token"]


async def test_login_fails_with_wrong_password(client: httpx.AsyncClient):
    await signup(client, "wrongpass@example.com", password="correct-horse-1")

    resp = await client.post(
        "/v1/auth/login", json={"email": "wrongpass@example.com", "password": "totally-wrong-1"}
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credentials"


async def test_login_fails_for_unknown_email_with_identical_error(client: httpx.AsyncClient):
    """Unknown email and wrong password must be indistinguishable to the caller."""
    resp = await client.post(
        "/v1/auth/login", json={"email": "never-signed-up@example.com", "password": "whatever123"}
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credentials"


async def test_login_rejects_social_only_account_with_distinct_error(client: httpx.AsyncClient):
    from control_plane import db as db_module
    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        await AccountRepository(db).create_social(email="social-only@example.com", github_id="gh-777")

    resp = await client.post(
        "/v1/auth/login", json={"email": "social-only@example.com", "password": "whatever123"}
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "no_password_set"


async def test_login_rate_limited_after_repeated_attempts(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_AUTH_RATE_LIMIT_PER_MINUTE", 3)

    for _ in range(3):
        await client.post(
            "/v1/auth/login", json={"email": "rate-limited@example.com", "password": "whatever123"}
        )

    resp = await client.post(
        "/v1/auth/login", json={"email": "rate-limited@example.com", "password": "whatever123"}
    )

    assert resp.status_code == 429
