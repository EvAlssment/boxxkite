"""Tests for Critical #2: /tool-call must never honor a caller-supplied session_id.

sidecar/main.py's ToolCallRequest.session_id is accepted for backwards
compatibility but must be silently discarded — the sidecar always forwards
its own `current_session["session_id"]` to the backend's internal
tool-call endpoint. Honoring a caller-supplied value would let sandboxed
code impersonate an arbitrary other session/tenant.
"""

import httpx
import main as sidecar_main
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def test_mismatched_caller_supplied_session_id_is_ignored(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    sidecar_main.current_session["session_id"] = "real-session-owned-by-this-pod"

    captured_payloads = []

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"result": "ok"}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, **kwargs):
            captured_payloads.append(json)
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    client = _client()
    response = client.post(
        "/tool-call",
        json={
            "tool_name": "some_tool",
            "arguments": {},
            # Attacker-controlled: an entirely different session/tenant.
            "session_id": "attacker-supplied-victim-session",
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert len(captured_payloads) == 1
    forwarded = captured_payloads[0]
    assert forwarded["session_id"] == "real-session-owned-by-this-pod"
    assert forwarded["session_id"] != "attacker-supplied-victim-session"


def test_missing_session_id_falls_back_to_current_session(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    sidecar_main.current_session["session_id"] = "real-session-2"

    captured_payloads = []

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"result": "ok"}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, **kwargs):
            captured_payloads.append(json)
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    client = _client()
    response = client.post(
        "/tool-call",
        json={"tool_name": "some_tool", "arguments": {}},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert captured_payloads[0]["session_id"] == "real-session-2"
