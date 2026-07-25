"""`boxkite-handoff <tool>` entrypoint."""

from __future__ import annotations

import argparse
import os
import sys

from boxkite_client import BoxkiteClient
from boxkite_client.exceptions import BoxkiteError

from .adapters import ADAPTERS
from .core import HandoffError
from .orchestrator import create_handoff_sandbox
from .terminal import run_terminal_passthrough


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boxkite-handoff")
    parser.add_argument("tool", choices=sorted(ADAPTERS) or ["<no adapters registered>"])
    parser.add_argument(
        "--session",
        dest="session_ref",
        default=None,
        help="Adapter-specific session selector; defaults to the most recent local session.",
    )
    parser.add_argument("--api-key", dest="api_key", default=os.environ.get("BOXKITE_API_KEY"))
    parser.add_argument("--base-url", dest="base_url", default=os.environ.get("BOXKITE_BASE_URL"))
    parser.add_argument(
        "--lifetime-minutes",
        dest="lifetime_minutes",
        type=int,
        default=120,
        help="Sandbox lifetime before automatic teardown.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.api_key or not args.base_url:
        print(
            "boxkite-handoff: BOXKITE_API_KEY and BOXKITE_BASE_URL "
            "(or --api-key/--base-url) are required.",
            file=sys.stderr,
        )
        return 2

    adapter_cls = ADAPTERS.get(args.tool)
    if adapter_cls is None:
        print(f"boxkite-handoff: unknown tool {args.tool!r}", file=sys.stderr)
        return 2
    adapter = adapter_cls()

    try:
        session = adapter.locate_session(session_ref=args.session_ref)
    except HandoffError as e:
        print(f"boxkite-handoff: {e}", file=sys.stderr)
        return 1

    try:
        with BoxkiteClient(base_url=args.base_url, api_key=args.api_key) as client:
            result = create_handoff_sandbox(
                client, session, label=f"handoff-{args.tool}", lifetime_minutes=args.lifetime_minutes
            )
            print(
                f"boxkite-handoff: attached to sandbox {result.sandbox_id} -- "
                f"resuming {args.tool} session {session.session_id}",
                file=sys.stderr,
            )
            run_terminal_passthrough(result.takeover_ws)
    except BoxkiteError as e:
        print(f"boxkite-handoff: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
