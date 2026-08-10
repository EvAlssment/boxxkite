"""`boxxkite handoff <tool>` — move an in-progress local Claude Code/Codex
CLI/opencode session into a fresh hosted sandbox, full conversation history
included, and keep talking to it from the takeover terminal.

Merged into the main CLI from the formerly-standalone `boxxkite-handoff`
package (never actually published — see docs/handoff-adapters.md) so
`pip install boxxkite-sandbox` alone is enough; no second install step.
Everything below composes existing, already-reviewed primitives (sandbox
creation, file_create, the takeover PTY) — see `../handoff/orchestrator.py`'s
module docstring for the credential-handling security design.

This command specifically requires hosted mode (`boxxkite config set-key`,
or `boxxkite signup`) — a handoff needs the takeover WebSocket, which only
exists on a real control-plane, not the local docker-compose sidecar
`boxxkite up` drives. `set-key` alone is enough for boxxkite's own hosted
SaaS (see config_store.DEFAULT_HOSTED_BASE_URL); self-hosters still need
`boxxkite config set-url <url>` too.
"""

from __future__ import annotations

import typer
from boxxkite_client import BoxxkiteClient
from boxxkite_client.exceptions import BoxxkiteError

from ..handoff.adapters import ADAPTERS
from ..handoff.core import HandoffError
from ..handoff.orchestrator import DEFAULT_LIFETIME_MINUTES, create_handoff_sandbox
from ..handoff.terminal import run_terminal_passthrough
from .context import resolve_context
from .errors import CliError

TOOL_NAMES = sorted(ADAPTERS)


def handoff(
    tool: str = typer.Argument(..., help=f"Which local tool to hand off a session from: {', '.join(TOOL_NAMES)}."),
    session_ref: str | None = typer.Option(
        None, "--session", help="Adapter-specific session selector; defaults to the most recent local session."
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Overrides the configured hosted API key (see `boxxkite config show`)."
    ),
    base_url: str | None = typer.Option(
        None, "--base-url", help="Overrides the configured hosted control-plane URL (see `boxxkite config show`)."
    ),
    lifetime_minutes: int = typer.Option(
        DEFAULT_LIFETIME_MINUTES, "--lifetime-minutes", help="Sandbox lifetime before automatic teardown."
    ),
) -> None:
    """Provision a fresh sandbox, push the local session's on-disk state
    into it, and open the same takeover terminal boxxkite already uses for
    human operators — with the resume command already typed and running."""
    adapter_cls = ADAPTERS.get(tool)
    if adapter_cls is None:
        raise CliError(f"Unknown tool {tool!r}. Supported: {', '.join(TOOL_NAMES)}.")

    if api_key and base_url:
        resolved_base_url, resolved_api_key = base_url.rstrip("/"), api_key
    else:
        ctx = resolve_context()
        if ctx.mode != "hosted":
            raise CliError(
                "`boxxkite handoff` needs a hosted control-plane target (it opens a takeover "
                "WebSocket, which the local docker-compose sidecar doesn't have) — run "
                "`boxxkite config set-key <key>` (or `boxxkite signup`) first, or pass "
                "--api-key/--base-url directly."
            )
        resolved_base_url = base_url.rstrip("/") if base_url else ctx.base_url
        resolved_api_key = api_key or ctx.api_key

    try:
        session = adapter_cls().locate_session(session_ref=session_ref)
    except HandoffError as exc:
        raise CliError(str(exc)) from exc

    try:
        with BoxxkiteClient(base_url=resolved_base_url, api_key=resolved_api_key) as client:
            result = create_handoff_sandbox(
                client, session, label=f"handoff-{tool}", lifetime_minutes=lifetime_minutes
            )
            typer.secho(
                f"Attached to sandbox {result.sandbox_id} — resuming {tool} session {session.session_id}",
                fg=typer.colors.GREEN,
                err=True,
            )
            run_terminal_passthrough(result.takeover_ws)
    except BoxxkiteError as exc:
        raise CliError(str(exc)) from exc
