"""
Shared helpers for warm pod claim age policy.

K8s pods have an `activeDeadlineSeconds` backstop (default 24h) that hard-kills
the pod regardless of activity.  If the warm pool claims a pod that's already
23h59m old, any tool call would fail within seconds.

This module provides the age-gating logic used by both SandboxManager (at claim
time) and WarmPoolManager (during pool scans) to reject pods that are too close
to their deadline.

See also: docs/architecture/sandbox-pod-lifecycle.md
"""

from datetime import datetime, timezone
from typing import Optional


def compute_max_claimable_age_seconds(
    active_deadline_seconds: int,
    claim_age_buffer_seconds: int,
    min_remaining_lifetime_seconds: int,
) -> int:
    """Return the maximum pod age (in seconds) that is still safe to claim.

    The result guarantees at least ``min_remaining_lifetime_seconds`` of
    usable time before the K8s activeDeadlineSeconds backstop kills the pod.

    ``claim_age_buffer_seconds`` is a softer preference — "don't claim pods
    older than (deadline - buffer)" — but it's clamped so it never violates
    the hard ``min_remaining_lifetime_seconds`` floor.

    Example with defaults (deadline=86400, buffer=3600, min_remaining=60):
        max_claimable_age = 86400 - 3600 = 82800s  (23h)
        → pods older than 23h are skipped.
    """
    claim_buffer = max(0, claim_age_buffer_seconds)
    min_remaining = max(1, min_remaining_lifetime_seconds)
    deadline = max(1, active_deadline_seconds)
    # Ensure the buffer doesn't consume the entire deadline, leaving zero
    # usable time.  Cap it so at least min_remaining seconds remain.
    max_buffer_without_zero_window = max(0, deadline - min_remaining)
    effective_buffer = min(claim_buffer, max_buffer_without_zero_window)
    return max(1, deadline - effective_buffer)


def pod_age_seconds(creation_timestamp: object) -> Optional[float]:
    """Return seconds since the pod was created, or None if unparseable.

    Tolerates ``datetime`` objects (from kubernetes_asyncio), ISO-8601 strings,
    and naive timestamps (assumed UTC).  Returns None on any parse failure so
    callers can fall back to a safe default.
    """
    if creation_timestamp is None:
        return None
    try:
        if isinstance(creation_timestamp, datetime):
            created_at = creation_timestamp
        elif isinstance(creation_timestamp, str):
            created_at = datetime.fromisoformat(creation_timestamp.replace("Z", "+00:00"))
        else:
            return None
        # Treat naive timestamps as UTC (K8s API always returns UTC).
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds())
    except Exception:
        return None
