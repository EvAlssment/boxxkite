"""Tests for webhook event enqueueing, HMAC signing, and the delivery
worker's retry/backoff behavior (docs/WEBHOOKS-DESIGN.md).

Covers:
- Per-account opt-in: creating/destroying a sandbox for an account with NO
  registered webhook enqueues zero deliveries and makes zero outbound HTTP
  calls.
- Creating/destroying a sandbox for an account WITH a matching, active
  subscription enqueues exactly one WebhookDelivery row per matching
  subscription, and the background worker delivers it with a verifiable
  HMAC signature.
- A receiver returning a non-2xx response schedules a retry with the
  expected exponential backoff, and after
  BOXKITE_WEBHOOK_MAX_DELIVERY_ATTEMPTS failures the delivery is marked
  "failed" permanently.
- An inactive or deleted subscription's in-flight delivery is marked
  failed rather than retried forever.
- webhooks.sign_payload/build_signature_header produce a signature a
  receiver can independently reproduce and verify.
- GitHub issue #125 (SIEM/audit-log export): an exec/file-op against a
  session enqueues an 'audit_log.entry' delivery for a matching
  subscription; a 'splunk_hec' payload_format subscription is delivered a
  Splunk HEC-shaped body (with the boxkite envelope preserved verbatim
  under 'event'), a stored HEC token is sent as an Authorization header,
  no HEC token means no such header, and the HMAC signature is always
  computed over the exact bytes actually sent.
- GitHub issue #148 (DNS-rebinding SSRF): every delivery attempt re-resolves
  and re-validates the destination via `host_safety.
  resolve_and_validate_destination_ip` before connecting -- a webhook whose
  DNS record is repointed to a private/metadata address after registration
  is refused at delivery time, not just checked once at registration.

Most tests below don't care about DNS at all, so `_patch_safe_webhook_dns`
(autouse) makes every hostname resolve to a safe public IP by default --
mirrors tests/test_sidecar_http_request.py's own `_patch_safe_dns` pattern
for the secrets broker. The dedicated DNS-rebinding test below overrides it
to exercise the real resolution path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from conftest import FakeSandboxManager, signup_and_get_api_key
from control_plane import db as db_module
from control_plane import host_safety
from control_plane import webhook_delivery as webhook_delivery_module
from control_plane.config import settings
from control_plane.models_orm import WebhookDelivery
from control_plane.webhook_delivery import _deliver_once, set_http_client_for_tests
from control_plane.webhooks import build_signature_header, sign_payload

pytestmark = pytest.mark.asyncio

_SAFE_DNS_IP = "93.184.216.34"


async def _register_webhook(
    client: httpx.AsyncClient, api_key: str, *, url: str, event_types=None, **kwargs
) -> dict:
    body = {"url": url, "event_types": event_types or ["sandbox.created", "sandbox.destroyed"]}
    body.update(kwargs)
    resp = await client.post(
        "/v1/webhooks",
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_sandbox(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _run_exec(client: httpx.AsyncClient, api_key: str, session_id: str, *, command: str = "echo hi") -> None:
    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": command},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200, resp.text


@pytest.fixture(autouse=True)
def _reset_webhook_http_client():
    set_http_client_for_tests(None)
    yield
    set_http_client_for_tests(None)


@pytest.fixture(autouse=True)
def _patch_safe_webhook_dns(monkeypatch):
    """Every test in this file uses example.com-style hostnames that don't
    actually resolve -- default to a safe public IP so the request-time
    re-validation added for GitHub issue #148 doesn't turn every other test
    in this file into a DNS-dependent one. The dedicated rebinding test
    below restores the real function and fakes `socket.getaddrinfo`
    instead, to exercise the actual resolution path."""

    async def _fake_resolve(hostname: str) -> str:
        return _SAFE_DNS_IP

    monkeypatch.setattr(webhook_delivery_module, "resolve_and_validate_destination_ip", _fake_resolve)


async def test_account_with_no_webhook_enqueues_nothing(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "webhook-noop@example.com")
    session_id = await _create_sandbox(client, key)

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        assert result.scalars().all() == []

    await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        assert result.scalars().all() == []


async def test_sandbox_created_and_destroyed_enqueue_matching_deliveries(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "webhook-enqueue@example.com")
    await _register_webhook(client, key, url="https://example.com/hooks/a")

    session_id = await _create_sandbox(client, key)

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        rows = list(result.scalars().all())
    assert len(rows) == 1
    assert rows[0].event_type == "sandbox.created"
    assert rows[0].payload["data"]["session_id"] == session_id
    assert rows[0].status == "pending"

    await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery).order_by(WebhookDelivery.created_at))
        rows = list(result.scalars().all())
    assert len(rows) == 2
    assert rows[1].event_type == "sandbox.destroyed"
    assert rows[1].payload["data"]["session_id"] == session_id


async def test_subscription_only_matching_one_event_type_does_not_receive_the_other(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "webhook-partial@example.com")
    await _register_webhook(client, key, url="https://example.com/hooks/created-only", event_types=["sandbox.created"])

    session_id = await _create_sandbox(client, key)
    await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        rows = list(result.scalars().all())
    assert [r.event_type for r in rows] == ["sandbox.created"]


async def test_worker_delivers_successfully_with_valid_signature(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "webhook-deliver-ok@example.com")
    created = await _register_webhook(client, key, url="https://receiver.example.com/hooks")
    raw_secret = created["secret"]

    await _create_sandbox(client, key)

    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={"ok": True})

    set_http_client_for_tests(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    await _deliver_once()

    assert len(received) == 1
    request = received[0]
    body = request.content.decode("utf-8")
    signature_header = request.headers["X-Boxkite-Webhook-Signature"]
    timestamp_str, signature = (part.split("=", 1)[1] for part in signature_header.split(","))
    expected_signature = sign_payload(secret=raw_secret, timestamp=int(timestamp_str), body=body)
    assert signature == expected_signature

    payload = json.loads(body)
    assert payload["event"] == "sandbox.created"

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row] = result.scalars().all()
    assert row.status == "delivered"
    assert row.response_status_code == 200
    assert row.attempt_count == 1


async def test_build_signature_header_is_independently_reproducible():
    """Simulates a receiver verifying a delivery -- see
    docs/WEBHOOKS-DESIGN.md's verification snippet."""
    secret = "whsec_test-secret"
    body = json.dumps({"event": "sandbox.created"}, sort_keys=True)
    header = build_signature_header(secret=secret, timestamp=1_700_000_000, body=body)
    t_part, v1_part = header.split(",")
    timestamp = int(t_part.split("=", 1)[1])
    signature = v1_part.split("=", 1)[1]

    recomputed = sign_payload(secret=secret, timestamp=timestamp, body=body)
    assert recomputed == signature

    wrong_secret_signature = sign_payload(secret="whsec_wrong", timestamp=timestamp, body=body)
    assert wrong_secret_signature != signature


async def test_non_2xx_response_schedules_retry_with_backoff(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    monkeypatch.setattr(settings, "BOXKITE_WEBHOOK_RETRY_BASE_SECONDS", 30)
    monkeypatch.setattr(settings, "BOXKITE_WEBHOOK_MAX_DELIVERY_ATTEMPTS", 6)

    key = await signup_and_get_api_key(client, "webhook-retry@example.com")
    await _register_webhook(client, key, url="https://receiver.example.com/hooks")
    await _create_sandbox(client, key)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream error")

    set_http_client_for_tests(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    before = datetime.now(timezone.utc)
    await _deliver_once()

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row] = result.scalars().all()

    assert row.status == "pending"
    assert row.attempt_count == 1
    assert row.response_status_code == 500
    # First retry waits ~BOXKITE_WEBHOOK_RETRY_BASE_SECONDS (30s).
    next_attempt_at = row.next_attempt_at
    if next_attempt_at.tzinfo is None:
        next_attempt_at = next_attempt_at.replace(tzinfo=timezone.utc)
    delta = (next_attempt_at - before).total_seconds()
    assert 25 <= delta <= 35


async def test_exhausted_retries_mark_delivery_permanently_failed(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    monkeypatch.setattr(settings, "BOXKITE_WEBHOOK_MAX_DELIVERY_ATTEMPTS", 2)
    monkeypatch.setattr(settings, "BOXKITE_WEBHOOK_RETRY_BASE_SECONDS", 0)

    key = await signup_and_get_api_key(client, "webhook-exhaust@example.com")
    await _register_webhook(client, key, url="https://receiver.example.com/hooks")
    await _create_sandbox(client, key)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="still down")

    set_http_client_for_tests(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    await _deliver_once()  # attempt 1/2 -> still pending
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row] = result.scalars().all()
    assert row.status == "pending"

    await _deliver_once()  # attempt 2/2 -> exhausted -> failed
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row] = result.scalars().all()
    assert row.status == "failed"
    assert row.attempt_count == 2


async def test_dns_rebinding_to_metadata_ip_refuses_delivery(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    """GitHub issue #148's exact attack: register a webhook against a
    hostname that resolves to a public IP at registration time (passing
    `_validate_webhook_url`), then have DNS repoint that hostname to the
    cloud-metadata address by the time the delivery worker actually fires --
    the delivery must be refused, never sent to the now-internal address.

    Restores the real `resolve_and_validate_destination_ip` (undoing the
    autouse `_patch_safe_webhook_dns` fixture for this test only) and fakes
    `socket.getaddrinfo` instead, mirroring
    tests/test_sidecar_http_request.py's own
    test_dns_rebinding_to_private_ip_is_refused."""
    key = await signup_and_get_api_key(client, "webhook-rebind@example.com")
    await _register_webhook(client, key, url="https://rebind-target.example.com/hooks")
    await _create_sandbox(client, key)

    monkeypatch.setattr(
        webhook_delivery_module,
        "resolve_and_validate_destination_ip",
        host_safety.resolve_and_validate_destination_ip,
    )

    def _fake_getaddrinfo(hostname, port):
        assert hostname == "rebind-target.example.com"
        return [(2, 1, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(host_safety.socket, "getaddrinfo", _fake_getaddrinfo)

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    set_http_client_for_tests(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await _deliver_once()

    assert calls == []  # never sent to the rebound metadata address

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row] = result.scalars().all()
    # Treated like any other transient delivery failure -- retried/backed
    # off, never silently dropped and never delivered to the unsafe address.
    assert row.status == "pending"
    assert row.attempt_count == 1
    assert row.failure_reason == "destination_not_allowed"


async def test_deleted_subscription_fails_in_flight_delivery(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "webhook-deleted-sub@example.com")
    created = await _register_webhook(client, key, url="https://receiver.example.com/hooks")
    await _create_sandbox(client, key)

    # Delete the subscription while a delivery is still pending for it.
    await client.delete(f"/v1/webhooks/{created['id']}", headers={"Authorization": f"Bearer {key}"})

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    set_http_client_for_tests(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await _deliver_once()

    assert calls == []  # never attempted -- nowhere left to deliver to
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row] = result.scalars().all()
    assert row.status == "failed"
    assert row.failure_reason == "subscription_deleted_or_inactive"


async def test_exec_enqueues_audit_log_entry_delivery(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "webhook-audit-enqueue@example.com")
    await _register_webhook(client, key, url="https://example.com/hooks/audit", event_types=["audit_log.entry"])

    session_id = await _create_sandbox(client, key)
    await _run_exec(client, key, session_id, command="echo hello")

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        rows = list(result.scalars().all())
    assert len(rows) == 1
    assert rows[0].event_type == "audit_log.entry"
    data = rows[0].payload["data"]
    assert data["session_id"] == session_id
    assert data["operation"] == "exec"
    assert data["detail"]["command"] == "echo hello"
    assert data["source"] == "agent"
    assert data["exit_code"] == 0
    assert "exec_log_entry_id" in data


async def test_audit_log_subscription_does_not_receive_sandbox_lifecycle_events(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "webhook-audit-only@example.com")
    await _register_webhook(client, key, url="https://example.com/hooks/audit-only", event_types=["audit_log.entry"])

    session_id = await _create_sandbox(client, key)  # fires sandbox.created -- should NOT match

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        rows = list(result.scalars().all())
    assert rows == []

    await _run_exec(client, key, session_id)
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        rows = list(result.scalars().all())
    assert [r.event_type for r in rows] == ["audit_log.entry"]


async def test_splunk_hec_delivery_body_shape_and_no_auth_header_without_token(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "webhook-hec-body@example.com")
    created = await _register_webhook(
        client,
        key,
        url="https://receiver.example.com/services/collector/event",
        event_types=["audit_log.entry"],
        payload_format="splunk_hec",
    )
    raw_secret = created["secret"]

    session_id = await _create_sandbox(client, key)
    await _run_exec(client, key, session_id, command="echo hec-test")

    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    set_http_client_for_tests(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await _deliver_once()

    assert len(received) == 1
    request = received[0]
    assert "Authorization" not in request.headers

    body_text = request.content.decode("utf-8")
    hec_body = json.loads(body_text)
    assert set(["time", "host", "source", "sourcetype", "event"]).issubset(hec_body.keys())
    assert hec_body["sourcetype"] == "_json"
    assert hec_body["event"]["event"] == "audit_log.entry"
    assert hec_body["event"]["data"]["operation"] == "exec"

    # Signature must be computed over the exact HEC-wrapped bytes sent, not
    # the pre-wrap boxkite envelope.
    signature_header = request.headers["X-Boxkite-Webhook-Signature"]
    timestamp_str, signature = (part.split("=", 1)[1] for part in signature_header.split(","))
    expected_signature = sign_payload(secret=raw_secret, timestamp=int(timestamp_str), body=body_text)
    assert signature == expected_signature


async def test_splunk_hec_delivery_sends_authorization_header_when_token_present(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "webhook-hec-token-header@example.com")
    await _register_webhook(
        client,
        key,
        url="https://receiver.example.com/services/collector/event",
        event_types=["audit_log.entry"],
        payload_format="splunk_hec",
        hec_token="hec-abc-123",
    )

    session_id = await _create_sandbox(client, key)
    await _run_exec(client, key, session_id)

    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    set_http_client_for_tests(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await _deliver_once()

    assert len(received) == 1
    assert received[0].headers["Authorization"] == "Splunk hec-abc-123"


async def test_boxkite_v1_format_never_sends_authorization_header(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A hec_token is only meaningful alongside payload_format='splunk_hec' --
    confirms the default format never adds the Splunk auth header even if a
    caller registered a token (defense against a future regression, not a
    currently-reachable API path since hec_token is only accepted validation-
    side; this documents the delivery-side invariant directly)."""
    key = await signup_and_get_api_key(client, "webhook-hec-boxkite-v1@example.com")
    await _register_webhook(
        client,
        key,
        url="https://receiver.example.com/hooks",
        event_types=["sandbox.created"],
        payload_format="boxkite_v1",
    )

    await _create_sandbox(client, key)

    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    set_http_client_for_tests(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await _deliver_once()

    assert len(received) == 1
    assert "Authorization" not in received[0].headers
    body = json.loads(received[0].content.decode("utf-8"))
    assert body["event"] == "sandbox.created"
    assert "time" not in body  # not HEC-wrapped
