"""`boxkite webhooks create/ls/rm/deliveries` — hosted-mode webhook
registration management (control-plane/src/control_plane/routers/webhooks.py,
docs/WEBHOOKS-DESIGN.md).

Hosted-only, authenticated with the same long-lived API key `boxkite exec`/
`session`/`secrets` use. `create` prints the signing secret exactly once --
the control-plane never returns it again after this response, matching
WebhookCreatedResponse's own docstring.
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
            "(`boxkite up`) has no webhook-delivery store of its own -- run `boxkite signup` "
            "or `boxkite config set-url`/`set-key` to target a hosted control-plane."
        )


def create(
    url: str = typer.Argument(..., help="HTTPS (or HTTP, for local testing) URL to POST events to."),
    event_type: list[str] = typer.Option(
        ...,
        "--event-type",
        help="Event type to register for (e.g. sandbox.created, sandbox.destroyed). "
        "Required, repeatable -- e.g. --event-type sandbox.created --event-type sandbox.destroyed.",
    ),
    description: str | None = typer.Option(
        None, "--description", help="Optional label for this webhook (e.g. 'Slack notifier')."
    ),
) -> None:
    """Register a new webhook. Prints the signing secret exactly once."""
    ctx = resolve_context()
    _require_hosted(ctx, "webhooks create")
    body: dict = {"url": url, "event_types": event_type}
    if description is not None:
        body["description"] = description
    result = hosted_request(ctx, "POST", "/v1/webhooks", json=body)
    typer.echo(f"Created webhook {result['id']} url={result['url']!r} event_types={result['event_types']!r}")
    typer.echo(
        f"Signing secret (shown once, save it now -- verify the "
        f"X-Boxkite-Webhook-Signature header with it): {result['secret']}"
    )


def ls() -> None:
    """List registered webhooks for the authenticated account. The signing secret is never shown here."""
    ctx = resolve_context()
    _require_hosted(ctx, "webhooks ls")
    result = hosted_request(ctx, "GET", "/v1/webhooks")
    if not result:
        typer.echo("No webhooks registered.")
        return
    for webhook in result:
        active = "active" if webhook.get("is_active") else "inactive"
        typer.echo(
            f"{webhook['id']}  {active:<8} url={webhook['url']!r}  event_types={webhook['event_types']!r}  "
            f"description={webhook.get('description') or '-'}"
        )


def rm(
    webhook_id: str = typer.Argument(..., help="Webhook ID to delete (see `boxkite webhooks ls`)."),
) -> None:
    """Delete a webhook."""
    ctx = resolve_context()
    _require_hosted(ctx, "webhooks rm")
    hosted_request(ctx, "DELETE", f"/v1/webhooks/{webhook_id}")
    typer.echo(f"Deleted webhook {webhook_id}")


def deliveries(
    webhook_id: str = typer.Argument(..., help="Webhook ID to inspect."),
    limit: int = typer.Option(20, "--limit", help="Maximum number of delivery attempts to return (server caps at 100)."),
    offset: int = typer.Option(0, "--offset", help="Number of delivery attempts to skip, newest-first."),
) -> None:
    """List recent delivery attempts for a webhook, newest first."""
    ctx = resolve_context()
    _require_hosted(ctx, "webhooks deliveries")
    result = hosted_request(
        ctx, "GET", f"/v1/webhooks/{webhook_id}/deliveries", params={"limit": limit, "offset": offset}
    )
    if not result:
        typer.echo("No delivery attempts.")
        return
    for delivery in result:
        response_status = delivery.get("response_status_code")
        suffix = f" response_status={response_status}" if response_status is not None else ""
        failure = delivery.get("failure_reason")
        suffix += f" failure_reason={failure!r}" if failure else ""
        typer.echo(
            f"{delivery['id']}  {delivery['status']:<9} event_type={delivery['event_type']}  "
            f"attempt_count={delivery['attempt_count']}{suffix}"
        )
