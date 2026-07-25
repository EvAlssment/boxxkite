"""Opt-in adaptive warm-pool sizing (issue #156): a rolling per-size-class
claim-rate signal that can drive each warm sub-pool's target size, instead
of the fixed WARM_POOL_SIZE_SMALL/MEDIUM/LARGE constants alone.

STATUS: implemented and unit-tested against synthetic claim sequences
(tests/test_warm_pool_sizing.py), NOT validated against real production
claim-rate logs -- issue #156 itself says this is "worth prototyping
against real claim-rate logs... before committing to a specific
algorithm." This ships a real, working mechanism with a reasonable
default algorithm (recent claims-per-second times a configurable
coverage window), not a claim that this algorithm or its default
constants are optimal or final. See docs/ADAPTIVE-WARM-POOL-SIZING.md.

Off by default (BOXKITE_ADAPTIVE_WARM_POOL_ENABLED=false): WarmPoolManager
uses WARM_POOL_SIZE_TARGETS exactly as before, unchanged. When enabled, the
existing WARM_POOL_SIZE_SMALL/MEDIUM/LARGE constants become each size's
FLOOR (never size below what the operator explicitly configured) and the
existing WARM_POOL_MAX becomes the shared CEILING (never size above the
pool's overall active-pod budget) -- an operator retains ultimate control
either way, this can't scale without bound.
"""

import os
import time
from collections import deque
from math import ceil
from typing import Optional

BOXKITE_ADAPTIVE_WARM_POOL_ENABLED_ENV = "BOXKITE_ADAPTIVE_WARM_POOL_ENABLED"
BOXKITE_ADAPTIVE_WARM_POOL_WINDOW_SECONDS_ENV = "BOXKITE_ADAPTIVE_WARM_POOL_WINDOW_SECONDS"
BOXKITE_ADAPTIVE_WARM_POOL_COVERAGE_SECONDS_ENV = "BOXKITE_ADAPTIVE_WARM_POOL_COVERAGE_SECONDS"
DEFAULT_ADAPTIVE_WARM_POOL_WINDOW_SECONDS = "300"
DEFAULT_ADAPTIVE_WARM_POOL_COVERAGE_SECONDS = "60"


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: str) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return int(default)
    return int(value.strip())


def adaptive_warm_pool_enabled() -> bool:
    """Opt-in flag, default off. Read fresh on every call (not cached at
    import time) so tests can monkeypatch it and an operator's env change
    takes effect without restarting the process importing this module a
    second time."""
    return _env_flag(BOXKITE_ADAPTIVE_WARM_POOL_ENABLED_ENV, "false")


def adaptive_warm_pool_window_seconds() -> int:
    """Trailing window (seconds) the rolling claim-rate estimate covers."""
    return _env_int(
        BOXKITE_ADAPTIVE_WARM_POOL_WINDOW_SECONDS_ENV,
        DEFAULT_ADAPTIVE_WARM_POOL_WINDOW_SECONDS,
    )


def adaptive_warm_pool_coverage_seconds() -> int:
    """How many seconds of claims, at the observed rate, the adaptive
    target aims to keep pre-warmed and ready to claim."""
    return _env_int(
        BOXKITE_ADAPTIVE_WARM_POOL_COVERAGE_SECONDS_ENV,
        DEFAULT_ADAPTIVE_WARM_POOL_COVERAGE_SECONDS,
    )


class ClaimRateTracker:
    """Per-size-class rolling window of warm-pod claim timestamps.

    Deliberately simple per issue #156's ask: a deque of monotonic
    timestamps per size class, pruned to the configured window on every
    read/write. In-process only -- no Redis or other external store, and
    NOT shared across multiple control-plane replicas. Each replica sizes
    its own view of recent claims independently; a replica-aggregated rate
    estimate (e.g. via Redis or the database) is future work if a
    per-replica signal proves too noisy in practice.
    """

    def __init__(self, window_seconds: Optional[int] = None):
        # None (the normal case) => read adaptive_warm_pool_window_seconds()
        # fresh on every prune/read, so an operator's env var change takes
        # effect immediately. A fixed override is mainly useful for tests
        # that want a deterministic window without touching env vars.
        self._fixed_window_seconds = window_seconds
        self._claims: dict[str, deque] = {}

    def _window_seconds(self) -> int:
        if self._fixed_window_seconds is not None:
            return self._fixed_window_seconds
        return adaptive_warm_pool_window_seconds()

    def record_claim(self, size: str, now: Optional[float] = None) -> None:
        """Record a single warm-pod claim event for `size` at `now`
        (defaults to time.monotonic())."""
        now = time.monotonic() if now is None else now
        bucket = self._claims.setdefault(size, deque())
        bucket.append(now)
        self._prune(size, now)

    def _prune(self, size: str, now: float) -> None:
        bucket = self._claims.get(size)
        if not bucket:
            return
        cutoff = now - self._window_seconds()
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def claim_rate_per_second(self, size: str, now: Optional[float] = None) -> float:
        """Claims/second for `size`, averaged over the trailing window.

        NOTE: divides by the *configured* window length, not by however
        much wall-clock time has actually elapsed since this tracker (or
        the process) started. During the first `window_seconds` of a
        fresh process this deliberately UNDER-estimates the true rate --
        fewer seconds have really passed than the fixed denominator
        assumes. That's an intentional, safe-direction simplification: an
        under-estimate can only push the computed target down towards the
        floor (see compute_adaptive_target), never below the
        operator-configured floor, and the estimate self-corrects once the
        window has fully filled with real data.
        """
        now = time.monotonic() if now is None else now
        self._prune(size, now)
        bucket = self._claims.get(size)
        if not bucket:
            return 0.0
        window = self._window_seconds()
        if window <= 0:
            return 0.0
        return len(bucket) / window


def compute_adaptive_target(rate_per_second: float, floor: int, ceiling: int, coverage_seconds: int) -> int:
    """Target pool size: enough pods to cover `coverage_seconds` of claims
    at `rate_per_second`, clamped to `[floor, ceiling]`.

    `floor` and `ceiling` are the operator's EXISTING static env-var
    constants (WARM_POOL_SIZE_SMALL/MEDIUM/LARGE as floor, WARM_POOL_MAX as
    ceiling) -- this can never size a sub-pool below what the operator
    explicitly configured, nor above the shared active-pod ceiling,
    regardless of observed claim rate. Assumes `floor <= ceiling`; a
    misconfigured `floor > ceiling` resolves to `ceiling` (the `min` is
    applied last).
    """
    raw_target = ceil(rate_per_second * coverage_seconds)
    return max(floor, min(ceiling, raw_target))


def resolve_warm_pool_size_targets(
    static_targets: dict, ceiling: int, tracker: ClaimRateTracker, *, now: Optional[float] = None
) -> dict:
    """The single integration point between the opt-in adaptive sizer and
    WarmPoolManager's existing static WARM_POOL_SIZE_TARGETS.

    Returns `static_targets` (copied, unmodified) when
    BOXKITE_ADAPTIVE_WARM_POOL_ENABLED is off -- the default -- so callers
    get byte-identical values to pre-adaptive-sizing behavior. Only
    computes adaptive per-size targets when the flag is on, treating each
    entry in `static_targets` as that size's floor and `ceiling` as the
    shared ceiling for all sizes.
    """
    if not adaptive_warm_pool_enabled():
        return dict(static_targets)
    coverage_seconds = adaptive_warm_pool_coverage_seconds()
    return {
        size: compute_adaptive_target(
            tracker.claim_rate_per_second(size, now=now),
            floor=floor,
            ceiling=ceiling,
            coverage_seconds=coverage_seconds,
        )
        for size, floor in static_targets.items()
    }


# Process-global tracker: WarmPoolManager (warm_pool.py, which reconciles
# the pool) and SandboxManager (_manager_warm_pool.py's
# _claim_warm_pod_via_k8s, the actual production claim path -- see that
# method's own docstring for why it exists separately from
# WarmPoolManager.claim_pod) run in the same control-plane process and both
# need to observe the SAME claim events, so this is a module-level
# singleton rather than an attribute on either class.
CLAIM_RATE_TRACKER = ClaimRateTracker()
