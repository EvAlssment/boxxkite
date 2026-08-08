"""Resolves which of the two modes a command should run in.

Hosted mode wins whenever an api_key is configured (`boxxkite config
set-key`, or `boxxkite signup`) -- base_url defaults to the hosted SaaS
(DEFAULT_HOSTED_BASE_URL) if `boxxkite config set-url` was never run, so a
fresh `pip install boxxkite-sandbox` + `set-key` is enough to reach hosted
mode; self-hosters still override it with `set-url` exactly as before.
Otherwise, if `boxxkite up` has written a local sidecar token, local mode is
used. If neither is configured, every command that needs a target fails
with a message explaining both options.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config_store import DEFAULT_HOSTED_BASE_URL, read_hosted_config, read_local_env
from .errors import CliError

Mode = Literal["hosted", "local"]


@dataclass
class Context:
    mode: Mode
    base_url: str | None = None
    api_key: str | None = None
    sidecar_url: str | None = None
    sidecar_token: str | None = None


def resolve_context() -> Context:
    hosted = read_hosted_config()
    if hosted.api_key:
        base_url = hosted.base_url or DEFAULT_HOSTED_BASE_URL
        return Context(mode="hosted", base_url=base_url.rstrip("/"), api_key=hosted.api_key)

    local = read_local_env()
    if local is not None:
        return Context(mode="local", sidecar_url=local.sidecar_url.rstrip("/"), sidecar_token=local.token)

    raise CliError(
        "No boxxkite target configured. Either:\n"
        "  - run `boxxkite up` to start a local docker-compose sidecar, or\n"
        "  - run `boxxkite config set-key <key>` (or `boxxkite signup`) to use boxxkite's "
        f"hosted SaaS ({DEFAULT_HOSTED_BASE_URL}), or `boxxkite config set-url <url>` first "
        "if you're pointing at your own self-hosted control-plane."
    )
