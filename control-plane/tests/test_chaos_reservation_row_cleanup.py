"""Fault-injection coverage for GitHub issue #151, scenario 1 (control-plane
half): does the `sandbox_sessions` reservation row `UsagePolicy.create_session`
inserts *before* calling `SandboxManager.create_session` get cleaned up when
that underlying call fails (e.g. a warm-pool-exhaustion-plus-cold-create
failure, per `tests/test_chaos_warm_pool_exhaustion.py` at the manager
layer)?

`usage_policy.py`'s own docstring and its `create_session` method already
describe the intended behavior (the `except Exception: ... delete_row(...);
raise` block) -- this file's job is to actually exercise it end-to-end
through the real HTTP route, not just read the source and trust it. Uses
the same `FakeSandboxManager.fail_next_create` hook `conftest.py` already
defines for exactly this purpose; before this file, nothing in the suite
set that flag.
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from control_plane import db as db_module
from control_plane.config import settings
from control_plane.models_orm import SandboxSession
from conftest import FakeSandboxManager, signup_and_get_api_key


async def test_reservation_row_is_deleted_when_sandbox_manager_create_fails(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Simulates warm-pool-exhaustion-plus-cold-create-failure surfacing as
    a raised exception out of `SandboxManager.create_session` (exactly what
    `tests/test_chaos_warm_pool_exhaustion.py` demonstrates at the manager
    layer). The reservation row `UsagePolicy.create_session` inserts before
    that call (`pod_name=None`, counted as "active" by `count_active_for_account`
    the instant it commits) must not survive the failure -- otherwise it
    would permanently consume a concurrency-limit slot the background
    reaper can never free, since it only ever tears down pods, and there is
    no pod for this row.
    """
    key = await signup_and_get_api_key(client, "reservation-cleanup@example.com")
    fake_manager.fail_next_create = True

    # httpx's ASGITransport re-raises an unhandled app exception rather than
    # returning a response for it (no generic Exception handler is
    # registered in main.py -- only ApiError/RequestValidationError are);
    # this mirrors what a real deployment's ASGI server would turn into a
    # 500 for the caller, one layer up from what this test can observe
    # directly.
    exc_info = None
    try:
        await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    except RuntimeError as exc:
        exc_info = exc
    assert exc_info is not None, "expected the simulated SandboxManager failure to propagate"
    assert "simulated SandboxManager failure" in str(exc_info)

    # The reservation row must be gone, not lingering as an orphaned
    # "active" session with no pod behind it.
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(SandboxSession))
        rows = result.scalars().all()
    assert rows == []

    # And the account's concurrency count reflects that cleanup -- a
    # follow-up create for the SAME account must not be blocked by a phantom
    # reservation the failed attempt left behind.
    fake_manager.fail_next_create = False
    second = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    assert second.status_code == 201


async def test_reservation_row_cleanup_frees_the_concurrency_slot_even_at_the_cap(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    """Sharper version of the above: with BOXKITE_MAX_CONCURRENT_SANDBOXES=1,
    a failed create must not permanently occupy that one slot -- if the
    reservation row cleanup in `usage_policy.py` regressed, this account
    would be locked out of ever creating a sandbox again after a single
    transient SandboxManager failure.
    """
    monkeypatch.setattr(settings, "BOXKITE_MAX_CONCURRENT_SANDBOXES", 1)
    key = await signup_and_get_api_key(client, "reservation-cleanup-at-cap@example.com")
    fake_manager.fail_next_create = True

    try:
        await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    except RuntimeError:
        pass

    # If the reservation row were left behind, this next call would 429
    # with concurrent_sandbox_limit_reached instead of succeeding.
    retry = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    assert retry.status_code == 201
