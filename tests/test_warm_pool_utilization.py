from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import boxxkite.manager as manager_module
import boxxkite.warm_pool as warm_pool_module
import boxxkite.warm_pool_sizing as warm_pool_sizing
from boxxkite.manager import SandboxManager
from boxxkite.warm_pool import WarmPoolManager
from boxxkite.warm_pool_sizing import ClaimRateTracker


@pytest.mark.asyncio
async def test_status_reports_live_counts_and_rolling_demand_by_size(monkeypatch):
    tracker = ClaimRateTracker(window_seconds=100)
    tracker.record_claim("small", now=10.0)
    tracker.record_claim("small", now=20.0)
    tracker.record_cold_fallthrough("small", now=30.0)
    tracker.record_cold_fallthrough("large", now=40.0)
    monkeypatch.setattr(warm_pool_sizing.time, "monotonic", lambda: 50.0)
    monkeypatch.setattr(warm_pool_module, "CLAIM_RATE_TRACKER", tracker)
    monkeypatch.setattr(
        warm_pool_module,
        "WARM_POOL_SIZE_TARGETS",
        {"small": 3, "medium": 2, "large": 1},
    )
    monkeypatch.setattr(warm_pool_module, "WARM_POOL_MAX", 6)

    manager = WarmPoolManager()

    async def _counts():
        return ({"small": 2, "medium": 0, "large": 1}, 4, {"warm": 3, "claimed": 1})

    monkeypatch.setattr(manager, "_get_pool_counts", _counts)

    status = await manager.get_status()

    assert status["utilization_window_seconds"] == 100
    assert status["utilization_by_size"] == {
        "small": {
            "target": 3,
            "actual_warm_count": 2,
            "claims_last_window": 2,
            "cold_fallthroughs_last_window": 1,
        },
        "medium": {
            "target": 2,
            "actual_warm_count": 0,
            "claims_last_window": 0,
            "cold_fallthroughs_last_window": 0,
        },
        "large": {
            "target": 1,
            "actual_warm_count": 1,
            "claims_last_window": 0,
            "cold_fallthroughs_last_window": 1,
        },
    }


@pytest.mark.asyncio
async def test_status_does_not_return_zero_counts_when_kubernetes_scan_fails():
    manager = WarmPoolManager()
    manager._k8s_core_api = SimpleNamespace(
        list_namespaced_pod=AsyncMock(side_effect=RuntimeError("api unavailable"))
    )

    with pytest.raises(RuntimeError, match="status scan is unavailable"):
        await manager.get_status()


@pytest.mark.asyncio
async def test_normal_claim_miss_records_one_cold_fallthrough(monkeypatch):
    tracker = ClaimRateTracker(window_seconds=100)
    monkeypatch.setattr(manager_module, "CLAIM_RATE_TRACKER", tracker)
    manager = SandboxManager()
    manager._init_k8s = AsyncMock()
    manager._claim_warm_pod_via_k8s = AsyncMock(return_value=None)
    manager._create_pod = AsyncMock(return_value="10.8.0.42")
    manager._get_http_client = lambda *_args, **_kwargs: SimpleNamespace(
        post=AsyncMock(
            return_value=SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"prefetched_files": []},
            )
        )
    )
    manager._k8s_core_api = SimpleNamespace(patch_namespaced_pod=AsyncMock())

    await manager._create_k8s_session(uuid4(), "session-cold-fallthrough", None, None)

    assert tracker.cold_fallthrough_count("small") == 1


@pytest.mark.asyncio
async def test_forced_cold_create_does_not_record_warm_pool_fallthrough(monkeypatch):
    tracker = ClaimRateTracker(window_seconds=100)
    monkeypatch.setattr(manager_module, "CLAIM_RATE_TRACKER", tracker)
    manager = SandboxManager()
    manager._init_k8s = AsyncMock()
    manager._claim_warm_pod_via_k8s = AsyncMock(return_value=None)
    manager._create_pod = AsyncMock(return_value="10.8.0.43")
    manager._get_http_client = lambda *_args, **_kwargs: SimpleNamespace(
        post=AsyncMock(
            return_value=SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"prefetched_files": []},
            )
        )
    )
    manager._k8s_core_api = SimpleNamespace(patch_namespaced_pod=AsyncMock())
    image_ref = "registry.internal/boxxkite-images/img@sha256:" + "a" * 64

    await manager._create_k8s_session(
        uuid4(), "session-forced-cold", None, None, image_ref=image_ref
    )

    manager._claim_warm_pod_via_k8s.assert_not_awaited()
    assert tracker.cold_fallthrough_count("small") == 0
