"""Tests for the sidecar's directory-watcher endpoint (POST /watch,
docs/FILE-WATCHER-DESIGN.md).

Linux-only (real `inotify` via ctypes -- no mocking of the syscalls
themselves, same "exercise the real mechanism" bar test_sidecar_pty.py
holds for the PTY takeover route). Skipped automatically on non-Linux dev
machines (see the `pytestmark` skip below) -- verified for real by running
this file inside a Linux container (`docker run --rm -v $(pwd):/repo -w
/repo/tests python:3.11-slim pytest test_sidecar_watch_directory.py`) at
the time this test was written.

Covers:
- /watch requires the same sidecar auth as every other route.
- A file created under the watched directory during the call is reported
  before the timeout elapses.
- No change within timeout_seconds returns timed_out=True and no changes.
- An out-of-bounds path (outside every allowed root) is rejected the same
  way /ls already rejects it -- same _resolve_ls_path call, no new
  containment logic.
"""

import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="inotify is Linux-only; see module docstring for how this was verified"
)

import main as sidecar_main
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def test_watch_requires_auth_like_every_other_route(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    response = client.post("/watch", json={"path": "/", "timeout_seconds": 1})

    assert response.status_code == 401


def test_watch_reports_a_file_created_during_the_call(monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))
    client = _client()

    import threading

    def _create_file_soon():
        time.sleep(0.3)
        (tmp_path / "output.txt").write_text("hello")

    threading.Thread(target=_create_file_soon, daemon=True).start()

    response = client.post(
        "/watch",
        json={"path": "/", "timeout_seconds": 5},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is False
    assert any(c["path"] == "output.txt" for c in body["changes"])


def test_watch_times_out_with_no_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))
    client = _client()

    response = client.post(
        "/watch",
        json={"path": "/", "timeout_seconds": 0.5},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is True
    assert body["changes"] == []


def test_watch_rejects_a_path_outside_every_allowed_root(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    response = client.post(
        "/watch",
        json={"path": "/etc", "timeout_seconds": 0.5},
        headers=_auth_headers(),
    )

    assert response.status_code == 400
