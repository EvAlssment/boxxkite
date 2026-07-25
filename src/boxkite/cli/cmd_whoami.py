"""`boxkite whoami` — account identity and current usage against fair-use
limits, using the already-configured API key. Hosted-only: local
docker-compose mode has no account concept to report on."""

from __future__ import annotations

import typer

from .client import hosted_request
from .context import resolve_context
from .errors import CliError


def whoami() -> None:
    """Show the account and current fair-use usage for the configured API key."""
    ctx = resolve_context()
    if ctx.mode != "hosted":
        raise CliError(
            "`boxkite whoami` needs a hosted control-plane -- local docker-compose mode "
            "has no account concept. Run `boxkite signup` or `boxkite config set-url`/`set-key` first."
        )

    account = hosted_request(ctx, "GET", "/v1/account")
    usage = hosted_request(ctx, "GET", "/v1/usage")

    typer.echo(f"email: {account['email']}")
    typer.echo(f"account id: {account['id']}")
    typer.echo(
        f"usage: {usage['monthly_sandbox_hours_used']}/{usage['monthly_sandbox_hours_limit']} "
        "sandbox-hours this month"
    )
    typer.echo(
        f"concurrent sandboxes: {usage['concurrent_sandboxes']}/{usage['concurrent_sandboxes_limit']}"
    )
