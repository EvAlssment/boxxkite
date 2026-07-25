"""Tests for SCIM 2.0 provisioning via WorkOS Directory Sync
(routers/scim.py, Phase 2 of issue #126, docs/ENTERPRISE-SSO-DESIGN.md).

Covers:
- The route 404s unless BOTH BOXKITE_SCIM_PROVISIONING_ENABLED and
  WORKOS_WEBHOOK_SECRET are set (mirrors every other opt-in
  auth/provisioning surface's "simply inactive" contract).
- WorkOS-Signature verification: a correctly signed request is accepted; a
  missing header, a tampered body, a wrong secret, and a stale timestamp
  are all rejected with 401 invalid_signature.
- dsync.user.created provisions a new, passwordless Account keyed by the
  WorkOS directory user id -- distinct from sso_provider_user_id.
- Redelivery (the same directory_user_id twice) is idempotent -- no
  duplicate account.
- An email collision with an existing, differently-provisioned account is
  skipped (not linked, not raised as a hard failure -- the webhook is
  still acknowledged).
- dsync.user.updated with state="inactive"/"suspended" deactivates the
  linked account, and deactivation ACTUALLY blocks further authentication
  -- password login, an already-issued API key, and an already-issued
  dashboard JWT all stop working on the very next request.
- state="active" on a previously deactivated account reactivates it.
- dsync.user.deleted deactivates (soft) rather than hard-deleting the
  Account row.
- An unhandled event type (e.g. dsync.group.created) is acknowledged
  (200) without acting on it.
- The one deliberate cross-feature interaction: a SCIM-provisioned
  account (no password, no social identity) completing its first
  interactive enterprise-SSO login auto-links rather than hitting the
  usual email-collision 409 -- see enterprise_sso.py's
  `_is_scim_only_shell`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest
from sqlalchemy import select

from conftest import create_api_key, signup
from control_plane import db as db_module
from control_plane.config import settings
from control_plane.enterprise_sso_client import EnterpriseSsoProfile
from control_plane.models_orm import Account
from control_plane.repository import AccountRepository
from control_plane.routers import enterprise_sso

SCIM_SECRET = "wh_sec_test_123"


def _sign(secret: str, timestamp_ms: int, body: bytes) -> str:
    signed_data = f"{timestamp_ms}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), signed_data, hashlib.sha256).hexdigest()


def _signature_header(secret: str, body: bytes, *, timestamp_ms: int | None = None) -> str:
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    signature = _sign(secret, timestamp_ms, body)
    return f"t={timestamp_ms},v1={signature}"


def _enable_scim(monkeypatch, secret: str = SCIM_SECRET) -> None:
    monkeypatch.setattr(settings, "BOXKITE_SCIM_PROVISIONING_ENABLED", True)
    monkeypatch.setattr(settings, "WORKOS_WEBHOOK_SECRET", secret)


def _dsync_user_event(*, event: str, directory_user_id: str, email: str, state: str = "active", organization_id: str | None = "org_1") -> bytes:
    payload = {
        "id": "event_01ABC",
        "event": event,
        "created_at": "2026-07-13T00:00:00.000Z",
        "data": {
            "id": directory_user_id,
            "directory_id": "directory_01XYZ",
            "organization_id": organization_id,
            "idp_id": "8931",
            "emails": [{"primary": True, "type": "work", "value": email}],
            "first_name": "Jordan",
            "last_name": "Chen",
            "username": email,
            "state": state,
        },
    }
    return json.dumps(payload).encode("utf-8")


async def _post_scim_webhook(client: httpx.AsyncClient, body: bytes, header: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if header is not None:
        headers["WorkOS-Signature"] = header
    return await client.post("/v1/auth/sso/scim-webhook", content=body, headers=headers)


# ── Gating ───────────────────────────────────────────────────────────────
async def test_scim_webhook_404s_when_disabled(client: httpx.AsyncClient):
    body = _dsync_user_event(event="dsync.user.created", directory_user_id="du_1", email="a@example.com")
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 404


async def test_scim_webhook_404s_when_only_master_flag_set(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_SCIM_PROVISIONING_ENABLED", True)
    # No WORKOS_WEBHOOK_SECRET configured -- still 404.
    body = _dsync_user_event(event="dsync.user.created", directory_user_id="du_1", email="a@example.com")
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 404


def test_scim_provisioning_defaults_off():
    assert settings.BOXKITE_SCIM_PROVISIONING_ENABLED is False


# ── Signature verification ───────────────────────────────────────────────
async def test_scim_webhook_accepts_valid_signature(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = _dsync_user_event(event="dsync.user.created", directory_user_id="du_valid", email="valid@enterprise.example.com")
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True}


async def test_scim_webhook_rejects_missing_signature_header(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = _dsync_user_event(event="dsync.user.created", directory_user_id="du_1", email="a@example.com")
    resp = await _post_scim_webhook(client, body, None)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


async def test_scim_webhook_rejects_tampered_body(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = _dsync_user_event(event="dsync.user.created", directory_user_id="du_1", email="a@example.com")
    header = _signature_header(SCIM_SECRET, body)
    tampered_body = _dsync_user_event(event="dsync.user.created", directory_user_id="du_1", email="attacker@evil.example.com")
    resp = await _post_scim_webhook(client, tampered_body, header)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


async def test_scim_webhook_rejects_wrong_secret(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = _dsync_user_event(event="dsync.user.created", directory_user_id="du_1", email="a@example.com")
    header = _signature_header("wrong-secret", body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


async def test_scim_webhook_rejects_stale_timestamp(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = _dsync_user_event(event="dsync.user.created", directory_user_id="du_1", email="a@example.com")
    stale_timestamp_ms = int(time.time() * 1000) - (settings.SCIM_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS + 60) * 1000
    header = _signature_header(SCIM_SECRET, body, timestamp_ms=stale_timestamp_ms)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


async def test_scim_webhook_rejects_malformed_header(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = _dsync_user_event(event="dsync.user.created", directory_user_id="du_1", email="a@example.com")
    resp = await _post_scim_webhook(client, body, "not-a-real-signature-header")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


# ── Account provisioning ─────────────────────────────────────────────────
async def test_dsync_user_created_provisions_passwordless_account(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = _dsync_user_event(
        event="dsync.user.created", directory_user_id="du_new_1", email="newhire@enterprise.example.com", organization_id="org_42"
    )
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 200

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_scim_directory_user_id("du_new_1")
        assert account is not None
        assert account.email == "newhire@enterprise.example.com"
        assert account.password_hash is None
        assert account.sso_provider_user_id is None
        assert account.sso_organization_id == "org_42"
        assert account.scim_deactivated_at is None


async def test_dsync_user_created_redelivery_is_idempotent(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = _dsync_user_event(event="dsync.user.created", directory_user_id="du_dup", email="dup@enterprise.example.com")
    header = _signature_header(SCIM_SECRET, body)
    resp1 = await _post_scim_webhook(client, body, header)
    assert resp1.status_code == 200
    header2 = _signature_header(SCIM_SECRET, body)
    resp2 = await _post_scim_webhook(client, body, header2)
    assert resp2.status_code == 200

    async with db_module.get_session_factory()() as db:
        accounts = AccountRepository(db)
        account = await accounts.get_by_email("dup@enterprise.example.com")
        assert account is not None
        # Only one account total for this directory user id.
        result = await db.execute(select(Account).where(Account.scim_directory_user_id == "du_dup"))
        rows = result.scalars().all()
        assert len(rows) == 1


async def test_dsync_user_created_skips_on_email_collision(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    existing = await signup(client, "collision@enterprise.example.com", password="hunter2pass")
    existing_account_id = existing["account"]["id"]

    body = _dsync_user_event(
        event="dsync.user.created", directory_user_id="du_collision", email="collision@enterprise.example.com"
    )
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 200  # acknowledged, not a hard failure

    async with db_module.get_session_factory()() as db:
        accounts = AccountRepository(db)
        # No new account was linked to this directory user id.
        assert await accounts.get_by_scim_directory_user_id("du_collision") is None
        # The pre-existing password account is untouched.
        existing_account = await accounts.get_by_id(existing_account_id)
        assert existing_account.scim_directory_user_id is None
        assert existing_account.password_hash is not None


async def test_dsync_user_deleted_deactivates_without_hard_delete(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    created_body = _dsync_user_event(
        event="dsync.user.created", directory_user_id="du_delete_me", email="leaving@enterprise.example.com"
    )
    await _post_scim_webhook(client, created_body, _signature_header(SCIM_SECRET, created_body))

    deleted_payload = {
        "id": "event_del",
        "event": "dsync.user.deleted",
        "created_at": "2026-07-13T00:00:00.000Z",
        "data": {"id": "du_delete_me"},
    }
    deleted_body = json.dumps(deleted_payload).encode("utf-8")
    resp = await _post_scim_webhook(client, deleted_body, _signature_header(SCIM_SECRET, deleted_body))
    assert resp.status_code == 200

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_scim_directory_user_id("du_delete_me")
        # Row still exists (soft, not hard, delete) -- but deactivated.
        assert account is not None
        assert account.scim_deactivated_at is not None


async def test_scim_webhook_ignores_unhandled_event_type(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    payload = {
        "id": "event_group",
        "event": "dsync.group.created",
        "created_at": "2026-07-13T00:00:00.000Z",
        "data": {"id": "group_1", "name": "Engineering"},
    }
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_scim_webhook(client, body, _signature_header(SCIM_SECRET, body))
    assert resp.status_code == 200
    assert resp.json() == {"received": True}


# ── Deactivation actually blocks authentication ──────────────────────────
async def test_deactivation_blocks_password_login_api_key_and_jwt(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)

    token_response = await signup(client, "deactivate-me@enterprise.example.com", password="hunter2pass")
    access_token = token_response["access_token"]
    key_response = await create_api_key(client, access_token, name="pre-deactivation key")
    api_key = key_response["key"]
    account_id = token_response["account"]["id"]

    # Link this pre-existing account to a directory user id directly (the
    # webhook's own email-collision path would correctly refuse to do this
    # linking itself -- see test_dsync_user_created_skips_on_email_collision
    # -- so seed the link the same way a completed enterprise-SSO login
    # would have, to isolate the deactivation behavior under test).
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one()
        account.scim_directory_user_id = "du_active_user"
        await db.commit()

    # Sanity: both credentials work before deactivation.
    sandbox_resp = await client.get("/v1/usage", headers={"Authorization": f"Bearer {api_key}"})
    assert sandbox_resp.status_code == 200
    dashboard_resp = await client.get(
        "/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert dashboard_resp.status_code == 200

    deactivate_body = _dsync_user_event(
        event="dsync.user.updated",
        directory_user_id="du_active_user",
        email="deactivate-me@enterprise.example.com",
        state="inactive",
    )
    resp = await _post_scim_webhook(client, deactivate_body, _signature_header(SCIM_SECRET, deactivate_body))
    assert resp.status_code == 200

    # Already-issued API key: rejected on the very next request.
    sandbox_resp_after = await client.get("/v1/usage", headers={"Authorization": f"Bearer {api_key}"})
    assert sandbox_resp_after.status_code == 401
    assert sandbox_resp_after.json()["error"]["code"] == "account_deactivated"

    # Already-issued dashboard JWT: rejected on the very next request.
    dashboard_resp_after = await client.get(
        "/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert dashboard_resp_after.status_code == 401
    assert dashboard_resp_after.json()["error"]["code"] == "account_deactivated"

    # Fresh password login: also rejected.
    login_resp = await client.post(
        "/v1/auth/login",
        json={"email": "deactivate-me@enterprise.example.com", "password": "hunter2pass"},
    )
    assert login_resp.status_code == 403
    assert login_resp.json()["error"]["code"] == "account_deactivated"


async def test_dsync_user_updated_active_reactivates_account(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    created_body = _dsync_user_event(
        event="dsync.user.created", directory_user_id="du_reactivate", email="reactivate@enterprise.example.com"
    )
    await _post_scim_webhook(client, created_body, _signature_header(SCIM_SECRET, created_body))

    deactivate_body = _dsync_user_event(
        event="dsync.user.updated", directory_user_id="du_reactivate", email="reactivate@enterprise.example.com", state="suspended"
    )
    await _post_scim_webhook(client, deactivate_body, _signature_header(SCIM_SECRET, deactivate_body))

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_scim_directory_user_id("du_reactivate")
        assert account.scim_deactivated_at is not None

    reactivate_body = _dsync_user_event(
        event="dsync.user.updated", directory_user_id="du_reactivate", email="reactivate@enterprise.example.com", state="active"
    )
    resp = await _post_scim_webhook(client, reactivate_body, _signature_header(SCIM_SECRET, reactivate_body))
    assert resp.status_code == 200

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_scim_directory_user_id("du_reactivate")
        assert account.scim_deactivated_at is None


# ── Cross-feature interaction: SCIM shell + first interactive SSO login ──
async def test_scim_provisioned_account_auto_links_on_first_sso_login(
    client: httpx.AsyncClient, monkeypatch, fake_enterprise_sso_client
):
    _enable_scim(monkeypatch)
    monkeypatch.setattr(settings, "BOXKITE_ENTERPRISE_SSO_ENABLED", True)
    monkeypatch.setattr(settings, "WORKOS_CLIENT_ID", "workos-client-id")
    monkeypatch.setattr(settings, "WORKOS_API_KEY", "workos-api-key")
    monkeypatch.setattr(enterprise_sso, "get_enterprise_sso_client", lambda: fake_enterprise_sso_client)

    created_body = _dsync_user_event(
        event="dsync.user.created",
        directory_user_id="du_first_login",
        email="firstlogin@enterprise.example.com",
        organization_id="org_1",
    )
    await _post_scim_webhook(client, created_body, _signature_header(SCIM_SECRET, created_body))

    async with db_module.get_session_factory()() as db:
        shell_account = await AccountRepository(db).get_by_scim_directory_user_id("du_first_login")
        assert shell_account.sso_provider_user_id is None
        shell_account_id = shell_account.id

    fake_enterprise_sso_client.seed_profile(
        "auth-code-first-login",
        EnterpriseSsoProfile(
            provider_user_id="prof_first_login",
            email="firstlogin@enterprise.example.com",
            organization_id="org_1",
            connection_id="conn_1",
        ),
    )

    from control_plane.security import create_enterprise_sso_state_token

    state = create_enterprise_sso_state_token(connection="conn_1", next_path=None)
    resp = await client.get(
        "/v1/auth/sso/callback", params={"code": "auth-code-first-login", "state": state}
    )
    # NOT the usual 409 account_email_exists -- this is the one deliberate
    # auto-link exception.
    assert resp.status_code == 200, resp.text
    assert resp.json()["account"]["id"] == shell_account_id

    async with db_module.get_session_factory()() as db:
        linked_account = await AccountRepository(db).get_by_id(shell_account_id)
        assert linked_account.sso_provider_user_id == "prof_first_login"
        assert linked_account.sso_connection_id == "conn_1"


async def test_scim_shell_with_different_organization_does_not_auto_link(
    client: httpx.AsyncClient, monkeypatch, fake_enterprise_sso_client
):
    """Cross-tenant account-takeover regression test: this control-plane can
    serve multiple enterprise customers over one WorkOS project, each with
    their own `connection` (disclosed Phase-1 scope cut -- `GET
    /v1/auth/sso/start`'s `connection` query param is caller-supplied). An
    admin of Customer A's own IdP must NOT be able to assert an SSO login
    for an email that happens to match a SCIM-provisioned shell account
    belonging to Customer B and get auto-linked to (i.e. take over)
    Customer B's account -- the auto-link exception must be bound to the
    SAME organization SCIM provisioned the shell account under, not email
    alone."""
    _enable_scim(monkeypatch)
    monkeypatch.setattr(settings, "BOXKITE_ENTERPRISE_SSO_ENABLED", True)
    monkeypatch.setattr(settings, "WORKOS_CLIENT_ID", "workos-client-id")
    monkeypatch.setattr(settings, "WORKOS_API_KEY", "workos-api-key")
    monkeypatch.setattr(enterprise_sso, "get_enterprise_sso_client", lambda: fake_enterprise_sso_client)

    # Customer B's shell account, provisioned by Customer B's own SCIM
    # directory under org_b.
    created_body = _dsync_user_event(
        event="dsync.user.created",
        directory_user_id="du_cross_tenant_victim",
        email="cross-tenant@enterprise.example.com",
        organization_id="org_b",
    )
    await _post_scim_webhook(client, created_body, _signature_header(SCIM_SECRET, created_body))

    async with db_module.get_session_factory()() as db:
        shell_account = await AccountRepository(db).get_by_scim_directory_user_id("du_cross_tenant_victim")
        assert shell_account is not None
        assert shell_account.sso_organization_id == "org_b"
        shell_account_id = shell_account.id

    # Customer A's own IdP asserts an SSO login for the SAME email, but
    # under Customer A's organization_id/connection_id (org_a/conn_a) --
    # this is the attacker-controlled side: nothing here proves ownership
    # of Customer B's directory identity.
    fake_enterprise_sso_client.seed_profile(
        "auth-code-cross-tenant-attack",
        EnterpriseSsoProfile(
            provider_user_id="prof_cross_tenant_attacker",
            email="cross-tenant@enterprise.example.com",
            organization_id="org_a",
            connection_id="conn_a",
        ),
    )

    from control_plane.security import create_enterprise_sso_state_token

    state = create_enterprise_sso_state_token(connection="conn_a", next_path=None)
    resp = await client.get(
        "/v1/auth/sso/callback", params={"code": "auth-code-cross-tenant-attack", "state": state}
    )
    # Falls through to the SAME refusal an ordinary email collision gets --
    # NOT auto-linked to Customer B's account.
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "account_email_exists"

    async with db_module.get_session_factory()() as db:
        shell_account_after = await AccountRepository(db).get_by_id(shell_account_id)
        assert shell_account_after is not None
        # Untouched -- no identity was linked from the mismatched-org login.
        assert shell_account_after.sso_provider_user_id is None
        assert shell_account_after.sso_connection_id is None
        assert shell_account_after.sso_organization_id == "org_b"
