"""Tests for network ingress preview URLs
(`POST /preview/{port}`, `ANY /preview/{port}/{path}`) — see
docs/NETWORK-INGRESS-DESIGN.md.

Mint is a normal API-key-authenticated, account-scoped route (mirrors every
other sandbox route: 404 for a foreign session_id). The proxy route is
public -- its entire authorization is the signed `token` query parameter --
so its own tests cover token validation (missing/expired/wrong session or
port), not account ownership.
"""

from __future__ import annotations

import time

import httpx
import jwt

from conftest import FakeSandboxManager, signup_and_get_api_key


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _mint_preview_url(
    client: httpx.AsyncClient, session_id: str, api_key: str, port: int = 3000, ttl_seconds: int | None = None
) -> dict:
    body = {} if ttl_seconds is None else {"ttl_seconds": ttl_seconds}
    resp = await client.post(
        f"/v1/sandboxes/{session_id}/preview/{port}",
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _extract_token(preview_url: str) -> str:
    assert "token=" in preview_url
    return preview_url.split("token=", 1)[1]


# ── Minting ──────────────────────────────────────────────────────────────


async def test_mint_preview_url_requires_authentication(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    resp = await client.post("/v1/sandboxes/some-session/preview/3000", json={})
    assert resp.status_code == 401


async def test_mint_preview_url_404s_for_unknown_session(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "preview-unknown@example.com")
    resp = await client.post(
        "/v1/sandboxes/does-not-exist/preview/3000",
        json={},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


async def test_account_cannot_mint_preview_url_for_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "preview-victim@example.com")
    session_id = await _create_session(client, key_a)

    key_b = await signup_and_get_api_key(client, "preview-attacker@example.com")
    resp = await client.post(
        f"/v1/sandboxes/{session_id}/preview/3000",
        json={},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404


async def test_mint_preview_url_returns_signed_url_and_expiry(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "preview-happy@example.com")
    session_id = await _create_session(client, key)

    body = await _mint_preview_url(client, session_id, key, port=3000, ttl_seconds=120)
    assert f"/v1/sandboxes/{session_id}/preview/3000/" in body["url"]
    assert "token=" in body["url"]
    assert body["expires_at"]


async def test_mint_preview_url_rejects_ttl_outside_bounds(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "preview-ttl@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/preview/3000",
        json={"ttl_seconds": 5},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422


# ── Proxying ─────────────────────────────────────────────────────────────


async def test_preview_proxy_forwards_request_using_valid_token(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "preview-proxy@example.com")
    session_id = await _create_session(client, key)
    body = await _mint_preview_url(client, session_id, key, port=3000)
    token = _extract_token(body["url"])

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/preview/3000/index.html", params={"token": token}
    )
    assert resp.status_code == 200
    assert resp.text == f"preview:{session_id}:3000:index.html"


async def test_preview_proxy_requires_no_api_key(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    """The whole point of a preview link is that it needs no API key -- a
    request with only the token and no Authorization header must succeed."""
    key = await signup_and_get_api_key(client, "preview-no-key@example.com")
    session_id = await _create_session(client, key)
    body = await _mint_preview_url(client, session_id, key, port=3000)
    token = _extract_token(body["url"])

    resp = await client.get(f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": token})
    assert resp.status_code == 200


async def test_preview_proxy_rejects_missing_token(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "preview-missing-token@example.com")
    session_id = await _create_session(client, key)

    resp = await client.get(f"/v1/sandboxes/{session_id}/preview/3000/")
    assert resp.status_code == 422  # token is a required query param


async def test_preview_proxy_rejects_garbage_token(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "preview-garbage-token@example.com")
    session_id = await _create_session(client, key)

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": "not-a-real-token"}
    )
    assert resp.status_code == 401


async def test_preview_proxy_rejects_expired_token(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    from control_plane.config import settings

    key = await signup_and_get_api_key(client, "preview-expired@example.com")
    session_id = await _create_session(client, key)

    expired_token = jwt.encode(
        {
            "sid": session_id,
            "port": 3000,
            "type": "sandbox_preview",
            "iat": int(time.time()) - 3600,
            "exp": int(time.time()) - 60,
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": expired_token}
    )
    assert resp.status_code == 401


async def test_preview_proxy_rejects_token_for_a_different_port(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "preview-wrong-port@example.com")
    session_id = await _create_session(client, key)
    body = await _mint_preview_url(client, session_id, key, port=3000)
    token = _extract_token(body["url"])

    # Same token, but the URL now claims a different port.
    resp = await client.get(
        f"/v1/sandboxes/{session_id}/preview/4000/", params={"token": token}
    )
    assert resp.status_code == 401


async def test_preview_proxy_rejects_token_for_a_different_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "preview-wrong-session-a@example.com")
    session_a = await _create_session(client, key)
    session_b = await _create_session(client, key)
    body = await _mint_preview_url(client, session_a, key, port=3000)
    token = _extract_token(body["url"])

    resp = await client.get(
        f"/v1/sandboxes/{session_b}/preview/3000/", params={"token": token}
    )
    assert resp.status_code == 401


async def test_preview_proxy_rejects_access_token_type_confusion(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A dashboard access token (different `type` claim) must never be
    accepted here, even though it's signed with the same secret."""
    from control_plane.security import create_access_token

    key = await signup_and_get_api_key(client, "preview-confusion@example.com")
    session_id = await _create_session(client, key)
    access_token, _ = create_access_token(account_id="whatever", email="x@example.com")

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": access_token}
    )
    assert resp.status_code == 401


async def test_preview_proxy_404s_after_session_destroyed(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "preview-destroyed@example.com")
    session_id = await _create_session(client, key)
    body = await _mint_preview_url(client, session_id, key, port=3000)
    token = _extract_token(body["url"])

    resp = await client.delete(
        f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 204

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": token}
    )
    assert resp.status_code == 404


# ── Revocation (docs/NETWORK-INGRESS-DESIGN.md's former "no revocation
# before expiry" limitation) ────────────────────────────────────────────


async def test_mint_response_includes_token_id(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "preview-token-id@example.com")
    session_id = await _create_session(client, key)

    body = await _mint_preview_url(client, session_id, key, port=3000)
    assert body["token_id"]
    assert isinstance(body["token_id"], str)


async def test_revoked_token_is_rejected_by_the_proxy(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "preview-revoke@example.com")
    session_id = await _create_session(client, key)
    body = await _mint_preview_url(client, session_id, key, port=3000)
    token = _extract_token(body["url"])

    # Works before revocation.
    resp = await client.get(f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": token})
    assert resp.status_code == 200

    revoke_resp = await client.post(
        f"/v1/sandboxes/{session_id}/preview/3000/revoke",
        json={"token_id": body["token_id"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert revoke_resp.status_code == 200, revoke_resp.text
    assert revoke_resp.json() == {"revoked": True, "token_id": body["token_id"]}

    # Rejected after revocation -- same session, same still-unexpired token.
    resp = await client.get(f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": token})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "preview_token_revoked"


async def test_revoking_one_token_does_not_affect_a_second_token_for_the_same_port(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Revocation is per-token (jti), not per session/port -- minting a
    second preview URL for the same port and revoking the first must leave
    the second one fully usable."""
    key = await signup_and_get_api_key(client, "preview-revoke-scoped@example.com")
    session_id = await _create_session(client, key)

    first = await _mint_preview_url(client, session_id, key, port=3000)
    second = await _mint_preview_url(client, session_id, key, port=3000)
    assert first["token_id"] != second["token_id"]

    revoke_resp = await client.post(
        f"/v1/sandboxes/{session_id}/preview/3000/revoke",
        json={"token_id": first["token_id"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert revoke_resp.status_code == 200

    first_token = _extract_token(first["url"])
    second_token = _extract_token(second["url"])

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": first_token}
    )
    assert resp.status_code == 401

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": second_token}
    )
    assert resp.status_code == 200


async def test_revoking_does_not_tear_down_the_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """The whole point of this feature: revoking a preview token must not
    destroy the sandbox session it belongs to."""
    key = await signup_and_get_api_key(client, "preview-revoke-session-alive@example.com")
    session_id = await _create_session(client, key)
    body = await _mint_preview_url(client, session_id, key, port=3000)

    revoke_resp = await client.post(
        f"/v1/sandboxes/{session_id}/preview/3000/revoke",
        json={"token_id": body["token_id"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert revoke_resp.status_code == 200

    resp = await client.get(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    assert resp.json()["destroyed_at"] is None


async def test_revoke_requires_authentication(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    resp = await client.post(
        "/v1/sandboxes/some-session/preview/3000/revoke", json={"token_id": "whatever"}
    )
    assert resp.status_code == 401


async def test_revoke_404s_for_unknown_session(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "preview-revoke-unknown@example.com")
    resp = await client.post(
        "/v1/sandboxes/does-not-exist/preview/3000/revoke",
        json={"token_id": "whatever"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


async def test_account_cannot_revoke_preview_token_for_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "preview-revoke-victim@example.com")
    session_id = await _create_session(client, key_a)
    body = await _mint_preview_url(client, session_id, key_a, port=3000)

    key_b = await signup_and_get_api_key(client, "preview-revoke-attacker@example.com")
    resp = await client.post(
        f"/v1/sandboxes/{session_id}/preview/3000/revoke",
        json={"token_id": body["token_id"]},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404

    # And the original token must still work -- the attacker's revoke
    # attempt against a session they don't own must not have succeeded.
    token = _extract_token(body["url"])
    resp = await client.get(f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": token})
    assert resp.status_code == 200


async def test_revoking_an_unknown_token_id_is_idempotent_not_an_error(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Revoking a token_id that was never minted (typo, already-expired-and-
    forgotten, or someone else's already-revoked jti) must not leak whether
    it ever existed -- same non-distinguishing-404 posture the rest of this
    API already has for cross-tenant lookups."""
    key = await signup_and_get_api_key(client, "preview-revoke-unknown-token@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/preview/3000/revoke",
        json={"token_id": "never-existed"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"revoked": True, "token_id": "never-existed"}


async def test_revoking_the_same_token_twice_is_idempotent(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "preview-revoke-twice@example.com")
    session_id = await _create_session(client, key)
    body = await _mint_preview_url(client, session_id, key, port=3000)

    for _ in range(2):
        resp = await client.post(
            f"/v1/sandboxes/{session_id}/preview/3000/revoke",
            json={"token_id": body["token_id"]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200


async def test_a_dashboard_access_token_type_confusion_has_no_jti_and_cannot_be_revoked_around(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A token with no jti claim at all (e.g. minted before this feature
    existed, or a forged token missing the field) must be treated as
    'cannot be revoked', never as passing the revocation check by accident
    (e.g. via a falsy/None jti matching a None row)."""
    from control_plane.config import settings

    key = await signup_and_get_api_key(client, "preview-no-jti@example.com")
    session_id = await _create_session(client, key)

    token_without_jti = jwt.encode(
        {
            "sid": session_id,
            "port": 3000,
            "type": "sandbox_preview",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/preview/3000/", params={"token": token_without_jti}
    )
    assert resp.status_code == 200
