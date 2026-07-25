"""Filesystem snapshot/restore — docs/SNAPSHOT-DESIGN.md.

Deliberately "filesystem snapshot," never bare "snapshot," everywhere this
touches user-facing text: this is a point-in-time copy of the sandbox's
workspace/output filesystem in blob storage, restorable into a *fresh* pod
later — it does not preserve running processes, open network connections,
or in-memory state (see the design doc's section 2 for why a true VM-level
checkpoint isn't what boxkite's plain-K8s-pod isolation can deliver).

Two routers live in this module because the design doc's API shape mixes
session-scoped and snapshot-scoped paths:
- `POST/GET /v1/sandboxes/{session_id}/snapshots` (session-scoped) share
  `routers/sandboxes.py`'s `/v1/sandboxes` prefix and its exact
  `_get_active_session_or_404`/`get_for_account` ownership pattern.
- `GET/DELETE /v1/snapshots/{snapshot_id}` and
  `POST /v1/snapshots/{snapshot_id}/restore` are scoped to the snapshot
  directly (a snapshot outlives its source session), under `/v1/snapshots`.

Every lookup here is scoped to `account.id` at the database layer
(`SnapshotRepository.get_for_account`/`list_for_session`), never
client-filtered — a foreign `snapshot_id` (or a `session_id` owned by a
different account) 404s, identically to every existing sandbox route, so a
caller can never even probe existence. This is called out in the design
doc's security section as the single highest-severity risk in this feature.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Path, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_account_via_api_key, get_manager, get_snapshot_storage, get_usage_policy
from ..errors import ApiError, LimitExceededError
from ..models_orm import Account
from ..rate_limit import enforce_rate_limit
from ..repository import SandboxSessionRepository, SnapshotRepository
from ..schemas import (
    SandboxCreatedResponse,
    SnapshotCreatedResponse,
    SnapshotCreateRequest,
    SnapshotOut,
    SnapshotRestoreRequest,
    UsageSummary,
)
from ..storage_client import SnapshotStorageClient
from ..usage_policy import UsagePolicy
from .sandboxes import _get_active_session_or_404, _to_out

logger = logging.getLogger(__name__)

# Nested under /v1/sandboxes -- create/list-by-session share the exact
# ownership-scoping and rate-limit conventions routers/sandboxes.py already
# established for every other session-scoped route.
sandbox_snapshots_router = APIRouter(prefix="/v1/sandboxes", tags=["snapshots"])

# A snapshot outlives its source session, so get/restore/delete are scoped
# to the snapshot itself, not nested under a session_id path.
snapshots_router = APIRouter(prefix="/v1/snapshots", tags=["snapshots"])


def _snapshot_storage_prefix(*, account_id: str, snapshot_id: str) -> str:
    """`snapshots/{account_id}/{snapshot_id}` -- namespaced by account_id so
    a bug in the DB-layer authorization check isn't the only thing standing
    between two tenants' snapshot data (design doc security section)."""
    return f"snapshots/{account_id}/{snapshot_id}"


def _session_storage_prefix(*, account_id: str, session_id: str) -> str:
    """Mirrors `SandboxManager._build_storage_prefix`'s own
    `sessions/{organization_id}/{session_id}` shape for a session created
    with no `work_item_id` -- this control plane always passes the
    account's id as `organization_id` (see usage_policy.py), and every
    session it creates today has no `work_item_id` concept, so this is safe
    to precompute here rather than needing SandboxManager to hand it back
    before /configure's prefetch has already run."""
    return f"sessions/{account_id}/{session_id}"


async def _enforce_snapshot_rate_limit(request: Request, response: Response, account: Account) -> None:
    """Snapshot create/restore/delete are heavier, potentially large
    storage-copy operations -- a distinct, lower bucket than either
    `sandbox_ops` or `sandbox_lifecycle`, per the design doc's security
    section ("treat them as a distinct bucket rather than silently
    inheriting the exec/file-op limit")."""
    await enforce_rate_limit(
        request,
        bucket="snapshot_ops",
        subject=str(account.id),
        limit=settings.BOXKITE_SNAPSHOT_RATE_LIMIT_PER_MINUTE,
        response=response,
    )


def _to_snapshot_out(row) -> SnapshotOut:
    return SnapshotOut.model_validate(row)


@sandbox_snapshots_router.post(
    "/{session_id}/snapshots",
    response_model=SnapshotCreatedResponse,
    status_code=201,
    summary="Create a filesystem snapshot of a live sandbox session",
    description=(
        "Forces a confirmed flush of the session's current workspace/output "
        "filesystem state, then issues a storage-side copy (never routed "
        "through the sidecar) into an immutable snapshots/{account_id}/"
        "{snapshot_id} prefix. This is a filesystem-only, point-in-time "
        "copy -- it does not preserve running processes, open network "
        "connections, or in-memory state. 404s for a session_id owned by a "
        "different account, identical to every other session-scoped route. "
        "429s with `snapshot_limit_reached` if this account is already at "
        "BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT."
    ),
)
async def create_snapshot(
    body: SnapshotCreateRequest,
    request: Request,
    response: Response,
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
    snapshot_storage: SnapshotStorageClient = Depends(get_snapshot_storage),
) -> SnapshotCreatedResponse:
    await _enforce_snapshot_rate_limit(request, response, account)
    await _get_active_session_or_404(session_id=session_id, account=account, db=db)

    snapshots = SnapshotRepository(db)
    active_count = await snapshots.count_active_for_account(account.id)
    if active_count >= settings.BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT:
        raise LimitExceededError(
            code="snapshot_limit_reached",
            message=(
                "Snapshot limit reached "
                f"({settings.BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT} at a time). "
                "Delete an existing snapshot before creating another."
            ),
            details={"limit": settings.BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT, "active": active_count},
        )

    snapshot_id = str(uuid4())
    dest_prefix = _snapshot_storage_prefix(account_id=account.id, snapshot_id=snapshot_id)
    row = await snapshots.create(
        snapshot_id=snapshot_id,
        account_id=account.id,
        session_id=session_id,
        label=body.label,
        storage_key_prefix=dest_prefix,
        status="pending",
    )

    try:
        manifest = await manager.snapshot(session_id)
    except Exception as exc:
        await snapshots.mark_failed(snapshot_id=snapshot_id)
        raise ApiError(
            502,
            "sandbox_operation_failed",
            "Failed to flush the sandbox session's filesystem for snapshotting. "
            "It may have become unavailable; try again.",
        ) from exc

    source_prefix = manifest.get("storage_prefix")
    storage_keys = manifest.get("storage_keys") or []
    if not source_prefix:
        await snapshots.mark_failed(snapshot_id=snapshot_id)
        raise ApiError(
            502,
            "sandbox_operation_failed",
            "Sandbox session has no storage prefix configured; cannot snapshot it.",
        )

    try:
        size_bytes = await snapshot_storage.copy_prefix(
            source_prefix=source_prefix, dest_prefix=dest_prefix, keys=storage_keys
        )
    except Exception as exc:
        await snapshots.mark_failed(snapshot_id=snapshot_id)
        logger.error("[snapshots] storage-side copy failed for snapshot %s: %s", snapshot_id, exc)
        raise ApiError(
            502,
            "snapshot_storage_failed",
            "Failed to copy the sandbox session's filesystem to snapshot storage.",
        ) from exc

    await snapshots.mark_completed(snapshot_id=snapshot_id, size_bytes=size_bytes)
    row.status = "completed"
    row.size_bytes = size_bytes
    return SnapshotCreatedResponse.model_validate(row)


@sandbox_snapshots_router.get(
    "/{session_id}/snapshots",
    response_model=list[SnapshotOut],
    summary="List filesystem snapshots taken from a sandbox session",
    description=(
        "Lists non-deleted snapshots whose source was this session, scoped "
        "to the authenticated account. Works for a destroyed session too -- "
        "a snapshot outlives the session it was taken from -- so this does "
        "NOT require the session to still be live, unlike POST .../snapshots. "
        "404s for a session_id owned by a different account or one that "
        "never existed."
    ),
)
async def list_snapshots_for_session(
    session_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[SnapshotOut]:
    session_row = await SandboxSessionRepository(db).get_for_account(session_id=session_id, account_id=account.id)
    if session_row is None:
        raise ApiError(404, "not_found", "Sandbox session not found")
    rows = await SnapshotRepository(db).list_for_session(session_id=session_id, account_id=account.id)
    return [_to_snapshot_out(r) for r in rows]


@snapshots_router.get(
    "/{snapshot_id}",
    response_model=SnapshotOut,
    summary="Get a single filesystem snapshot",
    description=(
        "Fetches one snapshot by id, scoped to the authenticated account. "
        "404s for a snapshot_id owned by a different account or one that "
        "never existed or has been deleted, identically."
    ),
)
async def get_snapshot(
    snapshot_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> SnapshotOut:
    row = await SnapshotRepository(db).get_for_account(snapshot_id=snapshot_id, account_id=account.id)
    if row is None or row.deleted_at is not None:
        raise ApiError(404, "not_found", "Snapshot not found")
    return _to_snapshot_out(row)


@snapshots_router.post(
    "/{snapshot_id}/restore",
    response_model=SandboxCreatedResponse,
    status_code=201,
    summary="Restore a filesystem snapshot into a new sandbox session",
    description=(
        "Creates a new sandbox session the same way POST /v1/sandboxes "
        "does -- subject to the same concurrent-sandbox and monthly-usage "
        "caps, a restore counts toward the same usage limits as a new "
        "session -- except the new pod's workspace/output filesystem is "
        "seeded from this snapshot's storage-side copy before the pod's "
        "first command runs. Nothing about pod spec, capabilities, or "
        "network policy differs from an ordinary create. 404s for a "
        "snapshot_id owned by a different account or one that has been "
        "deleted."
    ),
)
async def restore_snapshot(
    body: SnapshotRestoreRequest,
    request: Request,
    response: Response,
    snapshot_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    policy: UsagePolicy = Depends(get_usage_policy),
    snapshot_storage: SnapshotStorageClient = Depends(get_snapshot_storage),
) -> SandboxCreatedResponse:
    await _enforce_snapshot_rate_limit(request, response, account)
    snapshot = await SnapshotRepository(db).get_for_account(snapshot_id=snapshot_id, account_id=account.id)
    if snapshot is None or snapshot.deleted_at is not None or snapshot.status != "completed":
        raise ApiError(404, "not_found", "Snapshot not found")

    # Predetermine the new session's id so its live storage_prefix can be
    # seeded from the snapshot's immutable copy BEFORE
    # SandboxManager.create_session's /configure call runs its prefetch --
    # see usage_policy.py's `session_id` override docstring.
    new_session_id = str(uuid4())
    dest_prefix = _session_storage_prefix(account_id=account.id, session_id=new_session_id)
    try:
        snapshot_keys = await snapshot_storage.list_keys(prefix=snapshot.storage_key_prefix)
        await snapshot_storage.copy_prefix(
            source_prefix=snapshot.storage_key_prefix, dest_prefix=dest_prefix, keys=snapshot_keys
        )
    except Exception as exc:
        logger.error("[snapshots] restore seed copy failed for snapshot %s: %s", snapshot_id, exc)
        raise ApiError(
            502,
            "snapshot_storage_failed",
            "Failed to seed the new sandbox session from this snapshot.",
        ) from exc

    row, _manager_result = await policy.create_session(
        account,
        label=body.label,
        session_id=new_session_id,
        restore_from_snapshot_id=snapshot.id,
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


@snapshots_router.delete(
    "/{snapshot_id}",
    status_code=204,
    summary="Delete a filesystem snapshot",
    description=(
        "Deletes a snapshot's bookkeeping row AND its underlying storage "
        "objects -- never just the DB row, per the design doc's explicit "
        "requirement that a deleted snapshot must not leave an orphaned "
        "copy of potentially sensitive filesystem contents behind. 404s "
        "for a snapshot_id owned by a different account, identical to "
        "every other snapshot/sandbox route."
    ),
)
async def delete_snapshot(
    request: Request,
    response: Response,
    snapshot_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    snapshot_storage: SnapshotStorageClient = Depends(get_snapshot_storage),
) -> Response:
    await _enforce_snapshot_rate_limit(request, response, account)
    snapshots = SnapshotRepository(db)
    row = await snapshots.get_for_account(snapshot_id=snapshot_id, account_id=account.id)
    if row is None or row.deleted_at is not None:
        raise ApiError(404, "not_found", "Snapshot not found")

    try:
        await snapshot_storage.delete_prefix(prefix=row.storage_key_prefix)
    except Exception as exc:
        logger.error("[snapshots] failed to delete storage objects for snapshot %s: %s", snapshot_id, exc)
        raise ApiError(
            502,
            "snapshot_storage_failed",
            "Failed to delete this snapshot's underlying storage objects. Try again.",
        ) from exc

    await snapshots.mark_deleted(snapshot_id=snapshot_id)
    return Response(status_code=204, headers=dict(response.headers))
