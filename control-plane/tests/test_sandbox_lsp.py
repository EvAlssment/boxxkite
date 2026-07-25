"""Tests for POST /v1/sandboxes/{id}/lsp/{start,open,completion,stop} --
the control-plane proxy to SandboxManager.lsp_start/.lsp_open/
.lsp_completion/.lsp_stop (GitHub issue #183,
docs/LSP-SUPPORT-SCOPING.md). Mirrors test_sandbox_exec.py's and
test_sandbox_desktop_takeover.py's structure: BOXKITE_LSP_ENABLED gate
tests first (mirrors the desktop-takeover feature-flag pattern), then
cross-tenant/unknown-session 404s, then the happy path and failure
translation for each route.
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _enable_lsp(monkeypatch) -> None:
    import control_plane.routers.sandboxes as sandboxes_module

    monkeypatch.setattr(sandboxes_module.settings, "BOXKITE_LSP_ENABLED", True)


# ── BOXKITE_LSP_ENABLED gate ─────────────────────────────────────────────


async def test_lsp_start_404s_when_feature_disabled(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    """settings.BOXKITE_LSP_ENABLED defaults to False -- the route must
    404 unconditionally, even for a valid key and an owned session."""
    key = await signup_and_get_api_key(client, "lsp-start-disabled@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/lsp/start",
        json={"language": "python"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert fake_manager.lsp_start_calls == []


async def test_lsp_open_404s_when_feature_disabled(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "lsp-open-disabled@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/lsp/fake-lsp-1/open",
        json={"path": "main.py", "content": "x = 1"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert fake_manager.lsp_open_calls == []


async def test_lsp_completion_404s_when_feature_disabled(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "lsp-completion-disabled@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/lsp/fake-lsp-1/completion",
        json={"path": "main.py", "line": 0, "character": 0},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert fake_manager.lsp_completion_calls == []


async def test_lsp_stop_404s_when_feature_disabled(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "lsp-stop-disabled@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/lsp/fake-lsp-1/stop",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert fake_manager.lsp_stop_calls == []


# ── Happy paths (feature enabled) ────────────────────────────────────────


async def test_lsp_start_proxies_to_manager(client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch):
    _enable_lsp(monkeypatch)
    key = await signup_and_get_api_key(client, "lsp-start-ok@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/lsp/start",
        json={"language": "python"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    lsp_id = resp.json()["lsp_id"]
    assert lsp_id
    assert fake_manager.lsp_start_calls == [{"session_id": session_id, "language": "python"}]


async def test_lsp_open_proxies_to_manager(client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch):
    _enable_lsp(monkeypatch)
    key = await signup_and_get_api_key(client, "lsp-open-ok@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/lsp/lsp-handle-1/open",
        json={"path": "main.py", "content": "x = 1\n"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}
    assert fake_manager.lsp_open_calls == [
        {"session_id": session_id, "lsp_id": "lsp-handle-1", "path": "main.py", "content": "x = 1\n"}
    ]


async def test_lsp_completion_proxies_to_manager(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    _enable_lsp(monkeypatch)
    key = await signup_and_get_api_key(client, "lsp-completion-ok@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/lsp/lsp-handle-1/completion",
        json={"path": "main.py", "line": 3, "character": 5},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": []}
    assert fake_manager.lsp_completion_calls == [
        {"session_id": session_id, "lsp_id": "lsp-handle-1", "path": "main.py", "line": 3, "character": 5}
    ]


async def test_lsp_stop_proxies_to_manager(client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch):
    _enable_lsp(monkeypatch)
    key = await signup_and_get_api_key(client, "lsp-stop-ok@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/lsp/lsp-handle-1/stop",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}
    assert fake_manager.lsp_stop_calls == [{"session_id": session_id, "lsp_id": "lsp-handle-1"}]


# ── Cross-tenant / unknown session 404s ──────────────────────────────────


async def test_lsp_start_404s_for_a_foreign_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    _enable_lsp(monkeypatch)
    key_a = await signup_and_get_api_key(client, "lsp-owner-a@example.com")
    key_b = await signup_and_get_api_key(client, "lsp-owner-b@example.com")
    session_a_id = await _create_session(client, key_a)

    resp = await client.post(
        f"/v1/sandboxes/{session_a_id}/lsp/start",
        json={"language": "python"},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404
    assert fake_manager.lsp_start_calls == []


async def test_lsp_open_404s_for_unknown_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    _enable_lsp(monkeypatch)
    key = await signup_and_get_api_key(client, "lsp-open-unknown@example.com")

    resp = await client.post(
        "/v1/sandboxes/does-not-exist/lsp/lsp-handle-1/open",
        json={"path": "main.py", "content": "x = 1"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert fake_manager.lsp_open_calls == []


async def test_lsp_completion_404s_for_unknown_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    _enable_lsp(monkeypatch)
    key = await signup_and_get_api_key(client, "lsp-completion-unknown@example.com")

    resp = await client.post(
        "/v1/sandboxes/does-not-exist/lsp/lsp-handle-1/completion",
        json={"path": "main.py", "line": 0, "character": 0},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert fake_manager.lsp_completion_calls == []


async def test_lsp_stop_404s_for_unknown_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    _enable_lsp(monkeypatch)
    key = await signup_and_get_api_key(client, "lsp-stop-unknown@example.com")

    resp = await client.post(
        "/v1/sandboxes/does-not-exist/lsp/lsp-handle-1/stop",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert fake_manager.lsp_stop_calls == []


# ── Failure translation (exec-budget / sidecar-failure interaction) ──────


async def test_lsp_start_translates_sandbox_manager_failure_to_502(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    """lsp_start is exec-budgeted the same as /exec (SECURITY.md) -- a
    budget-exceeded (or any other) manager failure must translate to the
    same 502 sandbox_operation_failed envelope /exec's equivalent test
    asserts, never a raw 500 leaking the underlying exception."""
    _enable_lsp(monkeypatch)
    key = await signup_and_get_api_key(client, "lsp-start-failure@example.com")
    session_id = await _create_session(client, key)
    fake_manager.fail_next_lsp_start = True

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/lsp/start",
        json={"language": "python"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "sandbox_operation_failed"
    assert "simulated sidecar transport failure" not in resp.text


async def test_lsp_completion_translates_sandbox_manager_failure_to_502(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    """lsp_completion is also exec-budgeted (SECURITY.md) -- same 502
    translation as lsp_start above."""
    _enable_lsp(monkeypatch)
    key = await signup_and_get_api_key(client, "lsp-completion-failure@example.com")
    session_id = await _create_session(client, key)
    fake_manager.fail_next_lsp_completion = True

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/lsp/lsp-handle-1/completion",
        json={"path": "main.py", "line": 0, "character": 0},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "sandbox_operation_failed"
    assert "simulated sidecar transport failure" not in resp.text


async def test_lsp_routes_require_authentication(client: httpx.AsyncClient, monkeypatch):
    _enable_lsp(monkeypatch)
    resp = await client.post("/v1/sandboxes/some-session/lsp/start", json={"language": "python"})
    assert resp.status_code == 401
