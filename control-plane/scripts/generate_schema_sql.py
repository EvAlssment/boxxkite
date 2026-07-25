#!/usr/bin/env python3
"""Regenerate control-plane/src/control_plane/schema.sql from the ORM models.

schema.sql is a convenience mirror for operators who want to eyeball or apply
the schema with `psql`. The authoritative schema is the Alembic migration
chain (migrations/versions/, applied by db.init_schema at startup); this file
is derived from models_orm.py so it can never silently drift from the model.

Run after changing models_orm.py:

    python control-plane/scripts/generate_schema_sql.py

CI can run it with `--check` to fail if the committed schema.sql is stale.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

_SCRIPT_DIR = Path(__file__).resolve().parent
_MODELS_PATH = _SCRIPT_DIR.parent / "src" / "control_plane" / "models_orm.py"
_SCHEMA_PATH = _SCRIPT_DIR.parent / "src" / "control_plane" / "schema.sql"

_HEADER = """\
-- boxkite control-plane schema (PostgreSQL).
--
-- AUTO-GENERATED from src/control_plane/models_orm.py by
-- scripts/generate_schema_sql.py — do NOT edit by hand. Run that script after
-- changing the ORM models and commit the result.
--
-- This file is a convenience mirror for operators who want to inspect or apply
-- the schema directly (e.g. via `psql`). It is NOT what the running app
-- executes: at startup db.init_schema applies the Alembic migration chain
-- (migrations/versions/), which is the authoritative source of truth. When in
-- doubt about the exact live schema, read the migrations, not this file.
--
-- All statements use IF NOT EXISTS so this is safe to run against an
-- already-initialized database.
"""


def _load_metadata():
    spec = importlib.util.spec_from_file_location("_control_plane_models", _MODELS_PATH)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: with `from __future__ import annotations`, SQLAlchemy
    # resolves the string `Mapped[...]` annotations against the module's globals
    # via sys.modules, so the module must be present there while it executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Base.metadata


def render_schema() -> str:
    metadata = _load_metadata()
    dialect = postgresql.dialect()
    blocks: list[str] = [_HEADER]
    for table in metadata.sorted_tables:
        ddl = str(
            CreateTable(table, if_not_exists=True).compile(dialect=dialect)
        ).strip()
        blocks.append(ddl + ";")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            idx_ddl = str(
                CreateIndex(index, if_not_exists=True).compile(dialect=dialect)
            ).strip()
            blocks.append(idx_ddl + ";")
    return "\n\n".join(blocks) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed schema.sql is out of date.",
    )
    args = parser.parse_args()

    rendered = render_schema()
    if args.check:
        current = _SCHEMA_PATH.read_text() if _SCHEMA_PATH.exists() else ""
        if current != rendered:
            print(
                "schema.sql is out of date — run "
                "`python control-plane/scripts/generate_schema_sql.py`",
                file=sys.stderr,
            )
            return 1
        print("schema.sql is up to date.")
        return 0

    _SCHEMA_PATH.write_text(rendered)
    print(f"Wrote {_SCHEMA_PATH.relative_to(_SCRIPT_DIR.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
