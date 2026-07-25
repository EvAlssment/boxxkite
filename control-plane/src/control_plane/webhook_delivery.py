"""Background delivery worker for outbound webhooks (docs/WEBHOOKS-DESIGN.md).

Mirrors `reaper.py`'s shape exactly: a plain asyncio loop, started from
`main.py`'s lifespan and stopped via a `stop_event`, polling the database on
a fixed interval rather than any pub/sub mechanism -- the same
"don't over-engineer this" posture `docs/SANDBOX-OBSERVABILITY-DESIGN.md`
already applies to `/watch`. `webhooks.enqueue_event` only ever writes
`WebhookDelivery` rows; everything about actually reaching the receiver
(HTTP POST, signing, retry/backoff scheduling, giving up) lives here, kept
deliberately separate so a slow or unreachable receiver can never add
latency to the request that fired the event.

Retry/backoff: exponential, `BOXKITE_WEBHOOK_RETRY_BASE_SECONDS * 2 **
(attempt_count - 1)`, capped at `BOXKITE_WEBHOOK_RETRY_MAX_SECONDS`. A
delivery that has not succeeded after `BOXKITE_WEBHOOK_MAX_DELIVERY_ATTEMPTS`
attempts is marked `failed` permanently -- there is no dead-letter queue or
manual replay mechanism in this first cut (see the design doc's "what this
does NOT add" section).

DNS-rebinding-safe delivery (GitHub issue #148): `routers/webhooks.py`'s
`_validate_webhook_url` only checks a webhook's destination once, at
registration time -- a URL that resolves to a public IP then can later be
repointed via DNS to a private/link-local/metadata address, and that
repointed address would previously never be re-checked before the next
delivery attempt. `_attempt_delivery` below now re-resolves the destination
and validates the resolved IP on EVERY attempt
(`host_safety.resolve_and_validate_destination_ip`, mirroring the secrets
broker's own request-time re-resolution in
`sidecar/sidecar_secrets.py`'s `_resolve_and_validate_destination`), and
pins the actual outbound connection to that validated IP literal -- never a
bare hostname httpx would independently re-resolve, which would reopen the
same TOCTOU gap. A destination that fails this check is treated exactly
like any other delivery failure (network error, non-2xx response): recorded
via `_record_failure` and retried/backed-off/exhausted the same way, never
sent to the unsafe address.

Payload shape (GitHub issue #125): `subscription.payload_format` selects
the body actually POSTed -- the boxkite envelope as-is (`"boxkite_v1"`,
default) or that same envelope wrapped for Splunk HTTP Event Collector
ingestion (`"splunk_hec"`, via `webhooks.build_splunk_hec_payload`). Either
way the `X-Boxkite-Webhook-Signature` HMAC is computed over the EXACT bytes
sent, never the pre-wrap envelope, so a receiver's own signature
verification always matches the body it actually received. A
`splunk_hec`-format subscription with a stored HEC token additionally gets
an `Authorization: Splunk <token>` header, on top of (not instead of) the
usual boxkite signature headers -- one authenticates the delivery as
genuinely from boxkite, the other authenticates the caller to Splunk's own
HEC endpoint; they are unrelated credentials.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from .config import settings
from .db import get_session_factory
from .host_safety import resolve_and_validate_destination_ip
from .repository import WebhookDeliveryRepository, WebhookSubscriptionRepository
from .webhooks import (
    DELIVERY_ID_HEADER,
    EVENT_TYPE_HEADER,
    PAYLOAD_FORMAT_SPLUNK_HEC,
    SIGNATURE_HEADER,
    build_signature_header,
    build_splunk_hec_payload,
    decrypt_hec_token,
    decrypt_signing_secret,
)

logger = logging.getLogger(__name__)

# Caps how many delivery/receiver response bytes end up in the database per
# attempt -- same truncation philosophy as ExecLogEntry.output_truncated;
# a receiver's response body is caller-controlled-sized data.
_RESPONSE_BODY_MAX_LENGTH = 4096

# Deliberately its own httpx.AsyncClient (not the one src/boxkite's
# SandboxManager uses for manager<->sidecar traffic) -- this one calls
# arbitrary, caller-registered, external URLs, a fundamentally different
# trust boundary than the manager's pinned-cert sidecar channel. Overridable
# in tests via `set_http_client_for_tests` so no test ever makes a real
# network call.
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=settings.BOXKITE_WEBHOOK_DELIVERY_TIMEOUT_SECONDS)
    return _http_client


def set_http_client_for_tests(client: httpx.AsyncClient | None) -> None:
    """Test-only override -- see conftest.py fixtures. Passing None resets
    to the real, lazily-constructed client."""
    global _http_client
    _http_client = client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
    _http_client = None


def _next_backoff_seconds(attempt_count: int) -> float:
    """`attempt_count` is the count AFTER this failed attempt (1-indexed) --
    the first retry waits BOXKITE_WEBHOOK_RETRY_BASE_SECONDS, the second
    waits 2x that, etc., capped at BOXKITE_WEBHOOK_RETRY_MAX_SECONDS."""
    backoff = settings.BOXKITE_WEBHOOK_RETRY_BASE_SECONDS * (2 ** max(0, attempt_count - 1))
    return min(float(backoff), float(settings.BOXKITE_WEBHOOK_RETRY_MAX_SECONDS))


async def _attempt_delivery(delivery, subscription) -> None:
    """Performs exactly one delivery attempt and records the outcome. Never
    raises -- every failure mode (network error, non-2xx response, timeout,
    or a destination that fails request-time re-validation) is caught and
    recorded via `WebhookDeliveryRepository`, since this is called from a
    background loop with no caller to propagate an exception to."""
    import json as _json

    parsed_url = urlparse(subscription.url)
    hostname = parsed_url.hostname
    validated_ip = await resolve_and_validate_destination_ip(hostname) if hostname else None
    if validated_ip is None:
        logger.warning(
            "[webhook-delivery] Refusing delivery %s: destination %s failed request-time "
            "re-validation (private/link-local/loopback/metadata address, unresolvable, or "
            "no hostname at all)",
            delivery.id,
            hostname,
        )
        await _record_failure(
            delivery,
            response_status_code=None,
            response_body=None,
            failure_reason="destination_not_allowed",
        )
        return

    session_factory = get_session_factory()
    payload_format = getattr(subscription, "payload_format", None) or "boxkite_v1"
    body_payload = (
        build_splunk_hec_payload(delivery.payload) if payload_format == PAYLOAD_FORMAT_SPLUNK_HEC else delivery.payload
    )
    body = _json.dumps(body_payload, sort_keys=True, separators=(",", ":"))
    timestamp = int(time.time())

    try:
        secret = decrypt_signing_secret(subscription)
    except Exception as exc:
        logger.error(
            "[webhook-delivery] Failed to decrypt signing secret for subscription %s: %s",
            subscription.id,
            exc,
        )
        async with session_factory() as db:
            await WebhookDeliveryRepository(db).record_failed_attempt(
                delivery_id=delivery.id,
                next_attempt_at=None,
                response_status_code=None,
                response_body=None,
                failure_reason="signing_secret_unavailable",
                exhausted=True,
            )
        return

    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: build_signature_header(secret=secret, timestamp=timestamp, body=body),
        EVENT_TYPE_HEADER: delivery.event_type,
        DELIVERY_ID_HEADER: delivery.id,
        # Explicit, since the actual connection below is pinned to
        # `validated_ip`, not `hostname` -- without this, the Host header
        # would otherwise reflect the IP literal instead of the real
        # destination host.
        "Host": hostname,
    }

    if payload_format == PAYLOAD_FORMAT_SPLUNK_HEC:
        try:
            hec_token = decrypt_hec_token(subscription)
        except Exception as exc:
            logger.error(
                "[webhook-delivery] Failed to decrypt HEC token for subscription %s: %s",
                subscription.id,
                exc,
            )
            hec_token = None
        if hec_token:
            headers["Authorization"] = f"Splunk {hec_token}"

    # Pin the actual connection to `validated_ip` -- the exact address
    # `resolve_and_validate_destination_ip` just validated -- rather than
    # handing httpx the hostname and letting it perform its own, separate,
    # attacker-influenceable DNS lookup at connect time. Mirrors
    # `sidecar/sidecar_secrets.py`'s `http_request` route exactly: the
    # `sni_hostname` extension keeps TLS SNI/certificate-hostname
    # verification against the real hostname while the socket connects to
    # the literal IP.
    netloc_host = f"[{validated_ip}]" if ":" in validated_ip else validated_ip
    port_suffix = f":{parsed_url.port}" if parsed_url.port else ""
    pinned_url = parsed_url._replace(netloc=f"{netloc_host}{port_suffix}").geturl()

    client = _get_http_client()
    try:
        outgoing = client.build_request("POST", pinned_url, content=body, headers=headers)
        outgoing.extensions["sni_hostname"] = hostname
        response = await client.send(outgoing)
    except Exception as exc:
        await _record_failure(delivery, response_status_code=None, response_body=None, failure_reason=str(exc)[:500])
        return

    response_body = response.text[:_RESPONSE_BODY_MAX_LENGTH]
    if 200 <= response.status_code < 300:
        async with session_factory() as db:
            await WebhookDeliveryRepository(db).mark_delivered(
                delivery_id=delivery.id,
                response_status_code=response.status_code,
                response_body=response_body,
            )
            await WebhookSubscriptionRepository(db).touch_last_triggered(subscription.id)
        return

    await _record_failure(
        delivery,
        response_status_code=response.status_code,
        response_body=response_body,
        failure_reason=f"non_2xx_response:{response.status_code}",
    )


async def _record_failure(
    delivery, *, response_status_code: int | None, response_body: str | None, failure_reason: str
) -> None:
    next_attempt_count = delivery.attempt_count + 1
    exhausted = next_attempt_count >= settings.BOXKITE_WEBHOOK_MAX_DELIVERY_ATTEMPTS
    next_attempt_at = None
    if not exhausted:
        backoff = _next_backoff_seconds(next_attempt_count)
        next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)

    session_factory = get_session_factory()
    async with session_factory() as db:
        await WebhookDeliveryRepository(db).record_failed_attempt(
            delivery_id=delivery.id,
            next_attempt_at=next_attempt_at,
            response_status_code=response_status_code,
            response_body=response_body,
            failure_reason=failure_reason,
            exhausted=exhausted,
        )


async def _deliver_once() -> None:
    """One pass: fetch due deliveries (across all accounts -- see
    `WebhookDeliveryRepository.list_due`'s docstring), resolve each one's
    subscription, and attempt delivery. A subscription that's since been
    deleted (the account unregistered the webhook mid-retry) is treated as
    a permanent failure -- there's nowhere left to deliver to."""
    session_factory = get_session_factory()
    async with session_factory() as db:
        due = await WebhookDeliveryRepository(db).list_due(
            now=datetime.now(timezone.utc), limit=settings.BOXKITE_WEBHOOK_WORKER_BATCH_LIMIT
        )
        if not due:
            return
        resolved: list[tuple] = []
        for delivery in due:
            subscription = await WebhookSubscriptionRepository(db).get_for_account(
                subscription_id=delivery.subscription_id, account_id=delivery.account_id
            )
            resolved.append((delivery, subscription))

    for delivery, subscription in resolved:
        if subscription is None or not subscription.is_active:
            async with session_factory() as db:
                await WebhookDeliveryRepository(db).record_failed_attempt(
                    delivery_id=delivery.id,
                    next_attempt_at=None,
                    response_status_code=None,
                    response_body=None,
                    failure_reason="subscription_deleted_or_inactive",
                    exhausted=True,
                )
            continue
        try:
            await _attempt_delivery(delivery, subscription)
        except Exception as exc:
            logger.error("[webhook-delivery] Unexpected error delivering %s: %s", delivery.id, exc)


async def run_webhook_delivery_loop(*, stop_event: asyncio.Event) -> None:
    interval = settings.BOXKITE_WEBHOOK_WORKER_INTERVAL_SECONDS
    while not stop_event.is_set():
        try:
            await _deliver_once()
        except Exception as exc:
            logger.error("[webhook-delivery] Unexpected error during delivery cycle: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
