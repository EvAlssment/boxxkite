"""Tests for boxkite_bastion.auth_bridge -- the security-critical part of
the bastion (GitHub issue #134, docs/SSH-BASTION-DESIGN.md section 3): the
exchange of SSH (username=session_id, password=takeover token) credentials
for control-plane's existing `WS /v1/sandboxes/{id}/takeover?token=` route.

Written first (TDD), before boxkite_bastion.auth_bridge existed, per this
task's instructions -- the raw SSH protocol handling (asyncssh's own
listener, channel, pty-req plumbing) is deliberately NOT re-tested here;
that leans on asyncssh's own test coverage, per the design doc's scoping
of what actually needs new, project-specific test coverage.
"""

from __future__ import annotations

import pytest

from boxkite_bastion.auth_bridge import (
    TakeoverAuthError,
    build_takeover_ws_url,
    exchange_ssh_credentials_for_takeover_ws,
)


# ── build_takeover_ws_url ─────────────────────────────────────────────────


def test_build_takeover_ws_url_targets_the_existing_takeover_route():
    url = build_takeover_ws_url(
        control_plane_ws_base_url="wss://api.example.com",
        session_id="sess-123",
        token="tok-abc",
    )
    assert url == "wss://api.example.com/v1/sandboxes/sess-123/takeover?token=tok-abc"


def test_build_takeover_ws_url_strips_trailing_slash_on_base_url():
    url = build_takeover_ws_url(
        control_plane_ws_base_url="wss://api.example.com/",
        session_id="sess-123",
        token="tok-abc",
    )
    assert url == "wss://api.example.com/v1/sandboxes/sess-123/takeover?token=tok-abc"


def test_build_takeover_ws_url_quotes_special_characters_in_token():
    """JWT-shaped tokens are already URL-safe, but this must not assume
    that -- any value handed to it (an SSH client's raw password field) is
    treated as needing escaping, not trusted as pre-safe."""
    url = build_takeover_ws_url(
        control_plane_ws_base_url="wss://api.example.com",
        session_id="sess-123",
        token="a b/c+d",
    )
    assert "token=a%20b%2Fc%2Bd" in url


def test_build_takeover_ws_url_quotes_special_characters_in_session_id():
    url = build_takeover_ws_url(
        control_plane_ws_base_url="wss://api.example.com",
        session_id="weird/session id",
        token="tok",
    )
    assert "/v1/sandboxes/weird%2Fsession%20id/takeover" in url


def test_build_takeover_ws_url_supports_plain_ws_scheme_for_local_dev():
    url = build_takeover_ws_url(
        control_plane_ws_base_url="ws://localhost:8000",
        session_id="sess-1",
        token="tok-1",
    )
    assert url.startswith("ws://localhost:8000/v1/sandboxes/")


# ── exchange_ssh_credentials_for_takeover_ws: success path ───────────────


class _FakeConnection:
    """Stand-in for the object `websockets.connect(...)` returns when
    awaited directly -- an already-upgraded WS connection."""


async def test_exchange_returns_the_open_connection_on_success():
    """On success, the caller gets back the SAME connection object
    ws_connect produced -- the caller (bridge.py) must reuse this
    connection for the session's actual data relay, not reconnect, because
    the takeover token is single-use and a second redemption attempt would
    be rejected by control-plane."""
    fake_connection = _FakeConnection()
    calls: list[str] = []

    async def fake_ws_connect(url: str):
        calls.append(url)
        return fake_connection

    result = await exchange_ssh_credentials_for_takeover_ws(
        username="sess-1",
        password="tok-1",
        control_plane_ws_base_url="wss://api.example.com",
        ws_connect=fake_ws_connect,
    )

    assert result is fake_connection
    assert calls == ["wss://api.example.com/v1/sandboxes/sess-1/takeover?token=tok-1"]


async def test_exchange_uses_username_as_session_id_and_password_as_token():
    """Mirrors Daytona's `ssh <token>@host` shape per the design doc: the
    SSH *username* carries the session_id, the SSH *password* carries the
    takeover token -- not swapped, not combined."""
    captured = {}

    async def fake_ws_connect(url: str):
        captured["url"] = url
        return _FakeConnection()

    await exchange_ssh_credentials_for_takeover_ws(
        username="the-session-id",
        password="the-token-value",
        control_plane_ws_base_url="wss://api.example.com",
        ws_connect=fake_ws_connect,
    )

    assert "/v1/sandboxes/the-session-id/takeover" in captured["url"]
    assert "token=the-token-value" in captured["url"]


# ── exchange_ssh_credentials_for_takeover_ws: rejection path ─────────────


async def test_exchange_raises_takeover_auth_error_when_control_plane_rejects_upgrade():
    """When control-plane closes the WS upgrade before accept()  --
    invalid/expired/replayed/wrong-session token, or insufficient RBAC --
    `ws_connect` raises (this is exactly what `websockets.connect` does on
    a non-101 handshake response). The bastion must surface this uniformly
    as TakeoverAuthError, never leak which specific control-plane rejection
    reason applied -- the bastion has no independent opinion of a token's
    validity, only of whether control-plane accepted it."""

    class _FakeRejectedHandshake(Exception):
        pass

    async def fake_ws_connect(url: str):
        raise _FakeRejectedHandshake("server rejected WebSocket connection: HTTP 401")

    with pytest.raises(TakeoverAuthError):
        await exchange_ssh_credentials_for_takeover_ws(
            username="sess-1",
            password="bad-token",
            control_plane_ws_base_url="wss://api.example.com",
            ws_connect=fake_ws_connect,
        )


async def test_exchange_raises_takeover_auth_error_on_network_failure():
    """A connection-level failure (DNS, timeout, refused connection) to
    control-plane must fail the SSH auth attempt exactly the same way an
    explicit auth rejection does -- the bastion never treats "couldn't even
    reach control-plane" as a softer failure mode than "control-plane said
    no"."""

    async def fake_ws_connect(url: str):
        raise OSError("Connection refused")

    with pytest.raises(TakeoverAuthError):
        await exchange_ssh_credentials_for_takeover_ws(
            username="sess-1",
            password="tok-1",
            control_plane_ws_base_url="wss://api.example.com",
            ws_connect=fake_ws_connect,
        )


async def test_exchange_does_not_swallow_the_original_exception_context():
    """The original exception should still be chained (`raise ... from
    exc`) for operator-side debugging/logging, even though the caller-
    facing error type is deliberately uniform."""

    class _FakeRejectedHandshake(Exception):
        pass

    async def fake_ws_connect(url: str):
        raise _FakeRejectedHandshake("boom")

    with pytest.raises(TakeoverAuthError) as exc_info:
        await exchange_ssh_credentials_for_takeover_ws(
            username="sess-1",
            password="tok-1",
            control_plane_ws_base_url="wss://api.example.com",
            ws_connect=fake_ws_connect,
        )
    assert isinstance(exc_info.value.__cause__, _FakeRejectedHandshake)


# ── exchange_ssh_credentials_for_takeover_ws: input validation ───────────


async def test_exchange_rejects_empty_username_without_any_network_call():
    calls: list[str] = []

    async def fake_ws_connect(url: str):
        calls.append(url)
        return _FakeConnection()

    with pytest.raises(TakeoverAuthError):
        await exchange_ssh_credentials_for_takeover_ws(
            username="",
            password="tok-1",
            control_plane_ws_base_url="wss://api.example.com",
            ws_connect=fake_ws_connect,
        )
    assert calls == []


async def test_exchange_rejects_empty_password_without_any_network_call():
    calls: list[str] = []

    async def fake_ws_connect(url: str):
        calls.append(url)
        return _FakeConnection()

    with pytest.raises(TakeoverAuthError):
        await exchange_ssh_credentials_for_takeover_ws(
            username="sess-1",
            password="",
            control_plane_ws_base_url="wss://api.example.com",
            ws_connect=fake_ws_connect,
        )
    assert calls == []


async def test_exchange_never_forges_its_own_validity_opinion():
    """Regression guard for the design doc's core invariant: this module
    must contain no token-shape/signature/expiry logic of its own --
    a syntactically-nonsense token must still be forwarded to control-plane
    exactly as given, not pre-rejected by some local heuristic the bastion
    invented (e.g. "must look like a JWT")."""
    captured = {}

    async def fake_ws_connect(url: str):
        captured["url"] = url
        return _FakeConnection()

    await exchange_ssh_credentials_for_takeover_ws(
        username="sess-1",
        password="not-even-slightly-a-jwt",
        control_plane_ws_base_url="wss://api.example.com",
        ws_connect=fake_ws_connect,
    )
    assert "token=not-even-slightly-a-jwt" in captured["url"]
