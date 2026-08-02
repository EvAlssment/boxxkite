"""The security-critical core of the SSH bastion (GitHub issue #134,
docs/SSH-BASTION-DESIGN.md section 3). An incoming SSH connection
authenticates with `(username=<session_id>, password=<takeover token>)`.
This module does *nothing else* except attempt to redeem that pair against
control-plane's *existing*, already-reviewed
`WS /v1/sandboxes/{session_id}/takeover?token=` route -- exactly the same
request the dashboard's TakeoverTerminal makes before opening its own
WebSocket.

No token validation, no signature checking, no session lookup happens
here -- that would be re-implementing (and risking re-implementing
incorrectly) the RBAC/single-use/session-binding logic control-plane
already owns and already has its own dedicated test coverage for
(control-plane/tests/test_sandbox_log_watch_takeover.py). This module has
exactly one job: translate "did control-plane's WS route accept this?"
into an SSH auth success/failure, nothing more. If control-plane ever
changes what it accepts, this module's behavior changes with it for free,
without needing its own update -- that's the point of the design.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Protocol
from urllib.parse import quote

logger = logging.getLogger(__name__)


class TakeoverAuthError(Exception):
    """Raised whenever control-plane's `WS /takeover` route did not accept
    the (session_id, token) pair being exchanged -- an expired, invalid,
    replayed, or wrong-session token, an RBAC rejection, or a network
    failure reaching control-plane at all. Deliberately a single error type
    carrying no further interpretation: the bastion must never form its own
    opinion about *why* a token was rejected, only *that* it was, matching
    control-plane's own generic-failure discipline for other credential
    paths (e.g. password-reset's anti-enumeration behavior -- see
    SECURITY.md's "New credential paths" section)."""


class SupportsWebSocketConnect(Protocol):
    """Shape of `websockets.connect` as used here: a callable that, when
    awaited, performs the WS handshake and returns an open connection, or
    raises if control-plane rejected the upgrade. Kept as a Protocol
    (rather than importing `websockets` directly into this module's public
    signature) so tests can substitute a fake without a real network stack,
    and so this module's only real dependency is "something shaped like
    websockets.connect", not the library itself."""

    def __call__(self, url: str, **kwargs: Any) -> Awaitable[Any]: ...


def build_takeover_ws_url(*, control_plane_ws_base_url: str, session_id: str, token: str) -> str:
    """Builds the exact `WS /v1/sandboxes/{session_id}/takeover?token=...`
    URL the dashboard's TakeoverTerminal already calls against
    control-plane -- this is the one and only route the bastion is allowed
    to speak to for redeeming a credential; there is no second,
    bastion-specific auth endpoint on control-plane.

    `session_id` and `token` are URL-quoted defensively. Neither is
    expected to need escaping in practice (session_ids are UUID-shaped,
    tokens are JWTs using the URL-safe base64 alphabet), but both
    ultimately originate from an SSH client's raw username/password
    fields -- untrusted input by construction -- so this treats them as
    needing escaping regardless of what a well-behaved caller would send.

    `control_plane_ws_base_url` is expected to already be a `ws://`/`wss://`
    origin (e.g. `wss://api.example.com`) -- normalizing an `http(s)://`
    control-plane URL into that form is config.py's job, not this
    function's.
    """
    base = control_plane_ws_base_url.rstrip("/")
    quoted_session_id = quote(session_id, safe="")
    quoted_token = quote(token, safe="")
    return f"{base}/v1/sandboxes/{quoted_session_id}/takeover?token={quoted_token}"


async def exchange_ssh_credentials_for_takeover_ws(
    *,
    username: str,
    password: str,
    control_plane_ws_base_url: str,
    ws_connect: SupportsWebSocketConnect,
) -> Any:
    """THE auth bridge (see module docstring). `username` is treated as the
    `session_id`, `password` as the takeover token -- mirroring Daytona's
    `ssh <token>@host` shape (docs/SSH-BASTION-DESIGN.md section 3).

    Attempts to open `control_plane_ws_base_url`'s
    `WS /takeover?token=...` route. On success, returns the *open*
    connection object `ws_connect` produced -- the caller (bridge.py) must
    reuse this exact connection for the session's actual SSH<->WS data
    relay rather than reconnecting, because the takeover token is
    single-use and a second redemption attempt would be rejected by
    control-plane.

    On ANY failure -- a non-101 handshake response, a WS close during
    upgrade, a timeout, a DNS/connection error -- raises
    `TakeoverAuthError`. Never distinguishes *why* to the caller: the
    bastion has no independent opinion of a token's validity, only of
    whether control-plane accepted it (design doc section 3: "If
    control-plane rejects the WS upgrade ... the bastion reports SSH auth
    failure ... it never has its own opinion about whether a token is
    valid").

    Rejects an empty username or password outright, without even
    attempting a network call -- an empty session_id or token could never
    be redeemed successfully by control-plane, and asyncssh will invoke
    this for every auth attempt an SSH client makes, including malformed
    ones with blank fields."""
    if not username or not password:
        raise TakeoverAuthError("empty username or password")

    url = build_takeover_ws_url(
        control_plane_ws_base_url=control_plane_ws_base_url, session_id=username, token=password
    )
    try:
        connection = await ws_connect(url)
    except Exception as exc:  # noqa: BLE001 -- deliberately uniform, see docstring above
        logger.info("[bastion] takeover WS upgrade rejected for session %r: %s", username, exc)
        raise TakeoverAuthError("control-plane rejected the takeover WS upgrade") from exc
    return connection
