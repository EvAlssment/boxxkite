"""Concurrency test for the TOCTOU race in UsagePolicy.create_session: N
concurrent create-session calls for the same account (or across accounts,
for the global cap) must not collectively exceed the configured cap, even
though the count-check and the row-insert are separate awaits.

Uses UsagePolicy directly (not the HTTP layer) against a real SQLite-backed
SandboxSessionRepository so the count queries are genuine DB round-trips
that yield control back to the event loop -- exactly the window
`_create_session_lock` (usage_policy.py) has to close. A small artificial
delay in the fake manager's create_session widens that window so the test
would reliably fail (create more sessions than the cap allows) if the lock
were removed, rather than only occasionally.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from control_plane import db as db_module
from control_plane.config import settings
from control_plane.errors import LimitExceededError
from control_plane.repository import AccountRepository, SandboxSessionRepository
from control_plane.usage_policy import UsagePolicy


class _SlowFakeManager:
    """Like conftest.FakeSandboxManager, but create_session yields control
    (via a real sleep) between the caller's count-check and the row insert,
    to reliably widen the TOCTOU race window in a test environment."""

    def __init__(self) -> None:
        self.created: dict[str, dict] = {}

    async def create_session(self, organization_id, session_id: str, **_kwargs) -> dict:
        await asyncio.sleep(0.02)
        pod_name = f"race-pod-{session_id[:8]}"
        self.created[session_id] = {"organization_id": organization_id, "pod_name": pod_name}
        return {"pod_name": pod_name}

    async def destroy_session(self, session_id: str, **_kwargs) -> None:
        self.created.pop(session_id, None)


async def _attempt_create(manager, account_id: str) -> str:
    """Each attempt gets its own AsyncSession/UsagePolicy, exactly like each
    real HTTP request does via Depends(get_db)/Depends(get_usage_policy) --
    AsyncSession isn't safe to share across concurrently-running coroutines,
    so this mirrors production rather than reusing one session across
    gather() tasks. Returns 'created'/'limited' rather than
    propagating/raising, so gather() can collect every outcome."""
    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_id(account_id)
        policy = UsagePolicy(manager, SandboxSessionRepository(db))
        try:
            await policy.create_session(account)
        except LimitExceededError:
            return "limited"
        return "created"


@pytest.mark.parametrize("concurrency", [10])
async def test_concurrent_creates_do_not_exceed_per_account_cap(
    client: httpx.AsyncClient, monkeypatch, concurrency: int
):
    monkeypatch.setattr(settings, "BOXKITE_MAX_CONCURRENT_SANDBOXES", 3)
    monkeypatch.setattr(settings, "BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES", 100)

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).create(email="race-per-account@example.com", password_hash="x")

    manager = _SlowFakeManager()
    outcomes = await asyncio.gather(*(_attempt_create(manager, account.id) for _ in range(concurrency)))

    assert outcomes.count("created") == 3
    assert outcomes.count("limited") == concurrency - 3


@pytest.mark.parametrize("concurrency", [10])
async def test_concurrent_creates_across_accounts_do_not_exceed_global_cap(
    client: httpx.AsyncClient, monkeypatch, concurrency: int
):
    monkeypatch.setattr(settings, "BOXKITE_MAX_CONCURRENT_SANDBOXES", 100)
    monkeypatch.setattr(settings, "BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES", 4)

    account_ids = []
    async with db_module.get_session_factory()() as db:
        account_repo = AccountRepository(db)
        for i in range(concurrency):
            email = f"race-global-{i}@example.com"
            existing = await account_repo.get_by_email(email)
            account = existing or await account_repo.create(email=email, password_hash="x")
            account_ids.append(account.id)

    manager = _SlowFakeManager()
    outcomes = await asyncio.gather(*(_attempt_create(manager, account_id) for account_id in account_ids))

    assert outcomes.count("created") == 4
    assert outcomes.count("limited") == concurrency - 4


class _SelectivelySlowManager:
    """Only the designated account's create_session hangs -- models a real
    K8s pod-create that's slow/stuck for one tenant (a node autoscaling
    event, an image pull stall, an API server hiccup) without slowing down
    every other tenant's fake manager call too."""

    def __init__(self, *, slow_account_id: str, slow_delay_seconds: float) -> None:
        self._slow_account_id = slow_account_id
        self._slow_delay_seconds = slow_delay_seconds
        self.created: dict[str, dict] = {}

    async def create_session(self, organization_id, session_id: str, **_kwargs) -> dict:
        if str(organization_id) == str(self._slow_account_id):
            await asyncio.sleep(self._slow_delay_seconds)
        pod_name = f"pod-{session_id[:8]}"
        self.created[session_id] = {"pod_name": pod_name}
        return {"pod_name": pod_name}

    async def destroy_session(self, session_id: str, **_kwargs) -> None:
        self.created.pop(session_id, None)


async def test_slow_pod_create_for_one_account_does_not_block_another_account(
    client: httpx.AsyncClient, monkeypatch
):
    """Regression test for a real availability bug introduced by an earlier
    version of the TOCTOU fix: _create_session_lock used to wrap the ENTIRE
    create_session body, including the slow SandboxManager.create_session()
    K8s call. That closed the race but meant one slow/stuck pod create for
    ONE account serialized sandbox creation for every OTHER account in the
    process too -- a single backend hiccup became a full-process outage.
    The lock now only guards the cheap count-check-then-reserve step; the
    slow call happens outside it. If the lock regressed back to wrapping
    the whole method, the `wait_for` below would time out."""
    monkeypatch.setattr(settings, "BOXKITE_MAX_CONCURRENT_SANDBOXES", 100)
    monkeypatch.setattr(settings, "BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES", 100)

    async with db_module.get_session_factory()() as db:
        account_repo = AccountRepository(db)
        slow_account = await account_repo.create(email="slow-account@example.com", password_hash="x")
        fast_account = await account_repo.create(email="fast-account@example.com", password_hash="x")

    manager = _SelectivelySlowManager(slow_account_id=slow_account.id, slow_delay_seconds=2.0)

    slow_task = asyncio.create_task(_attempt_create(manager, slow_account.id))
    # Give the slow task a moment to acquire+release the lock and enter its
    # slow manager.create_session call, so the fast call below genuinely
    # races against it rather than just winning by going first.
    await asyncio.sleep(0.05)

    # Must complete in well under a tenth of the slow account's 2s delay --
    # if the lock still wrapped the manager call, this would block until
    # the slow task's sleep(2.0) finished and time out here instead.
    fast_outcome = await asyncio.wait_for(_attempt_create(manager, fast_account.id), timeout=0.5)
    assert fast_outcome == "created"

    slow_outcome = await slow_task
    assert slow_outcome == "created"
