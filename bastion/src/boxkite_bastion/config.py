"""bastion/ configuration. A deliberately small settings surface: this
component's only outbound dependency is control-plane's existing public
API (docs/SSH-BASTION-DESIGN.md section 4 -- "it needs outbound network
access to control-plane and nothing else; no sidecar credentials, no
cluster/API access, no volume mounts").

Uses plain `os.environ` reads rather than pulling in `pydantic-settings`
(control-plane's own choice) -- this is a small, standalone deployable
with no other reason to depend on pydantic, and a frozen dataclass gives
the same "validate once at startup, fail fast" property without the extra
dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class BastionConfigError(Exception):
    """Raised at startup when required configuration is missing or
    invalid -- fail fast rather than accepting SSH connections the bastion
    can never actually authenticate."""


def _normalize_to_ws_origin(control_plane_url: str) -> str:
    """Accepts either an `http(s)://` or already-`ws(s)://` control-plane
    base URL and returns the `ws(s)://` form `auth_bridge.build_takeover_ws_url`
    expects -- so an operator can point this at the same
    `BOXKITE_CONTROL_PLANE_URL`-shaped value used elsewhere in this repo's
    docs without having to know to swap the scheme themselves."""
    url = control_plane_url.rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    if url.startswith("wss://") or url.startswith("ws://"):
        return url
    raise BastionConfigError(
        f"BOXKITE_BASTION_CONTROL_PLANE_URL must start with http://, https://, ws://, or wss:// (got: {control_plane_url!r})"
    )


@dataclass(frozen=True)
class BastionSettings:
    # control-plane's own base URL -- the ONLY network destination this
    # component ever talks to (see module docstring). No default: an
    # operator must point this somewhere real before the bastion can
    # authenticate anything.
    control_plane_ws_base_url: str
    # SSH listener bind address/port. 2222 (not 22) by default, matching
    # this project's convention of not requiring root/CAP_NET_BIND_SERVICE
    # for a non-privileged port -- an operator fronting this with a
    # LoadBalancer/NodePort can still expose it externally as 22 if desired.
    listen_host: str = "0.0.0.0"  # noqa: S104 -- deliberately a public listener, see design doc section 3
    listen_port: int = 2222
    # Path to the bastion's own SSH host key (PEM). If unset, a fresh
    # ephemeral key is generated at process start -- fine for local dev,
    # but means the host key (and therefore every client's known_hosts
    # entry) changes across restarts; set this explicitly for anything
    # beyond a throwaway environment.
    host_key_path: str | None = None
    # Resource-exhaustion guards (security review follow-up, see
    # SECURITY.md's bastion trust-boundary entry): a cap on concurrently
    # open connections per source host, and how long an unauthenticated
    # connection may hold its slot before asyncssh disconnects it.
    max_connections_per_host: int = 10
    login_timeout_seconds: float = 30.0

    @staticmethod
    def from_env() -> "BastionSettings":
        raw_control_plane_url = os.environ.get("BOXKITE_BASTION_CONTROL_PLANE_URL", "").strip()
        if not raw_control_plane_url:
            raise BastionConfigError("BOXKITE_BASTION_CONTROL_PLANE_URL is required and was not set")
        return BastionSettings(
            control_plane_ws_base_url=_normalize_to_ws_origin(raw_control_plane_url),
            listen_host=os.environ.get("BOXKITE_BASTION_LISTEN_HOST", "0.0.0.0"),  # noqa: S104
            listen_port=int(os.environ.get("BOXKITE_BASTION_LISTEN_PORT", "2222")),
            host_key_path=os.environ.get("BOXKITE_BASTION_HOST_KEY_PATH") or None,
            max_connections_per_host=int(os.environ.get("BOXKITE_BASTION_MAX_CONNECTIONS_PER_HOST", "10")),
            login_timeout_seconds=float(os.environ.get("BOXKITE_BASTION_LOGIN_TIMEOUT_SECONDS", "30")),
        )
