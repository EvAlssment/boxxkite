"""Sandbox session lifecycle — authenticated with an API key
(`get_current_account_via_api_key`), never a dashboard JWT. The one
exception is `POST /v1/sandboxes` (create) itself, which additionally
accepts a short-lived, single-use `sandbox_create` token minted from a
dashboard JWT session via `POST /v1/account/sandbox-create-token` (GitHub
issue #221, see `deps.py`'s `get_current_account_via_api_key_or_sandbox_create_token`)
-- every other route below is unaffected and still API-key-only.

Every route here is scoped to `account.id` at the database layer
(`SandboxSessionRepository.get_for_account` / `list_for_account`), not just
filtered after the fact — see repository.py's docstring. A caller can never
observe whether a `session_id` belonging to a different account exists: a
lookup miss and a cross-tenant lookup both produce the same 404.

Actual pod lifecycle work is delegated to `UsagePolicy` (usage_policy.py),
which enforces fair-use limits before ever calling
`SandboxManager.create_session`/`destroy_session`.

The exec/file routes below (`/exec`, `/files`, `/files/view`,
`/files/str-replace`) are the operational counterpart to create/list/delete:
once a caller owns a session, these proxy straight to
`SandboxManager.execute`/`.file_create`/`.view`/`.str_replace` — the exact
same high-level, session_id-keyed methods `src/boxkite/tools/*.py` already
use internally (pod resolution, sidecar auth token lookup, and HTTP retry/
recovery all stay inside SandboxManager; nothing here reaches into its
private `_`-prefixed internals). Ownership is checked with the identical
`get_for_account` scoping as GET/DELETE before any sidecar call is made, so
a foreign session_id 404s the same way here too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
import websockets
from boxkite import resource_config
from boxkite.command_whitelist import validate_command_whitelist
from fastapi import APIRouter, Depends, Path, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as db_module
from ..config import settings
from ..db import get_db
from ..deps import (
    get_current_account_and_key_via_api_key,
    get_current_account_via_api_key,
    get_current_account_via_api_key_or_query,
    get_current_account_via_api_key_or_sandbox_create_token,
    get_manager,
    get_snapshot_storage,
    get_usage_policy,
    _reject_if_scim_deactivated,
)
from ..errors import ApiError
from ..pty_recording import PtyRecordingBuffer, finalize_takeover_recording
from ..models_orm import Account, ApiKey, SandboxSession
from ..rate_limit import enforce_rate_limit
from ..repository import (
    AccountRepository,
    ApiKeyRepository,
    ExecLogEntryRepository,
    PreviewTokenRevocationRepository,
    SandboxImageRepository,
    SandboxSessionRepository,
    SandboxVolumeRepository,
)
from ..schemas import (
    SANDBOX_FILE_CONTENT_MAX_LENGTH,
    SANDBOX_LOG_DEFAULT_LIMIT,
    SANDBOX_LOG_MAX_LIMIT,
    SANDBOX_PREVIEW_MAX_TTL_SECONDS,
    ExecLogEntryOut,
    SandboxConnectInfo,
    SandboxCreatedResponse,
    SandboxCreateRequest,
    SandboxExecRequest,
    SandboxExecResponse,
    SandboxFileCreateRequest,
    SandboxFileCreateResponse,
    SandboxFileViewRequest,
    SandboxFileViewResponse,
    SandboxGlobRequest,
    SandboxGlobResponse,
    SandboxGrepRequest,
    SandboxGrepResponse,
    SandboxHttpRequestRequest,
    SandboxHttpRequestResponse,
    SandboxLogResponse,
    SandboxLsRequest,
    SandboxLsResponse,
    SandboxLspCompletionRequest,
    SandboxLspCompletionResponse,
    SandboxLspOpenRequest,
    SandboxLspStartRequest,
    SandboxLspStartResponse,
    SandboxLspStatusResponse,
    SandboxPreviewRevokeRequest,
    SandboxPreviewRevokeResponse,
    SandboxPreviewUrlRequest,
    SandboxPreviewUrlResponse,
    SandboxProcessInputRequest,
    SandboxProcessInputResponse,
    SandboxProcessListResponse,
    SandboxProcessOutputResponse,
    SandboxProcessStartRequest,
    SandboxProcessStartResponse,
    SandboxProcessStopResponse,
    SandboxSessionOut,
    SandboxStrReplaceRequest,
    SandboxStrReplaceResponse,
    SandboxDesktopTokenResponse,
    SandboxTakeoverTokenRequest,
    SandboxTakeoverTokenResponse,
    UsageSummary,
)
from ..security import (
    can_initiate_takeover,
    create_desktop_token,
    create_preview_token,
    create_takeover_token,
    decode_desktop_token,
    decode_preview_token,
    decode_takeover_token,
)
from ..usage_policy import UsagePolicy
from ..webhooks import enqueue_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sandboxes", tags=["sandboxes"])

# ── Live watch (SSE) ─────────────────────────────────────────────────────
# Polling interval for GET .../watch -- deliberately a simple DB-poll loop,
# not a new pub/sub mechanism, per docs/SANDBOX-OBSERVABILITY-DESIGN.md's
# explicit "don't over-engineer this" guidance.
SANDBOX_WATCH_POLL_INTERVAL_SECONDS = 0.5
SANDBOX_WATCH_BATCH_LIMIT = 100

# GET .../processes/{id}/stream -- a poll-to-SSE bridge turning
# get_process_output's offset polling into a live stdout stream. Same
# "simple poll loop, don't over-engineer" posture as the watch stream above;
# no sidecar changes required.
SANDBOX_PROCESS_STREAM_POLL_INTERVAL_SECONDS = 0.5
# Emit an SSE keep-alive comment after this many consecutive idle polls
# (~15s) so intermediary proxies don't drop a quiet long-running stream.
SANDBOX_PROCESS_STREAM_HEARTBEAT_EVERY = 30

# ── Human takeover (WS proxy) ────────────────────────────────────────────
# How often the /takeover proxy flushes a snapshot of what the human typed
# to ExecLogEntry while a session is open, independent of the mandatory
# start/end log rows -- see docs/SANDBOX-OBSERVABILITY-DESIGN.md section 4
# ("Takeover logging is the load-bearing mitigation" for shipping without
# fine-grained RBAC).
TAKEOVER_SNAPSHOT_INTERVAL_SECONDS = 10
# Same size-cap philosophy as SANDBOX_FILE_CONTENT_MAX_LENGTH -- typed input
# is still caller-controlled-sized data ending up in this database.
TAKEOVER_TYPED_SNAPSHOT_MAX_LENGTH = SANDBOX_FILE_CONTENT_MAX_LENGTH

# In-process single-use guard for takeover tokens (security.py's
# create_takeover_token/decode_takeover_token) -- maps a token's `jti` to
# the epoch time it can be forgotten (its own expiry). A jti seen twice is
# rejected as a replay. Same documented limitation as rate_limit.py's
# in-memory counters: this state is per-process, not shared across
# replicas of a multi-instance deployment, so single-use is only
# guaranteed within one replica until a shared store backs it (tracked
# here rather than silently assumed away). The token's short TTL
# (BOXKITE_TAKEOVER_TOKEN_TTL_SECONDS) bounds the exposure either way.
_takeover_jti_seen: dict[str, float] = {}


def _consume_takeover_jti(jti: str, *, exp: float | int | None) -> bool:
    """Returns True the first time this jti is seen (and records it),
    False on any repeat -- i.e. single-use enforcement. Also opportunistically
    prunes entries past their own expiry so this dict doesn't grow
    unbounded across a long-running process."""
    now = time.time()
    expired = [seen_jti for seen_jti, seen_exp in _takeover_jti_seen.items() if seen_exp <= now]
    for seen_jti in expired:
        del _takeover_jti_seen[seen_jti]
    if jti in _takeover_jti_seen:
        return False
    _takeover_jti_seen[jti] = float(exp) if exp else now + 60
    return True


def reset_takeover_jti_guard_for_tests() -> None:
    """Test-only helper to avoid cross-test bleed of the in-memory replay guard."""
    _takeover_jti_seen.clear()


# Same in-process, per-replica single-use guard as `_takeover_jti_seen`
# above, kept as its own dict rather than shared: desktop tokens
# (security.py's `create_desktop_token`) are a distinct token `type` from
# takeover tokens, so there is no reason for the two token kinds' replay
# state to be entangled even though a jti collision between them is already
# astronomically unlikely (both are independent 16-byte random values).
_desktop_jti_seen: dict[str, float] = {}


def _consume_desktop_jti(jti: str, *, exp: float | int | None) -> bool:
    """Same single-use-enforcement shape as `_consume_takeover_jti` --
    returns True the first time this jti is seen, False on any repeat."""
    now = time.time()
    expired = [seen_jti for seen_jti, seen_exp in _desktop_jti_seen.items() if seen_exp <= now]
    for seen_jti in expired:
        del _desktop_jti_seen[seen_jti]
    if jti in _desktop_jti_seen:
        return False
    _desktop_jti_seen[jti] = float(exp) if exp else now + 60
    return True


def reset_desktop_jti_guard_for_tests() -> None:
    """Test-only helper to avoid cross-test bleed of the in-memory replay guard."""
    _desktop_jti_seen.clear()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _fire_audit_log_webhook_event(db: AsyncSession, *, account_id: str, data: dict) -> None:
    """Best-effort fan-out of an `ExecLogEntry` write to any subscription
    registered for the `audit_log.entry` event type (GitHub issue #125,
    SIEM/audit-log export) -- mirrors `usage_policy.py`'s
    `_fire_webhook_event` wrapper exactly: any exception from
    `webhooks.enqueue_event` is caught and logged here, never allowed to
    fail the exec/file-op call that triggered it. See `_log_exec_entry`'s
    docstring for why this is the single call site for this event, the same
    "one hook covers every route" pattern `UsagePolicy.create_session`/
    `destroy_session` already established for `sandbox.created`/
    `sandbox.destroyed`."""
    try:
        await enqueue_event(db, account_id=account_id, event_type="audit_log.entry", data=data)
    except Exception as exc:
        logger.error(
            "[sandboxes] Failed to enqueue audit_log.entry webhook event for account %s: %s",
            account_id,
            exc,
        )


async def _log_exec_entry(
    db: AsyncSession,
    *,
    session_id: str,
    account_id: str,
    operation: str,
    detail: dict,
    started_at: datetime,
    exit_code: int | None = None,
    output: str | None = None,
    source: str = "agent",
) -> None:
    """Shared audit-log write for every sandbox exec/file route — see
    `docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 3. Called once per route,
    right after the operation's own try/except block succeeds, so a failed
    operation (already translated to a 502 by `_sandbox_operation_error`)
    never produces a misleading "success" audit row.

    `output` is capped at `SANDBOX_FILE_CONTENT_MAX_LENGTH`, the same size
    cap this service already applies to file-op request payloads (schemas.py)
    — one truncation philosophy for anything user-controlled-sized that ends
    up in this database, request or response side.

    Also fans this same write out to the `audit_log.entry` webhook event
    type (GitHub issue #125) -- being the single shared call site every
    exec/file-op route already goes through (exec, file_create, view,
    str_replace, ls, glob, grep, and human-takeover keystrokes alike, via
    `source`), this one hook covers real-time SIEM/audit-log forwarding for
    all of them without a second, parallel bookkeeping path. Reuses the
    exact same subscribe/sign/retry pipeline `sandbox.created`/
    `sandbox.destroyed` already go through -- no new delivery machinery.
    """
    duration_ms = int((_utcnow() - started_at).total_seconds() * 1000)
    output_truncated = output[:SANDBOX_FILE_CONTENT_MAX_LENGTH] if output is not None else None
    entry = await ExecLogEntryRepository(db).create(
        session_id=session_id,
        account_id=account_id,
        source=source,
        operation=operation,
        detail=detail,
        exit_code=exit_code,
        output_truncated=output_truncated,
        started_at=started_at,
        duration_ms=duration_ms,
    )
    await _fire_audit_log_webhook_event(
        db,
        account_id=account_id,
        data={
            "exec_log_entry_id": entry.id,
            "session_id": session_id,
            "source": source,
            "operation": operation,
            "detail": detail,
            "exit_code": exit_code,
            "output_truncated": output_truncated,
            "started_at": started_at.isoformat(),
            "duration_ms": duration_ms,
        },
    )


async def _get_active_session_or_404(
    *, session_id: str, account: Account, db: AsyncSession
) -> SandboxSession:
    """Resolve session_id -> row, scoped to the caller's account, exactly
    like the DELETE route. A session belonging to another account, or one
    that never existed, or one already destroyed, all 404 identically."""
    row = await SandboxSessionRepository(db).get_for_account(session_id=session_id, account_id=account.id)
    if row is None or row.destroyed_at is not None:
        raise ApiError(404, "not_found", "Sandbox session not found")
    return row


async def _get_owned_session_or_404(
    *, session_id: str, account: Account, db: AsyncSession
) -> SandboxSession:
    """Like `_get_active_session_or_404` but permits an already-destroyed
    session -- used by `GET .../takeover-recordings/{entry_id}` (GitHub
    issue #133 replay route), since a takeover recording is durable
    object-storage content that outlives the pod/session it was captured
    from. Being able to replay a session's recording after the sandbox
    itself has since been torn down is the expected case, not an edge
    case -- unlike `/exec` or the other operational routes, there is no
    live pod this route needs to reach. Ownership is still the same
    structural, account-scoped `get_for_account` query every other route
    here uses -- a foreign session_id 404s identically either way."""
    row = await SandboxSessionRepository(db).get_for_account(session_id=session_id, account_id=account.id)
    if row is None:
        raise ApiError(404, "not_found", "Sandbox session not found")
    return row


async def _enforce_sandbox_rate_limit(request: Request, response: Response, account: Account) -> None:
    """Rate-limit exec/file-op routes per account (not per-IP — these routes
    are already API-key-authenticated, so many accounts can legitimately
    share an egress IP)."""
    await enforce_rate_limit(
        request,
        bucket="sandbox_ops",
        subject=str(account.id),
        limit=settings.BOXKITE_SANDBOX_RATE_LIMIT_PER_MINUTE,
        response=response,
    )


async def _enforce_sandbox_lifecycle_rate_limit(request: Request, response: Response, account: Account) -> None:
    """Rate-limit sandbox create/destroy separately from (and much lower
    than) exec/file-ops — these trigger real K8s pod create/delete calls
    against the shared cluster, not just a request to an already-running
    pod."""
    await enforce_rate_limit(
        request,
        bucket="sandbox_lifecycle",
        subject=str(account.id),
        limit=settings.BOXKITE_SANDBOX_LIFECYCLE_RATE_LIMIT_PER_MINUTE,
        response=response,
    )


def _sandbox_operation_error(operation: str, exc: Exception) -> ApiError:
    """Translate a SandboxManager/sidecar failure into this service's error
    envelope. Never leaks the raw exception (which can include internal pod
    names, stack traces, or transport details) to the caller."""
    return ApiError(
        502,
        "sandbox_operation_failed",
        f"Failed to run '{operation}' against the sandbox session. It may have "
        "become unavailable; try again or create a new session.",
    )


def _to_out(row: SandboxSession) -> SandboxSessionOut:
    expires_at = row.created_at + timedelta(minutes=settings.BOXKITE_MAX_SESSION_MINUTES)
    return SandboxSessionOut(
        id=row.id,
        status="destroyed" if row.destroyed_at else "active",
        label=row.label,
        created_at=row.created_at,
        destroyed_at=row.destroyed_at,
        expires_at=expires_at,
        connect=None if row.destroyed_at else SandboxConnectInfo(pod_name=row.pod_name),
    )


async def _resolve_image_ref_or_404(*, image_id: str | None, account: Account, db: AsyncSession) -> str | None:
    """Resolves a caller-supplied `image_id` (docs/DECLARATIVE-BUILDER-DESIGN.md)
    to its pinned `registry_ref`. 404s -- never silently falls back to the
    default image -- if `image_id` isn't owned by the caller's account, or
    isn't `status == "completed"` yet: creating a sandbox against a
    still-building, failed, or rejected image must fail closed. This is
    both a correctness and a security requirement (design doc section 3) --
    a caller must never be able to believe they're running against their
    reviewed custom package set while actually running against the shared
    default."""
    if image_id is None:
        return None
    row = await SandboxImageRepository(db).get_for_account(image_id=image_id, account_id=account.id)
    if row is None or row.deleted_at is not None or row.status != "completed" or not row.registry_ref:
        raise ApiError(404, "not_found", "Sandbox image not found or not ready")
    return row.registry_ref


async def _resolve_volume_mounts_or_404(
    *, volume_mounts: dict[str, str] | None, account: Account, db: AsyncSession
) -> list[dict] | None:
    """Resolves a caller-supplied {volume_id: mount_path} mapping
    (docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum) to
    {"pvc_name": ..., "mount_path": ...} dicts SandboxManager.create_session
    accepts. 404s -- never silently omits a volume -- if any volume_id
    isn't owned by the caller's account or isn't status == "ready" yet,
    same fail-closed pattern as _resolve_image_ref_or_404."""
    if not volume_mounts:
        return None
    volumes_repo = SandboxVolumeRepository(db)
    resolved: list[dict] = []
    for volume_id, mount_path in volume_mounts.items():
        row = await volumes_repo.get_for_account(volume_id=volume_id, account_id=account.id)
        if row is None or row.deleted_at is not None or row.status != "ready" or not row.pvc_name:
            raise ApiError(404, "not_found", f"Volume {volume_id} not found or not ready")
        resolved.append({"pvc_name": row.pvc_name, "mount_path": mount_path})
    return resolved


def _validate_gpu_count_or_422(gpu_count: int | None) -> None:
    """Validated here, before ever reaching UsagePolicy/SandboxManager, so
    an unsupported request 422s cleanly instead of surfacing
    SandboxManager's own ValueError (docs/GPU-SUPPORT-SCOPING.md) as an
    unhandled 500 -- the manager-level check still runs too (defense in
    depth), but this is the one a caller actually sees."""
    if gpu_count is None:
        return
    if not resource_config.gpu_enabled():
        raise ApiError(
            422,
            "gpu_support_disabled",
            "GPU support is an opt-in, experimental configuration (docs/GPU-SUPPORT-SCOPING.md) "
            "that this deployment has not enabled (BOXKITE_GPU_ENABLED).",
        )
    ceiling = resource_config.max_gpu_count_per_session()
    if gpu_count <= 0 or gpu_count > ceiling:
        raise ApiError(
            422,
            "invalid_gpu_count",
            f"gpu_count must be greater than 0 and at most {ceiling}",
        )


async def _create_one_sandbox(
    body: SandboxCreateRequest, account: Account, policy: UsagePolicy, db: AsyncSession
) -> SandboxCreatedResponse:
    image_ref = await _resolve_image_ref_or_404(image_id=body.image_id, account=account, db=db)
    volume_mounts = await _resolve_volume_mounts_or_404(
        volume_mounts=body.volume_mounts, account=account, db=db
    )
    _validate_gpu_count_or_422(body.gpu_count)
    row, _manager_result = await policy.create_session(
        account,
        label=body.label,
        size=body.size,
        storage_gb=body.storage_gb,
        lifetime_minutes=body.lifetime_minutes,
        secret_names=body.secret_names,
        image_ref=image_ref,
        volume_mounts=volume_mounts,
        mcp_connection_names=body.mcp_connection_names,
        gpu_count=body.gpu_count,
    )

    active_count = await SandboxSessionRepository(db).count_active_for_account(account.id)
    hours_used = await policy.monthly_hours_used(account.id)
    out = _to_out(row)
    return SandboxCreatedResponse(
        **out.model_dump(),
        usage=UsageSummary(
            monthly_sandbox_hours_used=round(hours_used, 4),
            monthly_sandbox_hours_limit=settings.BOXKITE_FREE_MONTHLY_SANDBOX_HOURS,
            concurrent_sandboxes=active_count,
            concurrent_sandboxes_limit=settings.BOXKITE_MAX_CONCURRENT_SANDBOXES,
        ),
    )


@router.post(
    "",
    response_model=SandboxCreatedResponse | list[SandboxCreatedResponse],
    status_code=201,
    summary="Create one or more sandbox sessions",
    description=(
        "Creates a new sandbox session via SandboxManager, scoped to the "
        "authenticated account. Enforces, in order: the concurrent-sandbox "
        "cap (BOXKITE_MAX_CONCURRENT_SANDBOXES) and the monthly usage cap "
        "(BOXKITE_FREE_MONTHLY_SANDBOX_HOURS) — a 429 with code "
        "`concurrent_sandbox_limit_reached` or `monthly_usage_limit_reached` "
        "is returned if either is hit, before any pod is created. Every "
        "session is also hard-capped at BOXKITE_MAX_SESSION_MINUTES by a "
        "background reaper, independent of this call. `size` and "
        "`storage_gb` are each capped per-account (BOXKITE_MAX_SANDBOX_SIZE, "
        "BOXKITE_MAX_SANDBOX_STORAGE_GB) — exceeding either returns a 429 "
        "before any pod is created. If `count` is greater than 1, a bare "
        "list of that many created sessions is returned instead of a single "
        "object, matching the shape `GET /v1/sandboxes` already uses for "
        "multiple sessions; each is created and limit-checked one at a time, "
        "so a later item in the batch can still fail the concurrent-sandbox "
        "or monthly usage cap even if earlier items in the same request "
        "succeeded. Accepts either a long-lived API key or a short-lived, "
        "single-use `sandbox_create` token from "
        "POST /v1/account/sandbox-create-token (GitHub issue #221) -- the "
        "one route in this router that isn't API-key-only."
    ),
)
async def create_sandbox(
    body: SandboxCreateRequest,
    request: Request,
    response: Response,
    account: Account = Depends(get_current_account_via_api_key_or_sandbox_create_token),
    policy: UsagePolicy = Depends(get_usage_policy),
    db: AsyncSession = Depends(get_db),
) -> SandboxCreatedResponse | list[SandboxCreatedResponse]:
    await _enforce_sandbox_lifecycle_rate_limit(request, response, account)

    if body.count == 1:
        return await _create_one_sandbox(body, account, policy, db)

    return [await _create_one_sandbox(body, account, policy, db) for _ in range(body.count)]


@router.get(
    "",
    response_model=list[SandboxSessionOut],
    summary="List your sandbox sessions",
    description=(
        "Lists sandbox sessions owned by the authenticated account only. "
        "Never returns another account's sessions, regardless of query "
        "parameters — ownership is enforced at the database query, not by "
        "client-side filtering."
    ),
)
async def list_sandboxes(
    active_only: bool = False,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[SandboxSessionOut]:
    rows = await SandboxSessionRepository(db).list_for_account(account_id=account.id, active_only=active_only)
    return [_to_out(r) for r in rows]


@router.get(
    "/{session_id}",
    response_model=SandboxSessionOut,
    summary="Get a single sandbox session",
    description=(
        "Fetches one sandbox session by id, scoped to the authenticated "
        "account. Unlike the exec/file routes, this resolves destroyed "
        "sessions too -- it's a lookup, not an operational route that "
        "requires a live pod. 404s for a session_id owned by a different "
        "account or one that never existed, identically."
    ),
)
async def get_sandbox(
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> SandboxSessionOut:
    row = await SandboxSessionRepository(db).get_for_account(session_id=session_id, account_id=account.id)
    if row is None:
        raise ApiError(404, "not_found", "Sandbox session not found")
    return _to_out(row)


@router.delete(
    "/{session_id}",
    status_code=204,
    summary="Destroy a sandbox session",
    description=(
        "Tears down a sandbox session via SandboxManager. Returns 404 — "
        "not 403 — for a session_id owned by a different account, so a "
        "caller cannot use this endpoint to probe whether a given "
        "session_id exists at all."
    ),
)
async def delete_sandbox(
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    policy: UsagePolicy = Depends(get_usage_policy),
) -> Response:
    await _enforce_sandbox_lifecycle_rate_limit(request, response, account)
    row = await SandboxSessionRepository(db).get_for_account(session_id=session_id, account_id=account.id)
    if row is None:
        raise ApiError(404, "not_found", "Sandbox session not found")
    if row.destroyed_at is not None:
        raise ApiError(404, "not_found", "Sandbox session not found")
    await policy.destroy_session(row, reason="caller_requested")
    return Response(status_code=204, headers=dict(response.headers))


@router.post(
    "/{session_id}/exec",
    response_model=SandboxExecResponse,
    summary="Run a command in a sandbox session",
    description=(
        "Runs a shell command inside the session's sandbox and returns its "
        "exit code, stdout, and stderr. Proxies to the same sidecar "
        "SandboxManager.execute() already uses for in-process tool calls — "
        "commands run synchronously; there is no streaming of partial "
        "output, and a command that outlives `timeout` seconds is killed "
        "and reported as failed. 404s for a session_id owned by a "
        "different account, identical to GET/DELETE."
    ),
)
async def exec_in_sandbox(
    body: SandboxExecRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxExecResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    if account.custom_allowed_commands:
        allowed, reason = validate_command_whitelist(body.command, account.custom_allowed_commands)
        if not allowed:
            raise ApiError(403, "command_not_allowed", reason)
    started_at = _utcnow()
    try:
        result = await manager.execute(
            session_id=session_id,
            command=body.command,
            timeout=body.timeout,
            description=body.description,
        )
    except Exception as exc:
        raise _sandbox_operation_error("exec", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="exec",
        detail={"command": body.command, "timeout": body.timeout},
        started_at=started_at,
        exit_code=result.get("exit_code"),
        output=f"{result.get('stdout', '')}{result.get('stderr', '')}",
    )
    return SandboxExecResponse(**result)


@router.post(
    "/{session_id}/lsp/start",
    response_model=SandboxLspStartResponse,
    summary="Start a persistent language server in a sandbox session",
    description=(
        "Starts a persistent language server (pyright for \"python\", "
        "typescript-language-server for \"typescript\"/\"javascript\") in the "
        "session's sandbox. Returns an lsp_id handle to pass to the "
        "open/completion/stop routes below. Proxies to "
        "SandboxManager.lsp_start(), which is exec-budgeted the same as "
        "/exec (docs/LSP-SUPPORT-SCOPING.md). 404s for a session_id owned "
        "by a different account, identical to /exec, and 404s "
        "unconditionally when BOXKITE_LSP_ENABLED is unset on this "
        "deployment."
    ),
)
async def start_lsp_in_sandbox(
    body: SandboxLspStartRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxLspStartResponse:
    if not settings.BOXKITE_LSP_ENABLED:
        raise ApiError(404, "not_found", "Language server support is not enabled on this deployment")
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        result = await manager.lsp_start(session_id=session_id, language=body.language)
    except Exception as exc:
        raise _sandbox_operation_error("lsp_start", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="lsp_start",
        detail={"language": body.language},
        started_at=started_at,
    )
    return SandboxLspStartResponse(**result)


@router.post(
    "/{session_id}/lsp/{lsp_id}/open",
    response_model=SandboxLspStatusResponse,
    summary="Open a document on a running language server",
    description=(
        "Opens (or, on a later call for the same path, full-document-"
        "replaces) a document on a running language server started by "
        "/lsp/start. Proxies to SandboxManager.lsp_open() -- not "
        "exec-budgeted (docs/LSP-SUPPORT-SCOPING.md). 404s for a "
        "session_id owned by a different account, and 404s unconditionally "
        "when BOXKITE_LSP_ENABLED is unset on this deployment."
    ),
)
async def open_lsp_document_in_sandbox(
    body: SandboxLspOpenRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    lsp_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxLspStatusResponse:
    if not settings.BOXKITE_LSP_ENABLED:
        raise ApiError(404, "not_found", "Language server support is not enabled on this deployment")
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        # Content deliberately never logged in `detail` -- may contain the
        # caller's full source file, same reasoning /http-request applies
        # to its headers/body.
        result = await manager.lsp_open(
            session_id=session_id, lsp_id=lsp_id, path=body.path, content=body.content
        )
    except Exception as exc:
        raise _sandbox_operation_error("lsp_open", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="lsp_open",
        detail={"path": body.path},
        started_at=started_at,
    )
    return SandboxLspStatusResponse(**result)


@router.post(
    "/{session_id}/lsp/{lsp_id}/completion",
    response_model=SandboxLspCompletionResponse,
    summary="Request completions from a running language server",
    description=(
        "Requests completions at a position from a running language server "
        "started by /lsp/start. `path` must already be open on this handle "
        "(see /lsp/{lsp_id}/open). Proxies to SandboxManager.lsp_completion(), "
        "which is exec-budgeted the same as /exec "
        "(docs/LSP-SUPPORT-SCOPING.md). 404s for a session_id owned by a "
        "different account, and 404s unconditionally when BOXKITE_LSP_ENABLED "
        "is unset on this deployment."
    ),
)
async def lsp_completion_in_sandbox(
    body: SandboxLspCompletionRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    lsp_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxLspCompletionResponse:
    if not settings.BOXKITE_LSP_ENABLED:
        raise ApiError(404, "not_found", "Language server support is not enabled on this deployment")
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        result = await manager.lsp_completion(
            session_id=session_id,
            lsp_id=lsp_id,
            path=body.path,
            line=body.line,
            character=body.character,
        )
    except Exception as exc:
        raise _sandbox_operation_error("lsp_completion", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="lsp_completion",
        detail={"path": body.path, "line": body.line, "character": body.character},
        started_at=started_at,
    )
    return SandboxLspCompletionResponse(**result)


@router.post(
    "/{session_id}/lsp/{lsp_id}/stop",
    response_model=SandboxLspStatusResponse,
    summary="Stop a running language server",
    description=(
        "Gracefully shuts down a running language server started by "
        "/lsp/start. Proxies to SandboxManager.lsp_stop() -- not "
        "exec-budgeted (docs/LSP-SUPPORT-SCOPING.md). 404s for a "
        "session_id owned by a different account, and 404s unconditionally "
        "when BOXKITE_LSP_ENABLED is unset on this deployment."
    ),
)
async def stop_lsp_in_sandbox(
    request: Request,
    response: Response,
    session_id: str = Path(...),
    lsp_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxLspStatusResponse:
    if not settings.BOXKITE_LSP_ENABLED:
        raise ApiError(404, "not_found", "Language server support is not enabled on this deployment")
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        result = await manager.lsp_stop(session_id=session_id, lsp_id=lsp_id)
    except Exception as exc:
        raise _sandbox_operation_error("lsp_stop", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="lsp_stop",
        detail={"lsp_id": lsp_id},
        started_at=started_at,
    )
    return SandboxLspStatusResponse(**result)


@router.post(
    "/{session_id}/http-request",
    response_model=SandboxHttpRequestResponse,
    summary="Secrets-broker HTTP request (docs/SECRETS-DESIGN.md)",
    description=(
        "Proxies to SandboxManager.http_request() -> the session's sidecar's own "
        "POST /http-request route, which substitutes any {{secret:name}} reference "
        "in headers/body for the real value in-process, enforces a DNS-rebinding-safe "
        "destination-host allowlist check, and scrubs secret values from the response "
        "before it is ever returned. This control-plane hop never sees a resolved "
        "secret value -- only the request/response envelope. 404s for a session_id "
        "owned by a different account, identical to /exec."
    ),
)
async def http_request_in_sandbox(
    body: SandboxHttpRequestRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxHttpRequestResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        result = await manager.http_request(
            session_id=session_id,
            method=body.method,
            url=body.url,
            headers=body.headers,
            body=body.body,
            timeout=body.timeout,
        )
    except Exception as exc:
        raise _sandbox_operation_error("http_request", exc) from exc
    # Deliberately never logs headers/body -- may contain {{secret:name}}
    # references, and this hop never sees the resolved plaintext value
    # anyway (substitution happens inside the sidecar). Mirrors
    # sidecar/main.py's own audit posture for this route.
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="http_request",
        detail={"method": body.method, "url": body.url},
        started_at=started_at,
        exit_code=0 if result.get("status_code", 500) < 400 else 1,
    )
    return SandboxHttpRequestResponse(**result)


@router.post(
    "/{session_id}/files",
    response_model=SandboxFileCreateResponse,
    summary="Create or overwrite a file in a sandbox session",
    description=(
        "Creates or overwrites a file in the session's sandbox workspace. "
        "Proxies to SandboxManager.file_create(), the same call "
        "src/boxkite/tools/file_tools.py's file_create tool makes. 404s for "
        "a session_id owned by a different account, identical to GET/DELETE."
    ),
)
async def create_file_in_sandbox(
    body: SandboxFileCreateRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxFileCreateResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        result = await manager.file_create(
            session_id=session_id,
            path=body.path,
            content=body.content,
            description=body.description,
        )
    except Exception as exc:
        raise _sandbox_operation_error("file_create", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="file_create",
        detail={"path": body.path},
        started_at=started_at,
    )
    return SandboxFileCreateResponse(**result)


@router.post(
    "/{session_id}/files/view",
    response_model=SandboxFileViewResponse,
    summary="View a file or directory in a sandbox session",
    description=(
        "Reads a text file's contents (optionally a line range), or lists a "
        "directory's entries. Proxies to SandboxManager.view(). Binary/image "
        "files are not supported here — this mirrors the sidecar's own "
        "/view route, which is text-only. 404s for a session_id owned by a "
        "different account, identical to GET/DELETE."
    ),
)
async def view_file_in_sandbox(
    body: SandboxFileViewRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxFileViewResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        result = await manager.view(
            session_id=session_id,
            path=body.path,
            view_range=body.view_range,
            description=body.description,
        )
    except Exception as exc:
        raise _sandbox_operation_error("view", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="view",
        detail={"path": body.path, "view_range": body.view_range},
        started_at=started_at,
    )
    return SandboxFileViewResponse(**result)


@router.post(
    "/{session_id}/files/str-replace",
    response_model=SandboxStrReplaceResponse,
    summary="Replace a unique string in a sandbox file",
    description=(
        "Replaces old_str with new_str in a file; old_str must appear "
        "exactly once unless replace_all is set. Proxies to "
        "SandboxManager.str_replace(). 404s for a session_id owned by a "
        "different account, identical to GET/DELETE."
    ),
)
async def str_replace_in_sandbox(
    body: SandboxStrReplaceRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxStrReplaceResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        result = await manager.str_replace(
            session_id=session_id,
            path=body.path,
            old_str=body.old_str,
            new_str=body.new_str,
            replace_all=body.replace_all,
            description=body.description,
        )
    except Exception as exc:
        raise _sandbox_operation_error("str_replace", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="str_replace",
        detail={"path": body.path, "replace_all": body.replace_all},
        started_at=started_at,
    )
    return SandboxStrReplaceResponse(**result)


@router.post(
    "/{session_id}/files/ls",
    response_model=SandboxLsResponse,
    summary="List the direct children of a directory in a sandbox session",
    description=(
        "Lists the direct children of a directory in the session's sandbox "
        "workspace. Proxies to SandboxManager.ls(). 404s for a session_id "
        "owned by a different account, identical to GET/DELETE."
    ),
)
async def ls_in_sandbox(
    body: SandboxLsRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxLsResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        entries = await manager.ls(session_id=session_id, path=body.path)
    except Exception as exc:
        raise _sandbox_operation_error("ls", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="ls",
        detail={"path": body.path, "entry_count": len(entries)},
        started_at=started_at,
    )
    return SandboxLsResponse(entries=entries)


@router.post(
    "/{session_id}/files/glob",
    response_model=SandboxGlobResponse,
    summary="Find files by name pattern in a sandbox session",
    description=(
        "Finds files matching a glob pattern (e.g. '**/*.py') under a "
        "directory in the session's sandbox workspace. Proxies to "
        "SandboxManager.glob(). 404s for a session_id owned by a different "
        "account, identical to GET/DELETE."
    ),
)
async def glob_in_sandbox(
    body: SandboxGlobRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxGlobResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        matches = await manager.glob(session_id=session_id, pattern=body.pattern, path=body.path)
    except Exception as exc:
        raise _sandbox_operation_error("glob", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="glob",
        detail={"pattern": body.pattern, "path": body.path, "match_count": len(matches)},
        started_at=started_at,
    )
    return SandboxGlobResponse(matches=matches)


@router.post(
    "/{session_id}/files/grep",
    response_model=SandboxGrepResponse,
    summary="Search file contents by regex in a sandbox session",
    description=(
        "Searches file contents by regex pattern under a directory in the "
        "session's sandbox workspace, optionally restricted to files "
        "matching `glob`. Proxies to SandboxManager.grep(). 404s for a "
        "session_id owned by a different account, identical to GET/DELETE."
    ),
)
async def grep_in_sandbox(
    body: SandboxGrepRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxGrepResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        result = await manager.grep(
            session_id=session_id,
            pattern=body.pattern,
            path=body.path,
            glob=body.glob,
            max_matches=body.max_matches,
        )
    except Exception as exc:
        raise _sandbox_operation_error("grep", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="grep",
        detail={
            "pattern": body.pattern,
            "path": body.path,
            "glob": body.glob,
            "match_count": len(result.get("matches", [])),
        },
        started_at=started_at,
    )
    return SandboxGrepResponse(**result)


# ── Background processes/sessions ────────────────────────────────────────
# Distinct from /exec: /exec is one-shot request/response, bounded by
# `timeout`. These routes track a process across multiple calls -- see
# docs/PROCESS-SESSIONS-DESIGN.md. Plain request/response only (no SSE
# streaming here -- that's an explicit, separate follow-up phase per the
# design doc, not part of this route set). Reuses the same `sandbox_ops`
# rate-limit bucket and ownership check as every exec/file-op route above;
# a dedicated bucket is only warranted once a streaming route exists (the
# design doc's rationale for a separate bucket is specifically about
# long-lived open connections tying up a worker, which a bounded
# request/response poll does not do).


@router.post(
    "/{session_id}/processes",
    response_model=SandboxProcessStartResponse,
    status_code=201,
    summary="Start a background process in a sandbox session",
    description=(
        "Starts a long-running background process (a dev server, a test "
        "watcher, a long build, a REPL) that keeps running after this call "
        "returns. Proxies to SandboxManager.start_process(). Distinct from "
        "POST /exec, which is one-shot and bounded by its own timeout. A "
        "background process is not reachable over the network from any "
        "other call by default -- the same per-exec network isolation "
        "applies here too -- unless `expose_port` is set, in which case "
        "see POST .../preview/{port} (docs/NETWORK-INGRESS-DESIGN.md) for "
        "how to reach it. 404s for a session_id owned by a different "
        "account, identical to GET/DELETE."
    ),
)
async def start_process_in_sandbox(
    body: SandboxProcessStartRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxProcessStartResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    if account.custom_allowed_commands:
        allowed, reason = validate_command_whitelist(body.command, account.custom_allowed_commands)
        if not allowed:
            raise ApiError(403, "command_not_allowed", reason)
    started_at = _utcnow()
    try:
        result = await manager.start_process(
            session_id=session_id,
            expose_port=body.expose_port,
            command=body.command,
            description=body.description,
            max_runtime_seconds=body.max_runtime_seconds,
        )
    except Exception as exc:
        raise _sandbox_operation_error("start_process", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="start_process",
        detail={
            "command": body.command,
            "max_runtime_seconds": body.max_runtime_seconds,
            "process_id": result.get("process_id"),
            "expose_port": body.expose_port,
        },
        started_at=started_at,
    )
    return SandboxProcessStartResponse(**result)


@router.get(
    "/{session_id}/processes",
    response_model=SandboxProcessListResponse,
    summary="List background processes in a sandbox session",
    description=(
        "Lists every background process currently tracked for this "
        "session (running, exited, or stopped). Proxies to "
        "SandboxManager.list_processes(). 404s for a session_id owned by a "
        "different account, identical to GET/DELETE."
    ),
)
async def list_processes_in_sandbox(
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxProcessListResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    try:
        processes = await manager.list_processes(session_id=session_id)
    except Exception as exc:
        raise _sandbox_operation_error("list_processes", exc) from exc
    return SandboxProcessListResponse(processes=processes)


@router.get(
    "/{session_id}/processes/{process_id}/output",
    response_model=SandboxProcessOutputResponse,
    summary="Poll a background process's output in a sandbox session",
    description=(
        "Polls a background process's output since a given byte offset. "
        "Proxies to SandboxManager.get_process_output(). Polling-style, not "
        "streaming -- see docs/PROCESS-SESSIONS-DESIGN.md. 404s for a "
        "session_id owned by a different account or an unknown "
        "process_id, identical to GET/DELETE."
    ),
)
async def get_process_output_in_sandbox(
    request: Request,
    response: Response,
    session_id: str = Path(...),
    process_id: str = Path(...),
    since_offset: int = Query(default=0, ge=0),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxProcessOutputResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    try:
        result = await manager.get_process_output(
            session_id=session_id,
            process_id=process_id,
            since_offset=since_offset,
        )
    except ValueError as exc:
        raise ApiError(404, "not_found", "Process not found") from exc
    except Exception as exc:
        raise _sandbox_operation_error("get_process_output", exc) from exc
    return SandboxProcessOutputResponse(**result)


def _process_output_sse_event(result: dict) -> str:
    # `type` is carried in the data payload too (not just the SSE `event:`
    # line) so header-less parsers -- e.g. the SDK's data-only SSE reader --
    # can still distinguish output from the terminal exit event.
    payload = {
        "type": "output",
        "stdout_chunk": result.get("stdout_chunk", ""),
        "next_offset": result.get("next_offset", 0),
        "truncated": result.get("truncated", False),
    }
    return f"event: output\ndata: {json.dumps(payload)}\n\n"


def _process_exit_sse_event(result: dict) -> str:
    payload = {"type": "exit", "status": result.get("status"), "exit_code": result.get("exit_code")}
    return f"event: exit\ndata: {json.dumps(payload)}\n\n"


async def _process_output_stream(
    request: Request,
    manager,
    *,
    session_id: str,
    process_id: str,
    first_result: dict,
    start_offset: int,
):
    """Turn get_process_output's offset polling into an SSE stream: emit each
    new stdout chunk as an `output` event, then a final `exit` event once the
    process is no longer running. Ends immediately if the client disconnects."""
    result = first_result
    offset = start_offset
    idle = 0
    while True:
        if await request.is_disconnected():
            return
        chunk = result.get("stdout_chunk") or ""
        if chunk:
            offset = result.get("next_offset", offset)
            yield _process_output_sse_event(result)
            idle = 0
        else:
            idle += 1
            if idle % SANDBOX_PROCESS_STREAM_HEARTBEAT_EVERY == 0:
                yield ": keep-alive\n\n"
        if result.get("status") != "running":
            yield _process_exit_sse_event(result)
            return
        await asyncio.sleep(SANDBOX_PROCESS_STREAM_POLL_INTERVAL_SECONDS)
        try:
            result = await manager.get_process_output(
                session_id=session_id, process_id=process_id, since_offset=offset
            )
        except Exception:
            # Session/process went away mid-stream (reaped, deleted); end cleanly.
            return


@router.get(
    "/{session_id}/processes/{process_id}/stream",
    summary="Live SSE stream of a background process's stdout",
    description=(
        "Server-Sent Events stream of a background process's output: each new "
        "chunk arrives as an `output` event, and an `exit` event (with the "
        "exit_code) is sent once the process finishes. A control-plane-side "
        "poll-to-stream bridge over SandboxManager.get_process_output -- the "
        "live alternative to polling GET .../output yourself. Accepts the API "
        "key as a header or, for browser EventSource clients that cannot set "
        "one, an `?api_key=` query parameter (same as GET .../watch). 404s for "
        "a session owned by a different account or an unknown process_id."
    ),
)
async def stream_process_output_in_sandbox(
    request: Request,
    session_id: str = Path(...),
    process_id: str = Path(...),
    since_offset: int = Query(default=0, ge=0),
    account: Account = Depends(get_current_account_via_api_key_or_query),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> StreamingResponse:
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    # Resolve the process once up front so an unknown process_id is a clean 404
    # rather than an error surfaced mid-stream after headers are sent.
    try:
        first_result = await manager.get_process_output(
            session_id=session_id, process_id=process_id, since_offset=since_offset
        )
    except ValueError as exc:
        raise ApiError(404, "not_found", "Process not found") from exc
    except Exception as exc:
        raise _sandbox_operation_error("get_process_output", exc) from exc
    return StreamingResponse(
        _process_output_stream(
            request,
            manager,
            session_id=session_id,
            process_id=process_id,
            first_result=first_result,
            start_offset=since_offset,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{session_id}/processes/{process_id}/input",
    response_model=SandboxProcessInputResponse,
    summary="Write to a background process's stdin in a sandbox session",
    description=(
        "Writes to a tracked background process's stdin pipe (e.g. "
        "answering an interactive prompt). Proxies to "
        "SandboxManager.send_process_input(). 404s for a session_id owned "
        "by a different account or an unknown process_id, identical to "
        "GET/DELETE."
    ),
)
async def send_process_input_in_sandbox(
    body: SandboxProcessInputRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    process_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxProcessInputResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        result = await manager.send_process_input(
            session_id=session_id,
            process_id=process_id,
            data=body.data,
        )
    except ValueError as exc:
        raise ApiError(404, "not_found", "Process not found") from exc
    except Exception as exc:
        raise _sandbox_operation_error("send_process_input", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="send_process_input",
        detail={"process_id": process_id, "bytes_written": result.get("bytes_written")},
        started_at=started_at,
    )
    return SandboxProcessInputResponse(**result)


@router.post(
    "/{session_id}/processes/{process_id}/stop",
    response_model=SandboxProcessStopResponse,
    summary="Stop a background process in a sandbox session",
    description=(
        "Stops a tracked background process: SIGTERM, a short grace "
        "period, then SIGKILL if still alive. Proxies to "
        "SandboxManager.stop_process(). 404s for a session_id owned by a "
        "different account or an unknown process_id, identical to "
        "GET/DELETE."
    ),
)
async def stop_process_in_sandbox(
    request: Request,
    response: Response,
    session_id: str = Path(...),
    process_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> SandboxProcessStopResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    started_at = _utcnow()
    try:
        result = await manager.stop_process(session_id=session_id, process_id=process_id)
    except ValueError as exc:
        raise ApiError(404, "not_found", "Process not found") from exc
    except Exception as exc:
        raise _sandbox_operation_error("stop_process", exc) from exc
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="stop_process",
        detail={"process_id": process_id, "exit_code": result.get("exit_code")},
        started_at=started_at,
    )
    return SandboxProcessStopResponse(**result)


# ── Observability: audit log, live watch, human takeover ────────────────
# See docs/SANDBOX-OBSERVABILITY-DESIGN.md for the full design rationale.
# GET .../log and GET .../watch share the exact same auth
# (get_current_account_via_api_key[_or_query]) and ownership check
# (_get_active_session_or_404) as every route above -- no new auth
# primitive, no new 403-vs-404 behavior to get wrong (design doc section 3).
# WS .../takeover and POST .../takeover-token additionally enforce
# `can_initiate_takeover` (an API key's `role` must be "admin") -- see
# `_authenticate_takeover_or_close` and `mint_sandbox_takeover_token` below,
# and SECURITY.md's "Human takeover" section for why this is no longer the
# no-RBAC-by-design gap it originally shipped as.
# GET .../takeover-recordings/{entry_id} (GitHub issue #133's "replay" half,
# see `get_takeover_recording` below) shares GET .../log's same auth and
# account-scoped ownership check, but deliberately does NOT require the
# session to still be active (`_get_owned_session_or_404`, not
# `_get_active_session_or_404`) -- a recording is durable object-storage
# content outliving the pod it was captured from.


@router.get(
    "/{session_id}/log",
    response_model=SandboxLogResponse,
    summary="Get paginated exec/file-op audit history for a sandbox session",
    description=(
        "Returns a page of ExecLogEntry rows for this session, oldest "
        "first -- one row per exec/file operation (agent-issued or, during "
        "a takeover session, human-issued; see `source`). 404s for a "
        "session_id owned by a different account, identical to GET/DELETE."
    ),
)
async def get_sandbox_log(
    session_id: str = Path(...),
    limit: int = Query(default=SANDBOX_LOG_DEFAULT_LIMIT, ge=1, le=SANDBOX_LOG_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> SandboxLogResponse:
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    repo = ExecLogEntryRepository(db)
    entries = await repo.list_for_session(session_id=session_id, limit=limit, offset=offset)
    total = await repo.count_for_session(session_id=session_id)
    return SandboxLogResponse(
        entries=[ExecLogEntryOut.model_validate(entry) for entry in entries],
        limit=limit,
        offset=offset,
        total=total,
    )


def _log_entry_sse_event(entry) -> str:
    payload = ExecLogEntryOut.model_validate(entry).model_dump(mode="json")
    return f"id: {entry.id}\ndata: {json.dumps(payload)}\n\n"


async def _watch_event_stream(request: Request, *, session_id: str):
    """Polls the DB for rows newer than the last one sent, every
    SANDBOX_WATCH_POLL_INTERVAL_SECONDS -- deliberately a simple polling
    loop rather than a new pub/sub system, per the design doc's explicit
    "don't over-engineer this" guidance. Opens a fresh short-lived DB
    session per poll rather than holding the request-scoped one for the
    stream's entire (potentially very long) lifetime.
    """
    after_id: str | None = None
    session_factory = db_module.get_session_factory()
    while True:
        if await request.is_disconnected():
            return
        async with session_factory() as poll_db:
            entries = await ExecLogEntryRepository(poll_db).list_after(
                session_id=session_id, after_id=after_id, limit=SANDBOX_WATCH_BATCH_LIMIT
            )
            events = [_log_entry_sse_event(entry) for entry in entries]
        if entries:
            after_id = entries[-1].id
        for event in events:
            yield event
        await asyncio.sleep(SANDBOX_WATCH_POLL_INTERVAL_SECONDS)


@router.get(
    "/{session_id}/watch",
    summary="Live SSE stream of new exec/file-op log entries for a sandbox session",
    description=(
        "Server-Sent Events stream that pushes each new ExecLogEntry row "
        "for this session as it's written, polling the database every "
        "~500ms. This is a deliberately cheaper version of 'live watch' "
        "than true mid-command stdout streaming -- it shows one "
        "exec/file-op at a time as each completes, not a live terminal "
        "(that's `WS .../takeover`). 404s for a session_id owned by a "
        "different account, identical to GET/DELETE."
    ),
)
async def watch_sandbox(
    request: Request,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key_or_query),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)
    return StreamingResponse(
        _watch_event_stream(request, session_id=session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _log_takeover_entry(
    *,
    session_id: str,
    account_id: str,
    operation: str,
    detail: dict,
    started_at: datetime,
    output: str | None = None,
) -> None:
    """Fresh short-lived DB session per write, same reasoning as
    `_watch_event_stream` -- a takeover WS connection can stay open far
    longer than a single request-scoped session should reasonably be held.
    Delegates to the same `_log_exec_entry` helper every other route uses,
    with `source="human_takeover"` -- this is the non-negotiable logging
    docs/SANDBOX-OBSERVABILITY-DESIGN.md section 2/4 requires, not a
    parallel/ad-hoc write path.
    """
    session_factory = db_module.get_session_factory()
    async with session_factory() as db:
        await _log_exec_entry(
            db,
            session_id=session_id,
            account_id=account_id,
            operation=operation,
            detail=detail,
            started_at=started_at,
            output=output,
            source="human_takeover",
        )


@dataclass(frozen=True)
class TakeoverApiKeyIdentity:
    """Which API key authenticated one takeover WS connection -- GitHub
    issue #132 design doc §5/§9's audit-identity gap: `ExecLogEntry`
    previously only recorded `account_id`, so two different admin-role API
    keys under the same account produced indistinguishable
    `takeover_start`/`takeover_input`/`takeover_end` rows. Threaded through
    `_authenticate_takeover_or_close` -> `takeover_sandbox` -> every
    `_log_takeover_entry` call's `detail`, for both credential paths (see
    that function's docstring). `api_key_id` is populated whenever a
    credential resolved successfully; `api_key_name` is best-effort (for
    the `?token=` path, the minting key must still exist and belong to the
    token's own account at redemption time -- see
    `_resolve_account_via_takeover_token`)."""

    api_key_id: str | None
    api_key_name: str | None


def _identity_detail(identity: TakeoverApiKeyIdentity) -> dict:
    return {"api_key_id": identity.api_key_id, "api_key_name": identity.api_key_name}


class _SharedTakeoverRecording:
    """A `PtyRecordingBuffer` plus how many currently-attached WS
    connections are mirroring into it. See `_acquire_takeover_recording`/
    `_release_takeover_recording`."""

    __slots__ = ("buffer", "ref_count")

    def __init__(self, buffer: PtyRecordingBuffer) -> None:
        self.buffer = buffer
        self.ref_count = 0


# session_id -> shared recording buffer + attach count (GitHub issue #132
# design doc §6/§9: tmux already lets N concurrent WS connections attach to
# the same underlying takeover session -- verified empirically in that
# document, not hypothetical -- and each connection independently
# instantiating its own `PtyRecordingBuffer` produced N redundant,
# overlapping recordings (each with the full shared output track but only
# its own connection's input track) and N `takeover_end` rows each claiming
# its own `recording_storage_key`, none of them a complete picture. Scoping
# ownership to `session_id` instead of to each connection fixes this: the
# first connection to attach creates the buffer, later concurrent
# connections mirror into the same one, and only the last to disconnect
# finalizes/uploads it. In-process only, same disclosed "not shared across
# control-plane replicas" limitation as `_takeover_jti_seen` above -- a
# multi-replica deployment needs a shared coordination store to fix this
# properly, explicitly out of scope for this fix (see the design doc's own
# call-out in section 9, item 2).
_takeover_recordings: dict[str, _SharedTakeoverRecording] = {}


def _acquire_takeover_recording(session_id: str) -> PtyRecordingBuffer:
    """Get-or-create the one shared `PtyRecordingBuffer` for `session_id`
    and register one more attached connection against it. There is no
    `await` between the dict read and the mutation below, so this is
    atomic under asyncio's single-threaded cooperative scheduling without
    needing an explicit lock (nothing here can yield control mid-operation)."""
    shared = _takeover_recordings.get(session_id)
    if shared is None:
        shared = _SharedTakeoverRecording(PtyRecordingBuffer())
        _takeover_recordings[session_id] = shared
    shared.ref_count += 1
    return shared.buffer


def _release_takeover_recording(session_id: str) -> bool:
    """Detach one connection from `session_id`'s shared recording. Returns
    True exactly when this was the last attached connection -- the caller
    is then responsible for calling `finalize_takeover_recording` on the
    buffer it already holds. Every other, non-last connection's release
    call returns False and must not finalize/upload anything, since the
    buffer is still being mirrored into by at least one other live
    connection."""
    shared = _takeover_recordings.get(session_id)
    if shared is None:
        # Shouldn't happen -- acquire always precedes release for the same
        # session_id -- but degrade to "finalize it, there's nothing else
        # to coordinate with" rather than raising out of a WS teardown path.
        return True
    shared.ref_count -= 1
    if shared.ref_count <= 0:
        del _takeover_recordings[session_id]
        return True
    return False


def reset_takeover_recordings_registry_for_tests() -> None:
    """Test-only helper to avoid cross-test bleed of the in-memory shared-
    recording registry, mirroring `reset_takeover_jti_guard_for_tests`."""
    _takeover_recordings.clear()


async def _resolve_account_via_takeover_token(
    token: str, *, session_id: str, db: AsyncSession
) -> tuple[Account, bool, TakeoverApiKeyIdentity]:
    """Redeem a short-lived, single-use takeover token (security.py's
    `create_takeover_token`) minted by `POST .../takeover-token` -- the
    replacement for putting the long-lived API key on the WS URL as
    `?api_key=...`. Raises `ApiError(401, ...)` on any failure: expired/
    malformed/wrong-type token, wrong session_id binding, an already-used
    `jti`, or an account that no longer exists -- one failure mode for the
    caller to handle, same discipline as every other credential path here.

    Returns `(account, read_only, identity)` -- `read_only` is the token's
    own `read_only` claim (GitHub issue #131), defaulting to `False` for a
    token minted before that claim existed. `identity` (GitHub issue #132
    design doc §5/§9) resolves the token's additive `api_key_id` claim back
    to the minting `ApiKey` row's current `name`, scoped to this same
    `account_id` so a forged/foreign key id simply resolves to no name
    rather than leaking another account's key metadata; `api_key_name` is
    `None` if the claim is absent (a token minted before it existed) or the
    key has since been revoked/deleted."""
    try:
        payload = decode_takeover_token(token)
    except jwt.PyJWTError as exc:
        raise ApiError(401, "invalid_takeover_token", "Takeover token is invalid or has expired") from exc
    if payload.get("session_id") != session_id:
        raise ApiError(401, "invalid_takeover_token", "Takeover token is not bound to this session")
    jti = payload.get("jti")
    if not jti or not _consume_takeover_jti(jti, exp=payload.get("exp")):
        raise ApiError(401, "invalid_takeover_token", "Takeover token has already been used")
    account = await AccountRepository(db).get_by_id(payload.get("account_id", ""))
    if account is None:
        raise ApiError(401, "invalid_takeover_token", "Account for this takeover token no longer exists")
    # This token is minted from an already-authenticated (and therefore
    # already deactivation-checked) API key, but its short TTL
    # (BOXKITE_TAKEOVER_TOKEN_TTL_SECONDS, default 30s) is still a real
    # window: an account deactivated between mint and redemption must not
    # be able to complete the takeover WS handshake on the strength of a
    # token minted moments before -- same already-issued-credential
    # discipline deps.py's _reject_if_scim_deactivated enforces everywhere
    # else.
    _reject_if_scim_deactivated(account)
    api_key_id = payload.get("api_key_id")
    api_key_name = None
    if api_key_id:
        key_row = await ApiKeyRepository(db).get_by_id_for_account(key_id=api_key_id, account_id=account.id)
        if key_row is not None:
            api_key_name = key_row.name
    identity = TakeoverApiKeyIdentity(api_key_id=api_key_id, api_key_name=api_key_name)
    return account, bool(payload.get("read_only", False)), identity


async def _authenticate_takeover_or_close(
    websocket: WebSocket, *, session_id: str
) -> tuple[Account, SandboxSession, bool, TakeoverApiKeyIdentity] | None:
    """Validates auth + RBAC + session ownership BEFORE accept() -- mirrors
    the sidecar's own `_check_pty_auth`. An unauthenticated, unauthorized, or
    cross-tenant upgrade must be rejected before any resource (here: the
    proxied sidecar PTY connection) is allocated, never after (see
    docs/SANDBOX-OBSERVABILITY-DESIGN.md section 4). Returns None (having
    already closed the socket) on any failure.

    Two distinct credential paths -- deliberately NOT the same `?api_key=`
    query-parameter path `/watch` still uses, per SECURITY.md's "Human
    takeover" section:
    - `Authorization: Bearer <api_key>` header -- for non-browser clients
      (the Python SDK, curl, websocat) that CAN set a custom header on the
      WS upgrade request. Requires the resolved API key's `role` to permit
      takeover (`can_initiate_takeover`) -- the fine-grained RBAC check.
      Always full-control (`read_only=False`) -- there is no read-only
      concept for a direct API-key credential, only for a minted token.
      The resolved `ApiKey` row's own `id`/`name` become the returned
      `TakeoverApiKeyIdentity` directly (GitHub issue #132 design doc §5).
    - `?token=<takeover_token>` query parameter -- for browser clients (the
      dashboard, the JS SDK) that cannot set a custom header at all. The
      token is minted server-side by `POST .../takeover-token` immediately
      before connecting (RBAC is checked at mint time, by that route), is
      bound to this exact session_id, and is single-use -- replacing the
      previous practice of putting the long-lived API key itself in this
      query string. Its own `read_only` claim (GitHub issue #131) is
      surfaced in the returned tuple's third element -- the caller
      (`takeover_sandbox`) is responsible for actually refusing to forward
      client->PTY input when it's `True`; this function only authenticates
      and reports it. Its own `api_key_id` claim is resolved to an identity
      the same way (`_resolve_account_via_takeover_token`).
    A request with neither, or an unrecognized/expired/replayed one, is
    rejected with 4401; an authenticated-but-insufficiently-privileged
    Authorization-header caller is rejected with 4403.

    Returns `(account, session, read_only, identity)` on success.
    """
    session_factory = db_module.get_session_factory()
    async with session_factory() as db:
        authorization = websocket.headers.get("authorization")
        token_param = websocket.query_params.get("token")
        read_only = False
        identity = TakeoverApiKeyIdentity(api_key_id=None, api_key_name=None)
        try:
            if authorization:
                account, key_row = await get_current_account_and_key_via_api_key(
                    authorization=authorization, db=db
                )
                if not can_initiate_takeover(key_row.role):
                    await websocket.close(
                        code=4403,
                        reason="This API key's role does not permit initiating a takeover session",
                    )
                    return None
                identity = TakeoverApiKeyIdentity(api_key_id=key_row.id, api_key_name=key_row.name)
            elif token_param:
                account, read_only, identity = await _resolve_account_via_takeover_token(
                    token_param, session_id=session_id, db=db
                )
            else:
                raise ApiError(401, "missing_credentials", "Missing Authorization header or token query parameter")
        except ApiError as exc:
            await websocket.close(code=4401, reason=exc.message[:120])
            return None
        row = await SandboxSessionRepository(db).get_for_account(session_id=session_id, account_id=account.id)
        if row is None or row.destroyed_at is not None:
            await websocket.close(code=4404, reason="Sandbox session not found")
            return None
        return account, row, read_only, identity


async def _relay_client_to_sidecar(
    websocket: WebSocket,
    sidecar_ws,
    typed_buffer: bytearray,
    *,
    read_only: bool = False,
    recording: PtyRecordingBuffer | None = None,
) -> None:
    """Relays the human's keystrokes to the sidecar PTY, and mirrors every
    byte into `typed_buffer` so the periodic snapshot flush (and the final
    end-of-session flush) can log it.

    When `read_only` is True (GitHub issue #131 -- a takeover token minted
    with `read_only=True`), the loop keeps receiving from the client WS (so
    it still detects a normal disconnect and `asyncio.wait`'s
    FIRST_COMPLETED in `takeover_sandbox` still works the same way), but
    every received byte is dropped here -- never forwarded to
    `sidecar_ws.send`, never mirrored into `typed_buffer` -- since nothing
    was actually typed into the PTY for a read-only observer.

    `recording` (GitHub issue #133, optional -- only passed when
    `BOXKITE_TAKEOVER_RECORDING_ENABLED`) mirrors the same bytes `typed_buffer`
    gets, as an "i" (input) asciicast event, for the full-duplex session
    recording -- same read_only exemption as `typed_buffer` above, since a
    read-only observer never actually sends input to the PTY."""
    while True:
        try:
            message = await websocket.receive()
        except WebSocketDisconnect:
            return
        if message.get("type") == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is None:
            text = message.get("text")
            data = text.encode("utf-8") if text is not None else None
        if not data:
            continue
        if read_only:
            continue
        typed_buffer.extend(data)
        if recording is not None:
            recording.record("i", data)
        await sidecar_ws.send(data)


async def _relay_sidecar_to_client(
    websocket: WebSocket, sidecar_ws, *, recording: PtyRecordingBuffer | None = None
) -> None:
    """Relays PTY output from the sidecar back to the human's WS.

    `recording` (GitHub issue #133, optional) mirrors every payload as an
    "o" (output) asciicast event for the full-duplex session recording,
    before it's forwarded to the client -- recorded even if the subsequent
    `send_bytes` fails/disconnects, since the sidecar did produce it."""
    async for data in sidecar_ws:
        payload = data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8")
        if recording is not None:
            recording.record("o", payload)
        try:
            await websocket.send_bytes(payload)
        except (WebSocketDisconnect, RuntimeError):
            return


def _build_takeover_end_detail(
    *, bytes_typed: int, recording_result: dict | None, identity: TakeoverApiKeyIdentity
) -> dict:
    """Builds the `takeover_end` audit row's `detail` dict -- pulled out as
    its own pure function so the recording-pointer-folding behavior (GitHub
    issue #133) and the API-key-identity fields (GitHub issue #132 design
    doc §5/§9) are unit-testable without a live WebSocket/sidecar
    connection, matching this route's existing "helpers are unit tested,
    the full WS handler mostly isn't" coverage split (see the module
    comment above the WS /takeover tests in
    test_sandbox_log_watch_takeover.py)."""
    detail: dict = {"bytes_typed": bytes_typed, **_identity_detail(identity)}
    if recording_result is not None:
        detail["recording_storage_key"] = recording_result["storage_key"]
        detail["recording_bytes"] = recording_result["bytes"]
        detail["recording_truncated"] = recording_result["truncated"]
    return detail


async def _flush_typed_snapshot(
    *,
    session_id: str,
    account_id: str,
    typed_buffer: bytearray,
    flush_state: dict,
    identity: TakeoverApiKeyIdentity,
) -> None:
    if len(typed_buffer) <= flush_state["flushed_len"]:
        return
    chunk = bytes(typed_buffer[flush_state["flushed_len"] :])
    flush_state["flushed_len"] = len(typed_buffer)
    await _log_takeover_entry(
        session_id=session_id,
        account_id=account_id,
        operation="takeover_input",
        detail=_identity_detail(identity),
        started_at=_utcnow(),
        output=chunk.decode("utf-8", errors="replace")[:TAKEOVER_TYPED_SNAPSHOT_MAX_LENGTH],
    )


async def _periodic_typed_snapshot_flush(
    *,
    session_id: str,
    account_id: str,
    typed_buffer: bytearray,
    flush_state: dict,
    identity: TakeoverApiKeyIdentity,
) -> None:
    """Non-negotiable per docs/SANDBOX-OBSERVABILITY-DESIGN.md section 4:
    logs what the human typed during a takeover session periodically, not
    only at session end, so a session that crashes or is killed mid-way
    still leaves an activity trail."""
    while True:
        await asyncio.sleep(TAKEOVER_SNAPSHOT_INTERVAL_SECONDS)
        await _flush_typed_snapshot(
            session_id=session_id,
            account_id=account_id,
            typed_buffer=typed_buffer,
            flush_state=flush_state,
            identity=identity,
        )


@router.post(
    "/{session_id}/takeover-token",
    response_model=SandboxTakeoverTokenResponse,
    summary="Mint a short-lived, single-use token for WS /takeover",
    description=(
        "Mints a short-lived (default 30s, BOXKITE_TAKEOVER_TOKEN_TTL_SECONDS), "
        "single-use token scoped to exactly this (account, session) pair, "
        "for browser clients (the dashboard, the JS SDK) that cannot set a "
        "custom Authorization header on a WebSocket upgrade request. Pass "
        "the returned `token` as `?token=` on the `WS .../takeover` URL "
        "immediately after minting it -- it is consumed on first use and "
        "expires quickly even if never redeemed. Replaces the previous "
        "practice of putting the long-lived API key itself on that URL. "
        "Requires an 'admin'-role API key (see `POST /v1/api-keys`' `role` "
        "field) -- a 'member'-role key gets 403 `takeover_not_permitted`. "
        "404s for a session_id owned by a different account, identical to "
        "GET/DELETE. Optionally accepts a JSON body with `read_only: true` "
        "(GitHub issue #131) to mint an observer-only token: `WS "
        ".../takeover` will still stream PTY output to it, but will refuse "
        "to forward any input typed by that connection."
    ),
)
async def mint_sandbox_takeover_token(
    request: Request,
    response: Response,
    session_id: str = Path(...),
    payload: SandboxTakeoverTokenRequest | None = None,
    account_and_key: tuple[Account, ApiKey] = Depends(get_current_account_and_key_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> SandboxTakeoverTokenResponse:
    account, key_row = account_and_key
    await _enforce_sandbox_rate_limit(request, response, account)
    if not can_initiate_takeover(key_row.role):
        raise ApiError(
            403,
            "takeover_not_permitted",
            "This API key's role does not permit initiating a takeover session",
        )
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)

    read_only = payload.read_only if payload is not None else False
    token, expires_at = create_takeover_token(
        account_id=account.id,
        session_id=session_id,
        ttl_seconds=settings.BOXKITE_TAKEOVER_TOKEN_TTL_SECONDS,
        read_only=read_only,
        api_key_id=key_row.id,
    )
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="takeover_token_mint",
        detail={"read_only": read_only, "api_key_id": key_row.id, "api_key_name": key_row.name},
        started_at=_utcnow(),
    )
    return SandboxTakeoverTokenResponse(token=token, expires_at=expires_at, read_only=read_only)


@router.websocket("/{session_id}/takeover")
async def takeover_sandbox(
    websocket: WebSocket,
    session_id: str = Path(...),
    manager=Depends(get_manager),
    storage=Depends(get_snapshot_storage),
) -> None:
    """Interactive human takeover of a sandbox session's shell, proxied to
    the sidecar's `WS /pty` (see sidecar/main.py's `pty_takeover`).

    Auth, RBAC (only 'admin'-role API keys, or a takeover token minted by
    one), and session ownership are all validated BEFORE
    `websocket.accept()` -- see `_authenticate_takeover_or_close`. Every
    keystroke sent by the human is still mirrored into ExecLogEntry
    (`source="human_takeover"`): a `takeover_start` row when the session
    begins, periodic `takeover_input` snapshots of what was typed while
    it's open, and a final `takeover_end` row (with any not-yet-flushed
    typed content) when it closes -- this predates the RBAC check above and
    remains in place as defense-in-depth, not a substitute for it; it is
    not optional and must not be skipped even on an abnormal disconnect.

    If the connection authenticated via a `read_only` takeover token
    (GitHub issue #131), server->client PTY output still streams normally,
    but `_relay_client_to_sidecar` drops every client->PTY byte instead of
    forwarding it -- see that function's docstring.

    GitHub issue #133: when `BOXKITE_TAKEOVER_RECORDING_ENABLED`, a
    full-duplex, timestamped asciicast-v2 recording of the whole session
    (not just typed input) is also built up in memory and uploaded to
    object storage (`pty_recording.py`) once the session ends, with a
    pointer to it (`recording_storage_key`) folded into the `takeover_end`
    row's `detail` -- a strictly additional artifact alongside the
    `exec_log_entries` trail above, never a replacement for it. GitHub
    issue #132's design doc §6 found this recording was originally scoped
    per WS *connection* rather than per *session* -- since tmux already
    lets multiple concurrent connections attach to the same underlying
    session (#130/#144), that produced N redundant, overlapping recordings
    for N concurrently-attached connections. `_acquire_takeover_recording`/
    `_release_takeover_recording` fix this: the buffer is shared by
    `session_id`, and only the last connection to disconnect finalizes and
    uploads it, so exactly one recording and one `recording_storage_key`
    result regardless of how many connections attached. Every connection
    still writes its own `takeover_start`/`takeover_end` rows, tagged with
    its own authenticating API key's identity (GitHub issue #132 design doc
    §5/§9) -- that per-connection attribution is unaffected by the
    recording-ownership fix above; only the recording pointer itself is
    deduplicated.
    """
    auth_result = await _authenticate_takeover_or_close(websocket, session_id=session_id)
    if auth_result is None:
        return
    account, _row, read_only, identity = auth_result

    try:
        target = await manager.get_sidecar_pty_target(session_id)
    except (ValueError, RuntimeError) as exc:
        logger.warning("[takeover] Failed to resolve sidecar PTY target for %s: %s", session_id, exc)
        await websocket.close(code=1011, reason="Sandbox session unavailable")
        return

    await websocket.accept()

    started_at = _utcnow()
    await _log_takeover_entry(
        session_id=session_id,
        account_id=account.id,
        operation="takeover_start",
        detail=_identity_detail(identity),
        started_at=started_at,
    )

    typed_buffer = bytearray()
    flush_state = {"flushed_len": 0}
    recording = _acquire_takeover_recording(session_id) if settings.BOXKITE_TAKEOVER_RECORDING_ENABLED else None
    try:
        async with websockets.connect(
            target["ws_url"],
            additional_headers={target["auth_header"]: target["auth_token"]},
            open_timeout=10,
        ) as sidecar_ws:
            snapshot_task = asyncio.ensure_future(
                _periodic_typed_snapshot_flush(
                    session_id=session_id,
                    account_id=account.id,
                    typed_buffer=typed_buffer,
                    flush_state=flush_state,
                    identity=identity,
                )
            )
            to_sidecar_task = asyncio.ensure_future(
                _relay_client_to_sidecar(
                    websocket, sidecar_ws, typed_buffer, read_only=read_only, recording=recording
                )
            )
            to_client_task = asyncio.ensure_future(
                _relay_sidecar_to_client(websocket, sidecar_ws, recording=recording)
            )
            try:
                await asyncio.wait({to_sidecar_task, to_client_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                snapshot_task.cancel()
                to_sidecar_task.cancel()
                to_client_task.cancel()
                await asyncio.gather(snapshot_task, to_sidecar_task, to_client_task, return_exceptions=True)
    except Exception as exc:
        logger.warning("[takeover] Sidecar proxy connection failed for %s: %s", session_id, exc)
    finally:
        try:
            await _flush_typed_snapshot(
                session_id=session_id,
                account_id=account.id,
                typed_buffer=typed_buffer,
                flush_state=flush_state,
                identity=identity,
            )
        except Exception as exc:
            # Must never crash this teardown path -- same "log, don't
            # propagate" precedent as `_fire_audit_log_webhook_event` above.
            # `_log_takeover_entry` -> `_log_exec_entry` does a raw DB write
            # with no guard of its own, and this call sits before the
            # recording release/finalize below: letting an exception here
            # escape the `finally` block would skip that release and leak
            # `session_id`'s entry in `_takeover_recordings` for the life of
            # the process (GitHub issue #133 adversarial review).
            logger.error(
                "[takeover] Failed to flush final typed-input snapshot for %s: %s", session_id, exc
            )
        recording_result = None
        if recording is not None:
            is_last_to_leave = _release_takeover_recording(session_id)
            if is_last_to_leave:
                recording_result = await finalize_takeover_recording(
                    recording, storage=storage, account_id=account.id, session_id=session_id
                )
        end_detail = _build_takeover_end_detail(
            bytes_typed=len(typed_buffer), recording_result=recording_result, identity=identity
        )
        await _log_takeover_entry(
            session_id=session_id,
            account_id=account.id,
            operation="takeover_end",
            detail=end_detail,
            started_at=started_at,
        )
        try:
            await websocket.close()
        except RuntimeError:
            pass


async def _log_desktop_entry(
    *,
    session_id: str,
    account_id: str,
    operation: str,
    detail: dict,
    started_at: datetime,
) -> None:
    """Fresh short-lived DB session per write, same reasoning as
    `_log_takeover_entry` -- a `WS .../desktop` connection can stay open far
    longer than a single request-scoped session should reasonably be held.
    `source="human_desktop_takeover"` keeps GUI-takeover audit rows
    distinguishable from PTY-takeover ones (`source="human_takeover"`) in
    `GET .../log`, rather than conflating two different session kinds under
    one label.

    Unlike `_log_takeover_entry`, there is no per-keystroke `output`
    equivalent here -- see `mint_sandbox_desktop_token`'s and
    `desktop_sandbox`'s docstrings for why: VNC's RFB protocol multiplexes
    framebuffer updates and input events in one binary stream that isn't
    meaningfully loggable as "what was typed" without actually parsing RFB,
    which is out of scope for this first pass. Only session start/end +
    identity + duration are audited for v1."""
    session_factory = db_module.get_session_factory()
    async with session_factory() as db:
        await _log_exec_entry(
            db,
            session_id=session_id,
            account_id=account_id,
            operation=operation,
            detail=detail,
            started_at=started_at,
            source="human_desktop_takeover",
        )


async def _resolve_account_via_desktop_token(
    token: str, *, session_id: str, db: AsyncSession
) -> tuple[Account, TakeoverApiKeyIdentity]:
    """Redeem a short-lived, single-use desktop token (security.py's
    `create_desktop_token`) -- the `WS .../desktop` counterpart to
    `_resolve_account_via_takeover_token`. Raises `ApiError(401, ...)` on
    any failure: expired/malformed/wrong-type token, wrong session_id
    binding, an already-used `jti`, or an account that no longer exists."""
    try:
        payload = decode_desktop_token(token)
    except jwt.PyJWTError as exc:
        raise ApiError(401, "invalid_desktop_token", "Desktop token is invalid or has expired") from exc
    if payload.get("session_id") != session_id:
        raise ApiError(401, "invalid_desktop_token", "Desktop token is not bound to this session")
    jti = payload.get("jti")
    if not jti or not _consume_desktop_jti(jti, exp=payload.get("exp")):
        raise ApiError(401, "invalid_desktop_token", "Desktop token has already been used")
    account = await AccountRepository(db).get_by_id(payload.get("account_id", ""))
    if account is None:
        raise ApiError(401, "invalid_desktop_token", "Account for this desktop token no longer exists")
    # Same already-issued-credential discipline as
    # `_resolve_account_via_takeover_token` -- an account deactivated
    # between mint and redemption must not complete the WS handshake on the
    # strength of a token minted moments before.
    _reject_if_scim_deactivated(account)
    api_key_id = payload.get("api_key_id")
    api_key_name = None
    if api_key_id:
        key_row = await ApiKeyRepository(db).get_by_id_for_account(key_id=api_key_id, account_id=account.id)
        if key_row is not None:
            api_key_name = key_row.name
    identity = TakeoverApiKeyIdentity(api_key_id=api_key_id, api_key_name=api_key_name)
    return account, identity


async def _authenticate_desktop_or_close(
    websocket: WebSocket, *, session_id: str
) -> tuple[Account, SandboxSession, TakeoverApiKeyIdentity] | None:
    """Validates auth + RBAC + session ownership BEFORE accept() -- the
    `WS .../desktop` counterpart to `_authenticate_takeover_or_close`. Same
    two credential paths (`Authorization: Bearer <api_key>` header, or
    `?token=<desktop_token>` for browser clients), same
    `can_initiate_takeover` RBAC gate reused as-is (GUI takeover being *at
    least* as gated as shell takeover is the safe default here, never
    looser -- a dedicated `can_initiate_desktop` permission is reasonable
    future work once there's a real usage pattern to design against, not
    invented speculatively for a first pass). Returns None (having already
    closed the socket) on any failure.
    """
    session_factory = db_module.get_session_factory()
    async with session_factory() as db:
        authorization = websocket.headers.get("authorization")
        token_param = websocket.query_params.get("token")
        identity = TakeoverApiKeyIdentity(api_key_id=None, api_key_name=None)
        try:
            if authorization:
                account, key_row = await get_current_account_and_key_via_api_key(
                    authorization=authorization, db=db
                )
                if not can_initiate_takeover(key_row.role):
                    await websocket.close(
                        code=4403,
                        reason="This API key's role does not permit initiating a desktop session",
                    )
                    return None
                identity = TakeoverApiKeyIdentity(api_key_id=key_row.id, api_key_name=key_row.name)
            elif token_param:
                account, identity = await _resolve_account_via_desktop_token(
                    token_param, session_id=session_id, db=db
                )
            else:
                raise ApiError(401, "missing_credentials", "Missing Authorization header or token query parameter")
        except ApiError as exc:
            await websocket.close(code=4401, reason=exc.message[:120])
            return None
        row = await SandboxSessionRepository(db).get_for_account(session_id=session_id, account_id=account.id)
        if row is None or row.destroyed_at is not None:
            await websocket.close(code=4404, reason="Sandbox session not found")
            return None
        return account, row, identity


@router.post(
    "/{session_id}/desktop-token",
    response_model=SandboxDesktopTokenResponse,
    summary="Mint a short-lived, single-use token for WS /desktop",
    description=(
        "Mints a short-lived (default 30s, BOXKITE_DESKTOP_TOKEN_TTL_SECONDS), "
        "single-use token scoped to exactly this (account, session) pair, "
        "for browser clients (the dashboard, the JS SDK) that cannot set a "
        "custom Authorization header on a WebSocket upgrade request. Pass "
        "the returned `token` as `?token=` on the `WS .../desktop` URL "
        "immediately after minting it -- it is consumed on first use and "
        "expires quickly even if never redeemed. Requires an 'admin'-role "
        "API key (see `POST /v1/api-keys`' `role` field) -- a 'member'-role "
        "key gets 403 `desktop_not_permitted`. 404s for a session_id owned "
        "by a different account, identical to GET/DELETE, and 404s "
        "unconditionally when BOXKITE_DESKTOP_ENABLED is unset on this "
        "deployment."
    ),
)
async def mint_sandbox_desktop_token(
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account_and_key: tuple[Account, ApiKey] = Depends(get_current_account_and_key_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> SandboxDesktopTokenResponse:
    if not settings.BOXKITE_DESKTOP_ENABLED:
        raise ApiError(404, "not_found", "GUI desktop takeover is not enabled on this deployment")
    account, key_row = account_and_key
    await _enforce_sandbox_rate_limit(request, response, account)
    if not can_initiate_takeover(key_row.role):
        raise ApiError(
            403,
            "desktop_not_permitted",
            "This API key's role does not permit initiating a desktop session",
        )
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)

    token, expires_at = create_desktop_token(
        account_id=account.id,
        session_id=session_id,
        ttl_seconds=settings.BOXKITE_DESKTOP_TOKEN_TTL_SECONDS,
        api_key_id=key_row.id,
    )
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="desktop_token_mint",
        detail={"api_key_id": key_row.id, "api_key_name": key_row.name},
        started_at=_utcnow(),
    )
    return SandboxDesktopTokenResponse(token=token, expires_at=expires_at)


@router.websocket("/{session_id}/desktop")
async def desktop_sandbox(
    websocket: WebSocket,
    session_id: str = Path(...),
    manager=Depends(get_manager),
) -> None:
    """Interactive GUI/remote-desktop human takeover of a sandbox session,
    proxied to the sidecar's `WS /desktop` (GitHub issue #184,
    docs/GUI-COMPUTER-USE-SCOPING.md; see sidecar/sidecar_desktop.py's
    `desktop_takeover`). Structurally mirrors `takeover_sandbox` above:
    auth/RBAC/ownership before `accept()`, a bidirectional byte relay once
    connected, and start/end audit rows -- with two deliberate first-pass
    differences from PTY takeover:

    1. No per-input-byte audit equivalent to `typed_buffer`/`takeover_input`
       snapshots -- see `_log_desktop_entry`'s docstring for why (VNC's RFB
       protocol isn't meaningfully loggable as "what was typed" without
       actually parsing it). Only `desktop_start`/`desktop_end` rows are
       written.
    2. No session recording (docs/GUI-COMPUTER-USE-SCOPING.md's deferred
       list) -- unlike `BOXKITE_TAKEOVER_RECORDING_ENABLED`, there is no
       pixel-stream recording option for this first pass.

    404s unconditionally when BOXKITE_DESKTOP_ENABLED is unset, checked
    BEFORE `_authenticate_desktop_or_close` runs any auth work at all (fail
    fast, no wasted auth work for a deployment that hasn't opted into this
    feature).
    """
    if not settings.BOXKITE_DESKTOP_ENABLED:
        await websocket.close(code=4404, reason="GUI desktop takeover is not enabled on this deployment")
        return

    auth_result = await _authenticate_desktop_or_close(websocket, session_id=session_id)
    if auth_result is None:
        return
    account, _row, identity = auth_result

    try:
        target = await manager.get_sidecar_desktop_target(session_id)
    except (ValueError, RuntimeError) as exc:
        logger.warning("[desktop] Failed to resolve sidecar desktop target for %s: %s", session_id, exc)
        await websocket.close(code=1011, reason="Sandbox session unavailable")
        return

    await websocket.accept()

    started_at = _utcnow()
    await _log_desktop_entry(
        session_id=session_id,
        account_id=account.id,
        operation="desktop_start",
        detail=_identity_detail(identity),
        started_at=started_at,
    )

    bytes_relayed = 0
    try:
        async with websockets.connect(
            target["ws_url"],
            additional_headers={target["auth_header"]: target["auth_token"]},
            open_timeout=10,
        ) as sidecar_ws:

            async def _relay_client_to_sidecar_desktop() -> None:
                nonlocal bytes_relayed
                while True:
                    try:
                        message = await websocket.receive()
                    except WebSocketDisconnect:
                        return
                    if message.get("type") == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data is None:
                        text = message.get("text")
                        data = text.encode("utf-8") if text is not None else None
                    if not data:
                        continue
                    bytes_relayed += len(data)
                    await sidecar_ws.send(data)

            async def _relay_sidecar_to_client_desktop() -> None:
                nonlocal bytes_relayed
                async for data in sidecar_ws:
                    payload = data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8")
                    bytes_relayed += len(payload)
                    try:
                        await websocket.send_bytes(payload)
                    except (WebSocketDisconnect, RuntimeError):
                        return

            to_sidecar_task = asyncio.ensure_future(_relay_client_to_sidecar_desktop())
            to_client_task = asyncio.ensure_future(_relay_sidecar_to_client_desktop())
            try:
                await asyncio.wait({to_sidecar_task, to_client_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                to_sidecar_task.cancel()
                to_client_task.cancel()
                await asyncio.gather(to_sidecar_task, to_client_task, return_exceptions=True)
    except Exception as exc:
        logger.warning("[desktop] Sidecar proxy connection failed for %s: %s", session_id, exc)
    finally:
        await _log_desktop_entry(
            session_id=session_id,
            account_id=account.id,
            operation="desktop_end",
            detail={"bytes_relayed": bytes_relayed, **_identity_detail(identity)},
            started_at=started_at,
        )
        try:
            await websocket.close()
        except RuntimeError:
            pass


@router.get(
    "/{session_id}/takeover-recordings/{entry_id}",
    summary="Fetch a stored takeover-session PTY recording (asciicast v2) for replay",
    description=(
        "Returns the raw asciicast v2 "
        "(https://docs.asciinema.org/manual/asciicast/v2/) recording captured "
        "during a human-takeover session (GitHub issue #133), identified by "
        "the `takeover_end` ExecLogEntry row's own `id` -- see `GET .../log`'s "
        "`entries[].id` for rows with `operation == 'takeover_end'` and a "
        "`detail.recording_storage_key`. Same API-key auth and account-scoped "
        "ownership check as `GET .../log`. 404s if `entry_id` doesn't belong "
        "to the caller's account, doesn't belong to this `session_id`, isn't "
        "a `takeover_end` row, or has no recording pointer at all (recording "
        "was disabled via BOXKITE_TAKEOVER_RECORDING_ENABLED, or nothing was "
        "ever typed/printed during that session). Works even after the "
        "sandbox session itself has been destroyed -- the recording is "
        "durable object-storage content, independent of the pod's lifecycle."
    ),
)
async def get_takeover_recording(
    session_id: str = Path(...),
    entry_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    storage=Depends(get_snapshot_storage),
) -> Response:
    await _get_owned_session_or_404(session_id=session_id, account=account, db=db)
    entry = await ExecLogEntryRepository(db).get_for_account(entry_id=entry_id, account_id=account.id)
    if entry is None or entry.session_id != session_id or entry.operation != "takeover_end":
        raise ApiError(404, "not_found", "Takeover recording not found")
    storage_key = (entry.detail or {}).get("recording_storage_key")
    if not storage_key:
        raise ApiError(404, "not_found", "This takeover session has no recording")
    try:
        data = await storage.download_bytes(key=storage_key)
    except Exception as exc:
        logger.error("[takeover-recording] Failed to download recording %s for entry %s: %s", storage_key, entry_id, exc)
        raise ApiError(502, "storage_error", "Failed to fetch the takeover recording") from exc
    return Response(content=data, media_type="application/x-asciicast")


# ── Network ingress preview URLs ─────────────────────────────────────────
# See docs/NETWORK-INGRESS-DESIGN.md for the full design. Three routes:
#   POST .../preview/{port}         -- API-key-authenticated, account-scoped,
#                                       mints a signed, time-limited token.
#   POST .../preview/{port}/revoke  -- API-key-authenticated, account-scoped,
#                                       denylists one token's jti early
#                                       (GitHub issue #78).
#   ANY  .../preview/{port}/{..}    -- public (token-authenticated, not
#                                       API-key), proxies one HTTP request,
#                                       now TRUE streamed rather than
#                                       buffered (GitHub issue #78).
# Mint/proxy intentionally use two different auth models on the same
# resource, same as every "signed download link" pattern: the mint step is
# a normal authenticated, ownership-checked API call; the proxy step is a
# public link whose entire authorization is the possession of a valid
# signature over (session_id, port, expiry, jti) that only the mint step
# could produce. Revoke uses the same auth model as mint (it's an
# account-scoped management operation on the resource mint created).


def _build_preview_url(*, session_id: str, port: int, token: str) -> str:
    """Build the preview URL, always with a trailing slash before the query
    string. This is deliberate, not cosmetic: the proxy route below is
    `.../preview/{port}/{path:path}`, which only matches when there's a
    path segment (even an empty one) after `{port}/` -- a URL without the
    trailing slash would 404 against a bare root fetch. See the design
    doc's "known limitations" section for the tradeoffs of a path-prefixed
    proxy (this one) vs. E2B/Daytona's subdomain-based approach."""
    base = settings.BOXKITE_PUBLIC_URL.rstrip("/")
    path = f"/v1/sandboxes/{session_id}/preview/{port}/?token={token}"
    return f"{base}{path}" if base else path


@router.post(
    "/{session_id}/preview/{port}",
    response_model=SandboxPreviewUrlResponse,
    summary="Mint a signed preview URL for a port exposed inside a sandbox session",
    description=(
        "Mints a signed, time-limited URL that proxies HTTP traffic to a "
        "port a background process opened inside this session (see "
        "POST .../processes' `expose_port`). The URL itself carries its "
        "own authorization -- no API key is required to use it, only to "
        "mint it. 404s for a session_id owned by a different account, "
        "identical to GET/DELETE. Does NOT verify the port is actually "
        "listening yet -- a preview URL can be minted before the dev "
        "server has started; the proxy route below returns 502 until it is."
    ),
)
async def create_sandbox_preview_url(
    body: SandboxPreviewUrlRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    port: int = Path(..., ge=1, le=65535),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> SandboxPreviewUrlResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)

    token, expires_at, token_id = create_preview_token(
        session_id=session_id, port=port, ttl_seconds=body.ttl_seconds
    )
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="preview_url_create",
        detail={"port": port, "ttl_seconds": body.ttl_seconds, "token_id": token_id},
        started_at=_utcnow(),
    )
    return SandboxPreviewUrlResponse(
        url=_build_preview_url(session_id=session_id, port=port, token=token),
        expires_at=expires_at,
        token_id=token_id,
    )


# MUST be registered before the `{path:path}` proxy catch-all further below
# (route order is registration order in FastAPI/Starlette, first match
# wins) -- otherwise a POST to `.../preview/{port}/revoke` would be treated
# as a proxied request with path="revoke" instead of reaching this route.
# This is also why a dev server that itself has a real "/revoke" path is a
# (documented, low-probability) path-prefix collision, same category of
# caveat this design already accepts for the "preview" segment itself.
@router.post(
    "/{session_id}/preview/{port}/revoke",
    response_model=SandboxPreviewRevokeResponse,
    summary="Revoke one previously-minted preview-URL token before it expires",
    description=(
        "Invalidates one specific preview-URL token (identified by the "
        "`token_id` returned from the mint call above) without tearing "
        "down the sandbox session, and without affecting any other "
        "preview token minted for the same session/port -- see "
        "docs/NETWORK-INGRESS-DESIGN.md's former \"no revocation before "
        "expiry\" limitation, closed by this route. Idempotent: revoking "
        "an already-revoked, already-expired, or unrecognized token_id "
        "still returns 200 (revoked=true) rather than a 404 -- the caller "
        "cannot distinguish 'this token never existed' from 'someone else "
        "already revoked it', which is the same information the mint "
        "route's own account-ownership check already withholds. "
        "API-key-authenticated and account-scoped exactly like the mint "
        "route: a session_id owned by a different account 404s."
    ),
)
async def revoke_sandbox_preview_url(
    body: SandboxPreviewRevokeRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    port: int = Path(..., ge=1, le=65535),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> SandboxPreviewRevokeResponse:
    await _enforce_sandbox_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)

    expires_at = _utcnow() + timedelta(seconds=SANDBOX_PREVIEW_MAX_TTL_SECONDS)
    await PreviewTokenRevocationRepository(db).revoke(
        jti=body.token_id, session_id=session_id, port=port, expires_at=expires_at
    )
    await _log_exec_entry(
        db,
        session_id=session_id,
        account_id=account.id,
        operation="preview_url_revoke",
        detail={"port": port, "token_id": body.token_id},
        started_at=_utcnow(),
    )
    return SandboxPreviewRevokeResponse(revoked=True, token_id=body.token_id)


# Not logged to ExecLogEntry per-request (unlike every route above): a
# single page load from a preview URL can fan out into dozens of asset
# requests, and this route has no account_id to attribute the log row to
# anyway (see get_by_id_unscoped's docstring) -- the mint step above is the
# auditable event. If per-request observability is ever needed here, it
# belongs in a dedicated, session-scoped counter/metric, not ExecLogEntry.
_PREVIEW_PROXY_DESCRIPTION = (
    "Public route: proxies one HTTP request to a port exposed inside a "
    "sandbox session. Authorization is entirely the `token` query "
    "parameter (a signed, time-limited token minted by "
    "POST .../preview/{port}) -- no API key, no Authorization header. "
    "404s if the token is valid but the session no longer exists; 401 "
    "if the token is missing, expired, malformed, or bound to a "
    "different session_id/port than the URL path, or has been revoked via "
    "POST .../preview/{port}/revoke; 502 if the token is valid but nothing "
    "is listening on the port yet."
)


# Explicit allowlist of client request headers forwarded to the sidecar
# preview proxy. Anything not on this list -- including sidecar-internal
# headers like X-Sidecar-Auth-Token -- is dropped, so a caller cannot
# override the sidecar auth header that httpx's default client headers set
# for this outbound call. Prefer extending this list over reverting to a
# denylist.
_PREVIEW_PROXY_FORWARDED_REQUEST_HEADERS = {
    "accept",
    "accept-encoding",
    "accept-language",
    "content-type",
    "user-agent",
    "cookie",
    "cache-control",
    "if-none-match",
    "if-modified-since",
    "range",
    "referer",
    "origin",
    "x-requested-with",
}


# Registered once per HTTP method (rather than one `api_route(methods=[...])`
# call) so each operation gets its own explicit, unique `operation_id` --
# FastAPI's default unique-id generator keys only off the route's name+path
# and the FIRST method in a multi-method route's method set, which produces
# colliding (and effectively random, since sets are unordered) operation_ids
# in the OpenAPI schema for every method beyond the first if registered as
# a single api_route.
def _register_preview_proxy_route(method: str, operation_id: str):
    return router.api_route(
        "/{session_id}/preview/{port}/{path:path}",
        methods=[method],
        summary="Proxy an HTTP request to a preview URL",
        description=_PREVIEW_PROXY_DESCRIPTION,
        operation_id=operation_id,
        response_model=None,
    )


async def _stream_preview_response_body(upstream_response, *, session_id: str, port: int):
    """Yield the upstream preview response body chunk by chunk instead of
    reading it into memory first -- true end-to-end streaming through this
    hop too, mirroring the sidecar's own `_stream_upstream_body` for its
    proxy leg (see docs/NETWORK-INGRESS-DESIGN.md's former "no true
    streaming" limitation, closed by this change). `upstream_response`
    itself was already returned in streaming mode by
    `SandboxManager.proxy_preview_request` (`client.send(..., stream=True)`),
    so no bytes have been read yet when this generator starts running --
    only after StreamingResponse begins draining it. Always closes the
    upstream response, even if the client disconnects mid-stream.
    """
    try:
        async for chunk in upstream_response.aiter_bytes():
            yield chunk
    except Exception as exc:
        logger.warning(
            "[preview] Streamed response interrupted for %s:%s: %s", session_id, port, exc
        )
    finally:
        await upstream_response.aclose()


@_register_preview_proxy_route("GET", "proxy_sandbox_preview_get")
@_register_preview_proxy_route("POST", "proxy_sandbox_preview_post")
@_register_preview_proxy_route("PUT", "proxy_sandbox_preview_put")
@_register_preview_proxy_route("PATCH", "proxy_sandbox_preview_patch")
@_register_preview_proxy_route("DELETE", "proxy_sandbox_preview_delete")
@_register_preview_proxy_route("OPTIONS", "proxy_sandbox_preview_options")
async def proxy_sandbox_preview(
    request: Request,
    response: Response,
    session_id: str = Path(...),
    port: int = Path(..., ge=1, le=65535),
    path: str = Path(...),
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> Response:
    try:
        payload = decode_preview_token(token)
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_preview_token", "Preview link is invalid or has expired")
    if payload.get("sid") != session_id or payload.get("port") != port:
        raise ApiError(401, "invalid_preview_token", "Preview link does not match this session/port")
    if await PreviewTokenRevocationRepository(db).is_revoked(payload.get("jti")):
        raise ApiError(401, "preview_token_revoked", "Preview link has been revoked")

    await enforce_rate_limit(
        request,
        bucket="sandbox_preview",
        subject=session_id,
        limit=settings.BOXKITE_PREVIEW_RATE_LIMIT_PER_MINUTE,
        response=response,
    )

    # Deliberately unscoped by account -- see get_by_id_unscoped's docstring
    # for why that's safe here (the token above already proves ownership).
    session_row = await SandboxSessionRepository(db).get_by_id_unscoped(session_id)
    if session_row is None or session_row.destroyed_at is not None:
        raise ApiError(404, "not_found", "Sandbox session not found")

    body = await request.body()
    # Allowlist rather than denylist: only forward headers that describe the
    # request body/content negotiation/client identity. This structurally
    # excludes any sidecar-internal header (e.g. X-Sidecar-Auth-Token) from
    # ever being set by an unauthenticated caller of this public proxy route,
    # rather than relying on a blocklist staying complete over time.
    forward_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _PREVIEW_PROXY_FORWARDED_REQUEST_HEADERS
    }
    forward_params = {key: value for key, value in request.query_params.items() if key != "token"}

    try:
        upstream_response = await manager.proxy_preview_request(
            session_id=session_id,
            port=port,
            path=path,
            method=request.method,
            params=forward_params,
            headers=forward_headers,
            content=body,
        )
    except (ValueError, RuntimeError):
        raise ApiError(404, "not_found", "Sandbox session not found")
    except Exception as exc:
        logger.warning("[preview] Proxy request failed for %s:%s: %s", session_id, port, exc)
        raise ApiError(502, "preview_unreachable", "Preview upstream is unreachable")

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in {"content-length", "transfer-encoding", "connection"}
    }
    return StreamingResponse(
        _stream_preview_response_body(upstream_response, session_id=session_id, port=port),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
