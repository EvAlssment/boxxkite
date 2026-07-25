"""Public, UNAUTHENTICATED demo playground (issue #103) -- lets an anonymous
marketing-site visitor create a real sandbox, run a handful of commands in
it, and tear it down again, with no signup and no API key.

This is deliberately not a code-escape risk: the product's whole isolation
story (SECURITY.md, site/app/page.tsx's isolation section) already holds for
arbitrary agent-issued commands regardless of who's driving them, and every
demo sandbox gets the plain default image with no image_id/volume_mounts/
secret_names -- nothing beyond what any anonymous visitor could already
reach. The actual risks this router defends against instead:

1. Resource/cost exhaustion -- every demo sandbox is created against the
   well-known internal "demo" Account (demo_account.py) so
   UsagePolicy/SandboxSessionRepository's existing bookkeeping works
   unmodified, but capacity is gated against BOXKITE_DEMO_MAX_CONCURRENT
   (this account's own active-session count), a small, separate ceiling
   from BOXKITE_MAX_CONCURRENT_SANDBOXES/BOXKITE_GLOBAL_MAX_CONCURRENT_
   SANDBOXES. If UsagePolicy.create_session's own (much larger-scoped)
   caps happen to bind first -- e.g. BOXKITE_MAX_CONCURRENT_SANDBOXES is
   configured below BOXKITE_DEMO_MAX_CONCURRENT -- that's caught and
   surfaced as the exact same 503 "at capacity" response, never a raw 429
   naming a real-account-shaped limit. Every demo sandbox is also hard-
   capped to BOXKITE_DEMO_LIFETIME_MINUTES (a real K8s activeDeadlineSeconds
   kill, not just bookkeeping -- see UsagePolicy.create_session's
   lifetime_minutes param) and reaped promptly on that same short cutoff
   (see reaper.py) even if the caller never calls DELETE.
2. Session hijacking -- POST /v1/demo/sandboxes mints a short-lived,
   session-scoped signed token (security.py's create_demo_session_token)
   bound to exactly this session_id. Every subsequent call must present it
   as the `X-Demo-Token` header; a bare session_id alone is never
   sufficient to act on a session.
3. Abuse volume -- every route here is rate-limited per source IP
   (BOXKITE_DEMO_RATE_LIMIT_PER_MINUTE), since there is no account to key
   on the way every other rate-limited route in this API has.

404s entirely (route-not-found, not a 403) unless
BOXKITE_DEMO_PLAYGROUND_ENABLED is set -- same convention
routers/images.py's `_require_builder_enabled` already uses for its own
opt-in feature.
"""

from __future__ import annotations

import asyncio
import logging

import jwt
from fastapi import APIRouter, Depends, Header, Path, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..demo_account import get_or_create_demo_account
from ..deps import get_manager, get_usage_policy
from ..errors import ApiError, LimitExceededError
from ..rate_limit import enforce_rate_limit
from ..repository import SandboxSessionRepository
from ..schemas import (
    DEMO_EXEC_OUTPUT_MAX_LENGTH,
    DemoSandboxCreatedResponse,
    DemoSandboxCreateRequest,
    DemoSandboxExecRequest,
    DemoSandboxExecResponse,
)
from ..security import create_demo_session_token, decode_demo_session_token
from ..usage_policy import UsagePolicy

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/demo", tags=["demo-playground"])

_AT_CAPACITY_MESSAGE = "Demo is at capacity, try again shortly."

# Serializes the BOXKITE_DEMO_MAX_CONCURRENT check against
# SandboxSessionRepository.count_active_for_account with the reservation
# that (eventually) makes that count reflect it -- without this, two
# concurrent POST /v1/demo/sandboxes requests can both read "below cap"
# before either has a row committed, bypassing the demo-specific ceiling
# this exact check exists to enforce (usage_policy.py's own
# `_create_session_lock` guards its OWN per-account/global caps the same
# way, but that lock is scoped to a different, much larger-scoped check --
# it doesn't protect this router's separate, smaller BOXKITE_DEMO_MAX_
# CONCURRENT ceiling, and can't be reused here directly since it's already
# acquired *inside* UsagePolicy.create_session, and asyncio.Lock isn't
# reentrant). Deliberately held across the full create_session call
# (including its K8s pod-creation round trip), not just the count check --
# BOXKITE_DEMO_MAX_CONCURRENT is small (default 3) and this is a marketing
# demo, not a high-throughput path, so serializing demo-sandbox creation
# one-at-a-time is an acceptable latency tradeoff for closing the race
# correctly rather than partially.
_demo_create_lock = asyncio.Lock()


def reset_demo_create_lock_for_tests() -> None:
    """Test-only helper mirroring usage_policy.py's
    reset_create_session_lock_for_tests -- avoids cross-test bleed of a
    module-level lock when tests run against the same event loop."""
    global _demo_create_lock
    _demo_create_lock = asyncio.Lock()


def _require_demo_enabled() -> None:
    if not settings.BOXKITE_DEMO_PLAYGROUND_ENABLED:
        raise ApiError(404, "not_found", "The public demo playground is not enabled on this deployment.")


async def _enforce_demo_rate_limit(request: Request, response: Response, *, bucket: str) -> None:
    """Keyed per source IP (no `subject`) -- there is no account on this
    unauthenticated surface to key on instead, same fallback every other
    pre-auth bucket in this API (signup/login/...) already uses."""
    await enforce_rate_limit(
        request,
        bucket=bucket,
        limit=settings.BOXKITE_DEMO_RATE_LIMIT_PER_MINUTE,
        response=response,
    )


async def _authenticate_demo_session(
    *, session_id: str, x_demo_token: str | None
) -> dict:
    """Verify the `X-Demo-Token` header binds this exact session_id -- a
    bare, guessable-ish session_id is never enough on its own. Raises 401
    for a missing/malformed/expired/mismatched token."""
    if not x_demo_token:
        raise ApiError(401, "missing_credentials", "Missing X-Demo-Token header")
    try:
        payload = decode_demo_session_token(x_demo_token)
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_token", "Demo session token is invalid or has expired") from None
    if payload.get("sid") != session_id:
        raise ApiError(401, "invalid_token", "Demo session token does not match this session")
    return payload


async def _get_active_demo_session_or_404(
    *, session_id: str, demo_account_id: str, db: AsyncSession
):
    row = await SandboxSessionRepository(db).get_for_account(session_id=session_id, account_id=demo_account_id)
    if row is None or row.destroyed_at is not None:
        raise ApiError(404, "not_found", "Demo sandbox session not found")
    return row


@router.post(
    "/sandboxes",
    response_model=DemoSandboxCreatedResponse,
    status_code=201,
    summary="Create a demo sandbox (unauthenticated)",
    description=(
        "Creates a short-lived, plain-default-image sandbox with no signup "
        "required, for the public marketing-site playground. Rate-limited "
        "per source IP (BOXKITE_DEMO_RATE_LIMIT_PER_MINUTE). Returns 503 "
        "`demo_at_capacity` if the demo pool's own concurrency ceiling "
        "(BOXKITE_DEMO_MAX_CONCURRENT) is in use. Returns a session-scoped "
        "token that must be presented as the `X-Demo-Token` header on every "
        "subsequent call for this session. 404s entirely if the demo "
        "playground isn't enabled on this deployment."
    ),
)
async def create_demo_sandbox(
    request: Request,
    response: Response,
    body: DemoSandboxCreateRequest | None = None,
    db: AsyncSession = Depends(get_db),
    policy: UsagePolicy = Depends(get_usage_policy),
) -> DemoSandboxCreatedResponse:
    _require_demo_enabled()
    await _enforce_demo_rate_limit(request, response, bucket="demo_playground_ops")

    demo_account = await get_or_create_demo_account(db)

    requested_minutes = body.lifetime_minutes if body is not None else None
    lifetime_minutes = (
        min(requested_minutes, settings.BOXKITE_DEMO_LIFETIME_MINUTES)
        if requested_minutes is not None
        else settings.BOXKITE_DEMO_LIFETIME_MINUTES
    )

    async with _demo_create_lock:
        sessions = SandboxSessionRepository(db)
        active_count = await sessions.count_active_for_account(demo_account.id)
        if active_count >= settings.BOXKITE_DEMO_MAX_CONCURRENT:
            raise ApiError(503, "demo_at_capacity", _AT_CAPACITY_MESSAGE)

        try:
            row, _manager_result = await policy.create_session(
                demo_account,
                label="demo-playground",
                size="small",
                lifetime_minutes=lifetime_minutes,
            )
        except LimitExceededError:
            # UsagePolicy's own per-account/global/monthly-usage caps are
            # scoped for real accounts, not this shared demo pool -- surface
            # any of them identically to our own precheck above rather than
            # leaking a real-account-shaped error code/message over a public,
            # unauthenticated route.
            raise ApiError(503, "demo_at_capacity", _AT_CAPACITY_MESSAGE) from None

    token, expires_at = create_demo_session_token(session_id=row.id, ttl_seconds=lifetime_minutes * 60)
    return DemoSandboxCreatedResponse(session_id=row.id, token=token, expires_at=expires_at)


@router.post(
    "/sandboxes/{session_id}/exec",
    response_model=DemoSandboxExecResponse,
    summary="Run a command in a demo sandbox (unauthenticated, token-scoped)",
    description=(
        "Requires the `X-Demo-Token` header minted by POST /v1/demo/sandboxes "
        "for this exact session_id. Runs with a fixed, non-negotiable "
        "timeout (BOXKITE_DEMO_EXEC_TIMEOUT_SECONDS) -- any caller-supplied "
        "timeout concept does not apply here. stdout/stderr are each "
        "truncated to a fixed cap to bound response size."
    ),
)
async def exec_in_demo_sandbox(
    body: DemoSandboxExecRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    x_demo_token: str | None = Header(default=None, alias="X-Demo-Token"),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> DemoSandboxExecResponse:
    _require_demo_enabled()
    await _enforce_demo_rate_limit(request, response, bucket="demo_playground_exec")
    await _authenticate_demo_session(session_id=session_id, x_demo_token=x_demo_token)

    demo_account = await get_or_create_demo_account(db)
    await _get_active_demo_session_or_404(session_id=session_id, demo_account_id=demo_account.id, db=db)

    try:
        result = await manager.execute(
            session_id=session_id,
            command=body.command,
            timeout=settings.BOXKITE_DEMO_EXEC_TIMEOUT_SECONDS,
            description="demo-playground",
        )
    except Exception as exc:
        raise ApiError(
            502,
            "sandbox_operation_failed",
            "Failed to run this command against the demo sandbox. It may have "
            "become unavailable; try creating a new demo session.",
        ) from exc

    stdout = str(result.get("stdout", ""))
    stderr = str(result.get("stderr", ""))
    truncated = len(stdout) > DEMO_EXEC_OUTPUT_MAX_LENGTH or len(stderr) > DEMO_EXEC_OUTPUT_MAX_LENGTH
    return DemoSandboxExecResponse(
        exit_code=result.get("exit_code", -1),
        stdout=stdout[:DEMO_EXEC_OUTPUT_MAX_LENGTH],
        stderr=stderr[:DEMO_EXEC_OUTPUT_MAX_LENGTH],
        truncated=truncated,
    )


@router.delete(
    "/sandboxes/{session_id}",
    status_code=204,
    summary="Destroy a demo sandbox (unauthenticated, token-scoped, best-effort)",
    description=(
        "Requires the `X-Demo-Token` header for this exact session_id. "
        "Intended to be called via `navigator.sendBeacon` on page unload -- "
        "idempotent: a session that's already destroyed or never existed "
        "still returns 204 rather than 404, so a duplicate/late beacon call "
        "never surfaces as an error the frontend has to handle."
    ),
)
async def destroy_demo_sandbox(
    request: Request,
    response: Response,
    session_id: str = Path(...),
    x_demo_token: str | None = Header(default=None, alias="X-Demo-Token"),
    db: AsyncSession = Depends(get_db),
    policy: UsagePolicy = Depends(get_usage_policy),
) -> Response:
    _require_demo_enabled()
    await _enforce_demo_rate_limit(request, response, bucket="demo_playground_ops")
    await _authenticate_demo_session(session_id=session_id, x_demo_token=x_demo_token)

    demo_account = await get_or_create_demo_account(db)
    row = await SandboxSessionRepository(db).get_for_account(session_id=session_id, account_id=demo_account.id)
    if row is not None and row.destroyed_at is None:
        await policy.destroy_session(row, reason="caller_requested")
    return Response(status_code=204, headers=dict(response.headers))
