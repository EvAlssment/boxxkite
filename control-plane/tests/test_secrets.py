"""Tests for the org-scoped secrets CRUD API and the KMS envelope-encryption
primitive (docs/SECRETS-DESIGN.md §3/4/6).

Covers:
- A secret's raw value is never returned by create/list.
- Cross-tenant isolation for delete (404, not 403, for a foreign id).
- 409 on a duplicate name within the same account.
- 422 for an allowed_hosts entry that resolves to a private/link-local/
  loopback address at creation time (the best-effort creation-time
  backstop).
- The KMS envelope-encryption round trip: encrypt then decrypt recovers the
  original plaintext, and the persisted ciphertext never contains the
  plaintext value.
- secret_names on SandboxCreateRequest resolves to a granted capability
  token/allowed_hosts passed through to SandboxManager.create_session, and
  404s (never leaking existence) for an unknown name.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import create_api_key, signup
from control_plane import secrets_kms
from control_plane.config import settings
from control_plane.secret_capability import (
    InvalidCapabilityToken,
    create_capability_token,
    decode_capability_token,
)


async def _account_with_key(client: httpx.AsyncClient, email: str) -> str:
    token_response = await signup(client, email)
    created = await create_api_key(client, token_response["access_token"], name="ci key")
    return created["key"]


async def test_create_secret_never_returns_value(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "secrets-create@example.com")

    resp = await client.post(
        "/v1/secrets",
        json={"name": "prod-stripe", "value": "sk_live_abc123", "allowed_hosts": ["api.stripe.com"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "prod-stripe"
    assert body["allowed_hosts"] == ["api.stripe.com"]
    assert "value" not in body
    assert "sk_live_abc123" not in resp.text


async def test_list_secrets_never_returns_value(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "secrets-list@example.com")
    await client.post(
        "/v1/secrets",
        json={"name": "s1", "value": "topsecretvalue", "allowed_hosts": ["example.com"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    resp = await client.get("/v1/secrets", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    assert "topsecretvalue" not in resp.text
    assert resp.json()[0]["name"] == "s1"


async def test_duplicate_secret_name_within_account_is_409(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "secrets-dup@example.com")
    body = {"name": "dup", "value": "v1", "allowed_hosts": ["example.com"]}
    first = await client.post("/v1/secrets", json=body, headers={"Authorization": f"Bearer {api_key}"})
    assert first.status_code == 201

    second = await client.post("/v1/secrets", json=body, headers={"Authorization": f"Bearer {api_key}"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "secret_name_taken"


async def test_cannot_delete_another_accounts_secret(client: httpx.AsyncClient):
    api_key_a = await _account_with_key(client, "secrets-owner-a@example.com")
    api_key_b = await _account_with_key(client, "secrets-owner-b@example.com")

    created = await client.post(
        "/v1/secrets",
        json={"name": "a-secret", "value": "v", "allowed_hosts": ["example.com"]},
        headers={"Authorization": f"Bearer {api_key_a}"},
    )
    secret_id = created.json()["id"]

    resp = await client.delete(
        f"/v1/secrets/{secret_id}", headers={"Authorization": f"Bearer {api_key_b}"}
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "unsafe_host",
    ["169.254.169.254", "127.0.0.1", "10.0.0.5", "metadata.google.internal", "localhost"],
)
async def test_allowed_hosts_rejects_private_and_metadata_addresses(
    client: httpx.AsyncClient, unsafe_host: str
):
    api_key = await _account_with_key(client, f"secrets-unsafe-{unsafe_host.replace('.', '-')}@example.com")

    resp = await client.post(
        "/v1/secrets",
        json={"name": "unsafe", "value": "v", "allowed_hosts": [unsafe_host]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsafe_allowed_host"


async def test_allowed_hosts_requires_at_least_one_entry(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "secrets-empty-hosts@example.com")

    resp = await client.post(
        "/v1/secrets",
        json={"name": "no-hosts", "value": "v", "allowed_hosts": []},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422


# ── trust_tier labeling (docs/WALLET-SECRETS-DESIGN.md §3/§6/§11 --
# testnet-tier wallet secrets ship now via this existing mechanism;
# mainnet requires the not-yet-built session-scoped signing mode) ───────


async def test_secret_without_trust_tier_omits_it_from_response(client: httpx.AsyncClient):
    """An ordinary (non-wallet) secret is unaffected -- trust_tier is
    optional and defaults to unset."""
    api_key = await _account_with_key(client, "secrets-no-tier@example.com")
    resp = await client.post(
        "/v1/secrets",
        json={"name": "prod-stripe-2", "value": "sk_live_xyz", "allowed_hosts": ["api.stripe.com"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["trust_tier"] is None


async def test_testnet_trust_tier_is_accepted_and_returned(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "secrets-testnet@example.com")
    resp = await client.post(
        "/v1/secrets",
        json={
            "name": "audit-agent-testnet",
            "value": "0xdeadbeef",
            "allowed_hosts": ["sepolia.infura.io"],
            "trust_tier": "testnet",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["trust_tier"] == "testnet"

    list_resp = await client.get("/v1/secrets", headers={"Authorization": f"Bearer {api_key}"})
    assert list_resp.status_code == 200
    [row] = [r for r in list_resp.json() if r["name"] == "audit-agent-testnet"]
    assert row["trust_tier"] == "testnet"


async def test_mainnet_trust_tier_is_refused_at_creation(client: httpx.AsyncClient):
    """Refused outright, not merely discouraged -- there is no
    session-scoped signing mechanism yet for a mainnet-tier grant to use
    safely (docs/WALLET-SECRETS-DESIGN.md §4b), so accepting the label
    without the enforcement it implies would be worse than not offering
    it at all."""
    api_key = await _account_with_key(client, "secrets-mainnet@example.com")
    resp = await client.post(
        "/v1/secrets",
        json={
            "name": "audit-agent-mainnet",
            "value": "0xdeadbeef",
            "allowed_hosts": ["mainnet.infura.io"],
            "trust_tier": "mainnet",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsupported_trust_tier"


async def test_unrecognized_trust_tier_is_rejected(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "secrets-bad-tier@example.com")
    resp = await client.post(
        "/v1/secrets",
        json={"name": "typo-tier", "value": "v", "allowed_hosts": ["example.com"], "trust_tier": "prod"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsupported_trust_tier"


async def test_secret_names_on_sandbox_create_grants_capability_token(
    client: httpx.AsyncClient, fake_manager
):
    original_url = settings.SECRETS_CONTROL_PLANE_URL
    settings.SECRETS_CONTROL_PLANE_URL = "https://control-plane.internal.example"
    try:
        api_key = await _account_with_key(client, "secrets-grant@example.com")
        await client.post(
            "/v1/secrets",
            json={"name": "granted", "value": "the-real-value", "allowed_hosts": ["api.example.com"]},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        resp = await client.post(
            "/v1/sandboxes",
            json={"secret_names": ["granted"]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201, resp.text
        session_id = resp.json()["id"]

        created = fake_manager.created[session_id]
        assert created["secret_grants"] == [{"name": "granted", "allowed_hosts": ["api.example.com"]}]
        token = created["secret_capability_token"]
        assert token

        payload = decode_capability_token(token, expected_session_id=session_id)
        assert payload["secret_names"] == ["granted"]
    finally:
        settings.SECRETS_CONTROL_PLANE_URL = original_url


async def test_secret_names_unknown_name_is_404_and_creates_no_session(
    client: httpx.AsyncClient, fake_manager
):
    api_key = await _account_with_key(client, "secrets-unknown@example.com")

    resp = await client.post(
        "/v1/sandboxes",
        json={"secret_names": ["does-not-exist"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "secret_not_found"
    assert fake_manager.created == {}


async def test_capability_token_rejected_for_a_different_session_id():
    token = create_capability_token(
        account_id="acct-1", session_id="session-a", secret_names=["s1"]
    )
    with pytest.raises(InvalidCapabilityToken):
        decode_capability_token(token, expected_session_id="session-b")


def test_kms_round_trip_recovers_plaintext_and_never_stores_it_verbatim():
    secrets_kms.reset_secrets_kms_client_for_tests()
    client = secrets_kms.get_secrets_kms_client()

    plaintext = "sk_live_super_secret_value_12345"
    encrypted = client.encrypt(plaintext)

    assert plaintext not in encrypted.ciphertext_b64
    assert plaintext not in encrypted.wrapped_data_key_b64

    decrypted = client.decrypt(encrypted)
    assert decrypted == plaintext
    secrets_kms.reset_secrets_kms_client_for_tests()


def test_kms_produces_different_ciphertext_for_the_same_plaintext():
    """Fresh nonce/data key per encryption -- two encryptions of the same
    value must not be byte-identical (defends against a naive implementation
    reusing a fixed nonce, which would break AES-GCM's security guarantees)."""
    secrets_kms.reset_secrets_kms_client_for_tests()
    client = secrets_kms.get_secrets_kms_client()

    a = client.encrypt("same-value")
    b = client.encrypt("same-value")
    assert a.ciphertext_b64 != b.ciphertext_b64 or a.nonce_b64 != b.nonce_b64
    secrets_kms.reset_secrets_kms_client_for_tests()
