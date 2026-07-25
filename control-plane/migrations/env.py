import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# This directory (control-plane/) isn't on sys.path when alembic is invoked
# from the repo root or another cwd -- add it so `import control_plane`
# resolves the same way it does for the app itself, without requiring the
# package to be installed first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from control_plane.config import settings  # noqa: E402
from control_plane.models_orm import Base  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # disable_existing_loggers=False -- the plain fileConfig() default
    # disables every OTHER already-configured logger app-wide the moment
    # this runs, since init_schema() (and so this env.py) executes on
    # every request/test, not just a one-off CLI invocation. Without this,
    # the app's own loggers -- and pytest's caplog fixture, which depends
    # on handlers/propagation staying intact -- silently stop capturing
    # anything after the first migration run in a process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# `models_orm.py` is the single source of truth for the schema (see
# db.py's own docstring) -- autogenerate diffs against this, not a second,
# hand-maintained copy.
target_metadata = Base.metadata

# The app's own settings.DATABASE_URL (config.py), not a URL hardcoded in
# alembic.ini -- so migrations always target whatever database the app
# itself is configured to use (sqlite for local dev/tests, Postgres in
# production, per db.py's own docstring), without keeping two configs of
# the same value in sync.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    If a connection was handed in via `config.attributes["connection"]`
    (control_plane.migrations.run_migrations' programmatic call, sharing
    the app's own already-open async connection instead of opening a
    second one -- Alembic's documented "connection sharing with asyncio"
    recipe), use it directly. Otherwise (the normal `alembic upgrade head`
    CLI invocation) open a fresh engine of our own, same as the
    unmodified template.
    """
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
