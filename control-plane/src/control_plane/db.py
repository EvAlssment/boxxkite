"""Async SQLAlchemy engine/session plumbing.

Tables are defined once in `models_orm.py`; the actual schema applied at
startup (`init_schema`, called from `main.py`'s lifespan) comes from
Alembic migrations (`../migrations/versions/`, run via
`migrations_runner.run_migrations`), not a `Base.metadata.create_all()`
call. `create_all()` can only ever create tables/columns that don't exist
yet -- it silently does nothing to an existing table whose columns have
since changed, which is exactly what let this project's live schema drift
out from under its own ORM models for months before a real production
outage (missing columns, then a stale NOT NULL constraint) surfaced it.
Alembic's migration history is the one mechanism that can actually alter
an existing, already-populated table, and it's the same code path for a
brand-new install (migrating from nothing) as for upgrading an existing
deployment, so the two can never diverge from each other the way
`create_all()` and a separately-hand-maintained migration set could.

`schema.sql` in this directory is a hand-maintained mirror of the same
tables as literal PostgreSQL DDL for operators who want to inspect the
schema by hand outside the app (e.g. via `psql`) — it is not executed by
the app itself, and is now doubly not authoritative (Alembic's migrations
are). Keep it in sync when a column changes, same as before.

Uses SQLite (via aiosqlite) for local dev/tests by default and Postgres
(via asyncpg) in production — see `config.py`. Both are supported by the
same ORM models because columns deliberately avoid Postgres-only types
(UUIDs and timestamps are stored as portable String/DateTime columns), and
by extension the same Alembic migrations apply cleanly to either.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        connect_args = {}
        if settings.DATABASE_URL.startswith("sqlite"):
            # Allow the same in-process connection to be used across the
            # request/dependency boundary in tests.
            connect_args = {"check_same_thread": False}
        _engine = create_async_engine(settings.DATABASE_URL, connect_args=connect_args)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def init_schema() -> None:
    """Applies every pending Alembic migration up to `head`
    (`migrations/versions/`) -- the same mechanism whether this is a
    brand-new install (migrating from nothing) or an existing deployment
    being upgraded. Idempotent and safe on every startup: with nothing new
    to apply, this is a fast no-op (Alembic just reads the one
    `alembic_version` row and returns)."""
    from .migrations_runner import run_migrations

    await run_migrations()


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session
