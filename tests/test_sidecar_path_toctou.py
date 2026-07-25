"""Tests for High: TOCTOU symlink race between path validation and use.

`_resolve_virtual_path` validated a path once via `os.path.realpath()`, but
`/file-create`, `/view`, and `/str-replace` then performed several more
filesystem operations (makedirs/open/chown) against the plain path string
with no re-validation. A backgrounded process inside the sandbox could swap
a path component to a symlink in that window and redirect the operation
outside the allowed roots (e.g. onto `/proc/self/environ`).

`_revalidate_path_or_400` re-resolves and re-checks the path immediately
before each syscall, so a swap performed after the initial validation is
caught instead of silently followed.
"""

import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main as sidecar_main


def test_revalidate_path_or_400_accepts_path_under_allowed_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))
    target = str(tmp_path / "note.txt")

    assert sidecar_main._revalidate_path_or_400(target) == os.path.realpath(target)


def test_revalidate_path_or_400_rejects_path_outside_allowed_roots(monkeypatch):
    with pytest.raises(HTTPException) as exc_info:
        sidecar_main._revalidate_path_or_400("/etc/shadow")

    assert exc_info.value.status_code == 400


def _make_swap_after_first_call(real_realpath, swapped_target: str, trigger: str):
    """Return a realpath() replacement that answers truthfully the first
    time it's asked about `trigger`, then simulates a symlink swap (as if a
    backgrounded sandbox process replaced a path component) on every call
    after that — modeling the TOCTOU window between initial validation and
    the handler's later filesystem syscalls."""
    call_count = {"n": 0}

    def _fake_realpath(path, *args, **kwargs):
        if path == trigger:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return real_realpath(path, *args, **kwargs)
            return swapped_target
        return real_realpath(path, *args, **kwargs)

    return _fake_realpath


def test_file_create_rejects_path_swapped_to_symlink_after_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))

    target_path = os.path.realpath(os.path.join(str(tmp_path), "note.txt"))
    real_realpath = os.path.realpath
    monkeypatch.setattr(
        sidecar_main.os.path,
        "realpath",
        _make_swap_after_first_call(real_realpath, "/etc/evil-target", target_path),
    )

    client = TestClient(sidecar_main.app)
    response = client.post(
        "/file-create",
        json={"path": "note.txt", "content": "hello"},
        headers={sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"},
    )

    assert response.status_code == 400
    assert not os.path.exists(target_path)


def test_view_rejects_path_swapped_to_symlink_after_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))

    target_path = os.path.realpath(os.path.join(str(tmp_path), "note.txt"))
    with open(target_path, "w") as f:
        f.write("legit contents")

    real_realpath = os.path.realpath
    monkeypatch.setattr(
        sidecar_main.os.path,
        "realpath",
        _make_swap_after_first_call(real_realpath, "/etc/evil-target", target_path),
    )

    client = TestClient(sidecar_main.app)
    response = client.post(
        "/view",
        json={"path": "note.txt"},
        headers={sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"},
    )

    assert response.status_code == 400


def _setup_present_files_test(tmp_path, monkeypatch) -> tuple[str, str, str]:
    """Shared setup for the two present-files tests below. Returns
    (workspace_dir, outputs_dir, target_path) for the source file 'note.txt'."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(workspace_dir))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(outputs_dir))
    monkeypatch.setitem(sidecar_main.current_session, "storage_prefix", "test-prefix")

    async def _fake_flush_outputs(**_kwargs):
        return set()

    monkeypatch.setattr(sidecar_main, "flush_outputs", _fake_flush_outputs)
    # os.chown to SANDBOX_UID/GID requires root; no-op it here since these
    # tests are about path re-validation timing, not ownership.
    monkeypatch.setattr(sidecar_main.os, "chown", lambda *a, **k: None)

    target_path = os.path.realpath(os.path.join(str(workspace_dir), "note.txt"))
    with open(target_path, "w") as f:
        f.write("legit contents")

    return str(workspace_dir), str(outputs_dir), target_path


def test_present_files_revalidates_both_paths_immediately_before_copying(tmp_path, monkeypatch):
    """present-files copies `full_path` into OUTPUTS_DIR via shutil.copy2 +
    os.chown -- both syscalls ran against paths resolved earlier in the same
    loop iteration with no re-validation immediately before them, unlike
    file_create/view/str_replace. A backgrounded /exec process swapping the
    source (or destination) path to a symlink in that window could
    exfiltrate sidecar-readable content to a user-downloadable outputs
    artifact, or write into an unintended location.

    Spies on _revalidate_path_or_400 (rather than mocking os.path.realpath,
    whose call-count/ordering across this handler's two _resolve_virtual_path
    calls -- source and OUTPUTS_DIR destination -- is too intertwined to
    simulate reliably) to directly confirm both paths are re-validated right
    before the copy.
    """
    _workspace_dir, outputs_dir, target_path = _setup_present_files_test(tmp_path, monkeypatch)

    real_revalidate = sidecar_main._revalidate_path_or_400
    revalidated_paths: list[str] = []

    def _spy_revalidate(path):
        revalidated_paths.append(path)
        return real_revalidate(path)

    monkeypatch.setattr(sidecar_main, "_revalidate_path_or_400", _spy_revalidate)

    client = TestClient(sidecar_main.app)
    response = client.post(
        "/present-files",
        json={"filepaths": ["note.txt"]},
        headers={sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"},
    )

    assert response.status_code == 200
    # Both the source and the OUTPUTS_DIR destination must be re-validated
    # immediately before the copy -- not just resolved once, much earlier.
    assert target_path in revalidated_paths
    assert any(p.startswith(outputs_dir) for p in revalidated_paths)


def test_present_files_rejection_at_revalidation_stops_the_copy(tmp_path, monkeypatch):
    """A rejection at the re-validation step (simulating a swap caught at
    the last possible moment) must stop the copy from happening at all --
    not just log a warning after the fact."""
    _workspace_dir, outputs_dir, target_path = _setup_present_files_test(tmp_path, monkeypatch)
    real_revalidate = sidecar_main._revalidate_path_or_400

    def _rejecting_revalidate(path):
        if path == target_path:
            raise HTTPException(status_code=400, detail="simulated swap detected")
        return real_revalidate(path)

    monkeypatch.setattr(sidecar_main, "_revalidate_path_or_400", _rejecting_revalidate)

    client = TestClient(sidecar_main.app)
    response = client.post(
        "/present-files",
        json={"filepaths": ["note.txt"]},
        headers={sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"},
    )

    assert response.status_code == 400
    assert list(Path(outputs_dir).iterdir()) == []


def test_str_replace_rejects_path_swapped_to_symlink_after_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))

    target_path = os.path.realpath(os.path.join(str(tmp_path), "note.txt"))
    with open(target_path, "w") as f:
        f.write("hello world")

    real_realpath = os.path.realpath
    monkeypatch.setattr(
        sidecar_main.os.path,
        "realpath",
        _make_swap_after_first_call(real_realpath, "/etc/evil-target", target_path),
    )

    client = TestClient(sidecar_main.app)
    response = client.post(
        "/str-replace",
        json={"path": "note.txt", "old_str": "hello", "new_str": "goodbye"},
        headers={sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"},
    )

    assert response.status_code == 400
    with open(target_path) as f:
        assert f.read() == "hello world"
