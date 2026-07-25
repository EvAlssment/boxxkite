"""Tests for the exec/file-op proxy endpoints
(`/exec`, `/files`, `/files/view`, `/files/str-replace`) — the operational
counterpart to create/list/delete. Mirrors test_sandbox_cross_tenant.py's
pattern: cross-tenant access must 404 identically to GET/DELETE, and a
successful call proxies straight through to SandboxManager (faked here via
FakeSandboxManager, no real K8s/sidecar).
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── Cross-tenant isolation ───────────────────────────────────────────────


async def test_account_cannot_exec_against_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "exec-victim@example.com")
    key_b = await signup_and_get_api_key(client, "exec-attacker@example.com")
    session_a_id = await _create_session(client, key_a)

    resp = await client.post(
        f"/v1/sandboxes/{session_a_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert fake_manager.exec_calls == []


async def test_account_cannot_create_file_against_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "file-victim@example.com")
    key_b = await signup_and_get_api_key(client, "file-attacker@example.com")
    session_a_id = await _create_session(client, key_a)

    resp = await client.post(
        f"/v1/sandboxes/{session_a_id}/files",
        json={"path": "hello.txt", "content": "hi"},
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert fake_manager._files.get(session_a_id, {}) == {}


async def test_account_cannot_view_file_in_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "view-victim@example.com")
    key_b = await signup_and_get_api_key(client, "view-attacker@example.com")
    session_a_id = await _create_session(client, key_a)
    await client.post(
        f"/v1/sandboxes/{session_a_id}/files",
        json={"path": "secret.txt", "content": "top secret"},
        headers={"Authorization": f"Bearer {key_a}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_a_id}/files/view",
        json={"path": "secret.txt"},
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_account_cannot_str_replace_in_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "replace-victim@example.com")
    key_b = await signup_and_get_api_key(client, "replace-attacker@example.com")
    session_a_id = await _create_session(client, key_a)
    await client.post(
        f"/v1/sandboxes/{session_a_id}/files",
        json={"path": "config.py", "content": "DEBUG = False"},
        headers={"Authorization": f"Bearer {key_a}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_a_id}/files/str-replace",
        json={"path": "config.py", "old_str": "False", "new_str": "True"},
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    # The victim's file must be untouched.
    view_as_a = await client.post(
        f"/v1/sandboxes/{session_a_id}/files/view",
        json={"path": "config.py"},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert view_as_a.json()["content"] == "DEBUG = False"


async def test_exec_against_unknown_session_id_returns_404(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "exec-unknown@example.com")

    resp = await client.post(
        "/v1/sandboxes/00000000-0000-0000-0000-000000000000/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 404


async def test_exec_against_destroyed_session_returns_404(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "exec-destroyed@example.com")
    session_id = await _create_session(client, key)
    destroy_resp = await client.delete(
        f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert destroy_resp.status_code == 204

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 404
    assert fake_manager.exec_calls == []


async def test_sandbox_exec_routes_require_authentication(client: httpx.AsyncClient):
    resp = await client.post(
        "/v1/sandboxes/some-session/exec",
        json={"command": "echo hi"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"


# ── Successful proxying to SandboxManager ───────────────────────────────


async def test_exec_proxies_command_to_sandbox_manager(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "exec-happy@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hello", "timeout": 45},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["exit_code"] == 0
    assert body["stdout"] == "ran: echo hello"
    assert fake_manager.exec_calls == [
        {"session_id": session_id, "command": "echo hello", "timeout": 45}
    ]


async def test_exec_timeout_is_clamped_by_request_validation(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "exec-timeout@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi", "timeout": 99999},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 422


async def test_exec_timeout_above_manager_request_timeout_margin_is_rejected(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """The exec `timeout` ceiling must stay below SandboxManager's
    REQUEST_TIMEOUT (120s) to the sidecar, or a long-running command would
    httpx.ReadTimeout on the manager side before the sidecar's own timeout
    ever fires, orphaning the sidecar-side process."""
    from control_plane.schemas import SANDBOX_EXEC_MAX_TIMEOUT_SECONDS

    key = await signup_and_get_api_key(client, "exec-timeout-boundary@example.com")
    session_id = await _create_session(client, key)

    too_long = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi", "timeout": SANDBOX_EXEC_MAX_TIMEOUT_SECONDS + 1},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert too_long.status_code == 422

    at_ceiling = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi", "timeout": SANDBOX_EXEC_MAX_TIMEOUT_SECONDS},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert at_ceiling.status_code == 200

    from boxkite.manager import REQUEST_TIMEOUT

    assert SANDBOX_EXEC_MAX_TIMEOUT_SECONDS < REQUEST_TIMEOUT


async def test_exec_translates_sandbox_manager_failure_to_502(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "exec-failure@example.com")
    session_id = await _create_session(client, key)
    fake_manager.fail_next_exec = True

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "sandbox_operation_failed"
    # The raw exception message must never leak to the caller.
    assert "simulated sidecar transport failure" not in resp.text


async def test_file_create_view_str_replace_round_trip(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "file-roundtrip@example.com")
    session_id = await _create_session(client, key)

    create_resp = await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "config.py", "content": "DEBUG = False"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert create_resp.status_code == 200
    assert create_resp.json() == {"path": "config.py", "size": 13, "created": True}

    view_resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/view",
        json={"path": "config.py"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert view_resp.status_code == 200
    assert view_resp.json()["content"] == "DEBUG = False"

    replace_resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/str-replace",
        json={"path": "config.py", "old_str": "False", "new_str": "True"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert replace_resp.status_code == 200
    assert replace_resp.json() == {"path": "config.py", "replaced": True, "occurrences": 1}

    final_view = await client.post(
        f"/v1/sandboxes/{session_id}/files/view",
        json={"path": "config.py"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert final_view.json()["content"] == "DEBUG = True"


async def test_view_missing_file_translates_to_502_not_raw_exception(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "file-missing@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/view",
        json={"path": "does-not-exist.txt"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "sandbox_operation_failed"


# ── Rate limiting ────────────────────────────────────────────────────────


async def test_exec_is_rate_limited_per_account(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_SANDBOX_RATE_LIMIT_PER_MINUTE", 3)

    key = await signup_and_get_api_key(client, "exec-rate-limited@example.com")
    session_id = await _create_session(client, key)

    for _ in range(3):
        resp = await client.post(
            f"/v1/sandboxes/{session_id}/exec",
            json={"command": "echo hi"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 429
    assert resp.json()["detail"]["error"]["code"] == "rate_limited"


async def test_file_create_rejects_oversized_content(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.schemas import SANDBOX_FILE_CONTENT_MAX_LENGTH

    key = await signup_and_get_api_key(client, "file-oversized@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "big.txt", "content": "x" * (SANDBOX_FILE_CONTENT_MAX_LENGTH + 1)},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 422
    assert fake_manager._files.get(session_id, {}) == {}


async def test_str_replace_rejects_oversized_new_str(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.schemas import SANDBOX_FILE_CONTENT_MAX_LENGTH

    key = await signup_and_get_api_key(client, "str-replace-oversized@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "config.py", "content": "DEBUG = False"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/str-replace",
        json={
            "path": "config.py",
            "old_str": "False",
            "new_str": "x" * (SANDBOX_FILE_CONTENT_MAX_LENGTH + 1),
        },
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 422


async def test_exec_rate_limit_is_scoped_per_account_not_global(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_SANDBOX_RATE_LIMIT_PER_MINUTE", 1)

    key_a = await signup_and_get_api_key(client, "exec-rate-a@example.com")
    key_b = await signup_and_get_api_key(client, "exec-rate-b@example.com")
    session_a = await _create_session(client, key_a)
    session_b = await _create_session(client, key_b)

    resp_a = await client.post(
        f"/v1/sandboxes/{session_a}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert resp_a.status_code == 200

    resp_b = await client.post(
        f"/v1/sandboxes/{session_b}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp_b.status_code == 200
