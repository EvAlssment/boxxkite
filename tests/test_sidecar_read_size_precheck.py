"""Tests for MEDIUM: /view, /read-image, and /str-replace read a file's
*entire* content into memory before checking its size against any limit --
an agent-writable file under /workspace/etc. can be arbitrarily large (via
/exec), so a single multi-GB file was a memory-exhaustion vector regardless
of the eventual truncation/limit applied after the read completed.

Each handler now checks size via os.stat() *before* opening the file, so an
oversized file is rejected without ever being buffered into memory. These
tests monkeypatch the size constants down to a tiny value (rather than
writing huge test fixtures) and spy on the actual file-open call to prove
the check short-circuits before any read happens.
"""

from __future__ import annotations

import aiofiles
from fastapi.testclient import TestClient

import main as sidecar_main


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def _spy_on_aiofiles_open(monkeypatch) -> list:
    """Returns a list that records every path aiofiles.open() is called
    with. If the size pre-check works, an oversized file's path never
    appears here."""
    calls: list[str] = []
    real_open = aiofiles.open

    def _spy(path, *args, **kwargs):
        calls.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(sidecar_main.aiofiles, "open", _spy)
    return calls


def test_view_rejects_oversized_file_without_opening_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(sidecar_main, "VIEW_MAX_FILE_SIZE_BYTES", 10)

    big_file = tmp_path / "big.txt"
    big_file.write_text("x" * 100)  # over the monkeypatched 10-byte limit

    calls = _spy_on_aiofiles_open(monkeypatch)

    response = _client().post("/view", json={"path": "big.txt"}, headers=_auth_headers())

    assert response.status_code == 422
    assert "over the" in response.json()["detail"]
    assert calls == []  # never opened


def test_view_accepts_file_at_or_under_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(sidecar_main, "VIEW_MAX_FILE_SIZE_BYTES", 1000)

    small_file = tmp_path / "small.txt"
    small_file.write_text("hello world")

    response = _client().post("/view", json={"path": "small.txt"}, headers=_auth_headers())

    assert response.status_code == 200
    assert response.json()["content"] == "hello world"


def test_str_replace_rejects_oversized_file_without_opening_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(sidecar_main, "FILE_CONTENT_MAX_LENGTH", 10)

    big_file = tmp_path / "big.py"
    big_file.write_text("x" * 100)

    calls = _spy_on_aiofiles_open(monkeypatch)

    response = _client().post(
        "/str-replace",
        json={"path": "big.py", "old_str": "x", "new_str": "y"},
        headers=_auth_headers(),
    )

    assert response.status_code == 422
    assert "over the" in response.json()["detail"]
    assert calls == []


def test_str_replace_still_works_on_a_file_under_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(sidecar_main.os, "fchown", lambda *a, **k: None)

    target = tmp_path / "config.py"
    target.write_text("hello world")

    response = _client().post(
        "/str-replace",
        json={"path": "config.py", "old_str": "hello", "new_str": "goodbye"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()["replaced"] is True
    assert target.read_text() == "goodbye world"


def test_read_image_rejects_oversized_image_without_opening_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))

    # A real (tiny) PNG so path resolution/mime-detection succeeds; the size
    # check itself is keyed off os.stat(), not content, so padding the file
    # past a monkeypatched-down limit is enough to exercise the guard.
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    big_image = tmp_path / "big.png"
    big_image.write_bytes(png_bytes + b"\x00" * 1000)

    real_stat = sidecar_main.os.stat

    def _fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path).endswith("big.png"):
            # Simulate a file over the real 50MB limit without actually
            # writing 50MB+ to disk in this test.
            import os as _os

            return _os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    60 * 1024 * 1024,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    monkeypatch.setattr(sidecar_main.os, "stat", _fake_stat)
    calls = _spy_on_aiofiles_open(monkeypatch)

    response = _client().post("/read-image", json={"path": "big.png"}, headers=_auth_headers())

    assert response.status_code == 400
    assert "too large" in response.json()["detail"].lower()
    assert calls == []
