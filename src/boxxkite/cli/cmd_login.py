"""`boxxkite login` — authenticate an existing account and mint a fresh API
key, saved the same way `boxxkite signup`/`boxxkite config set-key` would.

For someone who already has an account (ran `signup` here before, on
another machine, or via the dashboard) and just needs the CLI reconnected
-- as opposed to `signup`, which creates a brand-new account. Mirrors
`signup`'s own POST /v1/auth/login -> POST /v1/api-keys chain, just
exchanging an existing email+password instead of registering a new one.
"""

from __future__ import annotations

import httpx
import typer

from .config_store import DEFAULT_HOSTED_BASE_URL, read_hosted_config, validate_base_url_scheme, write_hosted_config
from .errors import CliError

DEFAULT_KEY_NAME = "boxxkite-cli"


def _extract_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return f"{err['message']} [{err.get('code', 'error')}]"
    return f"HTTP {resp.status_code}"


def login(
    email: str = typer.Option(..., "--email", prompt=True, help="Account email."),
    password: str = typer.Option(
        ..., "--password", prompt=True, hide_input=True, help="Account password."
    ),
    url: str | None = typer.Option(
        None,
        "--url",
        help=f"Base URL of the hosted control-plane. Defaults to a previously configured URL "
        f"(`boxxkite config set-url`), or {DEFAULT_HOSTED_BASE_URL} if none is configured.",
    ),
    key_name: str = typer.Option(DEFAULT_KEY_NAME, "--key-name", help="Name to give the new API key."),
) -> None:
    """Log in to an existing account and mint a fresh API key.

    Runs login -> create-api-key and saves the resulting base_url + api_key
    the same way `boxxkite config set-url`/`set-key` would.
    """
    base_url = (url or read_hosted_config().base_url or DEFAULT_HOSTED_BASE_URL).rstrip("/")
    validate_base_url_scheme(base_url)

    try:
        login_resp = httpx.post(f"{base_url}/v1/auth/login", json={"email": email, "password": password}, timeout=30)
    except httpx.HTTPError as exc:
        raise CliError(f"Could not reach {base_url}: {exc}") from exc
    if login_resp.status_code >= 400:
        raise CliError(_extract_error(login_resp))
    access_token = login_resp.json()["access_token"]

    try:
        key_resp = httpx.post(
            f"{base_url}/v1/api-keys",
            json={"name": key_name},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise CliError(f"Could not reach {base_url}: {exc}") from exc
    if key_resp.status_code >= 400:
        raise CliError(_extract_error(key_resp))
    api_key = key_resp.json()["key"]

    write_hosted_config(base_url=base_url, api_key=api_key)
    typer.secho(f"Logged in and API key saved for {base_url}.", fg=typer.colors.GREEN)
    typer.echo("You're ready to run: boxxkite session create")
