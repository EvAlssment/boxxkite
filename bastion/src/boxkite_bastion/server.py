"""Entry point: starts the bastion's SSH listener. Deliberately minimal --
this process's only job is to accept SSH connections, authenticate them via
`bridge.BastionSSHServer` (which itself delegates to
`auth_bridge.exchange_ssh_credentials_for_takeover_ws`), and relay bytes.
No sidecar credentials, no cluster/API access, no volume mounts -- see
docs/SSH-BASTION-DESIGN.md section 4 and this component's own README.
"""

from __future__ import annotations

import asyncio
import logging

import asyncssh
import websockets

from .bridge import build_server_factory
from .config import BastionSettings

logger = logging.getLogger(__name__)


async def run_bastion(settings: BastionSettings) -> asyncssh.SSHAcceptor:
    """Starts listening and returns the acceptor (mainly so tests can start
    and then explicitly close a real listener without also blocking on
    `main()`'s run-forever wait)."""
    if settings.host_key_path:
        host_keys: list = [settings.host_key_path]
    else:
        logger.warning(
            "[bastion] BOXKITE_BASTION_HOST_KEY_PATH not set -- generating an "
            "ephemeral host key for this process only. Every client will see "
            "a different host key (and a known_hosts warning) across "
            "restarts; set this explicitly outside of local/throwaway use."
        )
        host_keys = [asyncssh.generate_private_key("ssh-ed25519")]

    server_factory = build_server_factory(
        control_plane_ws_base_url=settings.control_plane_ws_base_url,
        ws_connect=websockets.connect,
        max_connections_per_host=settings.max_connections_per_host,
    )
    acceptor = await asyncssh.create_server(
        server_factory,
        settings.listen_host,
        settings.listen_port,
        server_host_keys=host_keys,
        # Raw bytes, not str -- PTY output isn't guaranteed to be valid
        # UTF-8 at arbitrary chunk boundaries, and bridge.py's relay is
        # written to pass bytes straight through in both directions.
        encoding=None,
        # Minimal runtime privilege (design doc section 4): this bastion
        # only ever bridges one interactive shell channel to the takeover
        # WS, so there is no legitimate use for forwarding an SSH agent or
        # an X11 display through it. x11_forwarding already defaults to
        # off in asyncssh; agent_forwarding does not (asyncssh's own
        # AllowAgentForwarding default is True) -- both are pinned off
        # explicitly rather than relying on either default.
        agent_forwarding=False,
        x11_forwarding=False,
        # Bounds how long an unauthenticated connection may hold a
        # per-host connection-limiter slot (see rate_limit.py) before
        # asyncssh disconnects it -- part of the same resource-exhaustion
        # guard, not just an auth nicety.
        login_timeout=settings.login_timeout_seconds,
    )
    logger.info("[bastion] listening on %s:%s", settings.listen_host, settings.listen_port)
    return acceptor


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    async def _run() -> None:
        settings = BastionSettings.from_env()
        await run_bastion(settings)
        await asyncio.Event().wait()  # run forever; SIGINT/SIGTERM stops the process

    asyncio.run(_run())


if __name__ == "__main__":
    main()
