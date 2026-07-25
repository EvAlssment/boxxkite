"""Guards the one thing conftest.py's `create_all()` test shortcut (see its
own module docstring) can't catch on its own: migrations/versions/ silently
drifting out of sync with models_orm.py -- e.g. a column added to a model
without a matching migration, which would work fine in this test suite
(create_all() always reads the live models) but leave a real deployment's
`init_schema()` (Alembic-driven) never actually applying that column.

Also exercises `init_schema()`/`migrations_runner.run_migrations` itself
directly (conftest.py's `client` fixture deliberately bypasses it for
speed), since nothing else in this suite calls the real startup path this
project's control-plane and every self-hosted deployment actually run.
"""

from __future__ import annotations

import uuid

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from control_plane.db import init_schema
from control_plane.models_orm import Base


async def test_init_schema_creates_every_table_from_nothing(tmp_path):
    db_path = tmp_path / f"migrations_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False})

    import control_plane.db as db_module

    original_engine, original_factory = db_module._engine, db_module._session_factory
    db_module._engine, db_module._session_factory = engine, None
    try:
        await init_schema()
    finally:
        db_module._engine, db_module._session_factory = original_engine, original_factory

    async with engine.connect() as conn:

        def get_table_names(sync_conn):
            from sqlalchemy import inspect

            return set(inspect(sync_conn).get_table_names())

        live_tables = await conn.run_sync(get_table_names)

    # "alembic_version" is Alembic's own migration-tracking bookkeeping
    # table, created alongside the real schema but never part of
    # Base.metadata -- expected to be the one extra table here.
    assert live_tables - {"alembic_version"} == set(Base.metadata.tables.keys())
    await engine.dispose()


async def test_migrations_have_zero_drift_from_current_models(tmp_path):
    """The `alembic revision --autogenerate` check: applies every migration
    up to head against a fresh database, then asks Alembic's own
    comparison engine whether the result still matches `Base.metadata`
    exactly. A non-empty diff here means a model changed
    (models_orm.py) without a corresponding migration being written --
    exactly the gap that let this project's live schema drift out from
    under its ORM models before (missing columns, then a stale NOT NULL
    constraint) and had to be fixed by hand against production."""
    db_path = tmp_path / f"migrations_drift_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False})

    import control_plane.db as db_module

    original_engine, original_factory = db_module._engine, db_module._session_factory
    db_module._engine, db_module._session_factory = engine, None
    try:
        await init_schema()
    finally:
        db_module._engine, db_module._session_factory = original_engine, original_factory

    async with engine.connect() as conn:

        def diff(sync_conn):
            migration_context = MigrationContext.configure(sync_conn)
            return compare_metadata(migration_context, Base.metadata)

        drift = await conn.run_sync(diff)

    assert drift == [], f"migrations/versions/ has drifted from models_orm.py: {drift}"
    await engine.dispose()
