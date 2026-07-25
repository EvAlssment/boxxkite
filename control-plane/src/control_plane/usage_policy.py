"""Fair-use policy layer: this is where BOXKITE_FREE_MONTHLY_SANDBOX_HOURS,
BOXKITE_MAX_SESSION_MINUTES, and BOXKITE_MAX_CONCURRENT_SANDBOXES are
actually enforced.

Deliberately NOT inside SandboxManager (per the task's explicit instruction
to keep this a control-plane-level policy layer): SandboxManager knows
nothing about accounts, usage windows, or limits — it only knows pods and
sessions. This module wraps it with exactly the account-scoped bookkeeping
(via SandboxSessionRepository, backed by the control plane's own Postgres)
needed to enforce those limits, then delegates the actual pod lifecycle
calls unchanged.

Enforcement walkthrough (also in the top-level report):
1. `create_session` first counts ALL accounts' currently-active rows
   combined -- if that's already at BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES,
   it raises LimitExceededError before checking anything account-specific.
   This is the cluster-wide ceiling: enough accounts hitting their own
   (much smaller) per-account cap could otherwise still collectively
   exhaust node capacity.
2. It then counts the account's own currently-active rows in
   `sandbox_sessions` (not SandboxManager/K8s state) — if that count is
   already at BOXKITE_MAX_CONCURRENT_SANDBOXES, it raises
   LimitExceededError before ever calling SandboxManager.
3. It then computes the account's total sandbox-hours consumed so far this
   calendar month (destroyed sessions' recorded durations, plus elapsed
   time for any still-active sessions) — if that total is at or past
   BOXKITE_FREE_MONTHLY_SANDBOX_HOURS, it raises LimitExceededError.
4. Only if all three checks pass does it insert a `sandbox_sessions` row
   (pod_name=None) to reserve the slot, THEN call
   `SandboxManager.create_session(...)` for real, then fill in the real
   pod_name once that returns.
5. Independently, a background reaper (reaper.py) periodically tears down
   any session whose wall-clock age exceeds BOXKITE_MAX_SESSION_MINUTES,
   regardless of whether the caller ever calls DELETE.

Steps 1-4's checks AND the reservation insert run inside
`_create_session_critical_section` so concurrent requests can't each
observe the same "below cap" count before any of them commits its new row
-- see that function's docstring for the race this closes, and
PostgresSessionLock's docstring for closing it across replicas too (via
BOXKITE_USAGE_LOCK_BACKEND=postgres; the default "memory" backend only
closes it within a single process, same caveat BOXKITE_RATE_LIMIT_BACKEND
documents for rate limiting). The critical section is exited before the
actual SandboxManager.create_session(...) call (a real K8s round trip that
can take tens of seconds) specifically so one slow/stuck pod-create doesn't
serialize sandbox creation for every other account -- only the cheap
count-check-then-reserve step is exclusive.

All limit-exceeded errors are 429s whose message never mentions a dollar
amount or a plan/tier name — see errors.py:LimitExceededError.

Webhooks (docs/WEBHOOKS-DESIGN.md): `create_session`/`destroy_session` are
the single call site for sandbox lifecycle changes regardless of whether
the caller was a normal API request or the reaper's own background
teardown (see reaper.py, which calls `destroy_session` too) -- so firing
`sandbox.created`/`sandbox.destroyed` from here, rather than from
`routers/sandboxes.py`, covers both paths with one hook instead of two.
Firing is fire-and-forget and best-effort, same posture as `AuditSink`: any
exception from `webhooks.enqueue_event` is caught and logged here, never
allowed to fail the sandbox operation that triggered it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from boxkite.capability_policy import assert_policy_invariants, build_session_capability_policy

from .config import settings
from .errors import ApiError, LimitExceededError
from .models_orm import Account, SandboxSession
from .repository import McpConnectionRepository, SandboxSessionRepository, SecretRepository
from .secret_capability import create_capability_token
from .webhooks import enqueue_event

logger = logging.getLogger(__name__)


async def _fire_webhook_event(db, *, account_id: str, event_type: str, data: dict) -> None:
    """Best-effort wrapper around `webhooks.enqueue_event` -- never allowed
    to raise into a sandbox lifecycle call. See this module's docstring for
    why `UsagePolicy` is the single call site for both event types."""
    try:
        await enqueue_event(db, account_id=account_id, event_type=event_type, data=data)
    except Exception as exc:
        logger.error(
            "[UsagePolicy] Failed to enqueue webhook event %s for account %s: %s",
            event_type,
            account_id,
            exc,
        )

# Ordering used to compare a requested sandbox size against
# settings.BOXKITE_MAX_SANDBOX_SIZE -- mirrors SandboxManager.create_session's
# own "small"/"medium"/"large" size presets.
_SANDBOX_SIZE_ORDER = {"small": 0, "medium": 1, "large": 2}

# Serializes the count-check-then-reserve critical section in
# create_session() below -- NOT the whole method. Without this, concurrent
# requests each read the same "below cap" count before any of them commits
# its new row, letting all of them through regardless of
# BOXKITE_MAX_CONCURRENT_SANDBOXES / BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES
# -- a classic TOCTOU race. Module-level (not per-UsagePolicy-instance,
# since one is constructed fresh per request) so it actually serializes
# across concurrent requests in this process.
#
# Deliberately released before the actual SandboxManager.create_session(...)
# K8s call: an earlier version of this fix held the lock across that entire
# call, which (correctly) closed the race but meant one slow/stuck pod
# create serialized sandbox creation for every other account in the
# process, turning a single backend hiccup into a full outage. The
# reserve-then-create pattern (insert a pod_name=None row while the lock is
# held, release, then do the slow call, then fill in pod_name) keeps the
# race closed without that cost.
#
# NOTE(v2), same caveat as rate_limit.py's per-process limiter: this only
# holds within a single control-plane process. A multi-replica deployment
# needs BOXKITE_USAGE_LOCK_BACKEND=postgres (see PostgresSessionLock below)
# for the cap to hold cluster-wide -- "memory" (this lock) silently allows
# limit * replica_count through instead, same failure mode
# BOXKITE_RATE_LIMIT_BACKEND documents for rate limiting.
_create_session_lock = asyncio.Lock()


def reset_create_session_lock_for_tests() -> None:
    """Test-only: an asyncio.Lock binds to whichever event loop first awaits
    it. The app has exactly one persistent event loop for its whole process
    lifetime, so this is a non-issue in production -- but pytest-asyncio's
    default per-test event loop means this module-level lock (created once
    for the whole test session) would otherwise stay bound to a prior test's
    now-closed loop and raise "bound to a different event loop" on the next
    test that awaits it. Mirrors rate_limit.py's reset_rate_limits_for_tests()."""
    global _create_session_lock
    _create_session_lock = asyncio.Lock()


class PostgresSessionLock:
    """Cross-replica mutex for create_session's count-check-then-reserve
    critical section, via Postgres's transaction-scoped advisory lock
    (`pg_advisory_xact_lock`) -- released automatically at commit/rollback
    of whichever transaction acquired it, so there's no explicit
    unlock/leak-handling to get wrong.

    Deliberately acquired on the CALLER'S OWN db session (the same one
    `SandboxSessionRepository` and the count-check queries that follow
    already use), not a separate connection from `get_session_factory()`.
    Postgres advisory locks are scoped to the transaction/connection that
    took them -- acquiring on a different connection than the one running
    the count-checks would mean two independent, unrelated locks that never
    actually serialize against each other. Piggybacking on the request's own
    session means the lock and the checks/reservation-insert that follow
    share one transaction, closing the race for real.

    A single fixed lock key (not one per account) mirrors
    `_create_session_lock`'s own scope exactly: that asyncio.Lock already
    serializes ALL accounts' create_session calls process-wide, not just
    same-account ones, because the global-capacity check needs a
    cluster-wide view regardless of which account is asking. Serializing
    only the fast count-check-then-reserve step (not the slow K8s call that
    follows) keeps this cheap even under real concurrent load.

    Postgres-only: advisory locks aren't part of ANSI SQL and have no SQLite
    equivalent, unlike rate_limit.py's counter-table approach (which works
    on both dialects). Only exercised when
    settings.BOXKITE_USAGE_LOCK_BACKEND == "postgres" -- the default
    "memory" backend never touches this class, so SQLite-backed tests are
    unaffected.
    """

    LOCK_KEY = "boxkite:usage_policy:create_session"

    @staticmethod
    def _advisory_lock_id(key: str) -> int:
        """`pg_advisory_xact_lock` takes a single bigint key -- hash the
        (fixed, human-readable) string key down to a signed 64-bit int."""
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    async def acquire(self, db: AsyncSession) -> None:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": self._advisory_lock_id(self.LOCK_KEY)},
        )


@asynccontextmanager
async def _create_session_critical_section(db: AsyncSession):
    """Serializes create_session's count-check-then-reserve step per
    settings.BOXKITE_USAGE_LOCK_BACKEND -- "memory" (default) uses the
    process-local _create_session_lock; "postgres" uses PostgresSessionLock
    on the same `db` session the checks/reservation-insert run against
    (see that class's docstring for why the same session matters).

    On the postgres path, an exception raised inside the block (e.g.
    LimitExceededError from a failed cap check) triggers an explicit
    rollback here so the advisory lock releases immediately -- matching the
    memory path's tight scope (an asyncio.Lock releases the instant its
    `async with` block exits) rather than staying held until the request's
    db session eventually closes at the end of the request.
    """
    if settings.BOXKITE_USAGE_LOCK_BACKEND == "postgres":
        await PostgresSessionLock().acquire(db)
        try:
            yield
        except Exception:
            await db.rollback()
            raise
    else:
        async with _create_session_lock:
            yield


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _start_of_current_month(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _session_hours(session: SandboxSession, *, now: datetime) -> float:
    """Hours consumed by one session, counting elapsed time for still-active
    sessions rather than waiting for teardown to account for them."""
    end = session.destroyed_at or now
    created_at = session.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    seconds = max(0.0, (end - created_at).total_seconds())
    return seconds / 3600.0


class UsagePolicy:
    """Wraps a SandboxManager with account-scoped fair-use enforcement."""

    def __init__(
        self,
        sandbox_manager,
        sessions: SandboxSessionRepository,
        secrets: SecretRepository | None = None,
        mcp_connections: McpConnectionRepository | None = None,
    ):
        self._manager = sandbox_manager
        self._sessions = sessions
        self._secrets = secrets
        self._mcp_connections = mcp_connections

    async def _resolve_secret_grants(
        self, *, account: Account, session_id: str, secret_names: list[str] | None
    ) -> tuple[list[dict], str | None]:
        """Resolve `secret_names` to (name, allowed_hosts) grants plus a
        session-bound capability token, per docs/SECRETS-DESIGN.md §4.
        Raises `ApiError(404, "secret_not_found", ...)` for any name that
        doesn't resolve for this account -- account-scoped, so this can
        never leak whether a name exists for a DIFFERENT account."""
        if not secret_names:
            return [], None

        if self._secrets is None:
            raise ApiError(
                500,
                "secrets_not_configured",
                "This deployment's UsagePolicy was not wired with a SecretRepository",
            )

        # Resolve names (a client-input error, 404) BEFORE checking whether
        # the operator has wired SECRETS_CONTROL_PLANE_URL (an operator
        # configuration error, 500) -- an unknown secret name should read
        # the same regardless of that deployment's transport config, and a
        # 404 for the caller's own typo shouldn't be masked by an unrelated
        # 500.
        resolved = await self._secrets.resolve_names_for_account(
            account_id=account.id, names=secret_names
        )
        missing = [name for name in secret_names if name not in resolved]
        if missing:
            raise ApiError(
                404,
                "secret_not_found",
                f"Secret(s) not found for this account: {', '.join(sorted(set(missing)))}",
            )

        if not settings.SECRETS_CONTROL_PLANE_URL:
            raise ApiError(
                500,
                "secrets_broker_not_configured",
                "SECRETS_CONTROL_PLANE_URL must be set for this account's operator "
                "before sandbox sessions can be granted secret_names",
            )

        grants = [
            {"name": name, "allowed_hosts": list(resolved[name].allowed_hosts)}
            for name in secret_names
        ]
        token = create_capability_token(
            account_id=account.id, session_id=session_id, secret_names=list(secret_names)
        )
        return grants, token

    async def _resolve_mcp_connection_grants(
        self, *, account: Account, mcp_connection_names: list[str] | None
    ) -> list[dict]:
        """Resolve `mcp_connection_names` to `{"name", "allowed_hosts"}`
        grants -- the exact shape `_resolve_secret_grants` above already
        produces for `secret_names` -- per docs/OUTBOUND-MCP-DESIGN.md §3.
        Raises `ApiError(404, "mcp_connection_not_found", ...)` for any
        name that doesn't resolve for this account -- account-scoped, so
        this can never leak whether a label exists for a DIFFERENT account,
        same precedent as `_resolve_secret_grants`.

        Unlike secrets, there is no capability token and no
        SECRETS_CONTROL_PLANE_URL-style transport requirement here: this
        pass only feeds the resolved catalog host into the session's
        per-pod NetworkPolicy egress allowlist (SandboxManager unions it
        with secret_grants) -- there is no MCP-proxy transport yet for the
        sidecar to use a capability token with (docs/OUTBOUND-MCP-DESIGN.md
        §6)."""
        if not mcp_connection_names:
            return []

        if self._mcp_connections is None:
            raise ApiError(
                500,
                "mcp_connections_not_configured",
                "This deployment's UsagePolicy was not wired with a McpConnectionRepository",
            )

        resolved = await self._mcp_connections.resolve_names_for_account(
            account_id=account.id, names=mcp_connection_names
        )
        missing = [name for name in mcp_connection_names if name not in resolved]
        if missing:
            raise ApiError(
                404,
                "mcp_connection_not_found",
                f"MCP connection(s) not found for this account: {', '.join(sorted(set(missing)))}",
            )

        return [
            {"name": name, "allowed_hosts": [resolved[name].host]}
            for name in mcp_connection_names
        ]

    async def monthly_hours_used(self, account_id: str, *, now: datetime | None = None) -> float:
        now = now or _utcnow()
        window_start = _start_of_current_month(now)
        sessions = await self._sessions.sessions_created_since(account_id=account_id, since=window_start)
        return sum(_session_hours(s, now=now) for s in sessions)

    async def monthly_hours_used_total(self, *, now: datetime | None = None) -> float:
        """Same accounting as monthly_hours_used, but summed across ALL
        accounts -- backs the admin cluster-metrics endpoint's
        `total_monthly_sandbox_hours_used` field (docs/ADMIN-ROLE-DESIGN.md).
        Callers must gate this behind an admin check themselves; this
        method has no notion of authorization on its own."""
        now = now or _utcnow()
        window_start = _start_of_current_month(now)
        sessions = await self._sessions.sessions_created_since_all(since=window_start)
        return sum(_session_hours(s, now=now) for s in sessions)

    async def create_session(
        self,
        account: Account,
        *,
        label: str | None = None,
        size: str = "small",
        storage_gb: float | None = None,
        lifetime_minutes: int | None = None,
        session_id: str | None = None,
        restore_from_snapshot_id: str | None = None,
        secret_names: list[str] | None = None,
        image_ref: str | None = None,
        volume_mounts: list[dict] | None = None,
        mcp_connection_names: list[str] | None = None,
        gpu_count: int | None = None,
    ) -> tuple[SandboxSession, dict]:
        # Validated before ever acquiring _create_session_lock -- these are
        # cheap, purely config-driven checks that don't need the
        # count-check-then-reserve critical section below.
        requested_rank = _SANDBOX_SIZE_ORDER.get(size)
        max_rank = _SANDBOX_SIZE_ORDER.get(settings.BOXKITE_MAX_SANDBOX_SIZE)
        if requested_rank is not None and max_rank is not None and requested_rank > max_rank:
            raise LimitExceededError(
                code="sandbox_size_limit_reached",
                message=(
                    "Requested sandbox size exceeds this account's maximum "
                    "allowed size."
                ),
                details={"requested_size": size, "max_size": settings.BOXKITE_MAX_SANDBOX_SIZE},
            )

        if storage_gb is not None and storage_gb > settings.BOXKITE_MAX_SANDBOX_STORAGE_GB:
            raise LimitExceededError(
                code="sandbox_storage_limit_reached",
                message=(
                    "Requested sandbox storage exceeds this account's maximum "
                    "allowed storage."
                ),
                details={"requested_storage_gb": storage_gb, "max_storage_gb": settings.BOXKITE_MAX_SANDBOX_STORAGE_GB},
            )

        # Holds ONLY for the count-check-then-reserve step -- see
        # _create_session_critical_section's docstring (and, for the
        # "memory" backend, _create_session_lock's own module-level
        # docstring) for the race this closes. Deliberately does NOT hold
        # across the SandboxManager call
        # below: that's a real K8s pod-create/warm-pool-claim round trip
        # that can take tens of seconds on a cold pod, and holding a single
        # process-wide lock across it would let one slow/stuck request
        # serialize sandbox creation for every other account too -- turning
        # a single backend hiccup into a full-process outage. Instead, the
        # bookkeeping row is inserted with pod_name=None as an immediate
        # reservation (count_active_total/count_active_for_account count it
        # right away, closing the race) and released before the slow call.
        # `session_id` override exists ONLY for snapshot restore
        # (routers/snapshots.py): a restore must copy the snapshot's storage
        # objects into the new session's own live storage_prefix BEFORE
        # SandboxManager.create_session's /configure call runs its prefetch,
        # so the caller needs to know (and control) the session_id ahead of
        # time rather than discovering it from this method's return value.
        session_id = session_id or str(uuid4())

        # Resolved before entering _create_session_critical_section -- a
        # 404 for an unknown secret name is a client-input error, not a
        # capacity check, and doesn't need the count-check-then-reserve
        # critical section below.
        secret_grants, secret_capability_token = await self._resolve_secret_grants(
            account=account, session_id=session_id, secret_names=secret_names
        )
        mcp_connection_grants = await self._resolve_mcp_connection_grants(
            account=account, mcp_connection_names=mcp_connection_names
        )

        # Observability + invariant-checking only (issue #155, phase 1) --
        # account.custom_allowed_commands is read live, per-request, by the
        # actual enforcement call sites (routers/sandboxes.py,
        # hosted_mcp.py) and can change mid-session; this snapshot is NOT
        # a substitute for those live reads. See
        # docs/UNIFIED-CAPABILITY-POLICY-SCOPING.md for why enforcement
        # itself isn't unified onto this object yet.
        policy = build_session_capability_policy(
            account_id=account.id,
            session_id=session_id,
            allowed_commands=account.custom_allowed_commands,
            secret_grants=secret_grants,
            mcp_connection_grants=mcp_connection_grants,
            secret_capability_token=secret_capability_token,
        )
        try:
            assert_policy_invariants(policy)
        except ValueError:
            # Phase 1 is observability/invariant-checking only (see the
            # module docstring on boxkite.capability_policy) -- it must
            # not change what create_session actually allows. A secret
            # and an MCP connection are only unique per-account within
            # their own tables (models_orm.py), so nothing stops the two
            # from sharing a name; that's a real, reachable account
            # configuration today, not malicious input, so log-and-continue
            # rather than hard-failing a request that used to succeed.
            logger.warning(
                "capability_policy invariant violation: account=%s session=%s",
                account.id,
                session_id,
                exc_info=True,
            )
        else:
            logger.info(
                "capability_policy constructed: account=%s session=%s exec_rules=%s network_grants=%d",
                account.id,
                session_id,
                len(policy.exec.rules) if policy.exec.rules is not None else "unrestricted",
                len(policy.network_grants),
            )

        async with _create_session_critical_section(self._sessions.db):
            now = _utcnow()

            global_active_count = await self._sessions.count_active_total()
            if global_active_count >= settings.BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES:
                raise LimitExceededError(
                    code="global_capacity_reached",
                    message="All sandbox capacity is in use right now. Please try again shortly.",
                    details={
                        "limit": settings.BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES,
                        "active": global_active_count,
                    },
                )

            active_count = await self._sessions.count_active_for_account(account.id)
            if active_count >= settings.BOXKITE_MAX_CONCURRENT_SANDBOXES:
                raise LimitExceededError(
                    code="concurrent_sandbox_limit_reached",
                    message=(
                        "Concurrent sandbox usage limit reached "
                        f"({settings.BOXKITE_MAX_CONCURRENT_SANDBOXES} at a time). "
                        "Destroy an existing sandbox session before creating another."
                    ),
                    details={"limit": settings.BOXKITE_MAX_CONCURRENT_SANDBOXES, "active": active_count},
                )

            hours_used = await self.monthly_hours_used(account.id, now=now)
            if hours_used >= settings.BOXKITE_FREE_MONTHLY_SANDBOX_HOURS:
                raise LimitExceededError(
                    code="monthly_usage_limit_reached",
                    message="Monthly usage limit reached. Try again next calendar month.",
                    details={
                        "limit_hours": settings.BOXKITE_FREE_MONTHLY_SANDBOX_HOURS,
                        "used_hours": round(hours_used, 4),
                    },
                )

            # Reserve the slot now, while the lock is still held, so the
            # counts above are correct for the next concurrent caller as
            # soon as this transaction commits -- the row exists (and counts
            # as active) well before any pod does.
            row = await self._sessions.create(
                session_id=session_id, account_id=account.id, pod_name=None, label=label
            )

        # Lock released. NOTE (judgment call, see the top-level report):
        # SandboxManager's `create_session` scopes S3/storage paths by
        # `organization_id`, not by any control-plane-specific concept --
        # there's no first-class multi-tenant "account" in SandboxManager's
        # own API. We pass this account's id as organization_id, which is
        # exactly the scoping boundary SandboxManager already uses to keep
        # storage prefixes (and thus one tenant's files) isolated from
        # another's, so no changes to SandboxManager were needed for this
        # to be safe.
        try:
            result = await self._manager.create_session(
                organization_id=UUID(account.id),
                session_id=session_id,
                size=size,
                storage_gb=storage_gb,
                lifetime_seconds=(lifetime_minutes * 60 if lifetime_minutes else None),
                restore_from_snapshot_id=restore_from_snapshot_id,
                secret_grants=secret_grants or None,
                secret_capability_token=secret_capability_token,
                secrets_control_plane_url=settings.SECRETS_CONTROL_PLANE_URL or None,
                image_ref=image_ref,
                volume_mounts=volume_mounts,
                mcp_connection_grants=mcp_connection_grants or None,
                gpu_count=gpu_count,
            )
        except Exception:
            # The reservation row exists but no pod was ever created for it;
            # without cleaning it up here it would sit "active" forever,
            # consuming a concurrency-limit slot the reaper can never free
            # (it only tears down pods, and there's no pod to tear down).
            await self._sessions.delete_row(session_id)
            raise
        pod_name = result.get("pod_name") if isinstance(result, dict) else None

        try:
            await self._sessions.set_pod_name(session_id, pod_name)
        except Exception:
            # The pod exists but its bookkeeping row was never updated to
            # reflect that; the reaper only ever acts off rows, so without
            # this the pod would leak forever (invisible to cleanup and to
            # the capacity count).
            try:
                await self._manager.destroy_session(session_id)
            except Exception as cleanup_exc:
                logger.warning(
                    "[UsagePolicy] failed to tear down orphaned pod %s after row-update error: %s",
                    session_id,
                    cleanup_exc,
                )
            raise
        row.pod_name = pod_name

        # Fired AFTER the row's pod_name is durably set -- a "sandbox.created"
        # webhook should only ever describe a session that's actually usable,
        # never one whose create call is still in flight or failed partway.
        # See this module's docstring for why UsagePolicy (not
        # routers/sandboxes.py) is the single call site for this event.
        await _fire_webhook_event(
            self._sessions.db,
            account_id=account.id,
            event_type="sandbox.created",
            data={
                "session_id": session_id,
                "label": label,
                "pod_name": pod_name,
                "size": size,
            },
        )
        return row, result

    async def destroy_session(self, row: SandboxSession, *, reason: str = "caller_requested") -> None:
        """Tear down via SandboxManager and record final usage.

        Takes an already-fetched `SandboxSession` row rather than a bare
        `session_id` *specifically* so ownership is checked once, by the
        caller, via `SandboxSessionRepository.get_for_account` (see
        routers/sandboxes.py) — this method has no way to be called with a
        session_id alone, which makes "forgot to check ownership before
        destroying" structurally impossible rather than a convention to
        remember.
        """
        now = _utcnow()
        try:
            await self._manager.destroy_session(row.id)
        except Exception as e:
            logger.warning("[UsagePolicy] SandboxManager.destroy_session failed for %s: %s", row.id, e)
            # Still record the teardown attempt so usage accounting doesn't
            # count a session as open forever if the pod is already gone.
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        duration_seconds = max(0.0, (now - created_at).total_seconds())
        await self._sessions.mark_destroyed(
            session_id=row.id, duration_seconds=duration_seconds, reason=reason
        )

        # Fired regardless of whether SandboxManager.destroy_session above
        # succeeded or merely logged a warning -- from the account's
        # perspective the session IS gone either way (mark_destroyed already
        # ran), and this single call site covers both caller-initiated
        # DELETE and the reaper's own forced teardown (reaper.py calls this
        # same method) -- see this module's docstring.
        await _fire_webhook_event(
            self._sessions.db,
            account_id=row.account_id,
            event_type="sandbox.destroyed",
            data={
                "session_id": row.id,
                "label": row.label,
                "duration_seconds": round(duration_seconds, 3),
                "reason": reason,
            },
        )
