"""Tests for opt-in adaptive warm-pool sizing (issue #156,
docs/ADAPTIVE-WARM-POOL-SIZING.md).

Covers the rolling claim-rate tracker itself, the floor/ceiling clamping
in compute_adaptive_target, and that BOXKITE_ADAPTIVE_WARM_POOL_ENABLED
being off (the default) is a true no-op -- byte-identical to today's
static WARM_POOL_SIZE_TARGETS behavior. Does NOT (and cannot, in this
environment) validate the chosen algorithm or its default constants
against real production claim-rate logs -- see warm_pool_sizing.py's own
module docstring for that disclosure.
"""

import pytest

import boxkite.warm_pool as warm_pool_module
from boxkite.warm_pool import WarmPoolManager
from boxkite.warm_pool_sizing import (
    BOXKITE_ADAPTIVE_WARM_POOL_COVERAGE_SECONDS_ENV,
    BOXKITE_ADAPTIVE_WARM_POOL_ENABLED_ENV,
    BOXKITE_ADAPTIVE_WARM_POOL_WINDOW_SECONDS_ENV,
    ClaimRateTracker,
    adaptive_warm_pool_coverage_seconds,
    adaptive_warm_pool_enabled,
    adaptive_warm_pool_window_seconds,
    compute_adaptive_target,
    resolve_warm_pool_size_targets,
)


@pytest.fixture(autouse=True)
def _clean_adaptive_warm_pool_env(monkeypatch):
    monkeypatch.delenv(BOXKITE_ADAPTIVE_WARM_POOL_ENABLED_ENV, raising=False)
    monkeypatch.delenv(BOXKITE_ADAPTIVE_WARM_POOL_WINDOW_SECONDS_ENV, raising=False)
    monkeypatch.delenv(BOXKITE_ADAPTIVE_WARM_POOL_COVERAGE_SECONDS_ENV, raising=False)


# ── env accessors ────────────────────────────────────────────────────────


def test_adaptive_warm_pool_disabled_by_default():
    assert adaptive_warm_pool_enabled() is False


def test_adaptive_warm_pool_enabled_when_flag_set(monkeypatch):
    monkeypatch.setenv(BOXKITE_ADAPTIVE_WARM_POOL_ENABLED_ENV, "true")
    assert adaptive_warm_pool_enabled() is True


def test_adaptive_warm_pool_window_seconds_default_is_300():
    assert adaptive_warm_pool_window_seconds() == 300


def test_adaptive_warm_pool_window_seconds_operator_configurable(monkeypatch):
    monkeypatch.setenv(BOXKITE_ADAPTIVE_WARM_POOL_WINDOW_SECONDS_ENV, "120")
    assert adaptive_warm_pool_window_seconds() == 120


def test_adaptive_warm_pool_coverage_seconds_default_is_60():
    assert adaptive_warm_pool_coverage_seconds() == 60


def test_adaptive_warm_pool_coverage_seconds_operator_configurable(monkeypatch):
    monkeypatch.setenv(BOXKITE_ADAPTIVE_WARM_POOL_COVERAGE_SECONDS_ENV, "30")
    assert adaptive_warm_pool_coverage_seconds() == 30


# ── ClaimRateTracker: rolling-window rate estimation ────────────────────


def test_claim_rate_tracker_reports_zero_with_no_claims():
    tracker = ClaimRateTracker(window_seconds=100)
    assert tracker.claim_rate_per_second("small", now=0.0) == 0.0


def test_claim_rate_tracker_computes_rate_over_fixed_window():
    # 10 claims spread across a 100s window => 0.1 claims/sec.
    tracker = ClaimRateTracker(window_seconds=100)
    for i in range(10):
        tracker.record_claim("small", now=float(i * 10))
    assert tracker.claim_rate_per_second("small", now=99.0) == pytest.approx(0.1)


def test_claim_rate_tracker_prunes_claims_older_than_window():
    tracker = ClaimRateTracker(window_seconds=10)
    tracker.record_claim("small", now=0.0)
    tracker.record_claim("small", now=1.0)
    # By t=50, both original claims are long outside the 10s window.
    assert tracker.claim_rate_per_second("small", now=50.0) == 0.0


def test_claim_rate_tracker_keeps_claims_still_inside_window():
    tracker = ClaimRateTracker(window_seconds=10)
    tracker.record_claim("small", now=0.0)
    tracker.record_claim("small", now=5.0)
    tracker.record_claim("small", now=9.0)
    # At now=9.0, cutoff is -1.0, so all 3 claims are still counted.
    assert tracker.claim_rate_per_second("small", now=9.0) == pytest.approx(3 / 10)


def test_claim_rate_tracker_tracks_size_classes_independently():
    tracker = ClaimRateTracker(window_seconds=10)
    for i in range(5):
        tracker.record_claim("small", now=float(i))
    tracker.record_claim("large", now=0.0)
    assert tracker.claim_rate_per_second("small", now=5.0) == pytest.approx(5 / 10)
    assert tracker.claim_rate_per_second("large", now=5.0) == pytest.approx(1 / 10)
    assert tracker.claim_rate_per_second("medium", now=5.0) == 0.0


def test_claim_rate_tracker_zero_window_never_divides_by_zero():
    tracker = ClaimRateTracker(window_seconds=0)
    tracker.record_claim("small", now=0.0)
    assert tracker.claim_rate_per_second("small", now=0.0) == 0.0


# ── compute_adaptive_target: floor/ceiling clamping ─────────────────────


def test_compute_adaptive_target_covers_observed_rate_within_bounds():
    # 1 claim/sec, 60s coverage => target 60, well within [0, 100].
    assert compute_adaptive_target(rate_per_second=1.0, floor=0, ceiling=100, coverage_seconds=60) == 60


def test_compute_adaptive_target_rounds_up_fractional_targets():
    # (1/40) claims/sec * 60s coverage = 1.5 -> rounds up to 2, never down
    # (better to have one spare pod than to be short when the rate ticks up).
    assert compute_adaptive_target(rate_per_second=1 / 40, floor=0, ceiling=100, coverage_seconds=60) == 2


def test_compute_adaptive_target_never_goes_below_floor():
    # Zero claim rate would naively size to 0, but the floor wins.
    assert compute_adaptive_target(rate_per_second=0.0, floor=3, ceiling=15, coverage_seconds=60) == 3


def test_compute_adaptive_target_never_goes_above_ceiling():
    # A huge burst rate would naively size far past any reasonable pool.
    assert compute_adaptive_target(rate_per_second=10.0, floor=0, ceiling=15, coverage_seconds=60) == 15


@pytest.mark.parametrize("rate_per_second", [0.0, 0.01, 0.5, 1.0, 5.0, 50.0, 1000.0])
def test_compute_adaptive_target_always_within_configured_bounds(rate_per_second):
    floor, ceiling = 3, 15
    target = compute_adaptive_target(rate_per_second, floor=floor, ceiling=ceiling, coverage_seconds=60)
    assert floor <= target <= ceiling


# ── resolve_warm_pool_size_targets: opt-in no-op behavior ───────────────


def test_resolve_targets_is_byte_identical_to_static_when_flag_off():
    static_targets = {"small": 3, "medium": 0, "large": 0}
    tracker = ClaimRateTracker(window_seconds=100)
    # Even with a huge recorded claim rate, the disabled flag must ignore it.
    for i in range(50):
        tracker.record_claim("small", now=float(i))
    result = resolve_warm_pool_size_targets(static_targets, ceiling=15, tracker=tracker, now=50.0)
    assert result == static_targets
    assert result is not static_targets  # copied, not the same object


def test_resolve_targets_computes_adaptive_values_when_enabled(monkeypatch):
    monkeypatch.setenv(BOXKITE_ADAPTIVE_WARM_POOL_ENABLED_ENV, "true")
    monkeypatch.setenv(BOXKITE_ADAPTIVE_WARM_POOL_COVERAGE_SECONDS_ENV, "60")
    static_targets = {"small": 3, "medium": 0, "large": 0}
    tracker = ClaimRateTracker(window_seconds=100)
    # 1 claim/sec of "small" claims => adaptive target 60, clamped to ceiling 15.
    for i in range(100):
        tracker.record_claim("small", now=float(i))
    result = resolve_warm_pool_size_targets(static_targets, ceiling=15, tracker=tracker, now=99.0)
    assert result["small"] == 15  # clamped to ceiling
    assert result["medium"] == 0  # no claims, floor stays 0
    assert result["large"] == 0


def test_resolve_targets_never_shrinks_below_static_floor_when_enabled(monkeypatch):
    monkeypatch.setenv(BOXKITE_ADAPTIVE_WARM_POOL_ENABLED_ENV, "true")
    static_targets = {"small": 3, "medium": 2, "large": 0}
    tracker = ClaimRateTracker(window_seconds=100)  # no claims recorded at all
    result = resolve_warm_pool_size_targets(static_targets, ceiling=15, tracker=tracker, now=0.0)
    assert result == {"small": 3, "medium": 2, "large": 0}


# ── WarmPoolManager integration: opt-in wiring ──────────────────────────


@pytest.fixture
def _isolated_claim_rate_tracker(monkeypatch):
    """Swap in a fresh tracker so this test's claims can't leak into (or be
    polluted by) any other test sharing the process-global singleton."""
    tracker = ClaimRateTracker(window_seconds=100)
    monkeypatch.setattr(warm_pool_module, "CLAIM_RATE_TRACKER", tracker)
    return tracker


def test_manager_targets_are_static_by_default(_isolated_claim_rate_tracker):
    manager = WarmPoolManager()
    # _current_warm_pool_size_targets() uses the real clock (no `now`
    # override), so claims are recorded via record_claim's own
    # time.monotonic() default -- this asserts they're still ignored
    # entirely while the flag is off, regardless of how they were timed.
    for _ in range(20):
        _isolated_claim_rate_tracker.record_claim("small")
    assert manager._current_warm_pool_size_targets() == warm_pool_module.WARM_POOL_SIZE_TARGETS


def test_manager_targets_adapt_when_flag_enabled(monkeypatch, _isolated_claim_rate_tracker):
    monkeypatch.setenv(BOXKITE_ADAPTIVE_WARM_POOL_ENABLED_ENV, "true")
    manager = WarmPoolManager()
    # 200 claims recorded back-to-back (same real-clock instant, for
    # practical purposes) is an extreme burst rate within the 100s window
    # -- must be clamped to WARM_POOL_MAX, never left to grow unbounded.
    for _ in range(200):
        _isolated_claim_rate_tracker.record_claim("small")
    targets = manager._current_warm_pool_size_targets()
    assert targets["small"] == warm_pool_module.WARM_POOL_MAX
    assert targets["small"] <= warm_pool_module.WARM_POOL_MAX
