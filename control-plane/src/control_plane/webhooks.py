"""Outbound webhook event types, payload construction, and HMAC signing
(docs/WEBHOOKS-DESIGN.md). This module owns everything about *what* gets
sent and how it's authenticated; `webhook_delivery.py` owns the actual HTTP
delivery loop and retry/backoff scheduling.

Per-account opt-in by construction, mirroring `enable_secret_env`/
`enable_git_tools`'s "new attack surface stays inert until explicitly
requested" posture but at the account level rather than a call-site
parameter: `enqueue_event` looks up this account's own active subscriptions
for the fired event type, and an account with zero registered webhooks (the
default for every account) gets an empty list back -- nothing is ever
enqueued, no outbound HTTP call is ever made, for any account that hasn't
explicitly registered a webhook. There is no global env-var gate on top of
this because none is needed: the feature has zero effect until a specific
account opts in for itself, the same reasoning docs/WEBHOOKS-DESIGN.md's
"why no global on/off flag" section spells out.

`audit_log.entry` (GitHub issue #125, SIEM/audit-log export) is the third
event type this module supports. It is fired once per `ExecLogEntry` write
(`routers/sandboxes.py`'s `_log_exec_entry`, the single shared call site
every exec/file-op route already goes through), reusing this exact
subscribe/enqueue/sign/retry pipeline rather than a dedicated delivery
path -- see that function's docstring for the full reasoning. This module
also owns `build_splunk_hec_payload`, the optional Splunk HTTP Event
Collector body shape a subscription can opt into via
`WebhookSubscription.payload_format="splunk_hec"`, so an enterprise buyer's
SIEM can ingest deliveries directly without a translation layer of their
own.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .repository import WebhookDeliveryRepository, WebhookSubscriptionRepository
from .secrets_kms import EncryptedSecret, get_secrets_kms_client

logger = logging.getLogger(__name__)

# Started as a minimal first cut per GitHub issue #82's acceptance
# criteria ("sandbox.created"/"sandbox.destroyed"); "audit_log.entry" was
# added per GitHub issue #125 (SIEM/audit-log export). schemas.py imports
# this constant directly (not duplicated) so the two never drift; adding a
# new event type means updating this tuple AND schemas.py's
# `WebhookEventType` Literal in lockstep.
WEBHOOK_EVENT_TYPES: tuple[str, ...] = ("sandbox.created", "sandbox.destroyed", "audit_log.entry")

# `WebhookSubscription.payload_format` values -- see this module's
# docstring and `build_splunk_hec_payload` below.
PAYLOAD_FORMAT_BOXKITE_V1 = "boxkite_v1"
PAYLOAD_FORMAT_SPLUNK_HEC = "splunk_hec"
WEBHOOK_PAYLOAD_FORMATS: tuple[str, ...] = (PAYLOAD_FORMAT_BOXKITE_V1, PAYLOAD_FORMAT_SPLUNK_HEC)

# Splunk HEC's own field names -- fixed by Splunk's ingestion contract, not
# a boxkite convention. See docs/WEBHOOKS-DESIGN.md's audit-log-export
# addendum for the full field mapping this mirrors.
_SPLUNK_HEC_SOURCE = "boxkite"
_SPLUNK_HEC_SOURCETYPE = "_json"

# Signed data is "{timestamp}.{body}", not the body alone -- mirrors the
# Stripe-style webhook-signing convention this design doc follows (see
# docs/WEBHOOKS-DESIGN.md section on signing) so a captured, valid signature
# can't be replayed indefinitely against a receiver that checks the
# timestamp itself. This module only produces the signature; enforcing a
# freshness window is the receiver's own responsibility, documented in the
# design doc's verification snippet.
SIGNATURE_HEADER = "X-Boxkite-Webhook-Signature"
EVENT_TYPE_HEADER = "X-Boxkite-Webhook-Event"
DELIVERY_ID_HEADER = "X-Boxkite-Webhook-Id"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sign_payload(*, secret: str, timestamp: int, body: str) -> str:
    """Returns the HMAC-SHA256 hex digest of "{timestamp}.{body}" under
    `secret`. Used both when actually delivering (webhook_delivery.py) and
    by a receiver verifying a delivery -- see docs/WEBHOOKS-DESIGN.md for
    the receiver-side verification snippet this must stay compatible with."""
    signed_data = f"{timestamp}.{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), signed_data, hashlib.sha256).hexdigest()


def build_signature_header(*, secret: str, timestamp: int, body: str) -> str:
    """`t=<unix_ts>,v1=<hex_hmac>` -- versioned scheme name (`v1`) so a
    future signing-algorithm change can add `v2` alongside it without
    breaking existing receivers, same reasoning Stripe's own
    `Stripe-Signature` header format uses."""
    signature = sign_payload(secret=secret, timestamp=timestamp, body=body)
    return f"t={timestamp},v1={signature}"


def encrypt_signing_secret(secret: str) -> EncryptedSecret:
    """Envelope-encrypts a freshly generated webhook signing secret using
    the same KMS primitive `Secret.ciphertext` uses -- see
    `WebhookSubscription`'s docstring for why this secret gets the same
    at-rest protection an org secret does."""
    return get_secrets_kms_client().encrypt(secret)


def decrypt_signing_secret(subscription) -> str:
    """Re-derives the raw signing secret from a `WebhookSubscription` row's
    encrypted columns -- called only from `webhook_delivery.py`, right
    before computing a delivery's signature. Never returned by any API
    route."""
    encrypted = EncryptedSecret(
        ciphertext_b64=subscription.ciphertext,
        nonce_b64=subscription.nonce,
        wrapped_data_key_b64=subscription.wrapped_data_key,
        encryption_key_id=subscription.encryption_key_id,
    )
    return get_secrets_kms_client().decrypt(encrypted)


def encrypt_hec_token(token: str) -> EncryptedSecret:
    """Envelope-encrypts an optional, caller-supplied Splunk HEC token using
    the same KMS primitive the signing secret above uses -- see
    `WebhookSubscription`'s docstring for why this destination credential
    gets the same at-rest protection."""
    return get_secrets_kms_client().encrypt(token)


def decrypt_hec_token(subscription) -> str | None:
    """Re-derives the raw HEC token from a `WebhookSubscription` row's
    encrypted `hec_token_*` columns, or `None` if the subscription was
    never given one (the common case -- HEC token is optional). Called
    only from `webhook_delivery.py`, right before building the delivery's
    headers. Never returned by any API route."""
    if subscription.hec_token_ciphertext is None:
        return None
    encrypted = EncryptedSecret(
        ciphertext_b64=subscription.hec_token_ciphertext,
        nonce_b64=subscription.hec_token_nonce,
        wrapped_data_key_b64=subscription.hec_token_wrapped_data_key,
        encryption_key_id=subscription.hec_token_encryption_key_id,
    )
    return get_secrets_kms_client().decrypt(encrypted)


def build_splunk_hec_payload(event_payload: dict[str, Any]) -> dict[str, Any]:
    """Wraps a boxkite event envelope (`build_event_payload`'s output) in a
    Splunk HTTP Event Collector-shaped body, so a subscription with
    `payload_format="splunk_hec"` can be POSTed directly at a Splunk HEC
    endpoint (`https://<host>:8088/services/collector/event`) with no
    translation layer of the receiver's own. Field mapping, fixed by
    Splunk's own ingestion contract (docs/WEBHOOKS-DESIGN.md):

    - `time`: HEC's required epoch-seconds field, parsed from the
      envelope's own `created_at` (always present, always UTC).
    - `host`/`source`: fixed to identify boxkite as the origin, mirroring
      how every other boxkite-issued credential/header is prefixed for
      greppability.
    - `sourcetype`: `_json`, Splunk's built-in JSON-parsing sourcetype --
      correct for this body without requiring the receiver to pre-register
      a custom sourcetype.
    - `event`: the FULL, unmodified boxkite envelope (`event_id`, `event`,
      `created_at`, `account_id`, `data`) -- nothing is dropped, so a
      receiver gets the exact same fields either payload_format sends, just
      wrapped differently.
    """
    created_at = datetime.fromisoformat(event_payload["created_at"])
    return {
        "time": created_at.timestamp(),
        "host": _SPLUNK_HEC_SOURCE,
        "source": f"{_SPLUNK_HEC_SOURCE}:{event_payload['event']}",
        "sourcetype": _SPLUNK_HEC_SOURCETYPE,
        "event": event_payload,
    }


def build_event_payload(*, event_type: str, account_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """The JSON envelope every webhook delivery carries. `event_id` is
    generated once per fired occurrence (not per delivery attempt) --
    every subscription matching this event, and every retry of each of
    those deliveries, carries the SAME `event_id`, so a receiver can
    de-duplicate retried/at-least-once deliveries by `event_id` alone (see
    docs/WEBHOOKS-DESIGN.md's "delivery is at-least-once, not exactly-once"
    section)."""
    return {
        "event_id": str(uuid.uuid4()),
        "event": event_type,
        "created_at": _utcnow().isoformat(),
        "account_id": account_id,
        "data": data,
    }


async def enqueue_event(
    db: AsyncSession, *, account_id: str, event_type: str, data: dict[str, Any]
) -> int:
    """Looks up this account's own active subscriptions for `event_type`
    and writes one `WebhookDelivery` row (status="pending") per match. Does
    NOT deliver anything itself -- delivery is entirely the background
    worker's job (`webhook_delivery.py`), so a slow or unreachable receiver
    can never add latency to the sandbox-lifecycle call that fired the
    event. Returns the number of deliveries enqueued (0 for an account with
    no matching subscriptions, the default/common case).

    Best-effort by design, same posture as `AuditSink`: any exception here
    is caught and logged by the caller (`usage_policy.py`), never allowed
    to fail the sandbox create/destroy operation that triggered it -- a
    webhook subsystem bug must never take down sandbox lifecycle
    management.
    """
    subscriptions = await WebhookSubscriptionRepository(db).list_active_for_account_and_event(
        account_id=account_id, event_type=event_type
    )
    if not subscriptions:
        return 0

    payload = build_event_payload(event_type=event_type, account_id=account_id, data=data)
    deliveries = WebhookDeliveryRepository(db)
    for subscription in subscriptions:
        await deliveries.create(
            subscription_id=subscription.id,
            account_id=account_id,
            event_type=event_type,
            payload=payload,
        )
    return len(subscriptions)
