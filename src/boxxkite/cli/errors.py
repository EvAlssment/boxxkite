"""A single user-facing error type for the CLI, plus a decorator that turns
it (and raw transport errors) into a clean stderr message + exit code 1
instead of a Python traceback.
"""

from __future__ import annotations

import functools
from typing import Callable, TypeVar

import httpx
import typer

F = TypeVar("F", bound=Callable[..., None])


class CliError(Exception):
    """Raised for any expected, user-actionable failure (bad config, a
    control-plane/sidecar error response, an ambiguous session, etc.)."""


def cli_error_boundary(func: F) -> F:
    """Wrap a command function so CliError/httpx errors print a short
    message on stderr and exit 1, rather than a raw traceback."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except CliError as exc:
            typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None
        except httpx.HTTPError as exc:
            typer.secho(f"Network error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    return wrapper  # type: ignore[return-value]
