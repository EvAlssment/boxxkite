"""`boxkite volumes create/get/ls/rm` — hosted-mode independent storage
volumes (control-plane/src/control_plane/routers/volumes.py,
docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum).

Hosted-only, authenticated with the same long-lived API key `boxkite exec`/
`session`/`secrets` use. Gated server-side by `BOXKITE_VOLUMES_ENABLED` (off
by default) -- this module does no client-side guessing about whether the
feature is enabled; a deployment that hasn't opted in surfaces the
control-plane's own 404 via `hosted_request`'s existing error handling.

An independent PVC-backed volume with its own lifecycle apart from any
single sandbox session -- mount one into a new session with `boxkite session
create --volume-mounts <volume_id>=<mount_path>`.
"""

from __future__ import annotations

import typer

from .client import hosted_request
from .context import Context, resolve_context
from .errors import CliError


def _require_hosted(ctx: Context, command: str) -> None:
    if ctx.mode != "hosted":
        raise CliError(
            f"`boxkite {command}` needs a hosted control-plane. Local docker-compose mode "
            "(`boxkite up`) has no independent volume store of its own -- run `boxkite signup` "
            "or `boxkite config set-url`/`set-key` to target a hosted control-plane."
        )


def create(
    size_gb: float = typer.Option(..., "--size-gb", help="Requested volume size in GB (max 1024)."),
    label: str | None = typer.Option(None, "--label", help="Optional label for your own reference."),
) -> None:
    """Queue creation of a storage volume. Always asynchronous -- poll `boxkite volumes get <id>` for progress."""
    ctx = resolve_context()
    _require_hosted(ctx, "volumes create")
    body: dict = {"size_gb": size_gb}
    if label is not None:
        body["label"] = label
    result = hosted_request(ctx, "POST", "/v1/volumes", json=body)
    typer.echo(f"Queued volume {result['id']} (status={result['status']})")


def get(
    volume_id: str = typer.Argument(..., help="Volume ID to look up (see `boxkite volumes ls`)."),
) -> None:
    """Get a single volume's status."""
    ctx = resolve_context()
    _require_hosted(ctx, "volumes get")
    volume = hosted_request(ctx, "GET", f"/v1/volumes/{volume_id}")
    typer.echo(
        f"{volume['id']}  {volume['status']:<10} size_gb={volume['size_gb']}  label={volume.get('label') or '-'}"
    )
    if volume.get("failure_reason"):
        typer.echo(f"  failure_reason={volume['failure_reason']}")


def ls() -> None:
    """List this account's volumes."""
    ctx = resolve_context()
    _require_hosted(ctx, "volumes ls")
    result = hosted_request(ctx, "GET", "/v1/volumes")
    if not result:
        typer.echo("No volumes.")
        return
    for volume in result:
        typer.echo(
            f"{volume['id']}  {volume['status']:<10} size_gb={volume['size_gb']}  label={volume.get('label') or '-'}"
        )


def rm(
    volume_id: str = typer.Argument(..., help="Volume ID to delete (see `boxkite volumes ls`)."),
) -> None:
    """Delete a volume's control-plane bookkeeping row and its underlying PVC.

    Does NOT retroactively unmount it from any already-running sandbox
    session -- Kubernetes keeps a PVC bound to a running pod alive until the
    pod is gone.
    """
    ctx = resolve_context()
    _require_hosted(ctx, "volumes rm")
    hosted_request(ctx, "DELETE", f"/v1/volumes/{volume_id}")
    typer.echo(f"Deleted volume {volume_id}")
