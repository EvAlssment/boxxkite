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
from ..errors import ApiError
from ..models_orm import Account
from ..repository import (
    AccountRepository,
    ExecLogEntryRepository,
    McpConnectionRepository,
    MemoryRepository,
    SandboxImageRepository,
    SandboxSessionRepository,
    SandboxVolumeRepository,
    SecretRepository,
    SnapshotRepository,
    WebhookSubscriptionRepository,
)
from ..schemas import (
    ADMIN_AUDIT_LOG_DEFAULT_LIMIT,
    AdminAccountDetail,
    AdminAccountFeatureUsage,
    AdminAccountUsage,
    AdminAuditLogEntryOut,
    AdminAuditLogResponse,
    AdminClusterMetrics,
)
from ..usage_policy import UsagePolicy

router = APIRouter(prefix="/v1/admin", tags=["admin"])


async def _compute_admin_cluster_metrics(
    *, db: AsyncSession, limit: int, offset: int
) -> AdminClusterMetrics:
    """Shared aggregation behind both the API-key route below and its
    dashboard-JWT mirror in `routers/account.py` -- same numbers regardless
    of which credential the caller used to prove they're an admin."""
    effective_limit = min(limit, settings.BOXXKITE_ADMIN_METRICS_MAX_ACCOUNTS)

    accounts_repo = AccountRepository(db)
    sessions_repo = SandboxSessionRepository(db)
    policy = UsagePolicy(sandbox_manager=None, sessions=sessions_repo)

    total_accounts = await accounts_repo.count_total()
    global_concurrent = await sessions_repo.count_active_total()
    total_monthly_hours = await policy.monthly_hours_used_total()
    active_by_account = await sessions_repo.count_active_by_account()
    total_by_account = await sessions_repo.count_total_by_account()

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
                total_sandboxes_created=total_by_account.get(account.id, 0),
            )
        )

    return AdminClusterMetrics(
        total_accounts=total_accounts,
        global_concurrent_sandboxes=global_concurrent,
        global_concurrent_sandboxes_limit=settings.BOXXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES,
        total_monthly_sandbox_hours_used=round(total_monthly_hours, 4),
        accounts=account_rows,
    )


async def _compute_admin_account_detail(*, db: AsyncSession, account_id: str) -> AdminAccountDetail:
    """Shared aggregation behind both the API-key route below and its
    dashboard-JWT mirror in `routers/account.py` -- same numbers regardless
    of which credential the caller used to prove they're an admin (mirrors
    `_compute_admin_cluster_metrics`'s own convention above).

    Deliberately scoped to ONE account: the six feature-usage counts below
    are each a separate query, which is fine for a single account but would
    be a real N+1 cost if run per-row across `AdminClusterMetrics`'s
    paginated account list -- see docs/ADMIN-ROLE-DESIGN.md's boundary
    section for why that list stays lean instead.
    """
    account = await AccountRepository(db).get_by_id(account_id)
    if account is None:
        raise ApiError(404, "account_not_found", "Account not found")

    sessions_repo = SandboxSessionRepository(db)
    policy = UsagePolicy(sandbox_manager=None, sessions=sessions_repo)

    concurrent = await sessions_repo.count_active_for_account(account_id)
    monthly_hours = await policy.monthly_hours_used(account_id)
    total_sandboxes = await sessions_repo.count_total_for_account(account_id)

    feature_usage = AdminAccountFeatureUsage(
        secrets=await SecretRepository(db).count_for_account(account_id),
        mcp_connections=await McpConnectionRepository(db).count_for_account(account_id),
        webhooks=await WebhookSubscriptionRepository(db).count_for_account(account_id),
        snapshots=await SnapshotRepository(db).count_active_for_account(account_id),
        sandbox_images=await SandboxImageRepository(db).count_active_for_account(account_id),
        sandbox_volumes=await SandboxVolumeRepository(db).count_active_for_account(account_id),
        memory=await MemoryRepository(db).metrics_for_account(account_id=account_id, scope=None),
    )

    return AdminAccountDetail(
        account_id=account.id,
        email=account.email,
        created_at=account.created_at,
        concurrent_sandboxes=concurrent,
        concurrent_sandboxes_limit=settings.BOXXKITE_MAX_CONCURRENT_SANDBOXES,
        monthly_sandbox_hours_used=round(monthly_hours, 4),
        total_sandboxes_created=total_sandboxes,
        feature_usage=feature_usage,
    )


@router.get(
    "/accounts/{account_id}",
    response_model=AdminAccountDetail,
    summary="One account's full usage and feature-adoption picture (admin only)",
    description=(
        "Admin-gated single-account view: the same usage numbers as this "
        "account's row in GET /v1/admin/metrics, plus a per-feature "
        "resource-count breakdown (secrets, MCP connections, webhooks, "
        "snapshots, sandbox images, sandbox volumes, memory) not exposed "
        "anywhere else. Pair with GET /v1/admin/audit-log?account_id=... "
        "for that account's recent activity. 404s if the account doesn't "
        "exist; 403s for a valid API key belonging to a non-admin account."
    ),
)
async def get_admin_account_detail(
    account_id: str,
    _admin: Account = Depends(get_current_admin_account),
    db: AsyncSession = Depends(get_db),
) -> AdminAccountDetail:
    return await _compute_admin_account_detail(db=db, account_id=account_id)


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
    return await _compute_admin_cluster_metrics(db=db, limit=limit, offset=offset)


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
    return await _compute_admin_audit_log(db=db, limit=limit, offset=offset, account_id=account_id)


async def _compute_admin_audit_log(
    *, db: AsyncSession, limit: int, offset: int, account_id: str | None
) -> AdminAuditLogResponse:
    """Shared aggregation behind both the API-key route above and its
    dashboard-JWT mirror in `routers/account.py` -- same convention as
    `_compute_admin_cluster_metrics`/`_compute_admin_account_detail`."""
    effective_limit = min(limit, settings.BOXXKITE_ADMIN_AUDIT_LOG_MAX_LIMIT)

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
