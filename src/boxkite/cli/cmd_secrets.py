"""`boxkite secrets create/ls/rm` — hosted-mode org-scoped secret CRUD
(control-plane/src/control_plane/routers/secrets.py, docs/SECRETS-DESIGN.md).

Hosted-only, authenticated with the same long-lived API key `boxkite exec`/
`session` use — a secret only exists to be granted to a sandbox session
created via that same API. The raw value is write-only end to end: this
module accepts it on `create` and never echoes, logs, or re-displays it
afterward, matching the control-plane's own SecretOut/SecretCreatedResponse
(both omit `value`).
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
            "(`boxkite up`) has no secrets-broker store of its own -- run `boxkite signup` "
            "or `boxkite config set-url`/`set-key` to target a hosted control-plane."
        )


def create(
    name: str = typer.Argument(..., help="Unique (per-account) name used to reference this secret."),
    value: str = typer.Option(
        ...,
        "--value",
        prompt=True,
        hide_input=True,
        help="The real credential value. Never echoed back or logged -- prompted securely if omitted.",
    ),
    allowed_hosts: list[str] = typer.Option(
        ...,
        "--allowed-hosts",
        help="Destination hostname this secret may be used against via {{secret:name}}. "
        "Required, repeatable -- e.g. --allowed-hosts api.example.com --allowed-hosts api2.example.com.",
    ),
    trust_tier: str | None = typer.Option(
        None,
        "--trust-tier",
        help="Only meaningful for wallet/private-key-style secrets (docs/WALLET-SECRETS-DESIGN.md) -- "
        "omit for an ordinary secret. Only 'testnet' is accepted today; 'mainnet' 422s "
        "(unsupported_trust_tier) since the session-scoped signing mechanism a mainnet grant "
        "would need doesn't exist yet.",
    ),
) -> None:
    """Create a new org-scoped secret. The value is write-only -- it is never printed or returned."""
    ctx = resolve_context()
    _require_hosted(ctx, "secrets create")
    body = {"name": name, "value": value, "allowed_hosts": allowed_hosts}
    if trust_tier is not None:
        body["trust_tier"] = trust_tier
    result = hosted_request(ctx, "POST", "/v1/secrets", json=body)
    tier_note = f" trust_tier={result['trust_tier']!r}" if result.get("trust_tier") else ""
    typer.echo(
        f"Created secret {result['id']} name={result['name']!r} allowed_hosts={result['allowed_hosts']!r}{tier_note}"
    )


def ls() -> None:
    """List secrets for the authenticated account. Raw values are never shown."""
    ctx = resolve_context()
    _require_hosted(ctx, "secrets ls")
    result = hosted_request(ctx, "GET", "/v1/secrets")
    if not result:
        typer.echo("No secrets.")
        return
    for secret in result:
        last_used = secret.get("last_used_at") or "never"
        tier_note = f"  trust_tier={secret['trust_tier']!r}" if secret.get("trust_tier") else ""
        typer.echo(
            f"{secret['id']}  name={secret['name']!r}  allowed_hosts={secret['allowed_hosts']!r}  "
            f"last_used={last_used}{tier_note}"
        )


def rm(
    secret_id: str = typer.Argument(..., help="Secret ID to delete (see `boxkite secrets ls`)."),
) -> None:
    """Delete a secret."""
    ctx = resolve_context()
    _require_hosted(ctx, "secrets rm")
    hosted_request(ctx, "DELETE", f"/v1/secrets/{secret_id}")
    typer.echo(f"Deleted secret {secret_id}")
