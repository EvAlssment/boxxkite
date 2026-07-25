"""Unit-level coverage for usage_policy.py's BOXKITE_USAGE_LOCK_BACKEND
dispatch -- no real Postgres needed (see test_create_session_race_postgres.py
for the real cross-process integration test that actually proves the
postgres backend closes the race).
"""

from __future__ import annotations

import pytest

from control_plane.config import settings
from control_plane.usage_policy import (
    PostgresSessionLock,
    _create_session_critical_section,
)


def test_usage_lock_backend_defaults_to_memory():
    assert settings.BOXKITE_USAGE_LOCK_BACKEND == "memory"


def test_advisory_lock_id_is_deterministic_and_fits_postgres_bigint():
    """pg_advisory_xact_lock takes a signed 64-bit int -- the same key must
    always hash to the same id (otherwise two replicas locking the "same"
    key would actually take different locks), and it must fit the range
    Postgres accepts."""
    first = PostgresSessionLock._advisory_lock_id(PostgresSessionLock.LOCK_KEY)
    second = PostgresSessionLock._advisory_lock_id(PostgresSessionLock.LOCK_KEY)
    assert first == second
    assert -(2**63) <= first < 2**63

    different_key_id = PostgresSessionLock._advisory_lock_id("some other key")
    assert different_key_id != first


class _FakeAsyncSession:
    """Records execute()/rollback() calls -- just enough to verify
    _create_session_critical_section's postgres path without a real DB."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict]] = []
        self.rolled_back = False

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))

    async def rollback(self) -> None:
        self.rolled_back = True


async def test_critical_section_acquires_postgres_advisory_lock_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_USAGE_LOCK_BACKEND", "postgres")
    db = _FakeAsyncSession()

    async with _create_session_critical_section(db):
        pass

    assert any("pg_advisory_xact_lock" in stmt for stmt, _ in db.executed)
    assert not db.rolled_back


async def test_critical_section_rolls_back_immediately_on_error_when_postgres_backend(monkeypatch):
    """The advisory lock is transaction-scoped -- releasing it promptly on
    the error path (rather than waiting for the request's db session to
    eventually close) matches the memory backend's tight scope. See
    _create_session_critical_section's docstring."""
    monkeypatch.setattr(settings, "BOXKITE_USAGE_LOCK_BACKEND", "postgres")
    db = _FakeAsyncSession()

    with pytest.raises(ValueError):
        async with _create_session_critical_section(db):
            raise ValueError("simulated LimitExceededError")

    assert db.rolled_back


async def test_critical_section_does_not_touch_db_when_memory_backend(monkeypatch):
    """Default backend must behave exactly as before this feature -- no new
    DB round-trip, no rollback call, just the existing asyncio.Lock."""
    monkeypatch.setattr(settings, "BOXKITE_USAGE_LOCK_BACKEND", "memory")
    db = _FakeAsyncSession()

    async with _create_session_critical_section(db):
        pass

    assert db.executed == []
    assert not db.rolled_back
