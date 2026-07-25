"""Email verification on signup (issue #79), opt-in via
BOXKITE_EMAIL_VERIFICATION_ENABLED.

Deliberately informational only today: verifying (or not) never blocks
login or any other route -- see test_unverified_account_can_still_log_in.
"""

from __future__ import annotations

import httpx

from conftest import FakeEmailSender, create_api_key, signup


async def test_verify_email_endpoint_404s_by_default(client: httpx.AsyncClient):
    resp = await client.post("/v1/auth/verify-email", json={"token": "whatever"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "feature_disabled"


async def test_resend_verification_404s_by_default(client: httpx.AsyncClient):
    signup_resp = await signup(client, "resend-disabled@example.com")
    resp = await client.post(
        "/v1/auth/resend-verification",
        headers={"Authorization": f"Bearer {signup_resp['access_token']}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "feature_disabled"


async def test_signup_sends_verification_email_when_enabled(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_EMAIL_VERIFICATION_ENABLED", True)

    body = await signup(client, "verify-me@example.com")
    assert body["account"]["email_verified_at"] is None

    assert len(fake_email_sender.verification_calls) == 1
    call = fake_email_sender.verification_calls[0]
    assert call["to_email"] == "verify-me@example.com"
    assert call["verification_token"]


async def test_signup_does_not_send_verification_email_when_disabled(
    client: httpx.AsyncClient, fake_email_sender: FakeEmailSender
):
    await signup(client, "no-verify-flag@example.com")
    assert fake_email_sender.verification_calls == []


async def test_verify_email_marks_account_verified(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_EMAIL_VERIFICATION_ENABLED", True)

    await signup(client, "verify-confirm@example.com")
    raw_token = fake_email_sender.verification_calls[0]["verification_token"]

    resp = await client.post("/v1/auth/verify-email", json={"token": raw_token})
    assert resp.status_code == 200

    # AccountOut now reflects the verified state via /v1/account/me.
    login_resp = await client.post(
        "/v1/auth/login", json={"email": "verify-confirm@example.com", "password": "hunter2pass"}
    )
    assert login_resp.json()["account"]["email_verified_at"] is not None


async def test_verify_email_token_is_single_use(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_EMAIL_VERIFICATION_ENABLED", True)

    await signup(client, "verify-single-use@example.com")
    raw_token = fake_email_sender.verification_calls[0]["verification_token"]

    first = await client.post("/v1/auth/verify-email", json={"token": raw_token})
    assert first.status_code == 200

    second = await client.post("/v1/auth/verify-email", json={"token": raw_token})
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "invalid_or_expired_token"


async def test_verify_email_rejects_unknown_token(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_EMAIL_VERIFICATION_ENABLED", True)

    resp = await client.post("/v1/auth/verify-email", json={"token": "not-a-real-token"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_or_expired_token"


async def test_verify_email_rejects_expired_token(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_EMAIL_VERIFICATION_ENABLED", True)
    # Negative TTL means the token is already expired the instant it's minted.
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_TOKEN_TTL_HOURS", -1)

    await signup(client, "expired-verify@example.com")
    raw_token = fake_email_sender.verification_calls[0]["verification_token"]

    resp = await client.post("/v1/auth/verify-email", json={"token": raw_token})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_or_expired_token"


async def test_resend_verification_sends_a_new_email(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_EMAIL_VERIFICATION_ENABLED", True)

    signup_resp = await signup(client, "resend@example.com")
    assert len(fake_email_sender.verification_calls) == 1

    resp = await client.post(
        "/v1/auth/resend-verification",
        headers={"Authorization": f"Bearer {signup_resp['access_token']}"},
    )
    assert resp.status_code == 200
    assert len(fake_email_sender.verification_calls) == 2


async def test_resend_verification_requires_dashboard_jwt_not_api_key(
    client: httpx.AsyncClient, monkeypatch
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_EMAIL_VERIFICATION_ENABLED", True)

    signup_resp = await signup(client, "resend-wrong-cred@example.com")
    created_key = await create_api_key(client, signup_resp["access_token"])

    resp = await client.post(
        "/v1/auth/resend-verification", headers={"Authorization": f"Bearer {created_key['key']}"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_resend_verification_no_ops_if_already_verified(
    client: httpx.AsyncClient, monkeypatch, fake_email_sender: FakeEmailSender
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_EMAIL_VERIFICATION_ENABLED", True)

    signup_resp = await signup(client, "already-verified@example.com")
    raw_token = fake_email_sender.verification_calls[0]["verification_token"]
    await client.post("/v1/auth/verify-email", json={"token": raw_token})

    resp = await client.post(
        "/v1/auth/resend-verification",
        headers={"Authorization": f"Bearer {signup_resp['access_token']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "Email is already verified."
    # No second email sent once already verified.
    assert len(fake_email_sender.verification_calls) == 1


async def test_unverified_account_can_still_log_in(client: httpx.AsyncClient, monkeypatch):
    """Verification is informational only today -- it must never lock a
    legitimate, unverified account out of login."""
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_EMAIL_VERIFICATION_ENABLED", True)

    await signup(client, "never-verifies@example.com", password="correct-horse-1")

    resp = await client.post(
        "/v1/auth/login", json={"email": "never-verifies@example.com", "password": "correct-horse-1"}
    )
    assert resp.status_code == 200
    assert resp.json()["account"]["email_verified_at"] is None
