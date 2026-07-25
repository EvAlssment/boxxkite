"""`boxkite log`/`boxkite watch` — hosted-mode exec/file-op audit history for
a sandbox session, per `docs/SANDBOX-OBSERVABILITY-DESIGN.md`. Hosted only:
the audit trail is a control-plane feature backed by the `ExecLogEntry`
table -- local docker-compose mode (`boxkite up`) has no audit store of its
own, same reasoning as `boxkite session`'s hosted-only restriction.
"""

from __future__ import annotations

import typer

from .client import hosted_request, hosted_stream_events, resolve_session_id
from .context import Context, resolve_context
from .errors import CliError

_SESSION_HELP = "Session ID to inspect. Auto-detected if exactly one active session exists."


def _require_hosted(ctx: Context, command: str) -> None:
    if ctx.mode != "hosted":
        raise CliError(
            f"`boxkite {command}` needs a hosted control-plane. Local docker-compose mode "
            "(`boxkite up`) has no audit-log store of its own -- run `boxkite signup` or "
            "`boxkite config set-url`/`set-key` to target a hosted control-plane."
        )


def _format_entry(entry: dict) -> str:
    detail = entry.get("detail") or {}
    summary = detail.get("command") or detail.get("path") or ""
    exit_code = entry.get("exit_code")
    suffix = f" exit={exit_code}" if exit_code is not None else ""
    return f"{entry['started_at']}  [{entry['source']}] {entry['operation']} {summary}{suffix}"


def log(
    session: str | None = typer.Option(None, "--session", help=_SESSION_HELP),
    limit: int = typer.Option(50, "--limit", help="Maximum number of entries to return (server caps at 500)."),
    offset: int = typer.Option(0, "--offset", help="Number of entries to skip, oldest-first."),
) -> None:
    """Show paginated exec/file-op audit history for a sandbox session."""
    ctx = resolve_context()
    _require_hosted(ctx, "log")
    session_id = resolve_session_id(ctx, session)
    result = hosted_request(ctx, "GET", f"/v1/sandboxes/{session_id}/log", params={"limit": limit, "offset": offset})
    entries = result.get("entries", []) if isinstance(result, dict) else []
    if not entries:
        typer.echo("No log entries.")
        return
    for entry in entries:
        typer.echo(_format_entry(entry))
    total = result.get("total", len(entries))
    typer.echo(f"({len(entries)} of {total} total, offset={offset})")


def watch(
    session: str | None = typer.Option(None, "--session", help=_SESSION_HELP),
) -> None:
    """Stream new exec/file-op log entries for a sandbox session as they happen.

    Blocks until interrupted (Ctrl-C). This is a live feed of *completed*
    exec/file operations, one per logged event -- not a live terminal
    stream of a command's stdout mid-run (interactive human takeover is a
    separate, dashboard-only feature today; see the design doc for why
    that's a materially different problem).
    """
    ctx = resolve_context()
    _require_hosted(ctx, "watch")
    session_id = resolve_session_id(ctx, session)
    try:
        for entry in hosted_stream_events(ctx, "GET", f"/v1/sandboxes/{session_id}/watch"):
            typer.echo(_format_entry(entry))
    except KeyboardInterrupt:
        pass
