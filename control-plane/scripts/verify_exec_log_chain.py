#!/usr/bin/env python
"""CLI verifier for `exec_log_entries`' hash chain (GitHub issue #136,
docs/TAMPER-EVIDENT-AUDIT-DESIGN.md §7) -- the control-plane analog of
`boxkite audit verify`.

Connects to the control-plane's own configured `DATABASE_URL` (same config
this service itself uses -- see `control_plane.config.settings`) and
recomputes the hash chain over `exec_log_entries`, reporting whether it is
intact or exactly where it first breaks. Exit code 0/1 makes this
scriptable in a compliance pipeline (nightly cron, CI job, etc.).

Usage:
    python scripts/verify_exec_log_chain.py [--session SESSION_ID]

Per the design doc's §7 guidance, the artifact an external auditor should
rely on is a verification run against an *exported* copy of the rows (or,
for the hosted product, a page through the existing `GET .../log` API),
not this script asking the live service "are your own logs valid?" -- this
script is provided as an operator convenience for the same live-database
check `boxkite audit verify` provides for the self-hosted SQLite path, not
as the primary compliance artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from control_plane import db as db_module
from control_plane.audit_chain import verify_exec_log_chain


async def _run(session_id: str | None) -> int:
    async with db_module.get_session_factory()() as db:
        result = await verify_exec_log_chain(db, session_id=session_id)

    print(f"rows_checked={result.rows_checked}")
    if result.ok:
        print(f"OK: {result.detail}")
        return 0
    print(f"BROKEN at row {result.first_break_at_row_id}: {result.detail}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify exec_log_entries' hash-chain integrity.")
    parser.add_argument(
        "--session",
        default=None,
        help="Scope verification to one session_id. Omit to verify every session in the database.",
    )
    args = parser.parse_args()
    exit_code = asyncio.run(_run(args.session))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
