"""Tests for GET/PUT/DELETE /v1/account/allowed-commands (persisted per-account
command allowlist) and its enforcement on POST /v1/sandboxes/{id}/exec.

Unrestricted by default is the load-bearing invariant here: an account that
has never called PUT must see identical exec behavior to before this
feature existed.
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _auth(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


# ── GET/PUT/DELETE round-trip ─────────────────────────────────────────────


async def test_allowed_commands_defaults_to_empty_and_unrestricted(client: httpx.AsyncClient):
    api_key = await signup_and_get_api_key(client, "allowlist-default@example.com")

    resp = await client.get("/v1/account/allowed-commands", headers=_auth(api_key))

    assert resp.status_code == 200
    assert resp.json() == {"rules": []}


async def test_put_then_get_round_trips_rules(client: httpx.AsyncClient):
    api_key = await signup_and_get_api_key(client, "allowlist-roundtrip@example.com")
    rules = [
        "grep",
        {"command": "curl", "args_allow": [r"https?://localhost(:\d+)?/"], "args_deny": [r"-X\s*POST"]},
    ]

    put_resp = await client.put("/v1/account/allowed-commands", json={"rules": rules}, headers=_auth(api_key))
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["rules"] == rules

    get_resp = await client.get("/v1/account/allowed-commands", headers=_auth(api_key))
    assert get_resp.json()["rules"] == rules


async def test_delete_clears_back_to_unrestricted(client: httpx.AsyncClient):
    api_key = await signup_and_get_api_key(client, "allowlist-clear@example.com")
    await client.put("/v1/account/allowed-commands", json={"rules": ["ls"]}, headers=_auth(api_key))

    delete_resp = await client.delete("/v1/account/allowed-commands", headers=_auth(api_key))
    assert delete_resp.status_code == 204

    get_resp = await client.get("/v1/account/allowed-commands", headers=_auth(api_key))
    assert get_resp.json() == {"rules": []}


async def test_put_requires_api_key_not_dashboard_token(client: httpx.AsyncClient):
    from conftest import signup

    signup_resp = await signup(client, "allowlist-wrong-cred@example.com")

    resp = await client.put(
        "/v1/account/allowed-commands",
        json={"rules": ["ls"]},
        headers={"Authorization": f"Bearer {signup_resp['access_token']}"},
    )
    assert resp.status_code == 401


# ── Write-time validation ─────────────────────────────────────────────────


async def test_put_rejects_invalid_regex(client: httpx.AsyncClient):
    api_key = await signup_and_get_api_key(client, "allowlist-badregex@example.com")

    resp = await client.put(
        "/v1/account/allowed-commands",
        json={"rules": [{"command": "curl", "args_allow": ["(unclosed"]}]},
        headers=_auth(api_key),
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_pattern"


async def test_put_rejects_oversized_pattern(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_MAX_ALLOWLIST_PATTERN_LENGTH", 5)
    api_key = await signup_and_get_api_key(client, "allowlist-longpattern@example.com")

    resp = await client.put(
        "/v1/account/allowed-commands",
        json={"rules": [{"command": "curl", "args_allow": ["this-pattern-is-too-long"]}]},
        headers=_auth(api_key),
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "pattern_too_long"


async def test_put_rejects_too_many_rules(client: httpx.AsyncClient, monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_MAX_ALLOWLIST_RULES", 2)
    api_key = await signup_and_get_api_key(client, "allowlist-toomany@example.com")

    resp = await client.put(
        "/v1/account/allowed-commands",
        json={"rules": ["ls", "grep", "cat"]},
        headers=_auth(api_key),
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "too_many_rules"


async def test_put_rejects_empty_rules_list(client: httpx.AsyncClient):
    api_key = await signup_and_get_api_key(client, "allowlist-empty@example.com")

    resp = await client.put("/v1/account/allowed-commands", json={"rules": []}, headers=_auth(api_key))

    assert resp.status_code == 422  # Pydantic min_length=1 on rules


# ── Enforcement on exec ────────────────────────────────────────────────────


async def test_exec_unrestricted_by_default(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    api_key = await signup_and_get_api_key(client, "allowlist-exec-default@example.com")
    session_id = await _create_session(client, api_key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec", json={"command": "rm -rf /tmp/whatever"}, headers=_auth(api_key)
    )

    assert resp.status_code == 200
    assert len(fake_manager.exec_calls) == 1


async def test_exec_blocked_when_command_not_in_custom_allowlist(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    api_key = await signup_and_get_api_key(client, "allowlist-exec-blocked@example.com")
    session_id = await _create_session(client, api_key)
    await client.put("/v1/account/allowed-commands", json={"rules": ["ls"]}, headers=_auth(api_key))

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec", json={"command": "cat /etc/passwd"}, headers=_auth(api_key)
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "command_not_allowed"
    assert fake_manager.exec_calls == []


async def test_exec_allowed_when_command_in_custom_allowlist(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    api_key = await signup_and_get_api_key(client, "allowlist-exec-allowed@example.com")
    session_id = await _create_session(client, api_key)
    await client.put("/v1/account/allowed-commands", json={"rules": ["ls"]}, headers=_auth(api_key))

    resp = await client.post(f"/v1/sandboxes/{session_id}/exec", json={"command": "ls -la"}, headers=_auth(api_key))

    assert resp.status_code == 200
    assert len(fake_manager.exec_calls) == 1


async def test_exec_enforces_args_deny(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    api_key = await signup_and_get_api_key(client, "allowlist-exec-argsdeny@example.com")
    session_id = await _create_session(client, api_key)
    await client.put(
        "/v1/account/allowed-commands",
        json={"rules": [{"command": "curl", "args_deny": [r"-X\s*POST"]}]},
        headers=_auth(api_key),
    )

    blocked = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "curl -X POST https://example.com"},
        headers=_auth(api_key),
    )
    allowed = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "curl https://example.com"},
        headers=_auth(api_key),
    )

    assert blocked.status_code == 403
    assert allowed.status_code == 200


async def test_allowed_commands_do_not_leak_across_accounts(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "allowlist-isolation-a@example.com")
    key_b = await signup_and_get_api_key(client, "allowlist-isolation-b@example.com")
    await client.put("/v1/account/allowed-commands", json={"rules": ["ls"]}, headers=_auth(key_a))

    session_b = await _create_session(client, key_b)
    resp = await client.post(
        f"/v1/sandboxes/{session_b}/exec", json={"command": "cat /etc/passwd"}, headers=_auth(key_b)
    )

    assert resp.status_code == 200  # account B never set a custom allowlist
