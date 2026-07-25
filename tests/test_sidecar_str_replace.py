"""Regression tests for the sidecar /str-replace handler.

Guards the bug where str_replace returned a 500 (surfaced by the control-plane
as a 502 `sandbox_operation_failed`) on every call against the hosted deployment.
Root cause found on the live cluster: file_create hands every file to
SANDBOX_UID (1001) via chown, and the sidecar runs as root but WITHOUT
CAP_DAC_OVERRIDE, so opening that 1001-owned file in place with 'w' fails EACCES
-- str_replace's write failed before it ever ran, while file_create (which
creates a brand-new root-owned file) worked. The fix writes a fresh root-owned
temp file, chowns it to SANDBOX_UID, then atomically os.replace()s it over the
target (needing only write on the 0777 workspace dir), so no in-place write of a
differently-owned file is ever attempted.

These call the real handler in-process with the exact JSON body the
SDK/control-plane send. SANDBOX_UID is left at its 1001 default so the temp-file
chown genuinely no-ops for a normal non-root test user (mirroring the best-effort
chown), which must not fail the edit.
"""

import os

from fastapi.testclient import TestClient

import main as sidecar_main

_AUTH = "the-real-secret"


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", _AUTH)
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))
    return TestClient(sidecar_main.app, raise_server_exceptions=False)


def _post_str_replace(client, body):
    return client.post(
        "/str-replace",
        json=body,
        headers={sidecar_main.SIDECAR_AUTH_HEADER: _AUTH},
    )


def test_str_replace_succeeds_when_fchown_to_sandbox_uid_is_not_permitted(tmp_path, monkeypatch):
    """The core regression: fchown to SANDBOX_UID (1001) fails for a normal
    test user, but the edit must still succeed (200) and be written to disk."""
    client = _client(monkeypatch, tmp_path)
    assert sidecar_main.SANDBOX_UID == 1001  # default; the test user is not 1001

    target = tmp_path / "config.py"
    target.write_text("DEBUG = False\n")

    resp = _post_str_replace(
        client,
        {
            "path": "config.py",
            "old_str": "False",
            "new_str": "True",
            "replace_all": False,
            "description": "flip debug",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["replaced"] is True
    assert resp.json()["occurrences"] == 1
    assert target.read_text() == "DEBUG = True\n"


def test_str_replace_replace_all_true_succeeds_without_chown(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)

    target = tmp_path / "vals.py"
    target.write_text("a = False\nb = False\n")

    resp = _post_str_replace(
        client,
        {
            "path": "vals.py",
            "old_str": "False",
            "new_str": "True",
            "replace_all": True,
            "description": "flip all",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["replaced"] is True
    assert resp.json()["occurrences"] == 2
    assert target.read_text() == "a = True\nb = True\n"


def test_str_replace_still_edits_when_fchown_succeeds(tmp_path, monkeypatch):
    """When fchown IS permitted (chown to the current user's own uid/gid, which
    a non-root process may do to a file it owns), behavior is unchanged."""
    monkeypatch.setattr(sidecar_main, "SANDBOX_UID", os.getuid())
    monkeypatch.setattr(sidecar_main, "SANDBOX_GID", os.getgid())
    client = _client(monkeypatch, tmp_path)

    target = tmp_path / "f.txt"
    target.write_text("hello world")

    resp = _post_str_replace(
        client,
        {"path": "f.txt", "old_str": "world", "new_str": "there", "replace_all": False},
    )

    assert resp.status_code == 200, resp.text
    assert target.read_text() == "hello there"


def test_str_replace_succeeds_when_target_is_not_writable_in_place(tmp_path, monkeypatch):
    """The real hosted regression, in unit-testable form: the target file cannot
    be opened for writing in place (mode 0o444), but str_replace must still
    succeed because it writes a temp file and atomically replaces the target
    (which needs only write permission on the enclosing dir, not the file). This
    is the local analog of the sidecar being root-without-CAP_DAC_OVERRIDE facing
    a SANDBOX_UID-owned file."""
    client = _client(monkeypatch, tmp_path)

    target = tmp_path / "ro.py"
    target.write_text("value = 1\n")
    os.chmod(target, 0o444)  # in-place open('w') would EACCES
    try:
        resp = _post_str_replace(
            client,
            {"path": "ro.py", "old_str": "value = 1", "new_str": "value = 42", "replace_all": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["replaced"] is True
        assert target.read_text() == "value = 42\n"
        # original mode is preserved on the replacement
        assert (os.stat(target).st_mode & 0o777) == 0o444
    finally:
        os.chmod(target, 0o644)


def test_str_replace_no_match_returns_not_replaced(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    (tmp_path / "f.txt").write_text("nothing here")

    resp = _post_str_replace(
        client,
        {"path": "f.txt", "old_str": "absent", "new_str": "x", "replace_all": False},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["replaced"] is False
    assert resp.json()["occurrences"] == 0
