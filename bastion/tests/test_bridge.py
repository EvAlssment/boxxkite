"""Tests for boxkite_bastion.bridge -- the asyncssh integration layer.
Deliberately lighter than test_auth_bridge.py, per this task's scoping: the
raw SSH protocol handling (asyncssh's own transport/channel machinery)
leans on asyncssh's own test coverage. What's tested here is this module's
own logic built on top of that: auth delegates to auth_bridge correctly,
and the channel<->WS byte relay preserves order and cleans up both sides.
"""

from __future__ import annotations

import asyncio

import pytest

from boxkite_bastion.bridge import BastionSSHServer, TakeoverSSHServerSession
from boxkite_bastion.rate_limit import PerHostConnectionLimiter


class _FakeConnection:
    """Stands in for asyncssh's `SSHServerConnection` in
    `connection_made`/`connection_lost` -- only the two members this
    module's rate-limiting integration actually touches."""

    def __init__(self, peer_host: str) -> None:
        self._peer_host = peer_host
        self.aborted = False

    def get_extra_info(self, name: str):
        assert name == "peername"
        return (self._peer_host, 22222)

    def abort(self) -> None:
        self.aborted = True


class _FakeChannel:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False
        self.exit_code: int | None = None

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        self.closed = True

    def exit(self, code: int) -> None:
        self.exit_code = code


class _FakeTakeoverWS:
    """Stands in for the object `websockets.connect` produces: an async
    iterable of received messages, plus `send`/`close`."""

    def __init__(self, incoming: list[bytes] | None = None) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self._incoming = list(incoming or [])

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._incoming:
            yield chunk


# ── BastionSSHServer.validate_password ───────────────────────────────────


async def test_validate_password_success_stores_the_takeover_ws():
    fake_ws = _FakeTakeoverWS()

    async def fake_ws_connect(url: str):
        return fake_ws

    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=fake_ws_connect)
    ok = await server.validate_password("sess-1", "tok-1")

    assert ok is True
    assert server.takeover_ws is fake_ws


async def test_validate_password_failure_leaves_takeover_ws_none():
    async def fake_ws_connect(url: str):
        raise RuntimeError("rejected")

    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=fake_ws_connect)
    ok = await server.validate_password("sess-1", "bad-tok")

    assert ok is False
    assert server.takeover_ws is None


async def test_validate_password_rejects_empty_credentials_without_network_call():
    calls: list[str] = []

    async def fake_ws_connect(url: str):
        calls.append(url)
        return _FakeTakeoverWS()

    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=fake_ws_connect)
    ok = await server.validate_password("", "")

    assert ok is False
    assert calls == []


async def test_validate_password_aborts_connection_after_max_failed_attempts():
    """Security review follow-up (issue #134): asyncssh itself imposes no
    cap on userauth attempts per connection, so this method must enforce
    its own after _MAX_PASSWORD_AUTH_ATTEMPTS consecutive failures."""
    from boxkite_bastion.bridge import _MAX_PASSWORD_AUTH_ATTEMPTS

    async def fake_ws_connect(url: str):
        raise RuntimeError("rejected")

    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=fake_ws_connect)
    conn = _FakeConnection("1.2.3.4")
    server.connection_made(conn)

    for attempt in range(1, _MAX_PASSWORD_AUTH_ATTEMPTS):
        ok = await server.validate_password("sess-1", f"bad-tok-{attempt}")
        assert ok is False
        assert conn.aborted is False, f"must not abort before the {_MAX_PASSWORD_AUTH_ATTEMPTS}th failure"

    ok = await server.validate_password("sess-1", "final-bad-tok")
    assert ok is False
    assert conn.aborted is True


async def test_validate_password_success_does_not_count_toward_failed_attempts():
    """A successful auth midway through must not itself abort the
    connection -- only consecutive *failures* should count."""
    fake_ws = _FakeTakeoverWS()

    async def fake_ws_connect(url: str):
        return fake_ws

    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=fake_ws_connect)
    conn = _FakeConnection("1.2.3.4")
    server.connection_made(conn)

    ok = await server.validate_password("sess-1", "tok-1")
    assert ok is True
    assert conn.aborted is False


def test_connection_made_without_a_limiter_stores_the_connection_for_auth_abort():
    """connection_made must record the connection even when no rate
    limiter is configured -- validate_password's failed-attempt cap needs
    it independently of the per-host connection limiter."""
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    conn = _FakeConnection("1.2.3.4")
    server.connection_made(conn)
    assert server._conn is conn


def test_connection_made_tolerates_a_missing_peername():
    """No real-world case for a plain TCP listener, but must not raise if
    get_extra_info("peername") ever returns None."""
    limiter = PerHostConnectionLimiter(max_connections_per_host=1)
    server = BastionSSHServer(
        control_plane_ws_base_url="wss://api.example.com", ws_connect=None, connection_limiter=limiter
    )

    class _NoPeernameConnection(_FakeConnection):
        def get_extra_info(self, name: str):
            return None

    conn = _NoPeernameConnection("unused")
    server.connection_made(conn)  # must not raise
    assert conn.aborted is False


def test_begin_auth_and_password_auth_supported_always_require_password():
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    assert server.begin_auth("anyone") is True
    assert server.password_auth_supported() is True


def test_session_requested_returns_a_session_bound_to_the_server():
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    session = server.session_requested()
    assert isinstance(session, TakeoverSSHServerSession)
    assert session._server is server


def test_exec_and_subsystem_requests_are_rejected():
    """scp/sftp/one-shot exec are explicitly out of scope -- only an
    interactive shell is supported."""
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    session = server.session_requested()
    assert session.exec_requested("ls -la") is False
    assert session.subsystem_requested("sftp") is False
    assert session.pty_requested("xterm", (80, 24, 0, 0), {}) is True
    assert session.shell_requested() is True


# ── TakeoverSSHServerSession: channel -> WS relay ordering ───────────────


async def test_channel_to_ws_relay_preserves_order_and_stops_on_eof():
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    fake_ws = _FakeTakeoverWS()
    server.takeover_ws = fake_ws

    session = TakeoverSSHServerSession(server)
    session.connection_made(_FakeChannel())
    session.session_started()

    session.data_received(b"first", None)
    session.data_received(b"second", None)
    session.data_received(b"third", None)
    session.eof_received()

    await asyncio.wait_for(session._to_ws_task, timeout=1)

    assert fake_ws.sent == [b"first", b"second", b"third"]


async def test_channel_to_ws_relay_encodes_str_chunks_defensively():
    """asyncssh hands bytes when the channel is opened with encoding=None
    (as server.py configures it), but this must not crash if a str ever
    arrives instead."""
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    fake_ws = _FakeTakeoverWS()
    server.takeover_ws = fake_ws

    session = TakeoverSSHServerSession(server)
    session.connection_made(_FakeChannel())
    session.session_started()

    session.data_received("hello", None)
    session.eof_received()
    await asyncio.wait_for(session._to_ws_task, timeout=1)

    assert fake_ws.sent == [b"hello"]


# ── TakeoverSSHServerSession: WS -> channel relay ─────────────────────────


async def test_ws_to_channel_relay_writes_every_message_and_closes_channel_at_end():
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    fake_ws = _FakeTakeoverWS(incoming=[b"out1", b"out2"])
    server.takeover_ws = fake_ws

    chan = _FakeChannel()
    session = TakeoverSSHServerSession(server)
    session.connection_made(chan)
    session.session_started()

    await asyncio.wait_for(session._from_ws_task, timeout=1)

    assert chan.written == [b"out1", b"out2"]
    assert chan.closed is True


async def test_session_started_with_no_takeover_ws_closes_channel_immediately():
    """Defensive fail-closed path -- should be unreachable in practice
    (session_requested only ever follows a successful validate_password),
    but must not silently hang or crash if it somehow happens."""
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    chan = _FakeChannel()
    session = TakeoverSSHServerSession(server)
    session.connection_made(chan)

    session.session_started()

    assert chan.closed is True
    assert chan.exit_code == 1


# ── TakeoverSSHServerSession: cleanup ─────────────────────────────────────


async def test_connection_lost_cancels_relay_tasks_and_closes_ws():
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    fake_ws = _FakeTakeoverWS()
    server.takeover_ws = fake_ws

    session = TakeoverSSHServerSession(server)
    session.connection_made(_FakeChannel())
    session.session_started()

    session.connection_lost(None)
    await asyncio.sleep(0.05)

    assert session._to_ws_task.cancelled() or session._to_ws_task.done()
    assert fake_ws.closed is True


async def test_terminal_size_changed_does_not_raise():
    """Known, disclosed gap: resize is accepted but not forwarded anywhere
    -- this must be a documented no-op, not an exception asyncssh would
    otherwise surface as a channel error."""
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    session = TakeoverSSHServerSession(server)
    session.terminal_size_changed(120, 40, 0, 0)


async def test_channel_to_ws_relay_ends_quietly_when_ws_send_raises():
    """If the takeover WS connection drops mid-session, the channel->WS
    relay task must end quietly (log and return) rather than crash the
    whole SSH session with an unhandled exception."""

    class _BrokenWS(_FakeTakeoverWS):
        async def send(self, data: bytes) -> None:
            raise ConnectionResetError("ws gone")

    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    fake_ws = _BrokenWS()
    server.takeover_ws = fake_ws

    session = TakeoverSSHServerSession(server)
    session.connection_made(_FakeChannel())
    session.session_started()

    session.data_received(b"anything", None)
    await asyncio.wait_for(session._to_ws_task, timeout=1)  # must not raise


async def test_ws_to_channel_relay_ends_quietly_and_closes_channel_on_iteration_error():
    """If iterating the takeover WS connection itself raises, the WS->channel
    relay must still close the channel in its `finally`, not leave it
    dangling open."""

    class _BrokenIterWS(_FakeTakeoverWS):
        def __aiter__(self):
            return self._broken_iter()

        async def _broken_iter(self):
            raise ConnectionResetError("ws gone")
            yield b""  # pragma: no cover -- makes this an async generator

    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    fake_ws = _BrokenIterWS()
    server.takeover_ws = fake_ws

    chan = _FakeChannel()
    session = TakeoverSSHServerSession(server)
    session.connection_made(chan)
    session.session_started()

    await asyncio.wait_for(session._from_ws_task, timeout=1)  # must not raise
    assert chan.closed is True


# ── BastionSSHServer: per-host connection limiting ────────────────────────


def test_connection_made_without_a_limiter_never_aborts():
    server = BastionSSHServer(control_plane_ws_base_url="wss://api.example.com", ws_connect=None)
    conn = _FakeConnection("1.2.3.4")
    server.connection_made(conn)
    assert conn.aborted is False


def test_connection_made_acquires_a_limiter_slot_and_accepts():
    limiter = PerHostConnectionLimiter(max_connections_per_host=1)
    server = BastionSSHServer(
        control_plane_ws_base_url="wss://api.example.com", ws_connect=None, connection_limiter=limiter
    )
    conn = _FakeConnection("1.2.3.4")
    server.connection_made(conn)
    assert conn.aborted is False


def test_connection_made_aborts_once_the_per_host_limit_is_reached():
    limiter = PerHostConnectionLimiter(max_connections_per_host=1)
    limiter.try_acquire("1.2.3.4")  # simulate an already-open connection from this host

    server = BastionSSHServer(
        control_plane_ws_base_url="wss://api.example.com", ws_connect=None, connection_limiter=limiter
    )
    conn = _FakeConnection("1.2.3.4")
    server.connection_made(conn)

    assert conn.aborted is True


def test_connection_lost_releases_the_limiter_slot_for_reuse():
    limiter = PerHostConnectionLimiter(max_connections_per_host=1)
    server = BastionSSHServer(
        control_plane_ws_base_url="wss://api.example.com", ws_connect=None, connection_limiter=limiter
    )
    server.connection_made(_FakeConnection("1.2.3.4"))
    server.connection_lost(None)

    # The slot must be free again -- a second connection from the same
    # host is accepted, not aborted.
    second = _FakeConnection("1.2.3.4")
    server.connection_made(second)
    assert second.aborted is False


def test_connection_lost_without_ever_acquiring_is_a_no_op():
    limiter = PerHostConnectionLimiter(max_connections_per_host=1)
    server = BastionSSHServer(
        control_plane_ws_base_url="wss://api.example.com", ws_connect=None, connection_limiter=limiter
    )
    server.connection_lost(None)  # must not raise, must not release a slot no one holds
    assert limiter.try_acquire("1.2.3.4") is True


async def test_close_quietly_swallows_close_errors():
    from boxkite_bastion.bridge import _close_quietly

    class _RaisingWS:
        async def close(self) -> None:
            raise RuntimeError("already closed")

    await _close_quietly(_RaisingWS())  # must not raise
