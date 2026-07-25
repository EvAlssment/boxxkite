"""`boxkite config set-key` / `set-url` / `show` — hosted-mode configuration.

Presence of both a base_url and an api_key here is what makes
`context.resolve_context()` pick hosted mode over local mode.
"""

from __future__ import annotations

import typer

from .config_store import read_hosted_config, write_hosted_config

_MASK_VISIBLE_CHARS = 10


def set_key(
    api_key: str = typer.Argument(..., help="Hosted control-plane API key (e.g. bxk_live_...)."),
) -> None:
    """Store the API key used for hosted-mode commands (session/exec/files)."""
    write_hosted_config(api_key=api_key)
    typer.echo("Saved API key.")


def set_url(
    base_url: str = typer.Argument(..., help="Base URL of a hosted boxkite control-plane, e.g. https://api.example.com."),
) -> None:
    """Store the base URL of a hosted control-plane."""
    cleaned = base_url.rstrip("/")
    write_hosted_config(base_url=cleaned)
    typer.echo(f"Saved control-plane URL: {cleaned}")


def show() -> None:
    """Show the currently configured hosted-mode target. The API key is masked."""
    cfg = read_hosted_config()
    base_url = cfg.base_url or "(not set)"
    if cfg.api_key and len(cfg.api_key) > _MASK_VISIBLE_CHARS:
        masked = cfg.api_key[:_MASK_VISIBLE_CHARS] + "..."
    elif cfg.api_key:
        masked = "***"
    else:
        masked = "(not set)"
    typer.echo(f"base_url: {base_url}")
    typer.echo(f"api_key:  {masked}")
