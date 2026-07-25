"""Tests for the background process/session proxy endpoints
(`POST /processes`, `GET /processes`, `GET /processes/{id}/output`,
`POST /processes/{id}/input`, `POST /processes/{id}/stop`) — mirrors
test_sandbox_search.py's pattern: cross-tenant access must 404 identically
to GET/DELETE, and a successful call proxies straight through to
SandboxManager (faked here via FakeSandboxManager, no real K8s/sidecar).
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _start_process(
    client: httpx.AsyncClient, session_id: str, api_key: str, command: str = "sleep 30"
) -> str:
    resp = await client.post(
        f"/v1/sandboxes/{session_id}/processes",
        json={"command": command, "max_runtime_seconds": 60},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["process_id"]


# ── Cross-tenant isolation ───────────────────────────────────────────────


async def test_account_cannot_start_process_in_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "proc-victim@example.com")
    key_b = await signup_and_get_api_key(client, "proc-attacker@example.com")
    session_a_id = await _create_session(client, key_a)

    resp = await client.post(
        f"/v1/sandboxes/{session_a_id}/processes",
        json={"command": "sleep 30", "max_runtime_seconds": 60},
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_account_cannot_list_processes_in_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "proc-list-victim@example.com")
    key_b = await signup_and_get_api_key(client, "proc-list-attacker@example.com")
    session_a_id = await _create_session(client, key_a)
    await _start_process(client, session_a_id, key_a)

    resp = await client.get(
        f"/v1/sandboxes/{session_a_id}/processes",
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_account_cannot_get_process_output_in_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "proc-output-victim@example.com")
    key_b = await signup_and_get_api_key(client, "proc-output-attacker@example.com")
    session_a_id = await _create_session(client, key_a)
    process_id = await _start_process(client, session_a_id, key_a)

    resp = await client.get(
        f"/v1/sandboxes/{session_a_id}/processes/{process_id}/output",
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_account_cannot_stop_process_in_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "proc-stop-victim@example.com")
    key_b = await signup_and_get_api_key(client, "proc-stop-attacker@example.com")
    session_a_id = await _create_session(client, key_a)
    process_id = await _start_process(client, session_a_id, key_a)

    resp = await client.post(
        f"/v1/sandboxes/{session_a_id}/processes/{process_id}/stop",
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_start_process_against_unknown_session_id_returns_404(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "proc-unknown@example.com")

    resp = await client.post(
        "/v1/sandboxes/00000000-0000-0000-0000-000000000000/processes",
        json={"command": "sleep 30", "max_runtime_seconds": 60},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 404


async def test_process_routes_require_authentication(client: httpx.AsyncClient):
    resp = await client.post(
        "/v1/sandboxes/some-session/processes",
        json={"command": "sleep 30", "max_runtime_seconds": 60},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"


# ── Successful proxying to SandboxManager ───────────────────────────────


async def test_start_process_proxies_to_sandbox_manager(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "proc-happy@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/processes",
        json={"command": "npm run dev", "description": "dev server", "max_runtime_seconds": 1800},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["process_id"].startswith("proc_")
    assert body["status"] == "running"
    assert fake_manager.start_process_calls == [
        {
            "session_id": session_id,
            "command": "npm run dev",
            "description": "dev server",
            "max_runtime_seconds": 1800,
            "expose_port": None,
        }
    ]


async def test_start_process_requires_max_runtime_seconds(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "proc-missing-ceiling@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/processes",
        json={"command": "sleep 5"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 422


async def test_start_process_rejects_max_runtime_seconds_over_ceiling(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.schemas import SANDBOX_PROCESS_MAX_RUNTIME_SECONDS_CEILING

    key = await signup_and_get_api_key(client, "proc-over-ceiling@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/processes",
        json={"command": "sleep 5", "max_runtime_seconds": SANDBOX_PROCESS_MAX_RUNTIME_SECONDS_CEILING + 1},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 422


async def test_list_processes_proxies_to_sandbox_manager(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "proc-list-happy@example.com")
    session_id = await _create_session(client, key)
    process_id = await _start_process(client, session_id, key, command="sleep 60")

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/processes",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    processes = resp.json()["processes"]
    assert [p["process_id"] for p in processes] == [process_id]
    assert processes[0]["status"] == "running"


async def test_get_process_output_proxies_since_offset(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "proc-output-happy@example.com")
    session_id = await _create_session(client, key)
    process_id = await _start_process(client, session_id, key)
    entry = fake_manager._processes[session_id][process_id]
    entry["stdout"] = "hello world"

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/processes/{process_id}/output",
        params={"since_offset": 6},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["stdout_chunk"] == "world"
    assert body["next_offset"] == 11


async def test_get_process_output_unknown_process_id_returns_404(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "proc-output-unknown@example.com")
    session_id = await _create_session(client, key)

    resp = await client.get(
        f"/v1/sandboxes/{session_id}/processes/proc_doesnotexist/output",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_send_process_input_proxies_to_sandbox_manager(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "proc-input-happy@example.com")
    session_id = await _create_session(client, key)
    process_id = await _start_process(client, session_id, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/processes/{process_id}/input",
        json={"data": "y\n"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    assert resp.json()["bytes_written"] == 2


async def test_stop_process_proxies_to_sandbox_manager(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "proc-stop-happy@example.com")
    session_id = await _create_session(client, key)
    process_id = await _start_process(client, session_id, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/processes/{process_id}/stop",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "stopped"
    assert body["exit_code"] == 143


async def test_stop_process_unknown_process_id_returns_404(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "proc-stop-unknown@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/processes/proc_doesnotexist/stop",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_start_process_translates_sandbox_manager_failure_to_502(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "proc-failure@example.com")
    session_id = await _create_session(client, key)
    fake_manager.fail_next_start_process = True

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/processes",
        json={"command": "sleep 5", "max_runtime_seconds": 60},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "sandbox_operation_failed"
    assert "simulated sidecar transport failure" not in resp.text


# ── Rate limiting ────────────────────────────────────────────────────────


async def test_start_process_is_rate_limited_per_account(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_SANDBOX_RATE_LIMIT_PER_MINUTE", 3)

    key = await signup_and_get_api_key(client, "proc-rate-limited@example.com")
    session_id = await _create_session(client, key)

    for _ in range(3):
        resp = await client.post(
            f"/v1/sandboxes/{session_id}/processes",
            json={"command": "sleep 5", "max_runtime_seconds": 60},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/processes",
        json={"command": "sleep 5", "max_runtime_seconds": 60},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 429
    assert resp.json()["detail"]["error"]["code"] == "rate_limited"
