"""`boxxkite whoami` — account identity and current usage against fair-use
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
            "`boxxkite whoami` needs a hosted control-plane -- local docker-compose mode "
            "has no account concept. Run `boxxkite signup` or `boxxkite config set-url`/`set-key` first."
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
    # .get(), not usage[...]: this CLI ships on PyPI independently of the
    # hosted deploy, so someone who upgrades before api.boxxkite.com does
    # would otherwise hit a raw KeyError on these two fields (added after
    # the rest of this response shape) instead of just not seeing the line.
    remaining = usage.get("sandbox_ops_rate_limit_remaining")
    rate_limit = usage.get("sandbox_ops_rate_limit")
    if remaining is not None and rate_limit is not None:
        typer.echo(f"exec/file-op rate limit: {remaining}/{rate_limit} remaining this minute")
