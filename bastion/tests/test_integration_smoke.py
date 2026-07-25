"""One real, end-to-end smoke test: a real asyncssh client speaking real
SSH protocol against a real asyncssh server (this component's actual
listener wiring), with only the outbound `websockets.connect` call faked
out (standing in for control-plane). This is NOT meant to re-test asyncssh
itself (per this task's scoping, that leans on asyncssh's own coverage) --
it exists to catch wiring mistakes the unit tests in test_bridge.py can't
see: real channel encoding (`encoding=None`/bytes mode), a real password
auth round-trip, and a real pty-req + shell-req sequence actually reaching
session_started.
"""

from __future__ import annotations

import asyncio

import asyncssh
import pytest

from boxkite_bastion.bridge import build_server_factory


class _FakeTakeoverWS:
    """Yields its `incoming` chunks and then blocks forever (rather than
    ending iteration) -- simulating an ongoing PTY session that hasn't been
    closed by the remote end yet, so the test can still exercise writing
    from the SSH client afterward without the relay tearing the channel
    down as soon as the fixed `incoming` list is exhausted."""

    def __init__(self, incoming: list[bytes]) -> None:
        self.sent: list[bytes] = []
        self._incoming = list(incoming)
        self.closed = False

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._incoming:
            yield chunk
        while not self.closed:
            await asyncio.sleep(0.05)


async def test_real_ssh_client_authenticates_and_receives_relayed_bytes():
    fake_ws = _FakeTakeoverWS(incoming=[b"hello from the sidecar pty\n"])

    async def fake_ws_connect(url: str):
        assert "/v1/sandboxes/real-session/takeover?token=real-token" in url
        return fake_ws

    server_factory = build_server_factory(
        control_plane_ws_base_url="wss://control-plane.invalid", ws_connect=fake_ws_connect
    )
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    acceptor = await asyncssh.create_server(
        server_factory, "127.0.0.1", 0, server_host_keys=[host_key], encoding=None
    )
    port = acceptor.sockets[0].getsockname()[1]

    try:
        async with asyncssh.connect(
            "127.0.0.1",
            port,
            username="real-session",
            password="real-token",
            known_hosts=None,
        ) as conn:
            stdin, stdout, _stderr = await conn.open_session(term_type="ansi", encoding=None)
            received = await asyncio.wait_for(stdout.readexactly(len(b"hello from the sidecar pty\n")), timeout=5)
            assert received == b"hello from the sidecar pty\n"

            stdin.write(b"typed by human\n")
            await stdin.drain()
            # Give the server's channel->WS relay task a moment to run.
            await asyncio.sleep(0.2)
            assert b"typed by human\n" in fake_ws.sent
    finally:
        acceptor.close()
        await acceptor.wait_closed()


async def test_real_ssh_client_with_wrong_password_is_rejected():
    async def fake_ws_connect(url: str):
        raise RuntimeError("control-plane rejected this token")

    server_factory = build_server_factory(
        control_plane_ws_base_url="wss://control-plane.invalid", ws_connect=fake_ws_connect
    )
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    acceptor = await asyncssh.create_server(
        server_factory, "127.0.0.1", 0, server_host_keys=[host_key], encoding=None
    )
    port = acceptor.sockets[0].getsockname()[1]

    try:
        with pytest.raises(asyncssh.PermissionDenied):
            async with asyncssh.connect(
                "127.0.0.1",
                port,
                username="real-session",
                password="wrong-token",
                known_hosts=None,
            ):
                pass
    finally:
        acceptor.close()
        await acceptor.wait_closed()
