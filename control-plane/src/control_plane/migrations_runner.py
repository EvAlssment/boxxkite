"""Applies pending Alembic migrations (migrations/versions/) using the
app's own async engine, via Alembic's documented "connection sharing with
asyncio" recipe (migrations/env.py's `run_migrations_online` checks for a
connection passed through `Config.attributes` and uses it directly instead
of opening a second engine).

Kept as its own module rather than inlined into db.py: it bridges a sync
`alembic.command` call into the already-running asyncio event loop via
`AsyncConnection.run_sync`, a distinct concern from db.py's own
request-scoped session plumbing.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection

_ALEMBIC_INI_PATH = Path(__file__).resolve().parent.parent.parent / "alembic.ini"


def _upgrade_head(connection: Connection, cfg: Config) -> None:
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


async def run_migrations() -> None:
    # Deferred import: db.py imports this module's `run_migrations` from
    # inside `init_schema()`, so importing db.get_engine at module load
    # time here would be circular.
    from .db import get_engine

    cfg = Config(str(_ALEMBIC_INI_PATH))
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade_head, cfg)
