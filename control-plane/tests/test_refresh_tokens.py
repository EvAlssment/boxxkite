"""Refresh-token rotation (issue #79), opt-in via BOXKITE_REFRESH_TOKENS_ENABLED.

Off by default: signup/login must keep returning `refresh_token: null` and
POST /v1/auth/refresh must 404 until a deployment explicitly enables it --
see test_refresh_tokens_absent_by_default / test_refresh_endpoint_404s_by_default.
"""

from __future__ import annotations

import httpx

from conftest import signup


async def test_refresh_token_absent_by_default(client: httpx.AsyncClient):
    body = await signup(client, "no-refresh-by-default@example.com")
    assert body["refresh_token"] is None


async def test_refresh_endpoint_404s_by_default(client: httpx.AsyncClient):
    resp = await client.post("/v1/auth/refresh", json={"refresh_token": "whatever"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "feature_disabled"


async def test_logout_endpoint_404s_by_default(client: httpx.AsyncClient):
    resp = await client.post("/v1/auth/logout", json={"refresh_token": "whatever"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "feature_disabled"


async def test_signup_returns_refresh_token_when_enabled(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)

    body = await signup(client, "with-refresh@example.com")
    assert body["refresh_token"] is not None
    assert isinstance(body["refresh_token"], str)
    assert len(body["refresh_token"]) > 20


async def test_login_returns_refresh_token_when_enabled(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)

    await signup(client, "with-refresh-login@example.com", password="correct-horse-1")
    resp = await client.post(
        "/v1/auth/login", json={"email": "with-refresh-login@example.com", "password": "correct-horse-1"}
    )
    assert resp.status_code == 200
    assert resp.json()["refresh_token"] is not None


async def test_refresh_rotates_and_returns_new_access_and_refresh_token(
    client: httpx.AsyncClient, monkeypatch
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)

    signup_resp = await signup(client, "rotate@example.com")
    old_refresh = signup_resp["refresh_token"]
    old_access = signup_resp["access_token"]

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["refresh_token"] != old_refresh
    # NOTE: access_token is NOT asserted to differ from old_access -- the JWT
    # payload (sub/email/type/iat/exp) can be byte-identical if both are
    # minted within the same wall-clock second, since HS256 signing is
    # deterministic. What actually matters (and is asserted above) is that
    # the refresh token itself is rotated to a brand new value.
    assert old_access
    assert body["account"]["email"] == "rotate@example.com"


async def test_refresh_rejects_unknown_token(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_refresh_token"


async def test_refresh_token_reuse_is_detected_and_revokes_whole_account(
    client: httpx.AsyncClient, monkeypatch
):
    """The core rotation guarantee: presenting an already-rotated-out
    refresh token a second time must not just fail -- it must revoke every
    other still-valid refresh token on the account too (theft response)."""
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)

    signup_resp = await signup(client, "reuse-detect@example.com")
    first_refresh = signup_resp["refresh_token"]

    # Rotate once -- this revokes first_refresh and mints a second one.
    first_rotate = await client.post("/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert first_rotate.status_code == 200
    second_refresh = first_rotate.json()["refresh_token"]

    # Replay the already-revoked first token.
    replay = await client.post("/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_reused"

    # The second (legitimately rotated-to) token must now ALSO be dead --
    # reuse detection revokes the whole account, not just the replayed
    # token. It was itself marked revoked by the incident-response revoke,
    # so presenting it now surfaces the same "already revoked" signal.
    second_use = await client.post("/v1/auth/refresh", json={"refresh_token": second_refresh})
    assert second_use.status_code == 401
    assert second_use.json()["error"]["code"] == "refresh_token_reused"


async def test_logout_revokes_refresh_token(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)

    signup_resp = await signup(client, "logout@example.com")
    refresh_token = signup_resp["refresh_token"]

    logout_resp = await client.post("/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout_resp.status_code == 204

    refresh_resp = await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_resp.status_code == 401


async def test_logout_with_unknown_token_is_a_silent_no_op(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)

    resp = await client.post("/v1/auth/logout", json={"refresh_token": "never-issued"})
    assert resp.status_code == 204


async def test_refresh_rejects_expired_token(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)
    # Negative TTL means the token is already expired the instant it's minted.
    monkeypatch.setattr(settings, "REFRESH_TOKEN_TTL_DAYS", -1)

    signup_resp = await signup(client, "expired-refresh@example.com")
    expired_refresh = signup_resp["refresh_token"]

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": expired_refresh})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_refresh_token"


async def test_refresh_rate_limited_after_repeated_attempts(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)
    monkeypatch.setattr(settings, "BOXKITE_REFRESH_RATE_LIMIT_PER_MINUTE", 3)

    for _ in range(3):
        await client.post("/v1/auth/refresh", json={"refresh_token": "whatever"})

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": "whatever"})
    assert resp.status_code == 429
