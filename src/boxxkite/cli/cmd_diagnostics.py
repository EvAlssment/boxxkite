"""Hosted sandbox diagnostics and plain-language failure summaries."""

from __future__ import annotations

import json

import typer

from .client import hosted_request, resolve_session_id
from .context import Context, resolve_context
from .errors import CliError
from .cmd_session import _require_hosted


def _load(ctx: Context, session_id: str | None, section: str) -> dict:
    _require_hosted(ctx, f"diagnostics {section}")
    resolved = resolve_session_id(ctx, session_id)
    result = hosted_request(ctx, "GET", f"/v1/sandboxes/{resolved}/diagnostics/{section}")
    if not isinstance(result, dict):
        raise CliError("The diagnostics response was not an object.")
    return result


def _emit(result: dict, *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(result, indent=2, default=str))
        return
    typer.echo(f"Sandbox {result.get('session_id')} ({result.get('status')})")
    typer.echo(f"Why: {result.get('why', 'unknown')}")
    if result.get("unavailable"):
        typer.echo(f"Note: {result['unavailable']}")


def summary(
    session_id: str | None = typer.Argument(None, help="Sandbox ID; omitted when exactly one active sandbox exists."),
    as_json: bool = typer.Option(False, "--json", help="Print the complete response as JSON."),
) -> None:
    """Show the reason a sandbox is running, stuck, or stopped."""
    _emit(_load(resolve_context(), session_id, "summary"), as_json=as_json)


def logs(
    session_id: str | None = typer.Argument(None, help="Sandbox ID."),
    as_json: bool = typer.Option(False, "--json", help="Print the complete response as JSON."),
) -> None:
    """Show recent container output and sandbox audit entries."""
    result = _load(resolve_context(), session_id, "logs")
    if as_json:
        _emit(result, as_json=True)
        return
    _emit(result, as_json=False)
    for item in result.get("logs", []):
        typer.echo(f"\n[{item.get('container')}]\n{item.get('output') or item.get('error') or '(no output)'}")


def inspect(
    session_id: str | None = typer.Argument(None, help="Sandbox ID."),
    as_json: bool = typer.Option(False, "--json", help="Print the complete response as JSON."),
) -> None:
    """Inspect pod and container state for a sandbox."""
    _emit(_load(resolve_context(), session_id, "inspect"), as_json=as_json)


def events(
    session_id: str | None = typer.Argument(None, help="Sandbox ID."),
    as_json: bool = typer.Option(False, "--json", help="Print the complete response as JSON."),
) -> None:
    """Show Kubernetes lifecycle and failure events for a sandbox."""
    result = _load(resolve_context(), session_id, "events")
    if as_json:
        _emit(result, as_json=True)
        return
    _emit(result, as_json=False)
    for event in result.get("events", []):
        typer.echo(f"{event.get('type') or 'Normal'} {event.get('reason') or ''}: {event.get('message') or ''}")
