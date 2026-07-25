"""Password-reset flow (issue #79), opt-in via BOXKITE_PASSWORD_RESET_ENABLED.

Email delivery is stubbed (email_sender.py) -- these tests intercept the
raw token via the `fake_email_sender` fixture (see conftest.py), the same
way a real deployment's mail transport would deliver it to the user.
"""

from __future__ import annotations

import httpx

from conftest import FakeEmailSender, signup


async def test_password_reset_request_404s_by_default(client: httpx.AsyncClient):
    resp = await client.post("/v1/auth/password-reset/request", json={"email": "nobody@example.com"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "feature_disabled"


async def test_password_reset_confirm_404s_by_default(client: httpx.AsyncClient):
    resp = await client.post(
        "/v1/auth/password-reset/confirm", json={"token": "whatever", "new_password": "newpassword1"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "feature_disabled"


async def test_request_reset_for_existing_account_sends_email_with_token(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_ENABLED", True)

    await signup(client, "reset-me@example.com", password="original-pass-1")

    resp = await client.post("/v1/auth/password-reset/request", json={"email": "reset-me@example.com"})
    assert resp.status_code == 200
    assert "message" in resp.json()

    assert len(fake_email_sender.password_reset_calls) == 1
    call = fake_email_sender.password_reset_calls[0]
    assert call["to_email"] == "reset-me@example.com"
    assert call["reset_token"]


async def test_request_reset_for_unknown_account_returns_identical_response(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    """No account-enumeration signal: same response, no email actually sent."""
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_ENABLED", True)

    resp_known_shape = await client.post(
        "/v1/auth/password-reset/request", json={"email": "never-signed-up-reset@example.com"}
    )
    assert resp_known_shape.status_code == 200
    assert resp_known_shape.json()["message"] == (
        "If an account with that email exists, a password reset link has been sent."
    )
    assert fake_email_sender.password_reset_calls == []


async def test_confirm_reset_updates_password_and_old_password_stops_working(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_ENABLED", True)

    await signup(client, "confirm-reset@example.com", password="original-pass-1")
    await client.post("/v1/auth/password-reset/request", json={"email": "confirm-reset@example.com"})
    raw_token = fake_email_sender.password_reset_calls[0]["reset_token"]

    confirm_resp = await client.post(
        "/v1/auth/password-reset/confirm", json={"token": raw_token, "new_password": "brand-new-pass-1"}
    )
    assert confirm_resp.status_code == 200

    old_login = await client.post(
        "/v1/auth/login", json={"email": "confirm-reset@example.com", "password": "original-pass-1"}
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/v1/auth/login", json={"email": "confirm-reset@example.com", "password": "brand-new-pass-1"}
    )
    assert new_login.status_code == 200


async def test_confirm_reset_token_is_single_use(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_ENABLED", True)

    await signup(client, "single-use@example.com", password="original-pass-1")
    await client.post("/v1/auth/password-reset/request", json={"email": "single-use@example.com"})
    raw_token = fake_email_sender.password_reset_calls[0]["reset_token"]

    first = await client.post(
        "/v1/auth/password-reset/confirm", json={"token": raw_token, "new_password": "first-new-pass-1"}
    )
    assert first.status_code == 200

    second = await client.post(
        "/v1/auth/password-reset/confirm", json={"token": raw_token, "new_password": "second-new-pass-1"}
    )
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "invalid_or_expired_token"


async def test_confirm_reset_rejects_unknown_token(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_ENABLED", True)

    resp = await client.post(
        "/v1/auth/password-reset/confirm", json={"token": "not-a-real-token", "new_password": "newpassword1"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_or_expired_token"


async def test_confirm_reset_rejects_short_new_password(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_ENABLED", True)

    await signup(client, "short-new-pass@example.com")
    await client.post("/v1/auth/password-reset/request", json={"email": "short-new-pass@example.com"})
    raw_token = fake_email_sender.password_reset_calls[0]["reset_token"]

    resp = await client.post(
        "/v1/auth/password-reset/confirm", json={"token": raw_token, "new_password": "short"}
    )
    assert resp.status_code == 422


async def test_new_reset_request_invalidates_previous_outstanding_token(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_ENABLED", True)

    await signup(client, "double-request@example.com", password="original-pass-1")
    await client.post("/v1/auth/password-reset/request", json={"email": "double-request@example.com"})
    first_token = fake_email_sender.password_reset_calls[0]["reset_token"]

    await client.post("/v1/auth/password-reset/request", json={"email": "double-request@example.com"})
    second_token = fake_email_sender.password_reset_calls[1]["reset_token"]

    stale_confirm = await client.post(
        "/v1/auth/password-reset/confirm", json={"token": first_token, "new_password": "brand-new-pass-1"}
    )
    assert stale_confirm.status_code == 400

    fresh_confirm = await client.post(
        "/v1/auth/password-reset/confirm", json={"token": second_token, "new_password": "brand-new-pass-1"}
    )
    assert fresh_confirm.status_code == 200


async def test_confirm_reset_revokes_outstanding_refresh_tokens(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    """A password reset is exactly the situation an existing session might
    be compromised in -- confirming a reset must kill any outstanding
    refresh token too, not just change the password."""
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_ENABLED", True)
    monkeypatch.setattr(settings, "BOXKITE_REFRESH_TOKENS_ENABLED", True)

    signup_resp = await signup(client, "reset-kills-refresh@example.com", password="original-pass-1")
    refresh_token = signup_resp["refresh_token"]
    assert refresh_token is not None

    await client.post("/v1/auth/password-reset/request", json={"email": "reset-kills-refresh@example.com"})
    raw_token = fake_email_sender.password_reset_calls[0]["reset_token"]
    await client.post(
        "/v1/auth/password-reset/confirm", json={"token": raw_token, "new_password": "brand-new-pass-1"}
    )

    refresh_attempt = await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_attempt.status_code == 401


async def test_confirm_reset_rejects_expired_token(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_ENABLED", True)
    # Negative TTL means the token is already expired the instant it's minted.
    monkeypatch.setattr(settings, "PASSWORD_RESET_TOKEN_TTL_MINUTES", -1)

    await signup(client, "expired-reset@example.com", password="original-pass-1")
    await client.post("/v1/auth/password-reset/request", json={"email": "expired-reset@example.com"})
    raw_token = fake_email_sender.password_reset_calls[0]["reset_token"]

    resp = await client.post(
        "/v1/auth/password-reset/confirm", json={"token": raw_token, "new_password": "brand-new-pass-1"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_or_expired_token"


async def test_password_reset_request_rate_limited(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_ENABLED", True)
    monkeypatch.setattr(settings, "BOXKITE_PASSWORD_RESET_RATE_LIMIT_PER_MINUTE", 2)

    for _ in range(2):
        await client.post("/v1/auth/password-reset/request", json={"email": "rate-limited-reset@example.com"})

    resp = await client.post(
        "/v1/auth/password-reset/request", json={"email": "rate-limited-reset@example.com"}
    )
    assert resp.status_code == 429
