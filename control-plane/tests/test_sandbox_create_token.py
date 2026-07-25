"""Tests for GitHub issue #221: `POST /v1/account/sandbox-create-token`
(security.py's `create_sandbox_create_token`) and its redemption at
`POST /v1/sandboxes` via `deps.py`'s
`get_current_account_via_api_key_or_sandbox_create_token`.

Mirrors test_sandbox_log_watch_takeover.py's "POST .../takeover-token"
section -- same shape of single-use/expiry/account-binding coverage, plus
an explicit check that every OTHER /v1/sandboxes/* route still rejects
this token type (the trust boundary is only widened for creation).
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from conftest import FakeSandboxManager, create_api_key, signup, signup_and_get_api_key
from control_plane import db as db_module
from control_plane.models_orm import Account


async def _deactivate_account(account_id: str) -> None:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one()
        account.scim_deactivated_at = datetime.now(timezone.utc)
        await db.commit()


# ── POST /v1/account/sandbox-create-token ───────────────────────────────


async def test_mint_sandbox_create_token_requires_authentication(client: httpx.AsyncClient):
    resp = await client.post("/v1/account/sandbox-create-token")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"


async def test_mint_sandbox_create_token_rejects_api_key(client: httpx.AsyncClient):
    """This route is JWT-only, like every other dashboard-auth route in
    account.py -- an API key must be rejected the same way `get_current_user`
    rejects one everywhere else."""
    key = await signup_and_get_api_key(client, "mint-sandbox-token-apikey@example.com")
    resp = await client.post(
        "/v1/account/sandbox-create-token", headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_mint_sandbox_create_token_succeeds_and_is_redeemable(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    signup_resp = await signup(client, "mint-sandbox-token-ok@example.com")
    access_token = signup_resp["access_token"]

    mint_resp = await client.post(
        "/v1/account/sandbox-create-token", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert mint_resp.status_code == 200, mint_resp.text
    body = mint_resp.json()
    assert isinstance(body["token"], str) and body["token"]
    assert "expires_at" in body

    create_resp = await client.post(
        "/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {body['token']}"}
    )
    assert create_resp.status_code == 201, create_resp.text


# ── POST /v1/sandboxes redemption behavior ───────────────────────────────


async def test_sandbox_create_token_is_single_use(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.security import create_sandbox_create_token

    signup_resp = await signup(client, "sandbox-token-replay@example.com")
    me_resp = await client.get(
        "/v1/account/me", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    account_id = me_resp.json()["id"]

    token, _expires_at = create_sandbox_create_token(account_id=account_id, ttl_seconds=90)

    first = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 201, first.text

    second = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {token}"})
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "invalid_token"


async def test_sandbox_create_token_rejects_expired_token(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.security import create_sandbox_create_token

    signup_resp = await signup(client, "sandbox-token-expired@example.com")
    me_resp = await client.get(
        "/v1/account/me", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    account_id = me_resp.json()["id"]

    token, _expires_at = create_sandbox_create_token(account_id=account_id, ttl_seconds=-1)

    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


async def test_sandbox_create_token_rejects_malformed_token(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    resp = await client.post(
        "/v1/sandboxes", json={}, headers={"Authorization": "Bearer not-a-real-jwt"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


async def test_sandbox_create_token_rejects_wrong_token_type(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A token minted for a different purpose (same signing key, different
    `type` claim) must never be accepted here -- the `type` check in
    `decode_sandbox_create_token` is load-bearing, not decorative."""
    from control_plane.security import create_demo_session_token

    demo_token, _expires_at = create_demo_session_token(session_id="some-demo-session", ttl_seconds=90)

    resp = await client.post(
        "/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {demo_token}"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


async def test_sandbox_create_token_rejects_deactivated_account(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A sandbox-create token is minted from an already-authenticated (and
    therefore already deactivation-checked) dashboard JWT, but its short TTL
    is still a real window: an account deactivated between mint and
    redemption must not be able to create a sandbox on the strength of a
    token minted moments before."""
    from control_plane.security import create_sandbox_create_token

    signup_resp = await signup(client, "sandbox-token-deactivated@example.com")
    me_resp = await client.get(
        "/v1/account/me", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    account_id = me_resp.json()["id"]

    token, _expires_at = create_sandbox_create_token(account_id=account_id, ttl_seconds=90)
    await _deactivate_account(account_id)

    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "account_deactivated"


async def test_sandbox_create_token_rejects_unknown_account(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.security import create_sandbox_create_token

    token, _expires_at = create_sandbox_create_token(
        account_id="00000000-0000-0000-0000-000000000000", ttl_seconds=90
    )

    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


# ── Every other /v1/sandboxes/* route stays API-key-only ─────────────────


async def test_sandbox_create_token_rejected_by_list_sandboxes(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.security import create_sandbox_create_token

    key = await signup_and_get_api_key(client, "sandbox-token-list-scope@example.com")
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]
    token, _expires_at = create_sandbox_create_token(account_id=account_id, ttl_seconds=90)

    resp = await client.get("/v1/sandboxes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_sandbox_create_token_rejected_by_get_sandbox(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.security import create_sandbox_create_token

    key = await signup_and_get_api_key(client, "sandbox-token-get-scope@example.com")
    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    session_id = create_resp.json()["id"]
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]
    token, _expires_at = create_sandbox_create_token(account_id=account_id, ttl_seconds=90)

    resp = await client.get(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_sandbox_create_token_rejected_by_delete_sandbox(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.security import create_sandbox_create_token

    key = await signup_and_get_api_key(client, "sandbox-token-delete-scope@example.com")
    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    session_id = create_resp.json()["id"]
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]
    token, _expires_at = create_sandbox_create_token(account_id=account_id, ttl_seconds=90)

    resp = await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_sandbox_create_token_rejected_by_exec(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.security import create_sandbox_create_token

    key = await signup_and_get_api_key(client, "sandbox-token-exec-scope@example.com")
    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    session_id = create_resp.json()["id"]
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]
    token, _expires_at = create_sandbox_create_token(account_id=account_id, ttl_seconds=90)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_sandbox_create_token_rejected_by_takeover_token_mint(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.security import create_sandbox_create_token

    key = await signup_and_get_api_key(client, "sandbox-token-takeover-scope@example.com", role="admin")
    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    session_id = create_resp.json()["id"]
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]
    token, _expires_at = create_sandbox_create_token(account_id=account_id, ttl_seconds=90)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/takeover-token", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


# ── security.py: create_sandbox_create_token/decode_sandbox_create_token ─


def test_create_sandbox_create_token_round_trips_account_id():
    from control_plane.security import create_sandbox_create_token, decode_sandbox_create_token

    token, _expires_at = create_sandbox_create_token(account_id="acct-1", ttl_seconds=90)
    payload = decode_sandbox_create_token(token)

    assert payload["account_id"] == "acct-1"
    assert payload["type"] == "sandbox_create"
    assert payload["jti"]


def test_decode_sandbox_create_token_rejects_access_token():
    import jwt as pyjwt

    from control_plane.security import create_access_token, decode_sandbox_create_token

    access_token, _ttl = create_access_token(account_id="acct-1", email="a@example.com")
    with pytest.raises(pyjwt.PyJWTError):
        decode_sandbox_create_token(access_token)
