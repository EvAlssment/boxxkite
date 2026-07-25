"""`boxkite exec` — the core command. Hosted mode proxies through the
control-plane's per-session `/v1/sandboxes/{id}/exec`; local mode calls the
docker-compose sidecar's own `/exec` route directly, since there is no
session concept to route through locally.
"""

from __future__ import annotations

import typer

from .client import hosted_request, local_request, resolve_session_id
from .context import resolve_context


def exec_cmd(
    command: str = typer.Argument(..., help="Shell command to run inside the sandbox."),
    session: str | None = typer.Option(
        None,
        "--session",
        help="Hosted mode only: session ID to run the command in. Auto-detected if exactly "
        "one active session exists; otherwise required.",
    ),
    timeout: int = typer.Option(30, "--timeout", help="Command timeout in seconds."),
) -> None:
    """Run a shell command in a sandbox: a hosted session, or the local docker-compose sidecar."""
    ctx = resolve_context()

    if ctx.mode == "hosted":
        session_id = resolve_session_id(ctx, session)
        result = hosted_request(
            ctx, "POST", f"/v1/sandboxes/{session_id}/exec", json={"command": command, "timeout": timeout}
        )
    else:
        if session:
            typer.secho(
                "Note: --session is ignored in local docker-compose mode (single sidecar, no sessions).",
                fg=typer.colors.YELLOW,
                err=True,
            )
        result = local_request(ctx, "POST", "/exec", json={"command": command, "timeout": timeout}, timeout=timeout + 10)

    _print_exec_result(result)


def _print_exec_result(result: dict) -> None:
    if result.get("stdout"):
        typer.echo(result["stdout"])
    if result.get("stderr"):
        typer.secho(result["stderr"], fg=typer.colors.YELLOW, err=True)

    exit_code = result.get("exit_code", 0)
    color = typer.colors.GREEN if exit_code == 0 else typer.colors.RED
    typer.secho(f"exit code: {exit_code}", fg=color)
    if exit_code != 0:
        raise typer.Exit(code=1)
