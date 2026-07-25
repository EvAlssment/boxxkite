"""Tests for GUI/remote-desktop human takeover (GitHub issue #184,
docs/GUI-COMPUTER-USE-SCOPING.md): `POST .../desktop-token` and
`WS .../desktop`. Mirrors test_sandbox_log_watch_takeover.py's own
takeover-token/takeover-WS test structure and its documented honest gap:
the full proxy-to-sidecar relay isn't exercised end-to-end against a live
sidecar `/desktop` WebSocket in this suite (FakeSandboxManager has no real
desktop-target equivalent) -- covered here at the
`_authenticate_desktop_or_close` function level, same as
`_authenticate_takeover_or_close` is for PTY takeover, plus one full
route-level test with a mocked sidecar connection.
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class _FakeDesktopWebSocket:
    """Duck-types just enough of `starlette.websockets.WebSocket` for
    `_authenticate_desktop_or_close` -- mirrors
    test_sandbox_log_watch_takeover.py's `_FakeTakeoverWebSocket`."""

    def __init__(
        self,
        *,
        authorization: str | None = None,
        token: str | None = None,
    ) -> None:
        self.headers = {"authorization": authorization} if authorization else {}
        query_params: dict[str, str] = {}
        if token:
            query_params["token"] = token
        self.query_params = query_params
        self.closed_with: tuple[int, str] | None = None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


# ── POST /{session_id}/desktop-token ─────────────────────────────────────


async def test_mint_desktop_token_404s_when_feature_disabled(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """settings.BOXKITE_DESKTOP_ENABLED defaults to False -- the route must
    404 unconditionally, even for a valid admin key and an owned session."""
    key = await signup_and_get_api_key(client, "desktop-token-disabled@example.com", role="admin")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/desktop-token",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


async def test_mint_desktop_token_requires_authentication(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    import control_plane.routers.sandboxes as sandboxes_module

    monkeypatch.setattr(sandboxes_module.settings, "BOXKITE_DESKTOP_ENABLED", True)

    resp = await client.post("/v1/sandboxes/some-session/desktop-token")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"


async def test_mint_desktop_token_403s_for_member_role_key(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    import control_plane.routers.sandboxes as sandboxes_module

    monkeypatch.setattr(sandboxes_module.settings, "BOXKITE_DESKTOP_ENABLED", True)

    admin_key = await signup_and_get_api_key(client, "desktop-rbac@example.com", role="admin")
    session_id = await _create_session(client, admin_key)

    token_response = await client.post(
        "/v1/auth/login", json={"email": "desktop-rbac@example.com", "password": "hunter2pass"}
    )
    member_key_resp = await client.post(
        "/v1/api-keys",
        json={"name": "member key", "role": "member"},
        headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
    )
    member_key = member_key_resp.json()["key"]

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/desktop-token",
        headers={"Authorization": f"Bearer {member_key}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "desktop_not_permitted"


async def test_mint_desktop_token_404s_for_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    import control_plane.routers.sandboxes as sandboxes_module

    monkeypatch.setattr(sandboxes_module.settings, "BOXKITE_DESKTOP_ENABLED", True)

    key_a = await signup_and_get_api_key(client, "desktop-victim@example.com", role="admin")
    key_b = await signup_and_get_api_key(client, "desktop-attacker@example.com", role="admin")
    session_id = await _create_session(client, key_a)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/desktop-token",
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_mint_desktop_token_succeeds_for_admin_role_key_and_is_redeemable(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    import control_plane.routers.sandboxes as sandboxes_module
    from control_plane.routers.sandboxes import _authenticate_desktop_or_close

    monkeypatch.setattr(sandboxes_module.settings, "BOXKITE_DESKTOP_ENABLED", True)

    key = await signup_and_get_api_key(client, "desktop-mint-ok@example.com", role="admin")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/desktop-token",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["token"], str) and body["token"]
    assert "expires_at" in body
    assert "read_only" not in body

    ws = _FakeDesktopWebSocket(token=body["token"])
    result = await _authenticate_desktop_or_close(ws, session_id=session_id)
    assert result is not None
    _account, row, _identity = result
    assert row.id == session_id
    assert ws.closed_with is None


async def test_desktop_token_is_single_use(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    import control_plane.routers.sandboxes as sandboxes_module
    from control_plane.routers.sandboxes import _authenticate_desktop_or_close

    monkeypatch.setattr(sandboxes_module.settings, "BOXKITE_DESKTOP_ENABLED", True)

    key = await signup_and_get_api_key(client, "desktop-replay@example.com", role="admin")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    from control_plane.security import create_desktop_token

    token, _ = create_desktop_token(account_id=account_id, session_id=session_id, ttl_seconds=30)

    first = await _authenticate_desktop_or_close(_FakeDesktopWebSocket(token=token), session_id=session_id)
    assert first is not None

    ws2 = _FakeDesktopWebSocket(token=token)
    second = await _authenticate_desktop_or_close(ws2, session_id=session_id)

    assert second is None
    assert ws2.closed_with[0] == 4401


# ── WS /{session_id}/desktop -- auth before accept() ─────────────────────


async def test_authenticate_desktop_rejects_missing_credentials(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_desktop_or_close

    key = await signup_and_get_api_key(client, "desktop-noauth@example.com")
    session_id = await _create_session(client, key)

    ws = _FakeDesktopWebSocket()
    result = await _authenticate_desktop_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4401


async def test_authenticate_desktop_rejects_member_role_key(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_desktop_or_close

    admin_key = await signup_and_get_api_key(client, "desktop-rbac-owner@example.com", role="admin")
    session_id = await _create_session(client, admin_key)

    token_response = await client.post(
        "/v1/auth/login",
        json={"email": "desktop-rbac-owner@example.com", "password": "hunter2pass"},
    )
    member_key_resp = await client.post(
        "/v1/api-keys",
        json={"name": "member key", "role": "member"},
        headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
    )
    member_key = member_key_resp.json()["key"]

    ws = _FakeDesktopWebSocket(authorization=f"Bearer {member_key}")
    result = await _authenticate_desktop_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4403


async def test_authenticate_desktop_accepts_admin_role_key(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_desktop_or_close

    key = await signup_and_get_api_key(client, "desktop-rbac-admin@example.com", role="admin")
    session_id = await _create_session(client, key)

    ws = _FakeDesktopWebSocket(authorization=f"Bearer {key}")
    result = await _authenticate_desktop_or_close(ws, session_id=session_id)

    assert result is not None
    _account, row, _identity = result
    assert row.id == session_id
    assert ws.closed_with is None


async def test_authenticate_desktop_rejects_unknown_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_desktop_or_close

    key = await signup_and_get_api_key(client, "desktop-unknown@example.com", role="admin")

    ws = _FakeDesktopWebSocket(authorization=f"Bearer {key}")
    result = await _authenticate_desktop_or_close(
        ws, session_id="00000000-0000-0000-0000-000000000000"
    )

    assert result is None
    assert ws.closed_with[0] == 4404


async def test_authenticate_desktop_rejects_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_desktop_or_close

    key_a = await signup_and_get_api_key(client, "desktop-victim2@example.com", role="admin")
    key_b = await signup_and_get_api_key(client, "desktop-attacker2@example.com", role="admin")
    session_id = await _create_session(client, key_a)

    ws = _FakeDesktopWebSocket(authorization=f"Bearer {key_b}")
    result = await _authenticate_desktop_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4404


async def test_authenticate_desktop_rejects_malformed_token(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_desktop_or_close

    key = await signup_and_get_api_key(client, "desktop-malformed@example.com")
    session_id = await _create_session(client, key)

    ws = _FakeDesktopWebSocket(token="not-a-real-jwt")
    result = await _authenticate_desktop_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4401


async def test_authenticate_desktop_rejects_takeover_token_used_as_desktop_token(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A token minted for a different purpose (same signing key, different
    `type` claim) must never be accepted here -- mirrors
    test_takeover_token_rejects_preview_token_used_as_takeover_token."""
    from control_plane.routers.sandboxes import _authenticate_desktop_or_close
    from control_plane.security import create_takeover_token

    key = await signup_and_get_api_key(client, "desktop-wrongtype@example.com")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    takeover_token, _ = create_takeover_token(account_id=account_id, session_id=session_id, ttl_seconds=30)

    ws = _FakeDesktopWebSocket(token=takeover_token)
    result = await _authenticate_desktop_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4401


# ── WS /{session_id}/desktop -- full route (mocked sidecar connection) ───


class _FakeSidecarDesktopWs:
    """Stands in for the `websockets` client connection to the sidecar's
    `/desktop` WS -- yields fixed frames then stops, and records anything
    sent to it."""

    def __init__(self, messages: list[bytes]) -> None:
        self._messages = list(messages)
        self.sent: list[bytes] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


class _FakeSidecarDesktopConnCtx:
    def __init__(self, ws: _FakeSidecarDesktopWs) -> None:
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


class _FakeClientDesktopWebSocket:
    """Stands in for the human's real WS for the full `desktop_sandbox`
    route -- `accept()`/`close()` are no-ops, `receive()` yields queued
    frames then disconnects, `send_bytes` records what was sent back."""

    def __init__(self, incoming: list[bytes], *, authorization: str | None = None) -> None:
        self._incoming = list(incoming)
        self.sent: list[bytes] = []
        self.accepted = False
        self.closed = False
        self.closed_with: tuple[int, str] | None = None
        self.headers = {"authorization": authorization} if authorization else {}
        self.query_params: dict[str, str] = {}

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict:
        if not self._incoming:
            from starlette.websockets import WebSocketDisconnect

            raise WebSocketDisconnect()
        return {"type": "websocket.receive", "bytes": self._incoming.pop(0)}

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.closed_with = (code, reason)


async def test_desktop_sandbox_route_404s_when_feature_disabled(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import desktop_sandbox

    key = await signup_and_get_api_key(client, "desktop-route-disabled@example.com", role="admin")
    session_id = await _create_session(client, key)

    ws = _FakeClientDesktopWebSocket(incoming=[])
    await desktop_sandbox(ws, session_id=session_id, manager=fake_manager)

    assert not ws.accepted


async def test_desktop_sandbox_route_relays_bytes_and_logs_start_end(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    import control_plane.routers.sandboxes as sandboxes_module
    from control_plane.repository import ExecLogEntryRepository
    from control_plane import db as db_module

    monkeypatch.setattr(sandboxes_module.settings, "BOXKITE_DESKTOP_ENABLED", True)

    key = await signup_and_get_api_key(client, "desktop-route-ok@example.com", role="admin")
    session_id = await _create_session(client, key)

    sidecar_ws = _FakeSidecarDesktopWs([b"framebuffer-bytes-1", b"framebuffer-bytes-2"])
    monkeypatch.setattr(
        sandboxes_module.websockets,
        "connect",
        lambda *a, **kw: _FakeSidecarDesktopConnCtx(sidecar_ws),
    )

    async def _fake_get_sidecar_desktop_target(session_id):
        return {
            "ws_url": f"wss://fake-sidecar.example/{session_id}/desktop",
            "auth_header": "X-Sidecar-Auth-Token",
            "auth_token": "fake-sidecar-token",
        }

    monkeypatch.setattr(
        fake_manager, "get_sidecar_desktop_target", _fake_get_sidecar_desktop_target, raising=False
    )

    ws = _FakeClientDesktopWebSocket(incoming=[b"mouse-move-bytes"], authorization=f"Bearer {key}")
    await sandboxes_module.desktop_sandbox(ws, session_id=session_id, manager=fake_manager)

    assert ws.accepted
    assert ws.closed
    assert ws.sent == [b"framebuffer-bytes-1", b"framebuffer-bytes-2"]
    assert sidecar_ws.sent == [b"mouse-move-bytes"]

    async with db_module.get_session_factory()() as db:
        entries = await ExecLogEntryRepository(db).list_for_session(
            session_id=session_id, limit=50, offset=0
        )
    operations = [e.operation for e in entries]
    assert "desktop_start" in operations
    assert "desktop_end" in operations
    end_entry = next(e for e in entries if e.operation == "desktop_end")
    assert end_entry.source == "human_desktop_takeover"
    assert end_entry.detail["bytes_relayed"] == len(b"mouse-move-bytesframebuffer-bytes-1framebuffer-bytes-2")
