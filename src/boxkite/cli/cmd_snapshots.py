"""`boxkite snapshots ...` -- hosted-mode filesystem snapshot management.

Hosted-only, like `boxkite session` and `boxkite images`: the local
docker-compose stack has no multi-session control-plane, so snapshots are a
hosted feature only.
"""

from __future__ import annotations

import typer

from .client import hosted_request
from .context import Context, resolve_context
from .errors import CliError


def _require_hosted(ctx: Context, command: str) -> None:
    if ctx.mode != "hosted":
        raise CliError(
            f"`boxkite {command}` needs a hosted control-plane. Local docker-compose "
            "mode (`boxkite up`) has no snapshot API of its own -- run `boxkite signup` "
            "or `boxkite config set-url`/`set-key` to target a hosted control-plane."
        )


def create(
    session_id: str = typer.Argument(..., help="Session ID to snapshot."),
    label: str | None = typer.Option(None, "--label", help="Optional label for the snapshot."),
) -> None:
    """Create a filesystem snapshot of a hosted sandbox session."""
    ctx = resolve_context()
    _require_hosted(ctx, "snapshots create")
    body: dict = {}
    if label is not None:
        body["label"] = label
    result = hosted_request(ctx, "POST", f"/v1/sandboxes/{session_id}/snapshots", json=body)
    typer.echo(f"Created snapshot {result['id']} (status={result['status']})")


def ls(
    session_id: str = typer.Argument(..., help="Session ID to list snapshots for."),
) -> None:
    """List filesystem snapshots taken from one hosted sandbox session."""
    ctx = resolve_context()
    _require_hosted(ctx, "snapshots ls")
    result = hosted_request(ctx, "GET", f"/v1/sandboxes/{session_id}/snapshots")
    if not result:
        typer.echo("No snapshots for this session.")
        return
    for snapshot in result:
        typer.echo(
            f"{snapshot['id']}  {snapshot['status']:<10} label={snapshot.get('label') or '-'}  "
            f"created_at={snapshot['created_at']}"
        )


def get(
    snapshot_id: str = typer.Argument(..., help="Snapshot ID to look up."),
) -> None:
    """Get a single filesystem snapshot."""
    ctx = resolve_context()
    _require_hosted(ctx, "snapshots get")
    snapshot = hosted_request(ctx, "GET", f"/v1/snapshots/{snapshot_id}")
    typer.echo(
        f"{snapshot['id']}  {snapshot['status']:<10} label={snapshot.get('label') or '-'}  "
        f"session_id={snapshot.get('session_id') or '-'}"
    )
    typer.echo(f"  storage_key_prefix={snapshot['storage_key_prefix']}  size_bytes={snapshot['size_bytes']}")
    if snapshot.get("deleted_at"):
        typer.echo(f"  deleted_at={snapshot['deleted_at']}")


def restore(
    snapshot_id: str = typer.Argument(..., help="Snapshot ID to restore."),
    label: str | None = typer.Option(None, "--label", help="Optional label for the new session."),
) -> None:
    """Restore a filesystem snapshot into a new sandbox session."""
    ctx = resolve_context()
    _require_hosted(ctx, "snapshots restore")
    body: dict = {}
    if label is not None:
        body["label"] = label
    result = hosted_request(ctx, "POST", f"/v1/snapshots/{snapshot_id}/restore", json=body)
    typer.echo(f"Created session {result['id']} from snapshot {snapshot_id} (status={result['status']})")


def rm(
    snapshot_id: str = typer.Argument(..., help="Snapshot ID to delete."),
) -> None:
    """Delete a filesystem snapshot."""
    ctx = resolve_context()
    _require_hosted(ctx, "snapshots rm")
    hosted_request(ctx, "DELETE", f"/v1/snapshots/{snapshot_id}")
    typer.echo(f"Deleted snapshot {snapshot_id}")
