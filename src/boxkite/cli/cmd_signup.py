"""`boxkite signup` — one command that chains
POST /v1/auth/signup -> POST /v1/api-keys and stores the resulting URL + key,
so the caller is immediately ready to run `boxkite session create`.

Signup alone only returns a short-lived dashboard JWT (see
control-plane/src/control_plane/routers/auth.py); a usable API key requires
the extra POST /v1/api-keys call authenticated with that JWT, exactly as
control-plane/tests/test_api_keys.py exercises.
"""

from __future__ import annotations

import httpx
import typer

from .config_store import read_hosted_config, validate_base_url_scheme, write_hosted_config
from .errors import CliError

DEFAULT_KEY_NAME = "boxkite-cli"


def _extract_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return f"{err['message']} [{err.get('code', 'error')}]"
    return f"HTTP {resp.status_code}"


def signup(
    email: str = typer.Option(..., "--email", prompt=True, help="Account email."),
    password: str = typer.Option(
        ..., "--password", prompt=True, hide_input=True, confirmation_prompt=True, help="Account password (minimum 8 characters)."
    ),
    url: str | None = typer.Option(
        None, "--url", help="Base URL of the hosted control-plane. Defaults to a previously configured URL (`boxkite config set-url`)."
    ),
    key_name: str = typer.Option(DEFAULT_KEY_NAME, "--key-name", help="Name to give the API key created for you."),
) -> None:
    """Sign up for a hosted control-plane account and provision an API key in one step.

    Runs signup -> login-token -> create-api-key and saves the resulting
    base_url + api_key the same way `boxkite config set-url`/`set-key` would.
    """
    base_url = (url or read_hosted_config().base_url or "").rstrip("/")
    if not base_url:
        raise CliError("Pass --url, or run `boxkite config set-url <url>` first.")
    # Validated here, BEFORE the signup/api-key requests below, not just at
    # the final write_hosted_config() call -- otherwise the freshly-issued
    # JWT (line ~53) and the new API key itself (line ~62) would already
    # have been sent to a non-https base_url in cleartext by the time that
    # later check ever ran.
    validate_base_url_scheme(base_url)

    try:
        signup_resp = httpx.post(f"{base_url}/v1/auth/signup", json={"email": email, "password": password}, timeout=30)
    except httpx.HTTPError as exc:
        raise CliError(f"Could not reach {base_url}: {exc}") from exc
    if signup_resp.status_code >= 400:
        raise CliError(_extract_error(signup_resp))
    access_token = signup_resp.json()["access_token"]

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
    typer.secho(f"Account created and API key saved for {base_url}.", fg=typer.colors.GREEN)
    typer.echo("You're ready to run: boxkite session create")
