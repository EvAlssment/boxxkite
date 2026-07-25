"""asyncssh glue: an `SSHServer` that authenticates via
`auth_bridge.exchange_ssh_credentials_for_takeover_ws`, and an
`SSHServerSession` that bridges an interactive shell channel's bytes to/from
the takeover WebSocket connection that auth produced.

This module owns the raw-SSH-protocol integration (channel lifecycle,
pty-req/shell-req acceptance, byte relay) -- per this task's scoping, this
leans on asyncssh's own test coverage for protocol correctness rather than
re-testing asyncssh itself; what IS tested here directly
(tests/test_bridge.py) is this module's own byte-ordering/relay/cleanup
logic, using fakes for the channel and the WS connection.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import asyncssh

from .auth_bridge import SupportsWebSocketConnect, TakeoverAuthError, exchange_ssh_credentials_for_takeover_ws
from .rate_limit import PerHostConnectionLimiter

logger = logging.getLogger(__name__)

# Sentinel pushed onto the channel->WS queue to signal "no more data, close
# the WS side" -- distinct from any real chunk of channel data (which is
# always `bytes`, never `None`).
_CLOSE_SENTINEL: bytes | None = None

# asyncssh has no built-in cap on userauth attempts per connection (verified
# against the installed asyncssh's own _process_userauth_request/
# _finish_userauth -- there is no attempt counter in the library), so a
# single connection could otherwise retry an unbounded number of passwords,
# each one a real outbound call to control-plane's takeover-auth path.
# `PerHostConnectionLimiter` (rate_limit.py) only bounds *concurrently open*
# connections per host, which does not by itself bound attempts *within* one
# already-open connection -- this cap closes that specific gap.
_MAX_PASSWORD_AUTH_ATTEMPTS = 5


class BastionSSHServer(asyncssh.SSHServer):
    """One instance per incoming SSH connection (asyncssh's
    `server_factory` contract) -- holds the takeover WS connection produced
    by a successful password auth, so `session_requested` can hand it to
    the session that bridges the shell channel to it.

    Deliberately does NOT talk to control-plane's `POST .../takeover-token`
    route -- that mint step happens before the developer ever runs `ssh`
    (docs/SSH-BASTION-DESIGN.md section 3, step 1); this class only
    consumes the token the human already has, exactly as an `ssh` client's
    password prompt would."""

    def __init__(
        self,
        *,
        control_plane_ws_base_url: str,
        ws_connect: SupportsWebSocketConnect,
        connection_limiter: PerHostConnectionLimiter | None = None,
    ) -> None:
        self._control_plane_ws_base_url = control_plane_ws_base_url
        self._ws_connect = ws_connect
        self._connection_limiter = connection_limiter
        self._limited_host: str | None = None
        self._conn: asyncssh.SSHServerConnection | None = None
        self._failed_password_attempts = 0
        self.takeover_ws: Any = None

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._conn = conn
        if self._connection_limiter is None:
            return
        peername = conn.get_extra_info("peername")
        if peername is None:
            # No known real-world case for a plain TCP listener, but fail
            # closed rather than raise out of an asyncssh callback if it
            # ever happens.
            return
        peer_host = peername[0]
        if self._connection_limiter.try_acquire(peer_host):
            self._limited_host = peer_host
        else:
            logger.warning(
                "[bastion] rejecting connection from %s: per-host concurrent connection limit reached",
                peer_host,
            )
            conn.abort()

    def connection_lost(self, exc: Exception | None) -> None:
        if self._connection_limiter is not None and self._limited_host is not None:
            self._connection_limiter.release(self._limited_host)
            self._limited_host = None

    def begin_auth(self, username: str) -> bool:
        # Returning True means "authentication is required" -- there is no
        # anonymous/no-auth path onto this listener.
        return True

    def password_auth_supported(self) -> bool:
        return True

    async def validate_password(self, username: str, password: str) -> bool:
        """The only auth method this bastion supports, by design (see
        docs/SSH-BASTION-DESIGN.md section 3, step 2 -- the token IS the SSH
        password). Public-key auth is deliberately never offered: this
        bastion has no user/key database of its own to check against, and
        adding one would be exactly the kind of parallel credential store
        SECURITY.md's "New credential paths" guidance treats as something
        to avoid, not reintroduce.

        asyncssh itself imposes no cap on how many times a single
        connection may call this (verified against the installed asyncssh's
        own userauth handling -- there is no attempt counter in the
        library), so this method enforces its own
        _MAX_PASSWORD_AUTH_ATTEMPTS and aborts the connection once exceeded
        -- otherwise one open connection could retry an unbounded number of
        tokens, each a real outbound call to control-plane's takeover-auth
        path."""
        try:
            self.takeover_ws = await exchange_ssh_credentials_for_takeover_ws(
                username=username,
                password=password,
                control_plane_ws_base_url=self._control_plane_ws_base_url,
                ws_connect=self._ws_connect,
            )
        except TakeoverAuthError:
            self._failed_password_attempts += 1
            if self._failed_password_attempts >= _MAX_PASSWORD_AUTH_ATTEMPTS and self._conn is not None:
                logger.warning(
                    "[bastion] closing connection after %d failed password attempts",
                    self._failed_password_attempts,
                )
                self._conn.abort()
            return False
        return True

    def session_requested(self) -> "TakeoverSSHServerSession":
        return TakeoverSSHServerSession(self)


class TakeoverSSHServerSession(asyncssh.SSHServerSession):
    """Bridges one SSH shell channel's bytes to/from the takeover
    WebSocket connection `BastionSSHServer.validate_password` already
    opened. Never talks to the sidecar or control-plane's DB directly --
    every byte crosses the exact same, already-authenticated,
    already-audit-logged WS connection the dashboard's TakeoverTerminal
    would have used for this same token."""

    def __init__(self, server: BastionSSHServer) -> None:
        self._server = server
        self._chan: asyncssh.SSHServerChannel | None = None
        self._to_ws_queue: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        self._to_ws_task: asyncio.Task | None = None
        self._from_ws_task: asyncio.Task | None = None

    def connection_made(self, chan: asyncssh.SSHServerChannel) -> None:
        self._chan = chan

    def pty_requested(self, term_type: str, term_size: tuple, term_modes: dict) -> bool:
        # Accepted so `ssh -t` / an interactive client's implicit pty-req
        # succeeds. Known, disclosed gap (design doc section 4): the
        # initial term_size is not forwarded anywhere -- the sidecar's PTY
        # is allocated with whatever size WS /pty already defaults to, and
        # later resizes (terminal_size_changed below) are not wired to a
        # TIOCSWINSZ ioctl. Not solved by this component; tracked as
        # future work, not silently dropped.
        return True

    def shell_requested(self) -> bool:
        return True

    def exec_requested(self, command: str) -> bool:
        # scp/sftp and one-shot `ssh host <command>` both need their own
        # file-transfer/exec bridge to the sidecar's HTTP routes -- out of
        # scope per docs/SSH-BASTION-DESIGN.md's "Before anyone builds
        # this" section. Only an interactive shell is supported.
        return False

    def subsystem_requested(self, subsystem: str) -> bool:
        return False

    def terminal_size_changed(self, width: int, height: int, pixwidth: int, pixheight: int) -> None:
        # Known, disclosed gap (design doc section 4) -- there is no
        # resize control message on the underlying takeover WS today, so
        # this is accepted (asyncssh requires a handler to exist to accept
        # `window-change` requests without an exception) but intentionally
        # a no-op. Logged at debug, not silently swallowed, so an operator
        # investigating "my terminal didn't resize" has a trail to follow.
        logger.debug(
            "[bastion] terminal_size_changed(%s, %s) received but not forwarded -- "
            "resize is a known gap, see docs/SSH-BASTION-DESIGN.md section 4",
            width,
            height,
        )

    def session_started(self) -> None:
        takeover_ws = self._server.takeover_ws
        if takeover_ws is None or self._chan is None:
            # Unreachable in practice -- session_requested is only ever
            # called after validate_password succeeded, which is the only
            # thing that sets takeover_ws -- but fail closed instead of
            # assuming.
            logger.error("[bastion] session_started with no takeover WS connection; closing channel")
            if self._chan is not None:
                self._chan.exit(1)
                self._chan.close()
            return
        self._to_ws_task = asyncio.ensure_future(self._relay_channel_to_ws(takeover_ws))
        self._from_ws_task = asyncio.ensure_future(self._relay_ws_to_channel(takeover_ws))

    def data_received(self, data: bytes | str, datatype: int | None) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._to_ws_queue.put_nowait(data)

    def eof_received(self) -> bool:
        self._to_ws_queue.put_nowait(_CLOSE_SENTINEL)
        return False

    def connection_lost(self, exc: Exception | None) -> None:
        self._to_ws_queue.put_nowait(_CLOSE_SENTINEL)
        if self._to_ws_task is not None:
            self._to_ws_task.cancel()
        if self._from_ws_task is not None:
            self._from_ws_task.cancel()
        takeover_ws = self._server.takeover_ws
        if takeover_ws is not None:
            asyncio.ensure_future(_close_quietly(takeover_ws))

    async def _relay_channel_to_ws(self, takeover_ws: Any) -> None:
        """SSH channel -> WS, in strict receive order. A single-consumer
        queue (rather than firing one task per `data_received` call) is
        what guarantees ordering here: `data_received` is a synchronous
        asyncssh callback that cannot itself await `ws.send`, and
        scheduling an independent task per call would let sends interleave
        out of order across event-loop yields."""
        try:
            while True:
                chunk = await self._to_ws_queue.get()
                if chunk is _CLOSE_SENTINEL:
                    return
                await takeover_ws.send(chunk)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 -- relay teardown, not a re-raised app error
            logger.info("[bastion] channel->WS relay ended: %s", exc)

    async def _relay_ws_to_channel(self, takeover_ws: Any) -> None:
        """WS (sidecar PTY output, via control-plane's existing proxy) ->
        SSH channel stdout."""
        try:
            async for message in takeover_ws:
                payload = message if isinstance(message, (bytes, bytearray)) else message.encode("utf-8")
                if self._chan is not None:
                    # The channel is opened with encoding=None (see
                    # server.py) so PTY output -- not guaranteed to be
                    # valid UTF-8 at arbitrary chunk boundaries -- is
                    # written through as raw bytes, never decoded/
                    # re-encoded by this relay.
                    self._chan.write(bytes(payload))
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 -- relay teardown, not a re-raised app error
            logger.info("[bastion] WS->channel relay ended: %s", exc)
        finally:
            if self._chan is not None:
                self._chan.close()


async def _close_quietly(takeover_ws: Any) -> None:
    try:
        await takeover_ws.close()
    except Exception:  # noqa: BLE001 -- best-effort cleanup only
        pass


def build_server_factory(
    *,
    control_plane_ws_base_url: str,
    ws_connect: SupportsWebSocketConnect,
    max_connections_per_host: int | None = None,
) -> Callable[[], BastionSSHServer]:
    """`asyncssh.create_server`'s `server_factory` argument -- a callable
    invoked once per new connection, per asyncio's own protocol-factory
    convention. Returns a fresh `BastionSSHServer` each time so
    `takeover_ws` is never shared across connections.

    The connection limiter, by contrast, is shared across every
    `BastionSSHServer` this factory produces -- it exists to cap
    concurrent connections *across* connections from the same host, so it
    must outlive any single one of them."""
    connection_limiter = (
        PerHostConnectionLimiter(max_connections_per_host) if max_connections_per_host is not None else None
    )

    def _factory() -> BastionSSHServer:
        return BastionSSHServer(
            control_plane_ws_base_url=control_plane_ws_base_url,
            ws_connect=ws_connect,
            connection_limiter=connection_limiter,
        )

    return _factory
