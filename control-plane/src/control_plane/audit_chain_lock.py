"""Per-session serialization for `exec_log_entries`' hash chain (GitHub
issue #136, `docs/TAMPER-EVIDENT-AUDIT-DESIGN.md`).

`ExecLogEntryRepository.create` reads the session's most recent chained
row's `row_hash` and computes the new row's `prev_hash`/`row_hash` from it
*before* inserting -- a read-then-write sequence that is not atomic on its
own. Two genuinely concurrent writes to the same session (e.g. an agent's
`/exec` racing a human-takeover periodic snapshot writer, both logging to
the same session_id) can both read the same prev row's hash and each
compute a hash chaining from it, producing a "forked" chain that
`verify_exec_log_chain` would then falsely report as tampered -- this
module closes that race.

Module-level (not per-`ExecLogEntryRepository`-instance, since one is
constructed fresh per request -- see `repository.py`), same reasoning
`usage_policy.py`'s `_create_session_lock` gives for its own module-level
lock, and per-session_id (not one global lock) so unrelated sessions'
audit-log writes never contend with each other.

NOTE: this only holds within a single control-plane process -- correct for
the reference deployment (`control-plane/Dockerfile`'s `uvicorn` command
runs a single worker, no `--workers N`), but a multi-replica/multi-worker
deployment would need a shared serialization point (e.g. a Postgres
`pg_advisory_xact_lock` keyed on the session_id) for the chain to stay
unforked across processes -- tracked here rather than silently assumed
away, same disclosed-tradeoff shape `usage_policy.py`'s own NOTE(v2)
already documents for its analogous per-process lock.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

_EXEC_LOG_CHAIN_LOCK_CACHE_MAX_ENTRIES = 512

_exec_log_chain_locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()


def get_exec_log_chain_lock(session_id: str) -> asyncio.Lock:
    """One lock per session_id, shared by every caller -- the cache/eviction
    shape mirrors
    `boxkite._manager_recovery.CreateSessionMixin._get_session_create_lock`,
    with the eviction guard's `_waiters` check specifically borrowed from
    that module's `_release_recovery_lock_if_idle` (stricter than
    `_get_session_create_lock`'s own trim, which checks only `.locked()`)."""
    lock = _exec_log_chain_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _exec_log_chain_locks[session_id] = lock
        _trim_exec_log_chain_lock_cache(preserve_session_id=session_id)
    _exec_log_chain_locks.move_to_end(session_id)
    return lock


def _trim_exec_log_chain_lock_cache(preserve_session_id: str | None = None) -> None:
    """Best-effort cap so a control-plane process that has ever logged for
    many sessions doesn't grow this dict forever; never evicts a lock that
    is currently held or has queued waiters."""
    while len(_exec_log_chain_locks) > _EXEC_LOG_CHAIN_LOCK_CACHE_MAX_ENTRIES:
        evicted = False
        for stale_session_id, stale_lock in list(_exec_log_chain_locks.items()):
            if preserve_session_id is not None and stale_session_id == preserve_session_id:
                continue
            if stale_lock.locked():
                continue
            # asyncio.Lock has no public waiter count -- a released lock can
            # still have queued waiters that have not resumed yet; evicting
            # it then would let a later caller create a second lock for the
            # same session_id and break the single-flight guarantee this
            # cache exists for.
            if getattr(stale_lock, "_waiters", None):
                continue
            _exec_log_chain_locks.pop(stale_session_id, None)
            evicted = True
            break
        if not evicted:
            break


def reset_exec_log_chain_locks_for_tests() -> None:
    """Test-only: an asyncio.Lock binds to whichever event loop first awaits
    it, and pytest-asyncio's default per-test event loop would otherwise
    leave this module-level cache holding locks bound to a prior test's
    now-closed loop. Mirrors usage_policy.py's
    reset_create_session_lock_for_tests()."""
    _exec_log_chain_locks.clear()
