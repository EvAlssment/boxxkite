"""Tests for the sidecar's session-scoped agent scratch memory (/scratch/*),
GitHub issue #74.

The security-relevant one here is
`test_configure_wipes_scratch_memory_for_the_next_tenant`: scratch memory is
in-process state that outlives a single request, so pod recycling must clear
it for exactly the reason docs/PROCESS-SESSIONS-DESIGN.md section 2(b)
gives for background processes.
"""

import os

import main as sidecar_main
import sidecar_scratch
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _empty_store():
    sidecar_scratch.clear_scratch_memory()
    yield
    sidecar_scratch.clear_scratch_memory()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "test-token")
    c = TestClient(sidecar_main.app)
    c.headers.update({"X-Sidecar-Auth-Token": "test-token"})
    return c


def test_set_then_get_round_trips_the_value(client):
    resp = client.post("/scratch/set", json={"key": "todo", "value": "1. read manager.py"})
    assert resp.status_code == 200
    assert resp.json()["keys"] == 1

    resp = client.get("/scratch/get", params={"key": "todo"})
    assert resp.status_code == 200
    assert resp.json() == {"key": "todo", "value": "1. read manager.py", "found": True}


def test_get_of_an_unset_key_is_found_false_not_404(client):
    """An agent checking whether it has stored something yet is normal
    control flow, not an error condition."""
    resp = client.get("/scratch/get", params={"key": "never-set"})
    assert resp.status_code == 200
    assert resp.json()["found"] is False
    assert resp.json()["value"] is None


def test_set_replaces_an_existing_value(client):
    client.post("/scratch/set", json={"key": "k", "value": "first"})
    client.post("/scratch/set", json={"key": "k", "value": "second"})

    assert client.get("/scratch/get", params={"key": "k"}).json()["value"] == "second"
    assert client.get("/scratch/list").json()["keys"] == 1


def test_delete_removes_a_key_and_is_a_noop_for_a_missing_one(client):
    client.post("/scratch/set", json={"key": "k", "value": "v"})

    resp = client.post("/scratch/delete", json={"key": "k"})
    assert resp.json() == {"key": "k", "deleted": True, "keys": 0}

    resp = client.post("/scratch/delete", json={"key": "k"})
    assert resp.json()["deleted"] is False


def test_list_reports_sizes_but_not_values(client):
    client.post("/scratch/set", json={"key": "b", "value": "xx"})
    client.post("/scratch/set", json={"key": "a", "value": "yyyy"})

    body = client.get("/scratch/list").json()

    assert [e["key"] for e in body["entries"]] == ["a", "b"]  # sorted
    assert [e["bytes"] for e in body["entries"]] == [4, 2]
    assert "value" not in body["entries"][0]
    assert body["keys"] == 2
    assert body["max_keys"] == sidecar_scratch.SCRATCH_MAX_KEYS


def test_empty_key_is_rejected(client):
    assert client.post("/scratch/set", json={"key": "   ", "value": "v"}).status_code == 400


def test_oversized_key_is_rejected(client):
    long_key = "k" * (sidecar_scratch.SCRATCH_MAX_KEY_LENGTH + 1)
    assert client.post("/scratch/set", json={"key": long_key, "value": "v"}).status_code == 400


def test_oversized_value_is_rejected(client):
    big = "x" * (sidecar_scratch.SCRATCH_MAX_VALUE_BYTES + 1)
    resp = client.post("/scratch/set", json={"key": "k", "value": big})
    assert resp.status_code == 413
    assert client.get("/scratch/list").json()["keys"] == 0


def test_key_count_ceiling_is_enforced_but_overwrites_still_work(client, monkeypatch):
    monkeypatch.setattr(sidecar_scratch, "SCRATCH_MAX_KEYS", 2)

    assert client.post("/scratch/set", json={"key": "a", "value": "1"}).status_code == 200
    assert client.post("/scratch/set", json={"key": "b", "value": "2"}).status_code == 200
    assert client.post("/scratch/set", json={"key": "c", "value": "3"}).status_code == 409

    # At the ceiling, replacing an existing key must still be allowed --
    # otherwise an agent that filled the store can no longer update its own
    # todo list, only delete it.
    assert client.post("/scratch/set", json={"key": "a", "value": "updated"}).status_code == 200
    assert client.get("/scratch/get", params={"key": "a"}).json()["value"] == "updated"


def test_total_byte_budget_is_enforced_across_keys(client, monkeypatch):
    monkeypatch.setattr(sidecar_scratch, "SCRATCH_MAX_TOTAL_BYTES", 100)

    assert client.post("/scratch/set", json={"key": "a", "value": "x" * 60}).status_code == 200
    assert client.post("/scratch/set", json={"key": "b", "value": "x" * 60}).status_code == 413

    # Shrinking an existing key is measured against the store as it would be
    # after the write, so it must not be rejected by the space the old value
    # was using.
    assert client.post("/scratch/set", json={"key": "a", "value": "x" * 10}).status_code == 200
    assert client.post("/scratch/set", json={"key": "b", "value": "x" * 60}).status_code == 200


def test_scratch_routes_require_the_sidecar_auth_token():
    unauthenticated = TestClient(sidecar_main.app)
    posts = ["/scratch/set", "/scratch/delete"]
    gets = ["/scratch/get", "/scratch/list"]

    for path in posts:
        resp = unauthenticated.post(path, json={"key": "k", "value": "v"})
        assert resp.status_code in (401, 503), f"POST {path} returned {resp.status_code}"
    for path in gets:
        resp = unauthenticated.get(path, params={"key": "k"})
        assert resp.status_code in (401, 503), f"GET {path} returned {resp.status_code}"


async def test_configure_wipes_scratch_memory_for_the_next_tenant(monkeypatch, tmp_path):
    """SECURITY: a recycled pod must not hand the next tenant the previous
    tenant's scratch keys -- same requirement as background processes and
    the persistent interpreter, which /configure already resets."""
    import sidecar_sync

    sidecar_scratch._scratch["previous-tenant-notes"] = "secret plan"

    for name in ("_kill_all_processes", "kill_takeover_tmux_session", "_reset_interpreter",
                 "_reset_node_interpreter", "_reset_browser", "kill_desktop_session",
                 "_kill_all_lsp_servers"):
        async def _noop(*args, **kwargs):
            return None
        monkeypatch.setattr(sidecar_main, name, _noop)

    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(sidecar_main, "SANDBOX_UID", os.getuid())
    monkeypatch.setattr(sidecar_main, "SANDBOX_GID", os.getgid())
    monkeypatch.setattr(sidecar_sync, "_clear_tmp_session_data", lambda: None)

    async def _no_prefetch(*args, **kwargs):
        return None
    monkeypatch.setattr(sidecar_sync, "prefetch_uploads_from_prefix", _no_prefetch)
    monkeypatch.setattr(sidecar_sync, "prefetch_files", _no_prefetch)
    monkeypatch.setattr(sidecar_sync, "prefetch_legacy_uploads", _no_prefetch)

    await sidecar_sync.configure(
        sidecar_main.ConfigureRequest(
            session_id="next-tenant-session",
            organization_id="org-2",
            work_item_id="wi-2",
            storage_prefix="sessions/org-2/next-tenant-session",
        )
    )

    assert sidecar_scratch._scratch == {}
