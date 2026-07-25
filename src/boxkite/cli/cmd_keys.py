"""`boxkite keys ls` / `keys rm` — API key management.

Unlike `session`/`exec`/`files`, these need a dashboard JWT
(`POST /v1/auth/login`), not the long-lived API key stored by
`config set-key`/`signup` — the control-plane deliberately keeps API-key
management on a separate credential type from sandbox operations (see
control-plane/src/control_plane/routers/api_keys.py's module docstring: "you
can't create a key using a key"). So these commands prompt for email +
password each time and use a fresh, single-use JWT rather than persisting
one, since a 30-minute dashboard token would go stale between invocations
anyway.
"""

from __future__ import annotations

import httpx
import typer

from .client import hosted_request
from .config_store import read_hosted_config
from .context import Context
from .errors import CliError


def _extract_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return f"{err['message']} [{err.get('code', 'error')}]"
    return f"HTTP {resp.status_code}"


def _login_and_get_context(email: str, password: str) -> Context:
    base_url = (read_hosted_config().base_url or "").rstrip("/")
    if not base_url:
        raise CliError("Pass --url, or run `boxkite config set-url <url>` first.")

    try:
        resp = httpx.post(f"{base_url}/v1/auth/login", json={"email": email, "password": password}, timeout=30)
    except httpx.HTTPError as exc:
        raise CliError(f"Could not reach {base_url}: {exc}") from exc
    if resp.status_code >= 400:
        raise CliError(_extract_error(resp))

    access_token = resp.json()["access_token"]
    return Context(mode="hosted", base_url=base_url, api_key=access_token)


def ls(
    email: str = typer.Option(..., "--email", prompt=True, help="Account email."),
    password: str = typer.Option(..., "--password", prompt=True, hide_input=True, help="Account password."),
) -> None:
    """List your API keys. Raw key values are never shown -- only id, name, prefix, and usage timestamps."""
    ctx = _login_and_get_context(email, password)
    result = hosted_request(ctx, "GET", "/v1/api-keys")
    if not result:
        typer.echo("No API keys.")
        return
    for key in result:
        status = "revoked" if key.get("revoked_at") else "active"
        last_used = key.get("last_used_at") or "never"
        typer.echo(f"{key['id']}  {status:<8} name={key['name']!r}  prefix={key['prefix']}  last_used={last_used}")


def rm(
    key_id: str = typer.Argument(..., help="API key ID to revoke (see `boxkite keys ls`)."),
    email: str = typer.Option(..., "--email", prompt=True, help="Account email."),
    password: str = typer.Option(..., "--password", prompt=True, hide_input=True, help="Account password."),
) -> None:
    """Revoke an API key."""
    ctx = _login_and_get_context(email, password)
    hosted_request(ctx, "DELETE", f"/v1/api-keys/{key_id}")
    typer.echo(f"Revoked API key {key_id}")
