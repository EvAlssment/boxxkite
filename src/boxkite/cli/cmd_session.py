"""`boxkite session create/ls/rm` — hosted-mode sandbox session management.

Deliberately hosted-only: local docker-compose mode (`boxkite up`) starts a
single sidecar with no session concept of its own — there's nothing to list,
create, or remove beyond the one stack `boxkite up` already started. Local
users get a clear explanation here rather than a CLI that pretends to manage
sessions it structurally cannot.
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
            "mode (`boxkite up`) has no session-management API — it's a single "
            "exec/file-ops sidecar, not a multi-session service. Run `boxkite signup` "
            "or `boxkite config set-url`/`set-key` to target a hosted control-plane."
        )


def _parse_volume_mounts(raw: list[str]) -> dict[str, str]:
    """Parse repeated `--volume-mounts volume_id=mount_path` values into the
    {volume_id: mount_path} mapping the control-plane expects."""
    mounts: dict[str, str] = {}
    for entry in raw:
        volume_id, sep, mount_path = entry.partition("=")
        if not sep or not volume_id or not mount_path:
            raise CliError(
                f"Invalid --volume-mounts value {entry!r}: expected volume_id=mount_path, "
                "e.g. --volume-mounts vol-1=/mnt/data."
            )
        mounts[volume_id] = mount_path
    return mounts


def create(
    label: str | None = typer.Option(None, "--label", help="Optional label for your own reference."),
    size: str = typer.Option("small", "--size", help="Sandbox CPU/memory size."),
    storage_gb: float | None = typer.Option(
        None,
        "--storage-gb",
        help="Override the sandbox's workspace/uploads/outputs/skills volume size, in GB.",
    ),
    lifetime_minutes: int | None = typer.Option(
        None, "--lifetime-minutes", help="Override how long the sandbox stays alive, in minutes."
    ),
    count: int = typer.Option(1, "--count", help="Number of sandboxes to create in this call."),
    image_id: str | None = typer.Option(
        None,
        "--image-id",
        help="Id of a custom image built via `boxkite images` (see POST /v1/images). "
        "Uses the operator's default image when omitted.",
    ),
    secret_names: list[str] | None = typer.Option(
        None,
        "--secret-names",
        help="Name of an account secret (see `boxkite secrets`/POST /v1/secrets) to grant "
        "this session access to via the sidecar's secrets-broker http_request tool. "
        "Repeat the flag for multiple secrets, e.g. --secret-names foo --secret-names bar.",
    ),
    volume_mounts: list[str] | None = typer.Option(
        None,
        "--volume-mounts",
        help="A volume_id=mount_path pair mounting an independent volume (see `boxkite volumes`/"
        "POST /v1/volumes) into this sandbox. Repeat the flag for multiple mounts, e.g. "
        "--volume-mounts vol-1=/mnt/data --volume-mounts vol-2=/mnt/cache.",
    ),
    gpu_count: int | None = typer.Option(
        None,
        "--gpu-count",
        help="Opt-in, experimental (docs/GPU-SUPPORT-SCOPING.md) -- requests this many GPUs. "
        "422s unless the deployment has BOXKITE_GPU_ENABLED set and a GPU-equipped node pool "
        "provisioned; not verified against real GPU hardware.",
    ),
) -> None:
    """Create a new hosted sandbox session."""
    ctx = resolve_context()
    _require_hosted(ctx, "session create")
    body: dict = {}
    if label:
        body["label"] = label
    if size != "small":
        body["size"] = size
    if storage_gb is not None:
        body["storage_gb"] = storage_gb
    if lifetime_minutes is not None:
        body["lifetime_minutes"] = lifetime_minutes
    if count != 1:
        body["count"] = count
    if image_id is not None:
        body["image_id"] = image_id
    if secret_names:
        body["secret_names"] = secret_names
    if volume_mounts:
        body["volume_mounts"] = _parse_volume_mounts(volume_mounts)
    if gpu_count is not None:
        body["gpu_count"] = gpu_count
    result = hosted_request(ctx, "POST", "/v1/sandboxes", json=body)
    # count>1 returns a bare list (SandboxCreatedResponse | list[SandboxCreatedResponse]
    # in control-plane's schema); count==1 (the default) returns a single object.
    sandboxes = result if isinstance(result, list) else [result]
    for sandbox in sandboxes:
        typer.echo(f"Created session {sandbox['id']} (status={sandbox['status']})")


def ls(
    active_only: bool = typer.Option(False, "--active-only", help="Only show sessions that haven't been destroyed."),
) -> None:
    """List hosted sandbox sessions owned by the configured account."""
    ctx = resolve_context()
    _require_hosted(ctx, "session ls")
    result = hosted_request(ctx, "GET", "/v1/sandboxes", params={"active_only": "true" if active_only else "false"})
    if not result:
        typer.echo("No sandbox sessions.")
        return
    for session in result:
        label = session.get("label") or "-"
        typer.echo(f"{session['id']}  {session['status']:<10} label={label}  created_at={session['created_at']}")


def get(
    session_id: str = typer.Argument(..., help="Session ID to look up."),
) -> None:
    """Get a single hosted sandbox session's status."""
    ctx = resolve_context()
    _require_hosted(ctx, "session get")
    session = hosted_request(ctx, "GET", f"/v1/sandboxes/{session_id}")
    label = session.get("label") or "-"
    typer.echo(f"{session['id']}  {session['status']:<10} label={label}  created_at={session['created_at']}")
    if session.get("destroyed_at"):
        typer.echo(f"  destroyed_at={session['destroyed_at']}")


def rm(
    session_id: str = typer.Argument(..., help="Session ID to destroy."),
) -> None:
    """Destroy a hosted sandbox session."""
    ctx = resolve_context()
    _require_hosted(ctx, "session rm")
    hosted_request(ctx, "DELETE", f"/v1/sandboxes/{session_id}")
    typer.echo(f"Destroyed session {session_id}")
