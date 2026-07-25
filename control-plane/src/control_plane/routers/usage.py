"""Standalone usage check — the same numbers already returned inline on
`POST /v1/sandboxes`, but queryable without creating a sandbox first — plus
the read-only compute-time/operation-count rollup (GitHub issue #162) built
on the exec-log data `ExecLogEntryRepository` already writes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_account_via_api_key, get_usage_policy
from ..errors import ApiError
from ..models_orm import Account
from ..repository import ExecLogEntryRepository, SandboxSessionRepository
from ..schemas import UsageRollupGroup, UsageRollupResponse, UsageSummary
from ..usage_policy import UsagePolicy

router = APIRouter(prefix="/v1/usage", tags=["usage"])


@router.get(
    "",
    response_model=UsageSummary,
    summary="Check current usage against fair-use limits",
    description=(
        "Returns the authenticated account's current concurrent-sandbox "
        "count and monthly sandbox-hours consumed, against the configured "
        "fair-use limits -- the same numbers already included inline on "
        "`POST /v1/sandboxes`, available here without creating a sandbox."
    ),
)
async def get_usage(
    account: Account = Depends(get_current_account_via_api_key),
    policy: UsagePolicy = Depends(get_usage_policy),
    db: AsyncSession = Depends(get_db),
) -> UsageSummary:
    active_count = await SandboxSessionRepository(db).count_active_for_account(account.id)
    hours_used = await policy.monthly_hours_used(account.id)
    return UsageSummary(
        monthly_sandbox_hours_used=round(hours_used, 4),
        monthly_sandbox_hours_limit=settings.BOXKITE_FREE_MONTHLY_SANDBOX_HOURS,
        concurrent_sandboxes=active_count,
        concurrent_sandboxes_limit=settings.BOXKITE_MAX_CONCURRENT_SANDBOXES,
    )


@router.get(
    "/rollup",
    response_model=UsageRollupResponse,
    summary="Compute-time attribution rollup over this account's exec log (read-only)",
    description=(
        "Read-only duration/operation-count attribution over the "
        "authenticated account's own exec-log rows (`ExecLogEntry` -- "
        "GitHub issue #162), grouped by session, by calendar day, or by "
        "operation, optionally narrowed to a `start`/`end` window. Scoped "
        "to the calling account only, the same way every other route in "
        "this API is -- there is no way to pass another account's id. "
        "Reports compute time (`duration_ms`) and operation counts only; "
        "this is not a cost or pricing figure."
    ),
)
async def get_usage_rollup(
    group_by: Literal["session", "day", "operation"] = Query(default="session"),
    start: datetime | None = Query(default=None, description="Inclusive lower bound on started_at."),
    end: datetime | None = Query(default=None, description="Exclusive upper bound on started_at."),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> UsageRollupResponse:
    if start is not None and end is not None and start >= end:
        raise ApiError(422, "invalid_range", "`start` must be before `end`")

    groups, total_duration_ms, total_operation_count, group_count = await ExecLogEntryRepository(
        db
    ).rollup_for_account(
        account_id=account.id,
        group_by=group_by,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )
    return UsageRollupResponse(
        group_by=group_by,
        start=start,
        end=end,
        total_duration_ms=total_duration_ms,
        total_operation_count=total_operation_count,
        groups=[
            UsageRollupGroup(key=key, duration_ms=duration_ms, operation_count=operation_count)
            for key, duration_ms, operation_count in groups
        ],
        group_count=group_count,
        limit=limit,
        offset=offset,
    )
