"""Standalone worker process for test_create_session_race_postgres.py.

Run as `python _pg_lock_race_worker.py <account_id> <concurrency>
<start_at_epoch_seconds>` in its own OS process (via
asyncio.create_subprocess_exec) -- NOT imported directly by pytest. This is
what actually makes the cross-replica race exercisable in a test:
`_create_session_lock` (usage_policy.py) is a module-level asyncio.Lock,
which only ever serializes coroutines within one Python process. Two calls
in the SAME test process, however concurrent, share that one lock and can
never reproduce the multi-replica race at all -- a real, separate OS
process (a fresh Python interpreter, its own module-level lock instance) is
required to genuinely simulate a second control-plane replica.

`start_at_epoch_seconds` is a shared wall-clock barrier (computed once by
the parent test, passed identically to every worker) rather than each
worker just starting immediately on launch: two `create_subprocess_exec`
calls don't launch in perfect lockstep (Python interpreter startup +
imports of sqlalchemy/asyncpg take real, variable time), so without a
shared barrier the two workers' concurrent-creation phases could drift
apart enough to stop actually overlapping, making the race intermittent
rather than reliable.

Reads DATABASE_URL and BOXKITE_USAGE_LOCK_BACKEND from the environment (set
by the parent test) and prints this process's own `created` count to
stdout, so the parent can sum both replicas' counts and check the combined
total against the configured cap.
"""

from __future__ import annotations

import asyncio
import sys
import time

from control_plane import db as db_module
from control_plane.errors import LimitExceededError
from control_plane.repository import AccountRepository, SandboxSessionRepository
from control_plane.usage_policy import UsagePolicy

# Widens the actual TOCTOU window this feature closes: the real race is
# between reading the "below cap" count and committing the reservation
# row, NOT around the (slow, but post-commit/post-critical-section)
# SandboxManager.create_session call below. That real window is normally
# just a couple of fast localhost DB round-trips wide -- reliably narrow
# enough that two separate OS processes' 8-way internal sequences often
# don't happen to collide within it purely by subprocess-launch/scheduling
# luck, even with the shared start_at barrier. Patching the count read to
# pause here, INSIDE the critical section (whichever backend guards it),
# makes the window wide and deterministic instead of luck-dependent --
# this is a test-only widening, not a change to the production code path.
_ORIGINAL_COUNT_ACTIVE_FOR_ACCOUNT = SandboxSessionRepository.count_active_for_account


async def _slow_count_active_for_account(self, account_id: str) -> int:
    count = await _ORIGINAL_COUNT_ACTIVE_FOR_ACCOUNT(self, account_id)
    await asyncio.sleep(0.3)
    return count


SandboxSessionRepository.count_active_for_account = _slow_count_active_for_account


class _FakeManager:
    """No artificial delay needed here -- unlike test_create_session_race.py's
    single-process test, this call happens AFTER the count-check-then-reserve
    critical section has already released its lock/committed, so slowing it
    down widens nothing relevant to the race this test exercises (see the
    `_slow_count_active_for_account` patch above for what actually does)."""

    def __init__(self) -> None:
        self.created: dict[str, dict] = {}

    async def create_session(self, organization_id, session_id: str, **_kwargs) -> dict:
        pod_name = f"race-pod-{session_id[:8]}"
        self.created[session_id] = {"organization_id": organization_id, "pod_name": pod_name}
        return {"pod_name": pod_name}

    async def destroy_session(self, session_id: str, **_kwargs) -> None:
        self.created.pop(session_id, None)


async def _attempt_create(manager: _FakeManager, account_id: str) -> str:
    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_id(account_id)
        policy = UsagePolicy(manager, SandboxSessionRepository(db))
        try:
            await policy.create_session(account)
        except LimitExceededError:
            return "limited"
        return "created"


async def main(account_id: str, concurrency: int, start_at_epoch_seconds: float) -> None:
    delay = start_at_epoch_seconds - time.time()
    if delay > 0:
        await asyncio.sleep(delay)

    manager = _FakeManager()
    outcomes = await asyncio.gather(*(_attempt_create(manager, account_id) for _ in range(concurrency)))
    sys.stdout.write(f"{outcomes.count('created')}\n")
    sys.stdout.flush()
    await db_module.dispose_engine()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), float(sys.argv[3])))
