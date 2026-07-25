"""Tests for the webhook registration/list/delete API
(docs/WEBHOOKS-DESIGN.md).

Covers:
- The signing secret is returned exactly once, on create, and never again
  by list/get.
- Cross-tenant isolation for delete/deliveries (404, not 403, for a
  foreign id) -- same discipline every other resource in this API follows.
- 429 once BOXKITE_MAX_WEBHOOKS_PER_ACCOUNT is reached.
- 422 for a url that resolves to a private/link-local/loopback address at
  registration time (the best-effort creation-time backstop, mirroring
  routers/secrets.py's allowed_hosts check).
- 422 for a url with neither http:// nor https:// scheme.
- 422 for an unrecognized event_type (validated at the Pydantic layer).
- The KMS envelope-encryption round trip: the persisted ciphertext never
  contains the plaintext signing secret.
- GitHub issue #125 (SIEM/audit-log export): 'audit_log.entry' is accepted
  as a registerable event_type, 'payload_format' defaults to 'boxkite_v1'
  and accepts 'splunk_hec', an invalid payload_format is rejected, and a
  supplied hec_token is envelope-encrypted at rest and never echoed back by
  create/list.
"""

from __future__ import annotations

import httpx

from conftest import signup_and_get_api_key
from control_plane import db as db_module
from control_plane.config import settings
from control_plane.models_orm import WebhookSubscription
from control_plane.webhooks import decrypt_hec_token, decrypt_signing_secret
from sqlalchemy import select


async def _register_webhook(
    client: httpx.AsyncClient, api_key: str, *, url: str = "https://example.com/hooks/boxkite", **kwargs
) -> httpx.Response:
    body = {"url": url, "event_types": ["sandbox.created", "sandbox.destroyed"]}
    body.update(kwargs)
    return await client.post("/v1/webhooks", json=body, headers={"Authorization": f"Bearer {api_key}"})


async def test_create_webhook_returns_secret_once(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-create@example.com")

    resp = await _register_webhook(client, key)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["url"] == "https://example.com/hooks/boxkite"
    assert body["event_types"] == ["sandbox.created", "sandbox.destroyed"]
    assert body["is_active"] is True
    assert body["secret"].startswith("whsec_")

    list_resp = await client.get("/v1/webhooks", headers={"Authorization": f"Bearer {key}"})
    assert list_resp.status_code == 200
    assert "secret" not in list_resp.json()[0]
    assert body["secret"] not in list_resp.text


async def test_signing_secret_is_never_stored_in_plaintext(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-kms@example.com")
    resp = await _register_webhook(client, key)
    raw_secret = resp.json()["secret"]
    subscription_id = resp.json()["id"]

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == subscription_id))
        row = result.scalar_one()

    assert raw_secret not in row.ciphertext
    assert decrypt_signing_secret(row) == raw_secret


async def test_delete_webhook_owned_by_different_account_is_404(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "webhooks-owner@example.com")
    key_b = await signup_and_get_api_key(client, "webhooks-other@example.com")

    created = await _register_webhook(client, key_a)
    subscription_id = created.json()["id"]

    cross_tenant_delete = await client.delete(
        f"/v1/webhooks/{subscription_id}", headers={"Authorization": f"Bearer {key_b}"}
    )
    assert cross_tenant_delete.status_code == 404

    own_delete = await client.delete(
        f"/v1/webhooks/{subscription_id}", headers={"Authorization": f"Bearer {key_a}"}
    )
    assert own_delete.status_code == 204


async def test_deliveries_for_foreign_subscription_is_404(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "webhooks-deliv-owner@example.com")
    key_b = await signup_and_get_api_key(client, "webhooks-deliv-other@example.com")

    created = await _register_webhook(client, key_a)
    subscription_id = created.json()["id"]

    resp = await client.get(
        f"/v1/webhooks/{subscription_id}/deliveries", headers={"Authorization": f"Bearer {key_b}"}
    )
    assert resp.status_code == 404


async def test_delete_unknown_subscription_id_is_404(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-unknown@example.com")
    resp = await client.delete(
        "/v1/webhooks/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


async def test_webhook_limit_reached_returns_429(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MAX_WEBHOOKS_PER_ACCOUNT", 2)
    key = await signup_and_get_api_key(client, "webhooks-limit@example.com")

    first = await _register_webhook(client, key, url="https://example.com/one")
    second = await _register_webhook(client, key, url="https://example.com/two")
    assert first.status_code == 201
    assert second.status_code == 201

    third = await _register_webhook(client, key, url="https://example.com/three")
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "webhook_limit_reached"


async def test_unsafe_webhook_url_is_rejected(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-ssrf@example.com")

    resp = await _register_webhook(client, key, url="http://169.254.169.254/latest/meta-data")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsafe_webhook_url"


async def test_webhook_url_without_http_scheme_is_rejected(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-scheme@example.com")

    resp = await client.post(
        "/v1/webhooks",
        json={"url": "ftp://example.com/hook", "event_types": ["sandbox.created"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422


async def test_unknown_event_type_is_rejected(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-event-type@example.com")

    resp = await client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hook", "event_types": ["exec.completed"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422


async def test_event_types_are_deduplicated(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-dedup@example.com")

    resp = await _register_webhook(
        client, key, event_types=["sandbox.created", "sandbox.created", "sandbox.destroyed"]
    )
    assert resp.status_code == 201
    assert resp.json()["event_types"] == ["sandbox.created", "sandbox.destroyed"]


async def test_audit_log_entry_is_a_registerable_event_type(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-audit-event-type@example.com")

    resp = await _register_webhook(client, key, event_types=["audit_log.entry"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["event_types"] == ["audit_log.entry"]


async def test_payload_format_defaults_to_boxkite_v1(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-format-default@example.com")

    resp = await _register_webhook(client, key)
    assert resp.status_code == 201, resp.text
    assert resp.json()["payload_format"] == "boxkite_v1"


async def test_payload_format_accepts_splunk_hec(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-format-splunk@example.com")

    resp = await _register_webhook(client, key, payload_format="splunk_hec")
    assert resp.status_code == 201, resp.text
    assert resp.json()["payload_format"] == "splunk_hec"

    list_resp = await client.get("/v1/webhooks", headers={"Authorization": f"Bearer {key}"})
    assert list_resp.json()[0]["payload_format"] == "splunk_hec"


async def test_invalid_payload_format_is_rejected(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-format-invalid@example.com")

    resp = await client.post(
        "/v1/webhooks",
        json={
            "url": "https://example.com/hook",
            "event_types": ["sandbox.created"],
            "payload_format": "syslog",
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422


async def test_hec_token_is_encrypted_at_rest_and_never_echoed_back(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-hec-token@example.com")

    resp = await _register_webhook(
        client, key, payload_format="splunk_hec", hec_token="hec-super-secret-token"
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "hec_token" not in body
    assert "hec-super-secret-token" not in resp.text

    subscription_id = body["id"]
    list_resp = await client.get("/v1/webhooks", headers={"Authorization": f"Bearer {key}"})
    assert "hec-super-secret-token" not in list_resp.text

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == subscription_id))
        row = result.scalar_one()

    assert row.hec_token_ciphertext is not None
    assert "hec-super-secret-token" not in row.hec_token_ciphertext
    assert decrypt_hec_token(row) == "hec-super-secret-token"


async def test_hec_token_is_optional_for_splunk_hec_format(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "webhooks-hec-token-optional@example.com")

    resp = await _register_webhook(client, key, payload_format="splunk_hec")
    assert resp.status_code == 201, resp.text

    subscription_id = resp.json()["id"]
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == subscription_id))
        row = result.scalar_one()
    assert row.hec_token_ciphertext is None
    assert decrypt_hec_token(row) is None
