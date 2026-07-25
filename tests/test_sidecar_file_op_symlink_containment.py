"""Tests for CRITICAL: /grep, /glob, /ls, and /present-files let a symlink
planted inside an allowed root (e.g. via `ln -s /proc/self/environ
workspace/x` through /exec) resolve to a target *outside* the allowed roots
without any containment check.

For /grep specifically this was a blind content-exfiltration oracle: a
match on the symlinked target's content raised an uncaught HTTPException
(building the match dict called _to_virtual_path, which 400s for an
out-of-bounds realpath) while a non-match returned 200 with no matches --
an attacker could bisect arbitrary sidecar-readable file content (including
the sidecar's own environment/credentials) one bit at a time using only
already-exposed tools, no RCE required. /glob degraded this to an
existence-only oracle; /ls disclosed the resolved target path directly with
no oracle needed at all.

_is_path_contained() now gives each of these a non-raising check (skip, not
raise, so a single out-of-bounds symlink doesn't turn into a request-status
side channel) applied before the file is opened/stat'd/returned.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

import main as sidecar_main


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def _plant_symlink_to_outside_secret(tmp_path, monkeypatch) -> tuple[str, str]:
    """Sets WORKSPACE_DIR to a fresh tmp_path, writes a 'secret' file outside
    it, and plants a symlink inside the workspace pointing at that secret.
    Returns (workspace_dir, secret_marker_content)."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(workspace_dir))

    secret_dir = tmp_path / "outside-the-sandbox-roots"
    secret_dir.mkdir()
    secret_file = secret_dir / "credentials.env"
    marker = "AWS_SECRET_ACCESS_KEY=super-secret-value-should-never-leak"
    secret_file.write_text(marker + "\n")

    symlink_path = workspace_dir / "planted-symlink"
    os.symlink(str(secret_file), str(symlink_path))

    return str(workspace_dir), marker


def test_grep_does_not_read_through_a_symlink_to_outside_content(tmp_path, monkeypatch):
    _workspace_dir, marker = _plant_symlink_to_outside_secret(tmp_path, monkeypatch)

    response = _client().post(
        "/grep",
        json={"pattern": "AWS_SECRET_ACCESS_KEY", "path": "."},
        headers=_auth_headers(),
    )

    # Must not crash (the old uncaught-HTTPException oracle) and must not
    # return the secret content.
    assert response.status_code == 200
    body = response.json()
    assert body["matches"] == []
    assert marker not in str(body)


def test_grep_no_match_and_out_of_bounds_symlink_are_indistinguishable(tmp_path, monkeypatch):
    """The core oracle-closure property: a request that would have matched
    (if the symlink were followed) and a request that legitimately matches
    nothing must produce the exact same shape of response -- no side
    channel revealing "a file existed here but was out of bounds"."""
    _workspace_dir, _marker = _plant_symlink_to_outside_secret(tmp_path, monkeypatch)

    response = _client().post(
        "/grep",
        json={"pattern": "this-pattern-matches-nothing-anywhere", "path": "."},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()["matches"] == []


def test_glob_does_not_list_a_symlink_resolving_outside_allowed_roots(tmp_path, monkeypatch):
    _workspace_dir, _marker = _plant_symlink_to_outside_secret(tmp_path, monkeypatch)

    response = _client().post(
        "/glob",
        json={"pattern": "*", "path": "."},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    matches = response.json()["matches"]
    assert all("planted-symlink" not in m["path"] for m in matches)
    assert all("outside-the-sandbox-roots" not in m["path"] for m in matches)


def test_ls_does_not_disclose_a_symlinks_out_of_bounds_target(tmp_path, monkeypatch):
    _workspace_dir, _marker = _plant_symlink_to_outside_secret(tmp_path, monkeypatch)

    response = _client().post(
        "/ls",
        json={"path": "."},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert all("outside-the-sandbox-roots" not in e["path"] for e in entries)


def test_grep_still_finds_real_matches_under_the_workspace(tmp_path, monkeypatch):
    """Sanity check the fix doesn't break legitimate grep functionality."""
    workspace_dir, _marker = _plant_symlink_to_outside_secret(tmp_path, monkeypatch)
    Path(workspace_dir, "real.txt").write_text("hello needle world\n")

    response = _client().post(
        "/grep",
        json={"pattern": "needle", "path": "."},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    matches = response.json()["matches"]
    assert len(matches) == 1
    assert "needle" in matches[0]["text"]


def test_ls_still_lists_real_files_under_the_workspace(tmp_path, monkeypatch):
    workspace_dir, _marker = _plant_symlink_to_outside_secret(tmp_path, monkeypatch)
    Path(workspace_dir, "real.txt").write_text("hello\n")

    response = _client().post(
        "/ls",
        json={"path": "."},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    paths = [e["path"] for e in response.json()["entries"]]
    assert any(p.endswith("real.txt") for p in paths)


def test_grep_times_out_gracefully_instead_of_hanging_the_request(monkeypatch):
    """A stuck search (e.g. catastrophic regex backtracking) must not hang
    the request forever -- asyncio.wait_for around the offloaded search
    must return a timeout error response instead."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "GREP_TIMEOUT_SECONDS", 0.05)

    def _stuck_search(*args, **kwargs):
        import time

        time.sleep(1.0)  # much longer than GREP_TIMEOUT_SECONDS above
        return [], False

    monkeypatch.setattr(sidecar_main, "_grep_search_sync", _stuck_search)

    response = _client().post(
        "/grep",
        json={"pattern": ".", "path": "/tmp"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["matches"] == []
    assert body["truncated"] is True
    assert "timed out" in (body.get("error") or "").lower()
