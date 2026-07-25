"""Tests for the three observability routes added per
`docs/SANDBOX-OBSERVABILITY-DESIGN.md`: `GET .../log`, `GET .../watch`, and
`WS .../takeover`. Mirrors test_sandbox_exec.py's fixtures and
test_exec_log_entries.py's pattern for asserting on ExecLogEntry rows
directly.

The takeover WebSocket proxy is only partially covered here -- see the
"WS /{session_id}/takeover" section's comment below for what is and isn't
verified without a live sidecar.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from conftest import FakeSandboxManager, create_api_key, signup, signup_and_get_api_key
from control_plane import db as db_module
from control_plane.models_orm import Account


async def _deactivate_account(account_id: str) -> None:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one()
        account.scim_deactivated_at = datetime.now(timezone.utc)
        await db.commit()


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── GET /{session_id}/log ────────────────────────────────────────────────


async def test_log_requires_authentication(client: httpx.AsyncClient):
    resp = await client.get("/v1/sandboxes/some-session/log")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"


async def test_log_404s_for_unknown_session(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "log-unknown@example.com")
    resp = await client.get(
        "/v1/sandboxes/00000000-0000-0000-0000-000000000000/log",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_log_404s_for_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "log-victim@example.com")
    key_b = await signup_and_get_api_key(client, "log-attacker@example.com")
    session_id = await _create_session(client, key_a)

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/log",
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_log_returns_entries_in_order_with_pagination(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "log-pagination@example.com")
    session_id = await _create_session(client, key)

    for i in range(5):
        resp = await client.post(
            f"/v1/sandboxes/{session_id}/exec",
            json={"command": f"echo {i}"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200

    first_page = await client.get(
        f"/v1/sandboxes/{session_id}/log",
        params={"limit": 2, "offset": 0},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert first_page.status_code == 200
    body = first_page.json()
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert body["total"] == 5
    assert [e["detail"]["command"] for e in body["entries"]] == ["echo 0", "echo 1"]

    second_page = await client.get(
        f"/v1/sandboxes/{session_id}/log",
        params={"limit": 2, "offset": 2},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert second_page.status_code == 200
    body2 = second_page.json()
    assert [e["detail"]["command"] for e in body2["entries"]] == ["echo 2", "echo 3"]

    last_page = await client.get(
        f"/v1/sandboxes/{session_id}/log",
        params={"limit": 2, "offset": 4},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert [e["detail"]["command"] for e in last_page.json()["entries"]] == ["echo 4"]


async def test_log_default_pagination_and_entry_shape(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "log-shape@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/log",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["total"] == 1
    entry = body["entries"][0]
    assert entry["session_id"] == session_id
    assert entry["source"] == "agent"
    assert entry["operation"] == "exec"
    assert entry["detail"] == {"command": "echo hi", "timeout": 30}
    assert entry["exit_code"] == 0
    assert "started_at" in entry
    assert "duration_ms" in entry
    # Hash-chain fields (GitHub issue #136, docs/TAMPER-EVIDENT-AUDIT-DESIGN.md)
    # must actually reach the JSON response, not just exist on the ORM row --
    # this is the API surface an external auditor independently verifies
    # against, so a regression here (e.g. building the response from a plain
    # dict instead of ExecLogEntryOut.model_validate(row)) must be caught.
    assert entry["row_hash"] is not None
    assert len(entry["row_hash"]) == 64
    assert entry["prev_hash"] is not None


async def test_log_rejects_limit_above_ceiling(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    from control_plane.schemas import SANDBOX_LOG_MAX_LIMIT

    key = await signup_and_get_api_key(client, "log-limit-ceiling@example.com")
    session_id = await _create_session(client, key)

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/log",
        params={"limit": SANDBOX_LOG_MAX_LIMIT + 1},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422


# ── GET /{session_id}/watch ──────────────────────────────────────────────


async def test_watch_requires_authentication(client: httpx.AsyncClient):
    resp = await client.get("/v1/sandboxes/some-session/watch")
    assert resp.status_code == 401


async def test_watch_404s_for_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "watch-victim@example.com")
    key_b = await signup_and_get_api_key(client, "watch-attacker@example.com")
    session_id = await _create_session(client, key_a)

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/watch",
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404


async def test_watch_route_wires_up_sse_streaming_response(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Verifies the route wires up StreamingResponse with the right media
    type/headers, calling the route function directly rather than through
    `client` -- httpx's ASGITransport (used by the `client` fixture) awaits
    the *entire* ASGI app call, including the full response body, before
    returning anything at all, so it can never be used against a
    never-ending SSE stream (not even to read just the headers); see
    `test_watch_event_stream_emits_existing_entries_as_sse` above for the
    generator-level coverage of the actual streamed content."""
    from control_plane import db as db_module
    from control_plane.repository import AccountRepository
    from control_plane.routers.sandboxes import watch_sandbox

    key = await signup_and_get_api_key(client, "watch-wiring@example.com")
    session_id = await _create_session(client, key)

    class _FakeRequest:
        async def is_disconnected(self) -> bool:
            return True

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_email("watch-wiring@example.com")
        response = await watch_sandbox(_FakeRequest(), session_id=session_id, account=account, db=db)

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"


async def test_watch_event_stream_emits_existing_entries_as_sse(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Exercises the `_watch_event_stream` generator directly (bounded via
    a fake Request whose `is_disconnected()` flips True after one poll) --
    this is the actual polling/SSE-formatting logic; the route-level test
    above only confirms it's wired up as a text/event-stream response."""
    from control_plane.routers.sandboxes import _watch_event_stream

    key = await signup_and_get_api_key(client, "watch-generator@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    class _FakeRequest:
        def __init__(self) -> None:
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            return self.calls > 1

    chunks = [chunk async for chunk in _watch_event_stream(_FakeRequest(), session_id=session_id)]

    assert len(chunks) == 1
    assert chunks[0].startswith("id: ")
    data_line = next(line for line in chunks[0].splitlines() if line.startswith("data: "))
    event = json.loads(data_line[len("data: "):])
    assert event["operation"] == "exec"
    assert event["detail"] == {"command": "echo hi", "timeout": 30}
    assert event["source"] == "agent"


async def test_watch_event_stream_picks_up_entries_written_between_polls(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Deterministic (no timing/concurrency) version of "watch sees new
    rows as they're written" -- writes a new entry between the generator's
    first and second poll instead of racing a background task against
    asyncio.sleep."""
    from control_plane.routers.sandboxes import _watch_event_stream

    key = await signup_and_get_api_key(client, "watch-generator-live@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo first"},
        headers={"Authorization": f"Bearer {key}"},
    )

    class _FakeRequest:
        def __init__(self) -> None:
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            if self.calls == 2:
                await client.post(
                    f"/v1/sandboxes/{session_id}/exec",
                    json={"command": "echo second"},
                    headers={"Authorization": f"Bearer {key}"},
                )
            return self.calls > 2

    chunks = [chunk async for chunk in _watch_event_stream(_FakeRequest(), session_id=session_id)]

    commands = []
    for chunk in chunks:
        data_line = next(line for line in chunk.splitlines() if line.startswith("data: "))
        commands.append(json.loads(data_line[len("data: "):])["detail"]["command"])

    assert commands == ["echo first", "echo second"]


# ── WS /{session_id}/takeover ─────────────────────────────────────────────
#
# `starlette.testclient.TestClient.websocket_connect` runs the ASGI app on
# its own background thread/event loop, which deadlocks against this test
# suite's aiosqlite engine (bound to the *outer* pytest-asyncio loop --
# aiosqlite/asyncio primitives are loop-bound, see conftest.py's
# `_reset_create_session_lock` fixture docstring for the same class of
# problem elsewhere in this suite). So instead of driving the route via a
# real WebSocket handshake, these call `_authenticate_takeover_or_close`
# directly against a minimal fake WebSocket, in the same event loop as
# every other test here -- it is the exact function the real
# `WS /takeover` route calls before `accept()`, so this still exercises the
# real auth-before-accept logic end to end at the function level.
#
# HONEST GAP: the full proxy-to-sidecar relay (bidirectional byte
# forwarding, the periodic/at-close ExecLogEntry writes for a real PTY
# session, and the real WebSocket handshake/close-code plumbing around
# `_authenticate_takeover_or_close`) is NOT exercised end-to-end against a
# live sidecar `/pty` WebSocket in this test suite -- FakeSandboxManager
# has no `/pty` equivalent, and standing up a real nsenter/PTY sidecar (or
# a working full-stack WebSocket test harness for this app) is out of
# scope for control-plane's own test suite under this deadline. That code
# path (the relay loop, `manager.get_sidecar_pty_target`, the real
# `websocket.accept()`/`.close()` calls) was verified by reading, not by a
# passing integration test.


class _FakeTakeoverWebSocket:
    """Duck-types just enough of `starlette.websockets.WebSocket` for
    `_authenticate_takeover_or_close` -- `.headers`, `.query_params`, and an
    async `.close()` that records its arguments instead of touching a real
    ASGI connection."""

    def __init__(
        self,
        *,
        authorization: str | None = None,
        api_key: str | None = None,
        token: str | None = None,
    ) -> None:
        self.headers = {"authorization": authorization} if authorization else {}
        query_params: dict[str, str] = {}
        if api_key:
            query_params["api_key"] = api_key
        if token:
            query_params["token"] = token
        self.query_params = query_params
        self.closed_with: tuple[int, str] | None = None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


async def test_takeover_rejects_missing_credentials_before_accepting(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "takeover-noauth@example.com")
    session_id = await _create_session(client, key)

    ws = _FakeTakeoverWebSocket()
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with is not None
    assert ws.closed_with[0] == 4401


async def test_takeover_rejects_invalid_api_key_before_accepting(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "takeover-badkey@example.com")
    session_id = await _create_session(client, key)

    ws = _FakeTakeoverWebSocket(authorization="Bearer bxk_live_not_a_real_key")
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4401


async def test_takeover_rejects_unknown_session_before_accepting(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "takeover-unknown@example.com")

    ws = _FakeTakeoverWebSocket(authorization=f"Bearer {key}")
    result = await _authenticate_takeover_or_close(
        ws, session_id="00000000-0000-0000-0000-000000000000"
    )

    assert result is None
    assert ws.closed_with[0] == 4404


async def test_takeover_rejects_another_accounts_session_before_accepting(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key_a = await signup_and_get_api_key(client, "takeover-victim@example.com")
    key_b = await signup_and_get_api_key(client, "takeover-attacker@example.com")
    session_id = await _create_session(client, key_a)

    ws = _FakeTakeoverWebSocket(authorization=f"Bearer {key_b}")
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4404


async def test_takeover_rejects_destroyed_session_before_accepting(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "takeover-destroyed@example.com")
    session_id = await _create_session(client, key)
    destroy_resp = await client.delete(
        f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert destroy_resp.status_code == 204

    ws = _FakeTakeoverWebSocket(authorization=f"Bearer {key}")
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4404


async def test_takeover_accepts_valid_owned_session_without_closing(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "takeover-valid@example.com")
    session_id = await _create_session(client, key)

    ws = _FakeTakeoverWebSocket(authorization=f"Bearer {key}")
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is not None
    account, row, read_only, _identity = result
    assert row.id == session_id
    assert row.account_id == account.id
    assert read_only is False
    assert ws.closed_with is None


async def test_takeover_rejects_long_lived_api_key_via_query_param(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Regression test for the fix this route received: an `api_key` query
    parameter (the long-lived, full-privilege credential) is no longer
    accepted for takeover at all -- only `Authorization` header (for
    non-browser clients) or a short-lived, single-use `?token=` minted by
    `POST .../takeover-token` (for browser clients) -- see SECURITY.md's
    "Human takeover" section. A valid key presented this way must be
    rejected exactly like a missing credential, not silently accepted."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "takeover-queryparam@example.com")
    session_id = await _create_session(client, key)

    ws = _FakeTakeoverWebSocket(api_key=key)
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with is not None
    assert ws.closed_with[0] == 4401


async def test_takeover_rejects_invalid_api_key_via_query_param(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """An `api_key` query param is ignored entirely now (see the test
    above), so an invalid one behaves identically to a missing credential
    -- still 4401, just via the "missing_credentials" path rather than
    "invalid_api_key"."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "takeover-badquery@example.com")
    session_id = await _create_session(client, key)

    ws = _FakeTakeoverWebSocket(api_key="bxk_live_not_a_real_key")
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4401


# ── WS /{session_id}/takeover -- RBAC (API key `role`) ───────────────────


async def test_takeover_rejects_member_role_key_via_header(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """The fine-grained RBAC gate this route received: a 'member'-role API
    key is otherwise perfectly valid (session ownership checks out) but
    must not be allowed to initiate a takeover session at all."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    admin_key = await signup_and_get_api_key(client, "takeover-rbac-owner@example.com", role="admin")
    session_id = await _create_session(client, admin_key)

    token_response = await client.post(
        "/v1/auth/login",
        json={"email": "takeover-rbac-owner@example.com", "password": "hunter2pass"},
    )
    assert token_response.status_code == 200
    member_key_resp = await client.post(
        "/v1/api-keys",
        json={"name": "member key", "role": "member"},
        headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
    )
    assert member_key_resp.status_code == 201
    member_key = member_key_resp.json()["key"]

    ws = _FakeTakeoverWebSocket(authorization=f"Bearer {member_key}")
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with is not None
    assert ws.closed_with[0] == 4403


async def test_takeover_accepts_admin_role_key_via_header(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """The default role ('admin') must keep working exactly like before
    RBAC was added -- no regression for the common case."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "takeover-rbac-admin@example.com", role="admin")
    session_id = await _create_session(client, key)

    ws = _FakeTakeoverWebSocket(authorization=f"Bearer {key}")
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is not None
    account, row, read_only, _identity = result
    assert row.id == session_id
    assert row.account_id == account.id
    assert read_only is False
    assert ws.closed_with is None


# ── WS /{session_id}/takeover -- takeover-token auth (browser clients) ───


async def test_takeover_accepts_valid_takeover_token_via_query_param(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """The replacement for the removed `?api_key=` path: a short-lived,
    single-use token minted via `security.create_takeover_token`,
    redeemed as `?token=`."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close
    from control_plane.security import create_takeover_token

    key = await signup_and_get_api_key(client, "takeover-token-valid@example.com")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    assert account_resp.status_code == 200
    account_id = account_resp.json()["id"]

    token, _expires_at = create_takeover_token(account_id=account_id, session_id=session_id, ttl_seconds=30)

    ws = _FakeTakeoverWebSocket(token=token)
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is not None
    account, row, read_only, _identity = result
    assert row.id == session_id
    assert read_only is False
    assert ws.closed_with is None


async def test_takeover_token_is_single_use(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close
    from control_plane.security import create_takeover_token

    key = await signup_and_get_api_key(client, "takeover-token-replay@example.com")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]
    token, _ = create_takeover_token(account_id=account_id, session_id=session_id, ttl_seconds=30)

    first = await _authenticate_takeover_or_close(_FakeTakeoverWebSocket(token=token), session_id=session_id)
    assert first is not None

    ws2 = _FakeTakeoverWebSocket(token=token)
    second = await _authenticate_takeover_or_close(ws2, session_id=session_id)

    assert second is None
    assert ws2.closed_with[0] == 4401


async def test_takeover_token_rejects_wrong_session_binding(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close
    from control_plane.security import create_takeover_token

    key = await signup_and_get_api_key(client, "takeover-token-wrongsession@example.com")
    session_id = await _create_session(client, key)
    other_session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    token, _ = create_takeover_token(account_id=account_id, session_id=session_id, ttl_seconds=30)

    ws = _FakeTakeoverWebSocket(token=token)
    result = await _authenticate_takeover_or_close(ws, session_id=other_session_id)

    assert result is None
    assert ws.closed_with[0] == 4401


async def test_takeover_token_rejects_expired_token(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close
    from control_plane.security import create_takeover_token

    key = await signup_and_get_api_key(client, "takeover-token-expired@example.com")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    token, _ = create_takeover_token(account_id=account_id, session_id=session_id, ttl_seconds=-1)

    ws = _FakeTakeoverWebSocket(token=token)
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4401


async def test_takeover_token_rejects_malformed_token(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "takeover-token-malformed@example.com")
    session_id = await _create_session(client, key)

    ws = _FakeTakeoverWebSocket(token="not-a-real-jwt")
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4401


async def test_takeover_token_rejects_preview_token_used_as_takeover_token(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A token minted for a different purpose (same signing key, different
    `type` claim) must never be accepted here -- the `type` check in
    `decode_takeover_token` is load-bearing, not decorative."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close
    from control_plane.security import create_preview_token

    key = await signup_and_get_api_key(client, "takeover-token-wrongtype@example.com")
    session_id = await _create_session(client, key)

    preview_token, _, _ = create_preview_token(session_id=session_id, port=8080, ttl_seconds=30)

    ws = _FakeTakeoverWebSocket(token=preview_token)
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4401


async def test_takeover_token_rejects_deactivated_account(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A takeover token is minted from an already-authenticated (and
    therefore already deactivation-checked) API key, but its short TTL
    (BOXKITE_TAKEOVER_TOKEN_TTL_SECONDS, default 30s) is still a real
    window: an account deactivated between mint and redemption must not be
    able to complete the takeover WS handshake on the strength of a token
    minted moments before."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close
    from control_plane.security import create_takeover_token

    key = await signup_and_get_api_key(client, "takeover-token-deactivated@example.com")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    token, _expires_at = create_takeover_token(account_id=account_id, session_id=session_id, ttl_seconds=30)
    await _deactivate_account(account_id)

    ws = _FakeTakeoverWebSocket(token=token)
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is None
    assert ws.closed_with[0] == 4401


# ── security.py: create_takeover_token/decode_takeover_token -- `read_only`
# claim (GitHub issue #131) ───────────────────────────────────────────────


def test_create_takeover_token_defaults_read_only_claim_to_false():
    """Additive-claim regression: a caller that never passes `read_only`
    (every pre-existing caller) still gets a token whose decoded payload
    carries `read_only: False`, not a missing key that would make
    `.get("read_only", False)` matter -- both must degrade to the same
    default-off behavior."""
    from control_plane.security import create_takeover_token, decode_takeover_token

    token, _expires_at = create_takeover_token(account_id="acct-1", session_id="sess-1", ttl_seconds=30)

    payload = decode_takeover_token(token)
    assert payload["read_only"] is False


def test_create_takeover_token_with_read_only_true_sets_claim():
    from control_plane.security import create_takeover_token, decode_takeover_token

    token, _expires_at = create_takeover_token(
        account_id="acct-1", session_id="sess-1", ttl_seconds=30, read_only=True
    )

    payload = decode_takeover_token(token)
    assert payload["read_only"] is True


# ── WS /{session_id}/takeover -- `read_only` takeover tokens (issue #131) ──


async def test_authenticate_takeover_surfaces_read_only_true_from_token(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """`_authenticate_takeover_or_close` must still accept a `read_only`
    token's WS upgrade (observer mode is not a rejection) and report the
    claim back to the caller so `takeover_sandbox` can enforce it."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close
    from control_plane.security import create_takeover_token

    key = await signup_and_get_api_key(client, "takeover-readonly-token@example.com")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    token, _ = create_takeover_token(account_id=account_id, session_id=session_id, ttl_seconds=30, read_only=True)

    ws = _FakeTakeoverWebSocket(token=token)
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is not None
    account, row, read_only, _identity = result
    assert row.id == session_id
    assert read_only is True
    assert ws.closed_with is None


async def test_authenticate_takeover_via_header_is_never_read_only(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """The `Authorization` header path (a direct API key, not a minted
    token) has no `read_only` concept -- it must always report
    `read_only=False`, regardless of the key's role."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "takeover-header-never-readonly@example.com")
    session_id = await _create_session(client, key)

    ws = _FakeTakeoverWebSocket(authorization=f"Bearer {key}")
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is not None
    _account, _row, read_only, _identity = result
    assert read_only is False


class _FakeClientRelayWebSocket:
    """Duck-types just enough of `starlette.websockets.WebSocket` for
    `_relay_client_to_sidecar`: yields each of `messages` in turn from
    `receive()`, then raises `WebSocketDisconnect` once exhausted -- the
    same "receive loop ends on disconnect" shape the real route relies on."""

    def __init__(self, messages: list[bytes]) -> None:
        self._messages = list(messages)

    async def receive(self) -> dict:
        if not self._messages:
            from starlette.websockets import WebSocketDisconnect

            raise WebSocketDisconnect()
        return {"type": "websocket.receive", "bytes": self._messages.pop(0)}


class _FakeSidecarRelayWs:
    """Records every byte string handed to `.send()` -- stands in for the
    real `websockets` client connection to the sidecar's PTY WS."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


async def test_relay_client_to_sidecar_forwards_input_when_not_read_only():
    """Regression for the default (non-read-only) path: every received byte
    is still both forwarded to the sidecar and mirrored into the audit
    buffer, unchanged from before this claim existed."""
    from control_plane.routers.sandboxes import _relay_client_to_sidecar

    client_ws = _FakeClientRelayWebSocket([b"ls\n", b"pwd\n"])
    sidecar_ws = _FakeSidecarRelayWs()
    typed_buffer = bytearray()

    await _relay_client_to_sidecar(client_ws, sidecar_ws, typed_buffer, read_only=False)

    assert sidecar_ws.sent == [b"ls\n", b"pwd\n"]
    assert bytes(typed_buffer) == b"ls\npwd\n"


async def test_relay_client_to_sidecar_drops_input_when_read_only():
    """The actual enforcement point for GitHub issue #131: when
    `read_only=True`, client->PTY bytes must never reach the sidecar and
    must never be mirrored into the audit buffer, even though the receive
    loop keeps running (so a real disconnect is still detected normally)."""
    from control_plane.routers.sandboxes import _relay_client_to_sidecar

    client_ws = _FakeClientRelayWebSocket([b"rm -rf /\n", b"curl evil.example\n"])
    sidecar_ws = _FakeSidecarRelayWs()
    typed_buffer = bytearray()

    await _relay_client_to_sidecar(client_ws, sidecar_ws, typed_buffer, read_only=True)

    assert sidecar_ws.sent == []
    assert bytes(typed_buffer) == b""


async def test_relay_client_to_sidecar_read_only_defaults_to_false():
    """`read_only` is keyword-only with a default of `False` -- every
    pre-existing call site (none of which pass it) keeps forwarding input,
    exactly as before this claim was added."""
    from control_plane.routers.sandboxes import _relay_client_to_sidecar

    client_ws = _FakeClientRelayWebSocket([b"echo hi\n"])
    sidecar_ws = _FakeSidecarRelayWs()
    typed_buffer = bytearray()

    await _relay_client_to_sidecar(client_ws, sidecar_ws, typed_buffer)

    assert sidecar_ws.sent == [b"echo hi\n"]
    assert bytes(typed_buffer) == b"echo hi\n"


# ── POST /{session_id}/takeover-token ─────────────────────────────────────


async def test_mint_takeover_token_requires_authentication(client: httpx.AsyncClient):
    resp = await client.post("/v1/sandboxes/some-session/takeover-token")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"


async def test_mint_takeover_token_404s_for_unknown_session(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "mint-takeover-unknown@example.com")
    resp = await client.post(
        "/v1/sandboxes/00000000-0000-0000-0000-000000000000/takeover-token",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_mint_takeover_token_404s_for_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "mint-takeover-victim@example.com")
    key_b = await signup_and_get_api_key(client, "mint-takeover-attacker@example.com")
    session_id = await _create_session(client, key_a)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/takeover-token",
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_mint_takeover_token_403s_for_member_role_key(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    admin_key = await signup_and_get_api_key(client, "mint-takeover-rbac@example.com", role="admin")
    session_id = await _create_session(client, admin_key)

    token_response = await client.post(
        "/v1/auth/login", json={"email": "mint-takeover-rbac@example.com", "password": "hunter2pass"}
    )
    member_key_resp = await client.post(
        "/v1/api-keys",
        json={"name": "member key", "role": "member"},
        headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
    )
    member_key = member_key_resp.json()["key"]

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/takeover-token",
        headers={"Authorization": f"Bearer {member_key}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "takeover_not_permitted"


async def test_mint_takeover_token_succeeds_for_admin_role_key_and_token_is_redeemable(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "mint-takeover-ok@example.com", role="admin")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/takeover-token",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["token"], str) and body["token"]
    assert "expires_at" in body
    assert body["read_only"] is False

    ws = _FakeTakeoverWebSocket(token=body["token"])
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)
    assert result is not None
    _account, _row, read_only, _identity = result
    assert read_only is False
    assert ws.closed_with is None


async def test_mint_takeover_token_omitting_body_defaults_to_full_control(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """No JSON body at all (the shape every pre-existing caller uses) must
    keep minting a full-control token -- this is the "don't break the
    default/non-read-only path" requirement for GitHub issue #131."""
    key = await signup_and_get_api_key(client, "mint-takeover-no-body@example.com", role="admin")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/takeover-token",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["read_only"] is False


async def test_mint_takeover_token_with_read_only_true_mints_read_only_token(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """`POST .../takeover-token` with `{"read_only": true}` mints a token
    that (1) reports `read_only: true` in its own response and (2) redeems,
    via `WS .../takeover`'s auth path, into a `read_only=True` result."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    key = await signup_and_get_api_key(client, "mint-takeover-readonly@example.com", role="admin")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/takeover-token",
        json={"read_only": True},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["read_only"] is True

    ws = _FakeTakeoverWebSocket(token=body["token"])
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)
    assert result is not None
    _account, _row, read_only, _identity = result
    assert read_only is True
    assert ws.closed_with is None


async def test_mint_takeover_token_403s_for_member_role_key_even_with_read_only_true(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """RBAC is still checked before the `read_only` claim is ever read --
    a `read_only` observer token is still a takeover-session credential, so
    a 'member'-role key must not be able to mint one either."""
    admin_key = await signup_and_get_api_key(client, "mint-takeover-rbac-readonly@example.com", role="admin")
    session_id = await _create_session(client, admin_key)

    token_response = await client.post(
        "/v1/auth/login", json={"email": "mint-takeover-rbac-readonly@example.com", "password": "hunter2pass"}
    )
    member_key_resp = await client.post(
        "/v1/api-keys",
        json={"name": "member key", "role": "member"},
        headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
    )
    member_key = member_key_resp.json()["key"]

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/takeover-token",
        json={"read_only": True},
        headers={"Authorization": f"Bearer {member_key}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "takeover_not_permitted"


async def test_get_current_account_via_api_key_or_query_accepts_header(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Regression coverage for the shared dependency /watch actually uses:
    header still works exactly like the header-only dependency every other
    route uses."""
    from control_plane import db as db_module
    from control_plane.deps import get_current_account_via_api_key_or_query

    key = await signup_and_get_api_key(client, "watch-dep-header@example.com")

    async with db_module.get_session_factory()() as db:
        account = await get_current_account_via_api_key_or_query(
            authorization=f"Bearer {key}", api_key=None, db=db
        )
    assert account.email == "watch-dep-header@example.com"


async def test_get_current_account_via_api_key_or_query_accepts_query_param(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Regression test for the actual bug this fixed: EventSource cannot
    set a custom Authorization header, so /watch must also accept the key
    as a query parameter -- this must resolve the same account, not 401."""
    from control_plane import db as db_module
    from control_plane.deps import get_current_account_via_api_key_or_query

    key = await signup_and_get_api_key(client, "watch-dep-query@example.com")

    async with db_module.get_session_factory()() as db:
        account = await get_current_account_via_api_key_or_query(
            authorization=None, api_key=key, db=db
        )
    assert account.email == "watch-dep-query@example.com"


async def test_get_current_account_via_api_key_or_query_rejects_neither(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane import db as db_module
    from control_plane.deps import get_current_account_via_api_key_or_query
    from control_plane.errors import ApiError

    async with db_module.get_session_factory()() as db:
        with pytest.raises(ApiError):
            await get_current_account_via_api_key_or_query(authorization=None, api_key=None, db=db)


# ── WS /{session_id}/takeover -- full-duplex PTY recording (issue #133) ──
#
# Mirrors the `_relay_client_to_sidecar`/`_relay_sidecar_to_client` unit-test
# style above: the relay helpers are tested directly against fake WS/sidecar
# objects (see `_FakeClientRelayWebSocket`/`_FakeSidecarRelayWs` above), and
# `finalize_takeover_recording`'s own upload/redaction behavior is covered
# in test_pty_recording.py -- these tests only cover the wiring between the
# two (the `recording=` parameter threading through the relay functions,
# and the full route's `takeover_end` detail folding in the recording
# pointer), not asciicast serialization itself.


class _FakeSidecarAsyncIterWs:
    """`_relay_sidecar_to_client` iterates `async for data in sidecar_ws`
    -- this stands in for the real `websockets` client connection with a
    fixed list of frames, then stops (mirroring the sidecar closing its
    end)."""

    def __init__(self, messages: list[bytes]) -> None:
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


async def test_relay_client_to_sidecar_records_input_when_recording_passed():
    from control_plane.pty_recording import PtyRecordingBuffer
    from control_plane.routers.sandboxes import _relay_client_to_sidecar

    client_ws = _FakeClientRelayWebSocket([b"ls\n", b"pwd\n"])
    sidecar_ws = _FakeSidecarRelayWs()
    typed_buffer = bytearray()
    recording = PtyRecordingBuffer()

    await _relay_client_to_sidecar(client_ws, sidecar_ws, typed_buffer, recording=recording)

    assert recording.event_count == 2


async def test_relay_client_to_sidecar_does_not_record_when_read_only():
    """A read-only observer's dropped input must not be mirrored into the
    recording either -- same exemption `typed_buffer` already gets."""
    from control_plane.pty_recording import PtyRecordingBuffer
    from control_plane.routers.sandboxes import _relay_client_to_sidecar

    client_ws = _FakeClientRelayWebSocket([b"rm -rf /\n"])
    sidecar_ws = _FakeSidecarRelayWs()
    typed_buffer = bytearray()
    recording = PtyRecordingBuffer()

    await _relay_client_to_sidecar(client_ws, sidecar_ws, typed_buffer, read_only=True, recording=recording)

    assert recording.event_count == 0


async def test_relay_client_to_sidecar_recording_defaults_to_none_and_is_a_noop():
    """No pre-existing call site (nor the read-only variants above) passes
    `recording` -- must not raise when omitted."""
    from control_plane.routers.sandboxes import _relay_client_to_sidecar

    client_ws = _FakeClientRelayWebSocket([b"echo hi\n"])
    sidecar_ws = _FakeSidecarRelayWs()
    typed_buffer = bytearray()

    await _relay_client_to_sidecar(client_ws, sidecar_ws, typed_buffer)  # must not raise

    assert bytes(typed_buffer) == b"echo hi\n"


class _FakeClientSendWebSocket:
    """Stands in for the human's WS for `_relay_sidecar_to_client` --
    records every `send_bytes` call."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


async def test_relay_sidecar_to_client_records_output_when_recording_passed():
    from control_plane.pty_recording import PtyRecordingBuffer
    from control_plane.routers.sandboxes import _relay_sidecar_to_client

    sidecar_ws = _FakeSidecarAsyncIterWs([b"file.txt\n", b"$ "])
    client_ws = _FakeClientSendWebSocket()
    recording = PtyRecordingBuffer()

    await _relay_sidecar_to_client(client_ws, sidecar_ws, recording=recording)

    assert recording.event_count == 2
    assert client_ws.sent == [b"file.txt\n", b"$ "]


async def test_relay_sidecar_to_client_recording_defaults_to_none_and_is_a_noop():
    from control_plane.routers.sandboxes import _relay_sidecar_to_client

    sidecar_ws = _FakeSidecarAsyncIterWs([b"hello\n"])
    client_ws = _FakeClientSendWebSocket()

    await _relay_sidecar_to_client(client_ws, sidecar_ws)  # must not raise

    assert client_ws.sent == [b"hello\n"]


def test_build_takeover_end_detail_without_recording_result():
    from control_plane.routers.sandboxes import TakeoverApiKeyIdentity, _build_takeover_end_detail

    identity = TakeoverApiKeyIdentity(api_key_id=None, api_key_name=None)
    detail = _build_takeover_end_detail(bytes_typed=42, recording_result=None, identity=identity)

    assert detail == {"bytes_typed": 42, "api_key_id": None, "api_key_name": None}


def test_build_takeover_end_detail_folds_in_recording_pointer():
    from control_plane.routers.sandboxes import TakeoverApiKeyIdentity, _build_takeover_end_detail

    identity = TakeoverApiKeyIdentity(api_key_id="key-1", api_key_name="Alice's key")
    detail = _build_takeover_end_detail(
        bytes_typed=42,
        recording_result={"storage_key": "takeover-recordings/acct-1/sess-1/123.cast", "bytes": 999, "truncated": False},
        identity=identity,
    )

    assert detail == {
        "bytes_typed": 42,
        "api_key_id": "key-1",
        "api_key_name": "Alice's key",
        "recording_storage_key": "takeover-recordings/acct-1/sess-1/123.cast",
        "recording_bytes": 999,
        "recording_truncated": False,
    }


async def test_takeover_finally_uploads_recording_and_folds_pointer_into_end_detail(
    fake_snapshot_storage,
):
    """End-to-end (within this process) exercise of the three new pieces
    together: a populated PtyRecordingBuffer -> finalize_takeover_recording
    -> _build_takeover_end_detail, using the same FakeSnapshotStorageClient
    the `client` fixture wires up for the real route via
    `get_snapshot_storage`."""
    from control_plane.pty_recording import PtyRecordingBuffer, finalize_takeover_recording
    from control_plane.routers.sandboxes import TakeoverApiKeyIdentity, _build_takeover_end_detail

    recording = PtyRecordingBuffer()
    recording.record("o", b"$ ")
    recording.record("i", b"whoami\n")
    recording.record("o", b"sandbox\n")

    recording_result = await finalize_takeover_recording(
        recording, storage=fake_snapshot_storage, account_id="acct-1", session_id="sess-1"
    )
    identity = TakeoverApiKeyIdentity(api_key_id=None, api_key_name=None)
    detail = _build_takeover_end_detail(bytes_typed=7, recording_result=recording_result, identity=identity)

    assert detail["bytes_typed"] == 7
    assert detail["recording_storage_key"].startswith("takeover-recordings/acct-1/sess-1/")
    assert detail["recording_truncated"] is False
    assert fake_snapshot_storage.upload_calls[0]["key"] == detail["recording_storage_key"]


# ── GET /{session_id}/takeover-recordings/{entry_id} (GitHub issue #133 --
# the "replay" half) ──────────────────────────────────────────────────────


async def _seed_takeover_end_entry(*, session_id: str, account_id: str, detail: dict) -> str:
    """Seeds a `takeover_end` ExecLogEntry row directly via the repository,
    bypassing a full WS takeover session -- this route's own tests only
    care about the read-side (auth/ownership/detail-pointer resolution),
    which the full-route tests below (recording ownership, audit identity)
    already exercise end to end via a real `takeover_sandbox` invocation."""
    from control_plane import db as db_module
    from control_plane.repository import ExecLogEntryRepository

    async with db_module.get_session_factory()() as db:
        entry = await ExecLogEntryRepository(db).create(
            session_id=session_id,
            account_id=account_id,
            source="human_takeover",
            operation="takeover_end",
            detail=detail,
            exit_code=None,
            output_truncated=None,
            started_at=datetime.now(timezone.utc),
            duration_ms=1000,
        )
    return entry.id


async def test_get_takeover_recording_requires_authentication(client: httpx.AsyncClient):
    resp = await client.get("/v1/sandboxes/some-session/takeover-recordings/some-entry")
    assert resp.status_code == 401


async def test_get_takeover_recording_404s_for_unknown_session(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "recording-unknown-session@example.com")
    resp = await client.get(
        "/v1/sandboxes/00000000-0000-0000-0000-000000000000/takeover-recordings/some-entry",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_get_takeover_recording_404s_for_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "recording-victim@example.com")
    key_b = await signup_and_get_api_key(client, "recording-attacker@example.com")
    session_id = await _create_session(client, key_a)

    account_a_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key_a}"})
    account_a_id = account_a_resp.json()["id"]
    entry_id = await _seed_takeover_end_entry(
        session_id=session_id,
        account_id=account_a_id,
        detail={"recording_storage_key": "takeover-recordings/a/1.cast", "recording_bytes": 5, "recording_truncated": False},
    )

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/takeover-recordings/{entry_id}",
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_get_takeover_recording_404s_when_entry_belongs_to_another_account(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Distinct from the test above: here the caller owns the *session* in
    the URL, but `entry_id` itself belongs to a different account entirely
    -- this is the structural, DB-layer `ExecLogEntryRepository.get_for_account`
    scoping doing its job, not the session-ownership check above."""
    key_a = await signup_and_get_api_key(client, "recording-entry-victim@example.com")
    key_b = await signup_and_get_api_key(client, "recording-entry-attacker@example.com")
    session_a = await _create_session(client, key_a)
    session_b = await _create_session(client, key_b)

    account_b_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key_b}"})
    account_b_id = account_b_resp.json()["id"]
    entry_id = await _seed_takeover_end_entry(
        session_id=session_b,
        account_id=account_b_id,
        detail={"recording_storage_key": "takeover-recordings/b/1.cast", "recording_bytes": 5, "recording_truncated": False},
    )

    resp = await client.get(
        f"/v1/sandboxes/{session_a}/takeover-recordings/{entry_id}",
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_get_takeover_recording_404s_for_session_id_mismatch(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Same account, but `entry_id` belongs to a *different session* than
    the one named in the URL -- must still 404, not silently serve a
    recording under the wrong session's URL."""
    key = await signup_and_get_api_key(client, "recording-session-mismatch@example.com")
    session_1 = await _create_session(client, key)
    session_2 = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    entry_id = await _seed_takeover_end_entry(
        session_id=session_1,
        account_id=account_id,
        detail={"recording_storage_key": "takeover-recordings/a/1.cast", "recording_bytes": 5, "recording_truncated": False},
    )

    resp = await client.get(
        f"/v1/sandboxes/{session_2}/takeover-recordings/{entry_id}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_get_takeover_recording_404s_for_non_takeover_end_row(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "recording-wrong-op@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key}"},
    )
    log_resp = await client.get(f"/v1/sandboxes/{session_id}/log", headers={"Authorization": f"Bearer {key}"})
    entry_id = log_resp.json()["entries"][0]["id"]

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/takeover-recordings/{entry_id}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_get_takeover_recording_404s_when_no_recording_pointer(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A real `takeover_end` row, but with no `recording_storage_key` in its
    detail -- e.g. recording was disabled, or nothing was ever typed or
    printed during that session -- must 404, not 500 or serve empty bytes."""
    key = await signup_and_get_api_key(client, "recording-no-pointer@example.com")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    entry_id = await _seed_takeover_end_entry(session_id=session_id, account_id=account_id, detail={"bytes_typed": 0})

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/takeover-recordings/{entry_id}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_get_takeover_recording_returns_asciicast_bytes(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, fake_snapshot_storage
):
    key = await signup_and_get_api_key(client, "recording-success@example.com")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    storage_key = f"takeover-recordings/{account_id}/{session_id}/123.cast"
    cast_bytes = b'{"version":2,"width":80,"height":24}\n[0.1,"o","$ "]\n'
    await fake_snapshot_storage.upload_bytes(key=storage_key, data=cast_bytes, content_type="application/x-asciicast")

    entry_id = await _seed_takeover_end_entry(
        session_id=session_id,
        account_id=account_id,
        detail={"recording_storage_key": storage_key, "recording_bytes": len(cast_bytes), "recording_truncated": False},
    )

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/takeover-recordings/{entry_id}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.content == cast_bytes
    assert resp.headers["content-type"] == "application/x-asciicast"


async def test_get_takeover_recording_works_after_session_destroyed(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, fake_snapshot_storage
):
    """The recording is durable object-storage content, independent of the
    sandbox pod's lifecycle -- must remain fetchable after DELETE .../{id}."""
    key = await signup_and_get_api_key(client, "recording-after-destroy@example.com")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    storage_key = f"takeover-recordings/{account_id}/{session_id}/456.cast"
    cast_bytes = b'{"version":2}\n'
    await fake_snapshot_storage.upload_bytes(key=storage_key, data=cast_bytes)
    entry_id = await _seed_takeover_end_entry(
        session_id=session_id,
        account_id=account_id,
        detail={"recording_storage_key": storage_key, "recording_bytes": len(cast_bytes), "recording_truncated": False},
    )

    destroy_resp = await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})
    assert destroy_resp.status_code == 204

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/takeover-recordings/{entry_id}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.content == cast_bytes


# ── WS /{session_id}/takeover -- full-route drive (GitHub issue #132 design
# doc §5/§6/§9's concurrency fixes) ────────────────────────────────────────
#
# Unlike every other WS /takeover test above (which call
# `_authenticate_takeover_or_close` directly -- see the module comment near
# the top of this file for why a real WebSocket handshake deadlocks this
# suite's event loop), the tests below call `takeover_sandbox` itself
# directly as a plain coroutine, with fakes standing in for both the
# client-facing WebSocket (`_FakeFullTakeoverWebSocket`) and the
# `websockets.connect(...)` call to the sidecar's own `/pty` WS
# (`_FakeSidecarConnCtx`/`_FakeSidecarFullDuplexWs`, monkeypatched in place
# of the real `websockets` module). This is the only way to exercise the
# recording-ownership registry (`_acquire_takeover_recording`/
# `_release_takeover_recording`) and the per-connection audit-identity
# folding under two genuinely concurrent connections without a real sidecar.


class _FakeFullTakeoverWebSocket:
    """Duck-types enough of `starlette.websockets.WebSocket` to drive the
    *entire* `takeover_sandbox` route function directly: `.headers`,
    `.query_params`, `.accept()`, `.receive()` (yielding each of `messages`
    then raising `WebSocketDisconnect`), `.send_bytes()`, and `.close()`."""

    def __init__(self, *, authorization: str | None = None, token: str | None = None, messages: list[bytes] | None = None) -> None:
        self.headers = {"authorization": authorization} if authorization else {}
        query_params: dict[str, str] = {}
        if token:
            query_params["token"] = token
        self.query_params = query_params
        self._messages = list(messages or [])
        self.accepted = False
        self.closed_with: tuple[int, str] | None = None
        self.sent_bytes: list[bytes] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict:
        if not self._messages:
            from starlette.websockets import WebSocketDisconnect

            raise WebSocketDisconnect()
        return {"type": "websocket.receive", "bytes": self._messages.pop(0)}

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


class _ConcurrentAttachBarrier:
    """Forces N concurrent `takeover_sandbox` invocations to genuinely
    overlap -- each blocks in `_FakeSidecarConnCtx.__aenter__` (i.e. AFTER
    that connection has already acquired its share of the recording
    registry, but BEFORE its relay tasks start) until all `expected`
    connections have arrived. Without this, `asyncio.gather` provides no
    guarantee two coroutines are ever simultaneously "attached" rather than
    running fully sequentially -- which would defeat the point of a test
    for a *concurrency* bug fix."""

    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.count = 0
        self.event = asyncio.Event()

    async def arrive_and_wait(self) -> None:
        self.count += 1
        if self.count >= self.expected:
            self.event.set()
        await self.event.wait()


class _FakeSidecarFullDuplexWs:
    """Stands in for the real `websockets` client connection to the
    sidecar's `/pty` WS for a full `takeover_sandbox` drive: supports
    `async for` (`_relay_sidecar_to_client`) over a fixed list of output
    frames, and records everything sent via `.send()`
    (`_relay_client_to_sidecar`)."""

    def __init__(self, output_frames: list[bytes] | None = None) -> None:
        self._frames = list(output_frames or [])
        self.sent: list[bytes] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


class _FakeSidecarConnCtx:
    """Stands in for `websockets.connect(...)`'s return value -- an async
    context manager yielding a `_FakeSidecarFullDuplexWs`. `barrier`, if
    given, is awaited inside `__aenter__` (see `_ConcurrentAttachBarrier`)."""

    def __init__(self, ws: _FakeSidecarFullDuplexWs, barrier: _ConcurrentAttachBarrier | None = None) -> None:
        self._ws = ws
        self._barrier = barrier

    async def __aenter__(self) -> _FakeSidecarFullDuplexWs:
        if self._barrier is not None:
            await self._barrier.arrive_and_wait()
        return self._ws

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


async def test_concurrent_takeover_connections_produce_exactly_one_recording(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, fake_snapshot_storage, monkeypatch
):
    """The actual regression test for the Bug A fix (GitHub issue #132
    design doc §6/§9): two genuinely concurrent WS /takeover connections to
    the SAME session_id must produce exactly one uploaded recording (one
    `finalize_takeover_recording` call, one storage object) -- not two
    redundant, overlapping ones -- even though each connection still writes
    its own independent `takeover_start`/`takeover_end` audit rows (section
    5's "per-connection attribution already works correctly" is unaffected
    by this fix; only the recording pointer itself is deduplicated, landing
    on exactly one of the two `takeover_end` rows)."""
    import control_plane.routers.sandboxes as sandboxes_module
    from control_plane.routers.sandboxes import takeover_sandbox

    barrier = _ConcurrentAttachBarrier(expected=2)
    monkeypatch.setattr(
        sandboxes_module.websockets,
        "connect",
        lambda *a, **kw: _FakeSidecarConnCtx(_FakeSidecarFullDuplexWs([b"welcome\n"]), barrier=barrier),
    )
    monkeypatch.setattr(sandboxes_module.settings, "BOXKITE_TAKEOVER_RECORDING_ENABLED", True)

    key = await signup_and_get_api_key(client, "concurrent-recording@example.com", role="admin")
    session_id = await _create_session(client, key)

    ws_a = _FakeFullTakeoverWebSocket(authorization=f"Bearer {key}", messages=[b"echo a\n"])
    ws_b = _FakeFullTakeoverWebSocket(authorization=f"Bearer {key}", messages=[b"echo b\n"])

    await asyncio.gather(
        takeover_sandbox(ws_a, session_id=session_id, manager=fake_manager, storage=fake_snapshot_storage),
        takeover_sandbox(ws_b, session_id=session_id, manager=fake_manager, storage=fake_snapshot_storage),
    )

    assert len(fake_snapshot_storage.upload_calls) == 1

    log_resp = await client.get(f"/v1/sandboxes/{session_id}/log", headers={"Authorization": f"Bearer {key}"})
    entries = log_resp.json()["entries"]
    end_rows = [e for e in entries if e["operation"] == "takeover_end"]
    assert len(end_rows) == 2
    rows_with_recording = [e for e in end_rows if "recording_storage_key" in e["detail"]]
    assert len(rows_with_recording) == 1
    assert rows_with_recording[0]["detail"]["recording_storage_key"] == fake_snapshot_storage.upload_calls[0]["key"]


async def test_concurrent_takeover_connections_three_connections_produce_exactly_one_recording(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, fake_snapshot_storage, monkeypatch
):
    """3-connection generalization of the test above: the reference-counted
    registry (`_acquire_takeover_recording`/`_release_takeover_recording`)
    must keep exactly one shared recording alive until the LAST of N
    concurrently-attached connections releases it, for N > 2 too -- not just
    the 2-connection case the original regression test exercised."""
    import control_plane.routers.sandboxes as sandboxes_module
    from control_plane.routers.sandboxes import takeover_sandbox

    barrier = _ConcurrentAttachBarrier(expected=3)
    monkeypatch.setattr(
        sandboxes_module.websockets,
        "connect",
        lambda *a, **kw: _FakeSidecarConnCtx(_FakeSidecarFullDuplexWs([b"welcome\n"]), barrier=barrier),
    )
    monkeypatch.setattr(sandboxes_module.settings, "BOXKITE_TAKEOVER_RECORDING_ENABLED", True)

    key = await signup_and_get_api_key(client, "concurrent-recording-3@example.com", role="admin")
    session_id = await _create_session(client, key)

    ws_a = _FakeFullTakeoverWebSocket(authorization=f"Bearer {key}", messages=[b"echo a\n"])
    ws_b = _FakeFullTakeoverWebSocket(authorization=f"Bearer {key}", messages=[b"echo b\n"])
    ws_c = _FakeFullTakeoverWebSocket(authorization=f"Bearer {key}", messages=[b"echo c\n"])

    await asyncio.gather(
        takeover_sandbox(ws_a, session_id=session_id, manager=fake_manager, storage=fake_snapshot_storage),
        takeover_sandbox(ws_b, session_id=session_id, manager=fake_manager, storage=fake_snapshot_storage),
        takeover_sandbox(ws_c, session_id=session_id, manager=fake_manager, storage=fake_snapshot_storage),
    )

    assert len(fake_snapshot_storage.upload_calls) == 1

    log_resp = await client.get(f"/v1/sandboxes/{session_id}/log", headers={"Authorization": f"Bearer {key}"})
    entries = log_resp.json()["entries"]
    end_rows = [e for e in entries if e["operation"] == "takeover_end"]
    assert len(end_rows) == 3
    rows_with_recording = [e for e in end_rows if "recording_storage_key" in e["detail"]]
    assert len(rows_with_recording) == 1
    assert rows_with_recording[0]["detail"]["recording_storage_key"] == fake_snapshot_storage.upload_calls[0]["key"]
    assert session_id not in sandboxes_module._takeover_recordings


async def test_takeover_flush_typed_snapshot_exception_still_releases_recording(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, fake_snapshot_storage, monkeypatch
):
    """Regression test for the GitHub issue #133 adversarial-review bug:
    `takeover_sandbox`'s teardown `finally` block used to run
    `_flush_typed_snapshot(...)` BEFORE `_release_takeover_recording(...)`,
    unguarded. `_flush_typed_snapshot` -> `_log_takeover_entry` ->
    `_log_exec_entry` does a raw, unguarded DB write (unlike
    `_fire_audit_log_webhook_event` elsewhere in this module, which IS
    wrapped in try/except specifically because audit logging must never
    crash the calling teardown path) -- if it raised, `_release_takeover_
    recording` never ran, so `session_id`'s entry in the process-global
    `_takeover_recordings` registry leaked permanently: any later connection
    to the same session_id would reuse the stale, already-abandoned buffer
    with an artificially-inflated ref count, and the recording would never
    be finalized/uploaded again for that session.

    Simulates the exact failure by monkeypatching `_flush_typed_snapshot` to
    raise, then asserts the registry entry is cleaned up (not leaked) and
    the recording is still finalized/uploaded despite the flush failure."""
    import control_plane.routers.sandboxes as sandboxes_module
    from control_plane.routers.sandboxes import takeover_sandbox

    monkeypatch.setattr(
        sandboxes_module.websockets,
        "connect",
        lambda *a, **kw: _FakeSidecarConnCtx(_FakeSidecarFullDuplexWs([b"welcome\n"])),
    )
    monkeypatch.setattr(sandboxes_module.settings, "BOXKITE_TAKEOVER_RECORDING_ENABLED", True)

    async def _boom(**kwargs):
        raise RuntimeError("simulated DB failure during typed-snapshot flush")

    monkeypatch.setattr(sandboxes_module, "_flush_typed_snapshot", _boom)

    key = await signup_and_get_api_key(client, "flush-exception@example.com", role="admin")
    session_id = await _create_session(client, key)

    ws = _FakeFullTakeoverWebSocket(authorization=f"Bearer {key}", messages=[b"echo hi\n"])

    # Must not raise -- the flush failure is caught and logged, not
    # propagated out of the teardown path.
    await takeover_sandbox(ws, session_id=session_id, manager=fake_manager, storage=fake_snapshot_storage)

    assert session_id not in sandboxes_module._takeover_recordings
    assert len(fake_snapshot_storage.upload_calls) == 1


async def test_takeover_recording_registry_shares_buffer_within_a_session_and_isolates_across_sessions():
    """Lower-level unit coverage of the registry itself, independent of the
    full-route drive above: same session_id shares one buffer and only the
    last release finalizes; different session_ids never share a buffer."""
    from control_plane.pty_recording import PtyRecordingBuffer
    from control_plane.routers.sandboxes import (
        _acquire_takeover_recording,
        _release_takeover_recording,
        reset_takeover_recordings_registry_for_tests,
    )

    reset_takeover_recordings_registry_for_tests()
    try:
        buffer_a1 = _acquire_takeover_recording("session-x")
        buffer_a2 = _acquire_takeover_recording("session-x")
        assert buffer_a1 is buffer_a2
        assert isinstance(buffer_a1, PtyRecordingBuffer)

        buffer_b = _acquire_takeover_recording("session-y")
        assert buffer_b is not buffer_a1

        assert _release_takeover_recording("session-x") is False
        assert _release_takeover_recording("session-x") is True
        assert _release_takeover_recording("session-y") is True
    finally:
        reset_takeover_recordings_registry_for_tests()


# ── WS /{session_id}/takeover -- audit-identity threading (GitHub issue #132
# design doc §5/§9) ────────────────────────────────────────────────────────


async def test_authenticate_takeover_via_header_returns_key_identity(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close

    token_response = await signup(client, "takeover-identity-header@example.com")
    key_response = await create_api_key(client, token_response["access_token"], name="Alice's laptop", role="admin")
    session_id = await _create_session(client, key_response["key"])

    ws = _FakeTakeoverWebSocket(authorization=f"Bearer {key_response['key']}")
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is not None
    _account, _row, _read_only, identity = result
    assert identity.api_key_id == key_response["id"]
    assert identity.api_key_name == "Alice's laptop"


async def test_authenticate_takeover_via_token_resolves_key_identity(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close
    from control_plane.security import create_takeover_token

    token_response = await signup(client, "takeover-identity-token@example.com")
    key_response = await create_api_key(client, token_response["access_token"], name="Bob's CLI", role="admin")
    session_id = await _create_session(client, key_response["key"])

    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key_response['key']}"})
    account_id = account_resp.json()["id"]

    takeover_token, _ = create_takeover_token(
        account_id=account_id, session_id=session_id, ttl_seconds=30, api_key_id=key_response["id"]
    )

    ws = _FakeTakeoverWebSocket(token=takeover_token)
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is not None
    _account, _row, _read_only, identity = result
    assert identity.api_key_id == key_response["id"]
    assert identity.api_key_name == "Bob's CLI"


async def test_authenticate_takeover_via_token_without_api_key_id_claim_has_no_identity(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A token minted before the `api_key_id` claim existed (or minted
    without passing it) must degrade to a fully-empty identity, not raise."""
    from control_plane.routers.sandboxes import _authenticate_takeover_or_close
    from control_plane.security import create_takeover_token

    key = await signup_and_get_api_key(client, "takeover-identity-token-legacy@example.com", role="admin")
    session_id = await _create_session(client, key)
    account_resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {key}"})
    account_id = account_resp.json()["id"]

    takeover_token, _ = create_takeover_token(account_id=account_id, session_id=session_id, ttl_seconds=30)

    ws = _FakeTakeoverWebSocket(token=takeover_token)
    result = await _authenticate_takeover_or_close(ws, session_id=session_id)

    assert result is not None
    _account, _row, _read_only, identity = result
    assert identity.api_key_id is None
    assert identity.api_key_name is None


async def test_takeover_route_folds_api_key_identity_into_audit_rows(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, fake_snapshot_storage, monkeypatch
):
    import control_plane.routers.sandboxes as sandboxes_module
    from control_plane.routers.sandboxes import takeover_sandbox

    monkeypatch.setattr(
        sandboxes_module.websockets,
        "connect",
        lambda *a, **kw: _FakeSidecarConnCtx(_FakeSidecarFullDuplexWs([b"welcome\n"])),
    )

    token_response = await signup(client, "takeover-identity-route@example.com")
    key_response = await create_api_key(client, token_response["access_token"], name="Carol's key", role="admin")
    session_id = await _create_session(client, key_response["key"])

    ws = _FakeFullTakeoverWebSocket(authorization=f"Bearer {key_response['key']}", messages=[b"whoami\n"])
    await takeover_sandbox(ws, session_id=session_id, manager=fake_manager, storage=fake_snapshot_storage)

    log_resp = await client.get(
        f"/v1/sandboxes/{session_id}/log", headers={"Authorization": f"Bearer {key_response['key']}"}
    )
    entries = log_resp.json()["entries"]
    start_row = next(e for e in entries if e["operation"] == "takeover_start")
    end_row = next(e for e in entries if e["operation"] == "takeover_end")
    assert start_row["detail"]["api_key_id"] == key_response["id"]
    assert start_row["detail"]["api_key_name"] == "Carol's key"
    assert end_row["detail"]["api_key_id"] == key_response["id"]
    assert end_row["detail"]["api_key_name"] == "Carol's key"


async def test_two_different_api_keys_produce_distinguishable_takeover_audit_rows(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, fake_snapshot_storage, monkeypatch
):
    """The actual regression test for the Bug B fix (GitHub issue #132
    design doc §5/§9): two different admin-role API keys under the SAME
    account, used for two separate takeover sessions against the same
    sandbox session, must produce `takeover_end` rows whose `detail`
    distinguishes which key was behind each -- previously both would have
    been indistinguishable, tagged only with the shared `account_id`."""
    import control_plane.routers.sandboxes as sandboxes_module
    from control_plane.routers.sandboxes import takeover_sandbox

    monkeypatch.setattr(
        sandboxes_module.websockets,
        "connect",
        lambda *a, **kw: _FakeSidecarConnCtx(_FakeSidecarFullDuplexWs([b"welcome\n"])),
    )

    token_response = await signup(client, "takeover-two-keys@example.com")
    key_alice = await create_api_key(client, token_response["access_token"], name="Alice's key", role="admin")
    key_bob = await create_api_key(client, token_response["access_token"], name="Bob's key", role="admin")
    session_id = await _create_session(client, key_alice["key"])

    ws_alice = _FakeFullTakeoverWebSocket(authorization=f"Bearer {key_alice['key']}", messages=[b"echo alice\n"])
    await takeover_sandbox(ws_alice, session_id=session_id, manager=fake_manager, storage=fake_snapshot_storage)

    ws_bob = _FakeFullTakeoverWebSocket(authorization=f"Bearer {key_bob['key']}", messages=[b"echo bob\n"])
    await takeover_sandbox(ws_bob, session_id=session_id, manager=fake_manager, storage=fake_snapshot_storage)

    log_resp = await client.get(
        f"/v1/sandboxes/{session_id}/log", headers={"Authorization": f"Bearer {key_alice['key']}"}
    )
    entries = log_resp.json()["entries"]
    end_rows = [e for e in entries if e["operation"] == "takeover_end"]
    assert len(end_rows) == 2
    api_key_ids = {row["detail"]["api_key_id"] for row in end_rows}
    assert api_key_ids == {key_alice["id"], key_bob["id"]}
    api_key_names = {row["detail"]["api_key_name"] for row in end_rows}
    assert api_key_names == {"Alice's key", "Bob's key"}
