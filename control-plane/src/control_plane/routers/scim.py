"""SCIM 2.0 provisioning via WorkOS Directory Sync -- Phase 2 of issue #126
(docs/ENTERPRISE-SSO-DESIGN.md's Phase 1 covers interactive SSO login;
this closes the admin-driven provisioning/deprovisioning half the design
doc explicitly deferred).

An enterprise IdP admin (Okta/Entra/Google Workspace, via WorkOS's
Directory Sync product, which speaks real SCIM 2.0 to the IdP on this
control-plane's behalf) provisions/deprovisions employee accounts without
those employees ever manually signing up: adding/removing/updating a user
in the IdP fires a WorkOS webhook here, and this route creates/updates/
deactivates the corresponding `Account` row.

This is a genuinely new trust boundary -- an unauthenticated-but-signed
webhook endpoint that can create and deactivate accounts -- see
SECURITY.md's "New trust boundary: SCIM 2.0 provisioning" section for the
full disclosure. Verification (`verify_workos_webhook_signature` below) is
checked BEFORE the request body is ever parsed as JSON or touches the
database, so an unsigned/incorrectly-signed request never reaches account
logic at all.

Event names, payload shape, and signature scheme below are all confirmed
against WorkOS's own documentation/SDKs, not invented -- see
docs/ENTERPRISE-SSO-DESIGN.md's SCIM section for the exact citations
(WorkOS's Directory Sync event-types docs, the Directory User object
reference, and the WorkOS-Signature header format documented across
WorkOS's own SDKs, e.g. the Ruby SDK's `WorkOS::Webhooks` module).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..errors import ApiError
from ..rate_limit import enforce_rate_limit
from ..repository import AccountRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth/sso", tags=["auth"])

WORKOS_SIGNATURE_HEADER = "WorkOS-Signature"

# WorkOS Directory Sync's full event-type set (confirmed against WorkOS's
# own Directory Sync docs). Only the three `dsync.user.*` events are acted
# on in this pass -- see this module's docstring and
# docs/ENTERPRISE-SSO-DESIGN.md's SCIM section for the explicit list of
# what's deliberately NOT handled (group events, org-level activation).
DIRECTORY_SYNC_EVENT_TYPES: tuple[str, ...] = (
    "dsync.activated",
    "dsync.deleted",
    "dsync.user.created",
    "dsync.user.updated",
    "dsync.user.deleted",
    "dsync.group.created",
    "dsync.group.updated",
    "dsync.group.deleted",
    "dsync.group.user_added",
    "dsync.group.user_removed",
)

# A WorkOS Directory User's `state` values NOT equal to "active" -- treated
# as deactivated here. Confirmed via WorkOS's Directory User API reference
# (legal state values: "active" | "inactive" | "suspended").
_DEACTIVATED_STATES = frozenset({"inactive", "suspended"})

# Match models_orm.py's Account.scim_directory_user_id/sso_organization_id
# column widths (both String(191)) -- rejecting an oversized value here
# instead of letting it reach the DB turns a driver-level "value too long"
# error (an unhandled exception -> 500) into a clean 400.
_MAX_DIRECTORY_USER_ID_LENGTH = 191
_MAX_ORGANIZATION_ID_LENGTH = 191


def _require_scim_enabled() -> None:
    if not (settings.BOXKITE_SCIM_PROVISIONING_ENABLED and settings.scim_provisioning_configured):
        raise ApiError(404, "not_found", "Not found")


def _parse_workos_signature_header(header_value: str) -> tuple[str, str]:
    """Parses `t=<epoch-ms>,v1=<hex-hmac-sha256>` into (timestamp, signature).
    Raises ApiError(401, "invalid_signature", ...) on any malformed field --
    a missing `t` or `v1` field is treated identically to a bad signature,
    never surfaced as a different error class that could help an attacker
    narrow down what's wrong with a forged header."""
    fields: dict[str, str] = {}
    for part in header_value.split(","):
        key, sep, value = part.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    timestamp = fields.get("t")
    signature = fields.get("v1")
    if not timestamp or not signature:
        raise ApiError(401, "invalid_signature", "Malformed WorkOS-Signature header")
    return timestamp, signature


def verify_workos_webhook_signature(
    *, secret: str, header_value: str | None, raw_body: bytes, tolerance_seconds: int
) -> None:
    """Verifies a WorkOS Directory Sync webhook delivery's `WorkOS-Signature`
    header. Raises `ApiError(401, "invalid_signature", ...)` on any failure:
    missing header, malformed header, a signature that doesn't match
    (constant-time compared via `hmac.compare_digest` -- never `==`, which
    would leak timing information about how close a forged signature got),
    or a timestamp further than `tolerance_seconds` from "now" in either
    direction (replay defense).

    Scheme (confirmed against WorkOS's own docs/SDKs, not invented):
    `WorkOS-Signature: t=<epoch-milliseconds>,v1=<hex-hmac-sha256>`, signed
    over the UTF-8 bytes of `f"{t}."` concatenated with the raw request
    body -- the exact same `f"{timestamp}.{body}"`-then-HMAC-SHA256-hex
    shape this codebase's own OUTBOUND webhook signing already uses
    (`webhooks.py`'s `sign_payload`/`build_signature_header`), just applied
    in the opposite direction against a different secret
    (`WORKOS_WEBHOOK_SECRET`, distinct from any secret this control-plane
    itself issues to ITS OWN webhook subscribers).

    `secret` must be checked non-empty by the caller (`scim_webhook`, via
    `_require_scim_enabled`'s `scim_provisioning_configured` gate) before
    this is ever called -- an empty secret would make every signature
    "valid" against an all-zero HMAC key, which must never be reachable.
    """
    if not header_value:
        raise ApiError(401, "invalid_signature", "Missing WorkOS-Signature header")

    timestamp_str, provided_signature = _parse_workos_signature_header(header_value)
    try:
        timestamp_ms = int(timestamp_str)
    except ValueError:
        raise ApiError(401, "invalid_signature", "Malformed WorkOS-Signature header") from None

    signed_data = f"{timestamp_str}.".encode("utf-8") + raw_body
    expected_signature = hmac.new(secret.encode("utf-8"), signed_data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise ApiError(401, "invalid_signature", "Webhook signature verification failed")

    timestamp_seconds = timestamp_ms / 1000
    if abs(time.time() - timestamp_seconds) > tolerance_seconds:
        raise ApiError(401, "invalid_signature", "Webhook timestamp outside the allowed tolerance window")


def _is_plausible_email(value: str) -> bool:
    """Syntax-only check (no deliverability/DNS lookup -- this is a webhook
    handler, not a signup form) via the same `email_validator` library
    schemas.py's EmailStr already uses elsewhere, so a directory entry
    can't smuggle a non-email string (an injection payload, a null byte, an
    oversized value) into Account.email just because it was truthy."""
    try:
        validate_email(value, check_deliverability=False)
    except EmailNotValidError:
        return False
    return True


def _extract_primary_email(data: dict) -> str | None:
    """A WorkOS Directory User's `emails` field is an array of
    `{primary, type, value}` objects (confirmed against WorkOS's Directory
    User object reference) -- prefer the entry marked `primary`, falling
    back to the first entry with a value if none is marked primary (some
    directory providers don't reliably set the flag).

    Defensive against a malformed/adversarial payload where `emails` isn't
    a list at all, or its entries aren't `{...}` objects, or `value` isn't
    a string -- each of those is just skipped rather than raising, since a
    crash here would be an unhandled 500 on attacker- or IdP-bug-controlled
    input (see test_scim_payload_fuzz.py). `_is_plausible_email` additionally
    rejects a syntactically-invalid `value` (garbage, an injection payload)
    rather than writing it straight into `Account.email`."""
    emails = data.get("emails")
    if not isinstance(emails, list):
        return None
    candidates = [
        entry
        for entry in emails
        if isinstance(entry, dict) and isinstance(entry.get("value"), str) and entry["value"]
    ]
    for entry in candidates:
        if entry.get("primary") and _is_plausible_email(entry["value"]):
            return entry["value"]
    for entry in candidates:
        if _is_plausible_email(entry["value"]):
            return entry["value"]
    return None


async def _handle_user_upsert(db: AsyncSession, data: dict) -> None:
    """Shared handling for `dsync.user.created` and `dsync.user.updated` --
    treated as an upsert keyed by `scim_directory_user_id` rather than two
    separate code paths, so an out-of-order redelivery (an `updated` event
    arriving before the `created` one WorkOS already sent, or a `created`
    event replayed after this deployment already has the row) is handled
    the same idempotent way either time."""
    directory_user_id = data.get("id")
    if (
        not isinstance(directory_user_id, str)
        or not directory_user_id
        or len(directory_user_id) > _MAX_DIRECTORY_USER_ID_LENGTH
    ):
        raise ApiError(400, "invalid_payload", "Directory user event missing a valid 'id'")
    email = _extract_primary_email(data)
    if not email:
        raise ApiError(400, "invalid_payload", "Directory user event missing a valid email")
    organization_id = data.get("organization_id")
    if not isinstance(organization_id, str) or len(organization_id) > _MAX_ORGANIZATION_ID_LENGTH:
        # A non-string, or an oversized, organization_id is nonsensical --
        # never bind it into the `sso_organization_id` string column below,
        # treat it the same as "not provided" instead of erroring the whole
        # event out over a field this handler doesn't gate account access on.
        organization_id = None
    state = data.get("state")
    if not isinstance(state, str):
        state = None

    accounts = AccountRepository(db)
    existing = await accounts.get_by_scim_directory_user_id(directory_user_id)

    if existing is None:
        email_owner = await accounts.get_by_email(email)
        if email_owner is not None:
            # A DIFFERENT account (password/social/SSO, or a different
            # directory user entirely) already owns this email -- do not
            # silently attach this directory user's id to it. Same
            # anti-takeover posture as social_login.py/enterprise_sso.py's
            # own email-collision refusal, adapted for an async webhook
            # context: there's no browser to show a 409 to, so this is
            # logged and the delivery is still acknowledged (200) rather
            # than raised, matching WorkOS's own expectation that a
            # webhook handler ack a delivery even when it declines to act
            # on it (an unacknowledged delivery is retried indefinitely).
            logger.warning(
                "[control-plane] SCIM webhook: directory_user_id=%s (email=%s) collides with "
                "an existing account (%s) not provisioned by this directory user -- skipping.",
                directory_user_id,
                email,
                email_owner.id,
            )
            return
        existing = await accounts.create_scim(
            email=email, scim_directory_user_id=directory_user_id, sso_organization_id=organization_id
        )
    else:
        await accounts.update_scim_profile(
            account_id=existing.id, email=email, sso_organization_id=organization_id
        )

    if state in _DEACTIVATED_STATES:
        await accounts.mark_scim_deactivated(existing.id)
    elif state == "active":
        await accounts.mark_scim_reactivated(existing.id)


async def _handle_user_deleted(db: AsyncSession, data: dict) -> None:
    """`dsync.user.deleted` fires on a hard delete in the directory (most
    IdPs soft-delete instead, which arrives as `dsync.user.updated` with
    `state="inactive"` -- see `_handle_user_upsert` above). Deliberately
    does NOT delete the `Account` row itself: `Account.api_keys`/
    `sandbox_sessions` cascade-delete on the account, which is a heavy,
    hard-to-reverse action this pass does not take on a webhook's say-so
    alone. Treated identically to deactivation instead -- see
    docs/ENTERPRISE-SSO-DESIGN.md's SCIM section for this disclosed scope
    decision."""
    directory_user_id = data.get("id")
    if not isinstance(directory_user_id, str) or not directory_user_id:
        return
    accounts = AccountRepository(db)
    existing = await accounts.get_by_scim_directory_user_id(directory_user_id)
    if existing is None:
        return
    await accounts.mark_scim_deactivated(existing.id)


@router.post(
    "/scim-webhook",
    summary="WorkOS Directory Sync (SCIM 2.0) provisioning webhook",
    dependencies=[Depends(_require_scim_enabled)],
    description=(
        "Receives WorkOS Directory Sync webhook events and provisions/deprovisions the "
        "corresponding Account. Authenticated via the WorkOS-Signature header (HMAC-SHA256) -- "
        "there is no API key on this route by design, since WorkOS itself is the caller, not "
        "an SDK/dashboard user. Off by default (BOXKITE_SCIM_PROVISIONING_ENABLED); see "
        "docs/ENTERPRISE-SSO-DESIGN.md's SCIM section."
    ),
)
async def scim_webhook(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    await enforce_rate_limit(
        request,
        bucket="scim_webhook",
        limit=settings.BOXKITE_SCIM_WEBHOOK_RATE_LIMIT_PER_MINUTE,
        response=response,
    )

    raw_body = await request.body()
    if len(raw_body) > settings.SCIM_WEBHOOK_MAX_BODY_BYTES:
        # Cheap length check, before spending a HMAC computation (or a JSON
        # parse) on a body far bigger than any real WorkOS Directory User
        # event -- a legitimate delivery is a few KB at most.
        raise ApiError(413, "payload_too_large", "Webhook payload exceeds the maximum allowed size")

    verify_workos_webhook_signature(
        secret=settings.WORKOS_WEBHOOK_SECRET,
        header_value=request.headers.get(WORKOS_SIGNATURE_HEADER),
        raw_body=raw_body,
        tolerance_seconds=settings.SCIM_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS,
    )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise ApiError(400, "invalid_payload", "Malformed JSON body") from None
    except UnicodeDecodeError:
        # `json.loads` sniffs the byte string's encoding from its leading
        # bytes (UTF-8/16/32, BOM or not) before decoding -- a body that
        # merely LOOKS like it starts a UTF-16/32 BOM (e.g. a leading
        # b"\x00\x01\x02...") gets misdetected and fails at the decode
        # step with UnicodeDecodeError, not JSONDecodeError.
        raise ApiError(400, "invalid_payload", "Malformed JSON body") from None
    except RecursionError:
        # A deeply nested body (e.g. thousands of levels of `[[[...]]]`)
        # blows Python's C-accelerated JSON decoder's own recursion limit
        # well before it hits the size cap above -- RecursionError isn't a
        # JSONDecodeError, so without this it would propagate as an
        # unhandled exception (a crash, 500) instead of a clean 400.
        raise ApiError(400, "invalid_payload", "Malformed JSON body") from None

    # WorkOS always sends a top-level JSON object -- a JSON array/string/
    # number/null body (or a signed-but-garbage `data` field) is rejected
    # as a 400 here rather than reaching `.get()` calls below that assume a
    # dict and would otherwise raise an unhandled AttributeError/TypeError
    # (500) on a signature-valid-but-malformed-shape delivery.
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_payload", "Payload must be a JSON object")

    event_type = payload.get("event")
    data = payload.get("data")
    if data is None:
        data = {}
    elif not isinstance(data, dict):
        raise ApiError(400, "invalid_payload", "'data' must be an object")

    if event_type in ("dsync.user.created", "dsync.user.updated"):
        await _handle_user_upsert(db, data)
    elif event_type == "dsync.user.deleted":
        await _handle_user_deleted(db, data)
    else:
        # dsync.group.*/dsync.activated/dsync.deleted -- explicitly NOT
        # handled in this pass (no group-based role mapping, no SCIM group
        # sync to internal permissions -- see docs/ENTERPRISE-SSO-DESIGN.md's
        # SCIM section's "not built" list). Still acknowledged with 200 so
        # WorkOS doesn't retry an event this deployment has no handler for.
        logger.info("[control-plane] SCIM webhook: ignoring unhandled event type %r", event_type)

    return {"received": True}
