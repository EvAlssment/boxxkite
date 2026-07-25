"""Webhook registration/list/delete API (docs/WEBHOOKS-DESIGN.md), and a
read-only delivery-history route for observability.

Authenticated with a long-lived API key -- same credential
`/v1/sandboxes/*`/`/v1/secrets/*` require -- since a webhook subscription
only exists to be notified about events on this same account's own sandbox
sessions. Ownership follows `routers/secrets.py`'s exact pattern: every
lookup is scoped to `account.id` at the database layer
(`WebhookSubscriptionRepository.get_for_account`), so a foreign
`subscription_id` 404s, never distinguishing "doesn't exist" from "belongs
to someone else".

The registered `url` is caller-supplied, external, and something this
service will make outbound HTTP requests to (see `webhook_delivery.py`) --
a genuinely new SSRF-adjacent surface, mirroring `routers/secrets.py`'s
`allowed_hosts` validation. See `_validate_webhook_url` below and
`docs/WEBHOOKS-DESIGN.md`'s security section for the full accounting of
what this check does and does not cover. `_validate_webhook_url` is only
the creation-time backstop -- the real, load-bearing, DNS-rebinding-safe
control now lives in `webhook_delivery.py`'s `_attempt_delivery`, which
re-resolves and re-validates the destination on every delivery attempt
(GitHub issue #148).
"""

from __future__ import annotations

import secrets as secrets_module
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_account_via_api_key
from ..errors import ApiError, LimitExceededError
from ..host_safety import resolve_host_is_unsafe
from ..models_orm import Account
from ..rate_limit import enforce_rate_limit
from ..repository import WebhookDeliveryRepository, WebhookSubscriptionRepository
from ..schemas import (
    WebhookCreatedResponse,
    WebhookCreateRequest,
    WebhookDeliveryOut,
    WebhookOut,
)
from ..webhooks import encrypt_hec_token, encrypt_signing_secret

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

WEBHOOK_DELIVERY_DEFAULT_LIMIT = 20
WEBHOOK_DELIVERY_MAX_LIMIT = 100


def _generate_signing_secret() -> str:
    """A fresh, high-entropy signing secret, prefixed so a leaked value is
    greppable/identifiable, same reasoning `security.py:generate_api_key`
    documents for API keys -- deliberately a distinct prefix (`whsec_`, the
    same convention Stripe's own webhook secrets use) so the two credential
    types are never visually confusable in logs."""
    return f"whsec_{secrets_module.token_urlsafe(32)}"


def _validate_webhook_url(url: str) -> None:
    """Best-effort, creation-time-only backstop against SSRF -- mirrors
    `routers/secrets.py`'s `_validate_allowed_hosts` exactly (same
    `host_safety.resolve_host_is_unsafe` check). Rejects a URL whose host
    resolves to a private/link-local/loopback/metadata address right now.

    This alone does NOT close the DNS-rebinding gap (a URL that resolves
    safely now can be repointed later) -- see docs/WEBHOOKS-DESIGN.md's
    security section. What closes it is `webhook_delivery.py`'s
    request-time re-validation via `host_safety.resolve_and_validate_
    destination_ip`, run on every delivery attempt, not just here."""
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ApiError(422, "invalid_webhook_url", "url has no hostname")
    if resolve_host_is_unsafe(parsed.hostname):
        raise ApiError(
            422,
            "unsafe_webhook_url",
            f"url {url!r} resolves to a private, link-local, loopback, or "
            "cloud-metadata address and cannot be used as a webhook destination.",
        )


async def _enforce_webhook_rate_limit(request: Request, response: Response, account: Account) -> None:
    await enforce_rate_limit(
        request,
        bucket="webhook_ops",
        subject=str(account.id),
        limit=settings.BOXKITE_WEBHOOK_RATE_LIMIT_PER_MINUTE,
        response=response,
    )


@router.post(
    "",
    response_model=WebhookCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook subscription",
    description=(
        "Registers a URL to receive POST notifications for the given "
        "event_types (see docs/WEBHOOKS-DESIGN.md for the full event "
        "catalog -- 'sandbox.created'/'sandbox.destroyed'/'audit_log.entry'). "
        "A fresh signing secret is generated and returned exactly once, in "
        "this response -- use it to verify the X-Boxkite-Webhook-Signature "
        "header on every delivery. The `url` is checked at registration "
        "time against the same private/link-local/loopback/metadata-address "
        "denylist POST /v1/secrets uses for allowed_hosts. `payload_format` "
        "optionally selects a Splunk HEC-shaped delivery body instead of "
        "this API's own envelope."
    ),
)
async def create_webhook(
    body: WebhookCreateRequest,
    request: Request,
    response: Response,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> WebhookCreatedResponse:
    await _enforce_webhook_rate_limit(request, response, account)

    webhooks = WebhookSubscriptionRepository(db)
    existing_count = await webhooks.count_for_account(account.id)
    if existing_count >= settings.BOXKITE_MAX_WEBHOOKS_PER_ACCOUNT:
        raise LimitExceededError(
            code="webhook_limit_reached",
            message="Webhook subscription limit reached for this account.",
            details={"limit": settings.BOXKITE_MAX_WEBHOOKS_PER_ACCOUNT},
        )

    _validate_webhook_url(body.url)

    secret = _generate_signing_secret()
    encrypted = encrypt_signing_secret(secret)

    hec_token_fields: dict[str, str | None] = {
        "hec_token_ciphertext": None,
        "hec_token_nonce": None,
        "hec_token_wrapped_data_key": None,
        "hec_token_encryption_key_id": None,
    }
    if body.hec_token:
        encrypted_hec_token = encrypt_hec_token(body.hec_token)
        hec_token_fields = {
            "hec_token_ciphertext": encrypted_hec_token.ciphertext_b64,
            "hec_token_nonce": encrypted_hec_token.nonce_b64,
            "hec_token_wrapped_data_key": encrypted_hec_token.wrapped_data_key_b64,
            "hec_token_encryption_key_id": encrypted_hec_token.encryption_key_id,
        }

    row = await webhooks.create(
        account_id=account.id,
        url=body.url,
        description=body.description,
        event_types=body.event_types,
        ciphertext=encrypted.ciphertext_b64,
        nonce=encrypted.nonce_b64,
        wrapped_data_key=encrypted.wrapped_data_key_b64,
        encryption_key_id=encrypted.encryption_key_id,
        payload_format=body.payload_format,
        **hec_token_fields,
    )
    return WebhookCreatedResponse(**WebhookOut.model_validate(row).model_dump(), secret=secret)


@router.get(
    "",
    response_model=list[WebhookOut],
    summary="List webhook subscriptions",
    description="Lists webhook subscriptions for the authenticated account. The signing secret is never returned here.",
)
async def list_webhooks(
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[WebhookOut]:
    rows = await WebhookSubscriptionRepository(db).list_for_account(account.id)
    return [WebhookOut.model_validate(r) for r in rows]


@router.delete(
    "/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a webhook subscription",
    description="Deletes a webhook subscription belonging to the authenticated account. 404 if already gone or never owned by this account.",
)
async def delete_webhook(
    subscription_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> Response:
    deleted = await WebhookSubscriptionRepository(db).delete(account_id=account.id, subscription_id=subscription_id)
    if not deleted:
        raise ApiError(404, "not_found", "Webhook subscription not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{subscription_id}/deliveries",
    response_model=list[WebhookDeliveryOut],
    summary="List recent delivery attempts for a webhook subscription",
    description=(
        "Returns recent delivery attempts (pending/delivered/failed) for "
        "this subscription, newest first -- observability into the retry/ "
        "backoff behavior described in docs/WEBHOOKS-DESIGN.md. 404 for a "
        "subscription_id owned by a different account, identical to DELETE."
    ),
)
async def list_webhook_deliveries(
    subscription_id: str = Path(...),
    limit: int = Query(default=WEBHOOK_DELIVERY_DEFAULT_LIMIT, ge=1, le=WEBHOOK_DELIVERY_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[WebhookDeliveryOut]:
    subscription = await WebhookSubscriptionRepository(db).get_for_account(
        subscription_id=subscription_id, account_id=account.id
    )
    if subscription is None:
        raise ApiError(404, "not_found", "Webhook subscription not found")
    rows = await WebhookDeliveryRepository(db).list_for_subscription(
        subscription_id=subscription_id, account_id=account.id, limit=limit, offset=offset
    )
    return [WebhookDeliveryOut.model_validate(r) for r in rows]
