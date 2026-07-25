"""Unit tests for rate_limit.py's PostgresRateLimiter -- the shared,
cross-replica backend (`BOXKITE_RATE_LIMIT_BACKEND=postgres`) that closes
the gap the in-memory limiter's own docstring flags: a multi-replica
deployment silently multiplying the effective limit by replica count.

These construct their own isolated SQLite engine per test rather than
using the `client`/`db` fixtures in conftest.py, so they can freely
instantiate multiple independent `PostgresRateLimiter`s against the same
backing store to simulate multiple control-plane replicas sharing one
database, without any app-level HTTP plumbing in the way.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.models_orm import Base
from control_plane.rate_limit import PostgresRateLimiter


@pytest.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / f"rl_{uuid.uuid4().hex}.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_first_hit_returns_count_one(session_factory):
    limiter = PostgresRateLimiter(session_factory)

    count = await limiter.hit_and_count("bucket:key-1")

    assert count == 1


async def test_repeated_hits_in_same_window_increment(session_factory):
    limiter = PostgresRateLimiter(session_factory)

    counts = [await limiter.hit_and_count("bucket:key-2") for _ in range(4)]

    assert counts == [1, 2, 3, 4]


async def test_distinct_keys_are_independent(session_factory):
    limiter = PostgresRateLimiter(session_factory)

    await limiter.hit_and_count("bucket:key-a")
    await limiter.hit_and_count("bucket:key-a")
    count_b = await limiter.hit_and_count("bucket:key-b")

    assert count_b == 1


async def test_different_windows_do_not_share_a_counter(session_factory):
    limiter = PostgresRateLimiter(session_factory)

    count_window_1 = await limiter.hit_and_count("bucket:key-3", window_seconds=60)
    # A distinct (much larger) window size maps to a distinct window_start,
    # simulating "time has moved into a new window" without sleeping.
    count_window_2 = await limiter.hit_and_count("bucket:key-3", window_seconds=3600)

    assert count_window_1 == 1
    assert count_window_2 == 1


async def test_two_replicas_share_the_combined_limit(session_factory):
    """The core multi-replica guarantee: two independent PostgresRateLimiter
    instances (standing in for two control-plane replicas), each pointed at
    the SAME backing store, must together enforce one combined limit --
    not one independent limit each, which is exactly the bug the in-memory
    backend has."""
    replica_a = PostgresRateLimiter(session_factory)
    replica_b = PostgresRateLimiter(session_factory)
    limit = 3
    key = "sandbox_ops:shared-account"

    counts = []
    counts.append(await replica_a.hit_and_count(key))  # 1
    counts.append(await replica_b.hit_and_count(key))  # 2
    counts.append(await replica_a.hit_and_count(key))  # 3
    counts.append(await replica_b.hit_and_count(key))  # 4 -- over the limit

    assert counts == [1, 2, 3, 4]
    over_limit = [c for c in counts if c > limit]
    assert len(over_limit) == 1
    assert counts[-1] > limit


async def test_enforce_rate_limit_uses_postgres_backend_when_configured(monkeypatch, session_factory):
    from types import SimpleNamespace

    from control_plane import rate_limit as rate_limit_module
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_RATE_LIMIT_BACKEND", "postgres")
    monkeypatch.setattr(
        rate_limit_module, "_postgres_limiter", PostgresRateLimiter(session_factory)
    )

    request = SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"))
    response = SimpleNamespace(headers={})

    for _ in range(2):
        await rate_limit_module.enforce_rate_limit(
            request, bucket="test_bucket", limit=2, response=response
        )

    with pytest.raises(Exception) as exc_info:
        await rate_limit_module.enforce_rate_limit(
            request, bucket="test_bucket", limit=2, response=response
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["X-RateLimit-Remaining"] == "0"
