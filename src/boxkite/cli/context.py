"""Resolves which of the two modes a command should run in.

Hosted mode wins when both a base_url and an api_key are configured
(`boxkite config set-url`/`set-key`, or `boxkite signup`). Otherwise, if
`boxkite up` has written a local sidecar token, local mode is used. If
neither is configured, every command that needs a target fails with a
message explaining both options.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config_store import read_hosted_config, read_local_env
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
    if hosted.base_url and hosted.api_key:
        return Context(mode="hosted", base_url=hosted.base_url.rstrip("/"), api_key=hosted.api_key)

    local = read_local_env()
    if local is not None:
        return Context(mode="local", sidecar_url=local.sidecar_url.rstrip("/"), sidecar_token=local.token)

    raise CliError(
        "No boxkite target configured. Either:\n"
        "  - run `boxkite up` to start a local docker-compose sidecar, or\n"
        "  - run `boxkite config set-url <url>` and `boxkite config set-key <key>`\n"
        "    (or `boxkite signup`) to use a hosted control-plane."
    )
