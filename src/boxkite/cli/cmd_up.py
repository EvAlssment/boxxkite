"""`boxkite up` — replaces the README's manual `openssl rand -hex 32` +
`export SIDECAR_AUTH_TOKEN=...` dance with one command: generate a token,
write it where the other CLI commands already know to look
(`~/.boxkite/local.env`), and start the docker-compose stack with it.
"""

from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path

import typer

from .config_store import LOCAL_ENV_FILE, write_local_env
from .errors import CliError

DEFAULT_SIDECAR_PORT = 8080
_COMPOSE_SEARCH_DEPTH = 6
# Auto-discovery walks up to 6 parent directories looking for a relative
# path match -- an unrelated project that happens to have its own
# deploy/docker-compose.yml somewhere above cwd would otherwise get silently
# `docker compose up --build`'d. These markers are only asserted for
# auto-discovered files; an explicit --compose-file is the caller's own
# choice and is used as-is.
_BOXKITE_COMPOSE_MARKERS = ("SIDECAR_AUTH_TOKEN", "sidecar")


def _looks_like_boxkite_compose_file(path: Path) -> bool:
    try:
        content = path.read_text()
    except OSError:
        return False
    return all(marker in content for marker in _BOXKITE_COMPOSE_MARKERS)


def _find_compose_file(explicit: Path | None, *, search_root: Path | None = None) -> Path:
    if explicit is not None:
        if not explicit.exists():
            raise CliError(f"Compose file not found: {explicit}")
        return explicit

    candidate = search_root if search_root is not None else Path.cwd()
    for _ in range(_COMPOSE_SEARCH_DEPTH):
        compose_path = candidate / "deploy" / "docker-compose.yml"
        if compose_path.exists():
            if not _looks_like_boxkite_compose_file(compose_path):
                raise CliError(
                    f"Found {compose_path}, but it doesn't look like a boxkite compose file "
                    "-- refusing to auto-start an unrelated stack. Pass --compose-file <path> "
                    "if this is intentional."
                )
            return compose_path
        if candidate.parent == candidate:
            break
        candidate = candidate.parent

    raise CliError(
        "Could not find deploy/docker-compose.yml. Run `boxkite up` from inside "
        "a boxkite git checkout, or pass --compose-file <path>."
    )


def up(
    compose_file: Path | None = typer.Option(
        None,
        "--compose-file",
        help="Path to docker-compose.yml. Defaults to searching for deploy/docker-compose.yml "
        "starting from the current directory and walking up.",
    ),
    port: int = typer.Option(
        DEFAULT_SIDECAR_PORT, "--port", help="Local port the sidecar's HTTP API is published on."
    ),
    no_build: bool = typer.Option(False, "--no-build", help="Skip `--build` and reuse existing images."),
) -> None:
    """Start the local docker-compose dev stack (sandbox + sidecar + MinIO).

    Generates a fresh SIDECAR_AUTH_TOKEN, writes it to ~/.boxkite/local.env so
    `boxkite exec` and `boxkite files` pick it up automatically, then runs
    `docker compose up -d --build` with that token injected into the
    sidecar's environment. This is local-only, single-sidecar dev mode — see
    `boxkite session --help` for why hosted mode is a separate thing.
    """
    compose_path = _find_compose_file(compose_file)
    repo_root = compose_path.parent.parent

    token = secrets.token_hex(32)
    sidecar_url = f"http://localhost:{port}"
    write_local_env(token=token, sidecar_url=sidecar_url)

    typer.echo(f"Generated a new SIDECAR_AUTH_TOKEN and wrote it to {LOCAL_ENV_FILE}")
    typer.echo(f"Starting docker compose stack from {compose_path} ...")

    cmd = ["docker", "compose", "-f", str(compose_path), "up", "-d"]
    if not no_build:
        cmd.append("--build")

    env = {**os.environ, "SIDECAR_AUTH_TOKEN": token}
    result = subprocess.run(cmd, cwd=repo_root, env=env)
    if result.returncode != 0:
        raise CliError("`docker compose up` failed — see output above.")

    typer.echo("")
    typer.secho("boxkite is up.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  Health check:   curl {sidecar_url}/health")
    typer.echo(f"  Token stored:   {LOCAL_ENV_FILE}")
    typer.echo("  boxkite exec/files now talk to this stack automatically.")
    typer.echo("")
    typer.echo('Try it:   boxkite exec "python3 -c \'print(1 + 1)\'"')
    typer.echo("")
    typer.echo(
        "Local docker-compose mode is a single exec/file-ops target — there is no "
        "multi-session management API here (see `boxkite session --help`). For "
        "that, use `boxkite signup` or `boxkite config set-url`/`set-key` against "
        "a hosted control-plane."
    )
