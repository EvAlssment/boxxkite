"""Cross-REPLICA (not just cross-coroutine) integration test for
PostgresSessionLock (usage_policy.py).

test_create_session_race.py already proves `_create_session_lock` (an
asyncio.Lock) correctly serializes concurrent create_session calls WITHIN
one process. That says nothing about multiple control-plane replicas,
because an asyncio.Lock is module-level but PROCESS-local -- a second
replica gets its own, entirely independent lock instance. Reproducing the
real bug (and proving BOXKITE_USAGE_LOCK_BACKEND=postgres fixes it) requires
literally separate OS processes hitting the same account concurrently, via
_pg_lock_race_worker.py, against a real Postgres (advisory locks have no
SQLite equivalent -- see PostgresSessionLock's docstring).

Requires a real, reachable Postgres instance -- skips gracefully (does not
fail the suite) if one isn't available, per this project's own convention
for tests against an external toolchain (see the root pyproject.toml's
`integration` marker).

Set BOXKITE_TEST_POSTGRES_URL to point at a specific instance; defaults to
`postgresql+asyncpg://localhost:5432/boxkite_test_scratch` (a local
Postgres on the default port, database created ahead of time -- this test
does NOT create the database itself, only the tables inside it).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import asyncpg
import pytest
import sqlalchemy

from control_plane import db as db_module
from control_plane.config import settings
from control_plane.models_orm import Base
from control_plane.repository import AccountRepository

pytestmark = pytest.mark.integration

_DEFAULT_TEST_POSTGRES_URL = "postgresql://localhost:5432/boxkite_test_scratch"
_WORKER_SCRIPT = Path(__file__).parent / "_pg_lock_race_worker.py"


def _asyncpg_url(sqlalchemy_style_url: str) -> str:
    """asyncpg.connect() wants a plain postgresql:// URL, not SQLAlchemy's
    postgresql+asyncpg:// dialect-qualified form."""
    return sqlalchemy_style_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture
async def postgres_url() -> str:
    """The SQLAlchemy-style (postgresql+asyncpg://) test database URL.
    Skips the test outright if this Postgres isn't reachable -- this is an
    environment/infra check, not something this test suite should ever
    fail the whole run over."""
    raw_url = os.environ.get(
        "BOXKITE_TEST_POSTGRES_URL", _DEFAULT_TEST_POSTGRES_URL
    )
    sqlalchemy_url = (
        raw_url
        if raw_url.startswith("postgresql+asyncpg://")
        else raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    try:
        conn = await asyncpg.connect(_asyncpg_url(sqlalchemy_url), timeout=2.0)
        await conn.close()
    except Exception as exc:
        pytest.skip(f"real Postgres not available at {raw_url!r} for this integration test: {exc}")
    return sqlalchemy_url


@pytest.fixture
async def postgres_account_id(postgres_url: str):
    """Points the app's DB layer at the real Postgres instance, creates the
    schema (idempotent -- safe if it already exists from a prior run),
    truncates this test's own tables for a clean slate, and yields a fresh
    account id. Restores the original DATABASE_URL/engine afterward so this
    integration test can't leak Postgres state into the rest of the
    (SQLite-backed) suite."""
    original_database_url = settings.DATABASE_URL
    settings.DATABASE_URL = postgres_url
    db_module._engine = None
    db_module._session_factory = None

    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with db_module.get_session_factory()() as db:
        await db.execute(sqlalchemy.text("DELETE FROM sandbox_sessions"))
        await db.execute(
            sqlalchemy.text("DELETE FROM accounts WHERE email = 'pg-lock-race@example.com'")
        )
        await db.commit()
        account = await AccountRepository(db).create(
            email="pg-lock-race@example.com", password_hash="x"
        )
        account_id = account.id

    await db_module.dispose_engine()

    yield account_id

    settings.DATABASE_URL = original_database_url
    db_module._engine = None
    db_module._session_factory = None


async def _run_replica(
    *,
    database_url: str,
    lock_backend: str,
    account_id: str,
    concurrency: int,
    max_concurrent_sandboxes: int,
    global_max_concurrent_sandboxes: int,
    start_at_epoch_seconds: float,
) -> int:
    """Spawn one real OS process running `concurrency` concurrent
    create_session attempts for `account_id`, and return how many it
    reports as 'created'. A genuinely separate Python interpreter -- its own
    module-level `_create_session_lock`, exactly like a second
    control-plane replica.

    Caps are passed via environment variables, NOT `monkeypatch.setattr` --
    `settings` is a fresh pydantic-settings instance in the child process,
    read from its own environment; a parent-process monkeypatch has no
    effect on it at all.

    `start_at_epoch_seconds` is a shared wall-clock barrier (the same value
    passed to every sibling `_run_replica` call) -- see
    _pg_lock_race_worker.py's module docstring for why launch order alone
    isn't a reliable enough overlap guarantee.
    """
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "BOXKITE_USAGE_LOCK_BACKEND": lock_backend,
        "BOXKITE_MAX_CONCURRENT_SANDBOXES": str(max_concurrent_sandboxes),
        "BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES": str(global_max_concurrent_sandboxes),
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_WORKER_SCRIPT),
        account_id,
        str(concurrency),
        str(start_at_epoch_seconds),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, (
        f"race worker process failed (exit {proc.returncode}): {stderr.decode()}"
    )
    return int(stdout.decode().strip())


async def test_postgres_backend_caps_concurrent_creates_across_two_replica_processes(
    postgres_account_id: str, postgres_url: str
):
    """The actual fix: with BOXKITE_USAGE_LOCK_BACKEND=postgres, two
    SEPARATE OS processes (simulating two control-plane replicas) racing to
    create sessions for the same account must never collectively exceed
    BOXKITE_MAX_CONCURRENT_SANDBOXES, even though each process has its own,
    entirely independent in-memory lock."""
    start_at = time.time() + 1.5
    created_a, created_b = await asyncio.gather(
        _run_replica(
            database_url=postgres_url,
            lock_backend="postgres",
            account_id=postgres_account_id,
            concurrency=8,
            max_concurrent_sandboxes=3,
            global_max_concurrent_sandboxes=100,
            start_at_epoch_seconds=start_at,
        ),
        _run_replica(
            database_url=postgres_url,
            lock_backend="postgres",
            account_id=postgres_account_id,
            concurrency=8,
            max_concurrent_sandboxes=3,
            global_max_concurrent_sandboxes=100,
            start_at_epoch_seconds=start_at,
        ),
    )

    assert created_a + created_b == 3, (
        f"expected exactly 3 sessions created across both replicas combined, "
        f"got {created_a} + {created_b} = {created_a + created_b}"
    )


async def test_memory_backend_can_overshoot_the_cap_across_two_replica_processes(
    postgres_account_id: str, postgres_url: str
):
    """Characterization test for the bug this feature fixes: with the
    DEFAULT "memory" backend, two separate replica processes each enforce
    their own independent asyncio.Lock, so the combined total created across
    both can exceed BOXKITE_MAX_CONCURRENT_SANDBOXES. This is deliberately
    asserted (not just described in a comment) so a future change that
    silently made `_run_replica`/the worker script stop exercising real
    cross-process concurrency would be caught here, rather than only
    the `postgres` test above vacuously passing for the wrong reason."""
    start_at = time.time() + 1.5
    created_a, created_b = await asyncio.gather(
        _run_replica(
            database_url=postgres_url,
            lock_backend="memory",
            account_id=postgres_account_id,
            concurrency=8,
            max_concurrent_sandboxes=3,
            global_max_concurrent_sandboxes=100,
            start_at_epoch_seconds=start_at,
        ),
        _run_replica(
            database_url=postgres_url,
            lock_backend="memory",
            account_id=postgres_account_id,
            concurrency=8,
            max_concurrent_sandboxes=3,
            global_max_concurrent_sandboxes=100,
            start_at_epoch_seconds=start_at,
        ),
    )

    assert created_a + created_b > 3, (
        "expected the memory backend to overshoot the cap across two replica "
        f"processes (that's the bug PostgresSessionLock fixes), but got only "
        f"{created_a} + {created_b} = {created_a + created_b}"
    )
