"""Tests for the read-only search proxy endpoints
(`/files/ls`, `/files/glob`, `/files/grep`) — mirrors test_sandbox_exec.py's
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


async def test_account_cannot_ls_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "ls-victim@example.com")
    key_b = await signup_and_get_api_key(client, "ls-attacker@example.com")
    session_a_id = await _create_session(client, key_a)

    resp = await client.post(
        f"/v1/sandboxes/{session_a_id}/files/ls",
        json={"path": "/"},
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_account_cannot_glob_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "glob-victim@example.com")
    key_b = await signup_and_get_api_key(client, "glob-attacker@example.com")
    session_a_id = await _create_session(client, key_a)

    resp = await client.post(
        f"/v1/sandboxes/{session_a_id}/files/glob",
        json={"pattern": "**/*.py"},
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_account_cannot_grep_another_accounts_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "grep-victim@example.com")
    key_b = await signup_and_get_api_key(client, "grep-attacker@example.com")
    session_a_id = await _create_session(client, key_a)
    await client.post(
        f"/v1/sandboxes/{session_a_id}/files",
        json={"path": "secret.txt", "content": "top secret token"},
        headers={"Authorization": f"Bearer {key_a}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_a_id}/files/grep",
        json={"pattern": "secret"},
        headers={"Authorization": f"Bearer {key_b}"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_ls_against_unknown_session_id_returns_404(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "ls-unknown@example.com")

    resp = await client.post(
        "/v1/sandboxes/00000000-0000-0000-0000-000000000000/files/ls",
        json={"path": "/"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 404


async def test_search_routes_require_authentication(client: httpx.AsyncClient):
    resp = await client.post(
        "/v1/sandboxes/some-session/files/ls",
        json={"path": "/"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"


# ── Successful proxying to SandboxManager ───────────────────────────────


async def test_ls_proxies_to_sandbox_manager(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "ls-happy@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "hello.txt", "content": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/ls",
        json={"path": "/"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert {"path": "hello.txt", "is_dir": False, "size": 2} in entries


async def test_ls_defaults_path_to_root(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "ls-default@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/ls",
        json={},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"entries": []}


async def test_glob_proxies_pattern_to_sandbox_manager(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "glob-happy@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "main.py", "content": "print('hi')"},
        headers={"Authorization": f"Bearer {key}"},
    )
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "readme.md", "content": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/glob",
        json={"pattern": "*.py"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    matches = resp.json()["matches"]
    assert [m["path"] for m in matches] == ["main.py"]


async def test_grep_proxies_pattern_to_sandbox_manager(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "grep-happy@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "config.py", "content": "DEBUG = False\nTOKEN = 'abc'"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/grep",
        json={"pattern": "TOKEN"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is False
    assert body["error"] is None
    assert body["matches"] == [{"path": "config.py", "line": 2, "text": "TOKEN = 'abc'"}]


async def test_grep_respects_glob_filter(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "grep-glob-filter@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "config.py", "content": "needle"},
        headers={"Authorization": f"Bearer {key}"},
    )
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "notes.md", "content": "needle"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/grep",
        json={"pattern": "needle", "glob": "*.py"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200
    matches = resp.json()["matches"]
    assert [m["path"] for m in matches] == ["config.py"]


async def test_grep_max_matches_is_validated(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    from control_plane.schemas import SANDBOX_GREP_MAX_MATCHES_CEILING

    key = await signup_and_get_api_key(client, "grep-max-matches@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/grep",
        json={"pattern": "x", "max_matches": SANDBOX_GREP_MAX_MATCHES_CEILING + 1},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 422


async def test_ls_translates_sandbox_manager_failure_to_502(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    async def _fail(*args, **kwargs):
        raise RuntimeError("simulated sidecar transport failure")

    monkeypatch.setattr(fake_manager, "ls", _fail)

    key = await signup_and_get_api_key(client, "ls-failure@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/ls",
        json={"path": "/"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "sandbox_operation_failed"
    assert "simulated sidecar transport failure" not in resp.text


# ── Rate limiting ────────────────────────────────────────────────────────


async def test_ls_is_rate_limited_per_account(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_SANDBOX_RATE_LIMIT_PER_MINUTE", 3)

    key = await signup_and_get_api_key(client, "ls-rate-limited@example.com")
    session_id = await _create_session(client, key)

    for _ in range(3):
        resp = await client.post(
            f"/v1/sandboxes/{session_id}/files/ls",
            json={"path": "/"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/ls",
        json={"path": "/"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 429
    assert resp.json()["detail"]["error"]["code"] == "rate_limited"
