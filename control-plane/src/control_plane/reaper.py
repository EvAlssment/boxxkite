"""Background task enforcing BOXKITE_MAX_SESSION_MINUTES server-side,
independent of whether the caller ever calls DELETE /v1/sandboxes/{id}.

Without this, the per-session cap would only be a number shown to the
caller (via `expires_at` in the API response) with nothing actually
stopping a session from running indefinitely. Runs as an asyncio background
task started from main.py's lifespan, scanning for active sessions older
than the cap on a fixed interval (BOXKITE_SESSION_REAPER_INTERVAL_SECONDS).

Also reaps the public demo playground's sessions (issue #103,
demo_account.py) on their own, much shorter BOXKITE_DEMO_LIFETIME_MINUTES
cutoff, in addition to the global BOXKITE_MAX_SESSION_MINUTES cutoff every
other account gets above -- a demo session's pod is already killed by its
own K8s activeDeadlineSeconds at BOXKITE_DEMO_LIFETIME_MINUTES, but nothing
else would mark its `sandbox_sessions` row destroyed until the much longer
global cutoff elapses, silently holding a BOXKITE_DEMO_MAX_CONCURRENT slot
for a pod that's already gone.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .config import settings
from .db import get_session_factory
from .demo_account import DEMO_ACCOUNT_EMAIL
from .repository import AccountRepository, SandboxSessionRepository
from .usage_policy import UsagePolicy

logger = logging.getLogger(__name__)


async def _reap_once(manager) -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.BOXKITE_MAX_SESSION_MINUTES)

    session_factory = get_session_factory()
    async with session_factory() as db:
        sessions_repo = SandboxSessionRepository(db)
        expired = await sessions_repo.list_active_older_than(cutoff=cutoff)
        expired_reasons = {row.id: "max_session_minutes_exceeded" for row in expired}

        if settings.BOXKITE_DEMO_PLAYGROUND_ENABLED:
            demo_account = await AccountRepository(db).get_by_email(DEMO_ACCOUNT_EMAIL)
            if demo_account is not None:
                demo_cutoff = now - timedelta(minutes=settings.BOXKITE_DEMO_LIFETIME_MINUTES)
                demo_expired = await sessions_repo.list_active_older_than_for_account(
                    account_id=demo_account.id, cutoff=demo_cutoff
                )
                for row in demo_expired:
                    if row.id not in expired_reasons:
                        expired.append(row)
                    expired_reasons[row.id] = "demo_lifetime_exceeded"

        if not expired:
            return
        policy = UsagePolicy(manager, sessions_repo)
        for row in expired:
            reason = expired_reasons[row.id]
            logger.info(
                "[reaper] Destroying session %s (account=%s) — %s",
                row.id,
                row.account_id,
                reason,
            )
            try:
                await policy.destroy_session(row, reason=reason)
            except Exception as e:
                logger.error("[reaper] Failed to destroy session %s: %s", row.id, e)


async def run_reaper_loop(manager, *, stop_event: asyncio.Event) -> None:
    interval = settings.BOXKITE_SESSION_REAPER_INTERVAL_SECONDS
    while not stop_event.is_set():
        try:
            await _reap_once(manager)
        except Exception as e:
            logger.error("[reaper] Unexpected error during reap cycle: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
