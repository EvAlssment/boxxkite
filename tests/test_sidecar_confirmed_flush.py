"""`/flush/confirmed` -- docs/SNAPSHOT-DESIGN.md's confirmed-flush endpoint.

Unlike `/flush` (which echoes `pending_sync_files`, the *before* state),
this must return `flush_outputs()`'s own `ready` set, expressed as storage
keys relative to `storage_prefix` -- see sidecar/main.py's docstring on the
endpoint for why that mapping matters to the control plane's storage-side
copy.
"""

import main as sidecar_main
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def test_confirmed_flush_returns_storage_keys_relative_to_prefix(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    sidecar_main.current_session["storage_prefix"] = "sessions/org-1/session-1"

    async def fake_flush_outputs(*, reason, discover_untracked):
        return {"/workspace/foo.py", "/mnt/user-data/outputs/report.pdf"}

    monkeypatch.setattr(sidecar_main, "flush_outputs", fake_flush_outputs)

    def fake_bucket_for_virtual_path(virtual_path: str):
        if virtual_path == "/workspace/foo.py":
            return ("workspace", "foo.py")
        if virtual_path == "/mnt/user-data/outputs/report.pdf":
            return ("outputs", "report.pdf")
        return None

    monkeypatch.setattr(sidecar_main, "_storage_bucket_for_virtual_path", fake_bucket_for_virtual_path)

    client = _client()
    response = client.post("/flush/confirmed", headers=_auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "flushed"
    assert body["storage_prefix"] == "sessions/org-1/session-1"
    assert sorted(body["storage_keys"]) == ["outputs/report.pdf", "workspace/foo.py"]

    sidecar_main.current_session["storage_prefix"] = None


def test_confirmed_flush_drops_paths_with_no_storage_mapping(monkeypatch):
    """A ready path outside the known namespace roots (shouldn't happen in
    practice, but flush_outputs' contract doesn't guarantee it) must be
    silently dropped from storage_keys rather than crashing the endpoint."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    sidecar_main.current_session["storage_prefix"] = "sessions/org-1/session-1"

    async def fake_flush_outputs(*, reason, discover_untracked):
        return {"/some/unmapped/path.txt"}

    monkeypatch.setattr(sidecar_main, "flush_outputs", fake_flush_outputs)
    monkeypatch.setattr(sidecar_main, "_storage_bucket_for_virtual_path", lambda _p: None)

    client = _client()
    response = client.post("/flush/confirmed", headers=_auth_headers())

    assert response.status_code == 200
    assert response.json()["storage_keys"] == []

    sidecar_main.current_session["storage_prefix"] = None
