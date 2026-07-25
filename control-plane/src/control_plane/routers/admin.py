"""Admin-role cross-account metrics — docs/ADMIN-ROLE-DESIGN.md, closing
GitHub issue #72.

Every route here is gated by `deps.get_current_admin_account`, which
requires `Account.is_admin` (never self-serve granted -- see that column's
docstring) and durably logs the access to `AdminAccessLog` before the
handler runs. This is the ONLY place in this codebase a route legitimately
reads across every account at once; every other router scopes by
`account.id` at the database layer per repository.py's module docstring.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_admin_account
from ..models_orm import Account
from ..repository import AccountRepository, ExecLogEntryRepository, SandboxSessionRepository
from ..schemas import (
    ADMIN_AUDIT_LOG_DEFAULT_LIMIT,
    AdminAccountUsage,
    AdminAuditLogEntryOut,
    AdminAuditLogResponse,
    AdminClusterMetrics,
)
from ..usage_policy import UsagePolicy

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get(
    "/metrics",
    response_model=AdminClusterMetrics,
    summary="Cluster-wide usage aggregation across all accounts (admin only)",
    description=(
        "Admin-gated cluster-wide view: total accounts, global concurrent-"
        "sandbox count against the fleet-wide cap, total monthly "
        "sandbox-hours across every account, and a paginated per-account "
        "breakdown. Distinct from GET /v1/usage, which is scoped to the "
        "calling account only -- see docs/ADMIN-ROLE-DESIGN.md's boundary "
        "section. 403s for a valid API key belonging to a non-admin account."
    ),
)
async def get_admin_cluster_metrics(
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
    _admin: Account = Depends(get_current_admin_account),
    db: AsyncSession = Depends(get_db),
) -> AdminClusterMetrics:
    effective_limit = min(limit, settings.BOXKITE_ADMIN_METRICS_MAX_ACCOUNTS)

    accounts_repo = AccountRepository(db)
    sessions_repo = SandboxSessionRepository(db)
    policy = UsagePolicy(sandbox_manager=None, sessions=sessions_repo)

    total_accounts = await accounts_repo.count_total()
    global_concurrent = await sessions_repo.count_active_total()
    total_monthly_hours = await policy.monthly_hours_used_total()
    active_by_account = await sessions_repo.count_active_by_account()

    page = await accounts_repo.list_all(limit=effective_limit, offset=offset)
    account_rows = []
    for account in page:
        account_rows.append(
            AdminAccountUsage(
                account_id=account.id,
                email=account.email,
                concurrent_sandboxes=active_by_account.get(account.id, 0),
                monthly_sandbox_hours_used=round(
                    await policy.monthly_hours_used(account.id), 4
                ),
            )
        )

    return AdminClusterMetrics(
        total_accounts=total_accounts,
        global_concurrent_sandboxes=global_concurrent,
        global_concurrent_sandboxes_limit=settings.BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES,
        total_monthly_sandbox_hours_used=round(total_monthly_hours, 4),
        accounts=account_rows,
    )


@router.get(
    "/audit-log",
    response_model=AdminAuditLogResponse,
    summary="Cross-account exec/file-op audit log aggregation (admin only)",
    description=(
        "Admin-gated aggregation of exec_log_entries across every sandbox "
        "session in every account, newest first, optionally narrowed to "
        "one account via `account_id`. Distinct from "
        "GET /v1/sandboxes/{session_id}/log, which is scoped to a single "
        "session the calling account already owns -- see "
        "docs/ADMIN-ROLE-DESIGN.md. 403s for a valid API key belonging to "
        "a non-admin account."
    ),
)
async def get_admin_audit_log(
    limit: int = Query(default=ADMIN_AUDIT_LOG_DEFAULT_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
    account_id: str | None = Query(default=None),
    _admin: Account = Depends(get_current_admin_account),
    db: AsyncSession = Depends(get_db),
) -> AdminAuditLogResponse:
    effective_limit = min(limit, settings.BOXKITE_ADMIN_AUDIT_LOG_MAX_LIMIT)

    repo = ExecLogEntryRepository(db)
    entries = await repo.list_across_accounts(
        account_id=account_id, limit=effective_limit, offset=offset
    )
    total = await repo.count_across_accounts(account_id=account_id)

    return AdminAuditLogResponse(
        entries=[AdminAuditLogEntryOut.model_validate(entry) for entry in entries],
        limit=effective_limit,
        offset=offset,
        total=total,
    )
