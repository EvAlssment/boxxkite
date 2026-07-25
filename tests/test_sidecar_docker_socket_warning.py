"""Tests for the sidecar's startup-time warning when it can reach the host's
Docker socket. Neither deploy/pod-template.yaml nor deploy/docker-compose.yml
mounts this socket (compose mode switched from `docker exec` to nsenter, so
it no longer needs the socket at all) -- this warning is now a regression
tripwire for a custom/misconfigured deployment reintroducing the mount, not
an expected condition. See `_warn_if_docker_socket_mounted`'s docstring.

Exercises `_warn_if_docker_socket_mounted()` directly rather than actually
starting the app -- it's the entire detection/warning logic that runs from
the `startup` event handler.
"""

import os
import socket as socket_module
import tempfile
import uuid

import main as sidecar_main


def test_no_warning_when_socket_path_does_not_exist(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sidecar_main, "_DOCKER_SOCKET_PATH", str(tmp_path / "docker.sock"))

    sidecar_main._warn_if_docker_socket_mounted()

    assert "docker.sock" not in caplog.text


def test_no_warning_when_path_exists_but_is_a_regular_file(tmp_path, monkeypatch, caplog):
    fake_path = tmp_path / "docker.sock"
    fake_path.write_text("not actually a socket")
    monkeypatch.setattr(sidecar_main, "_DOCKER_SOCKET_PATH", str(fake_path))

    sidecar_main._warn_if_docker_socket_mounted()

    assert "CRITICAL" not in caplog.text


def test_warns_when_path_is_a_real_unix_socket(monkeypatch, caplog):
    # AF_UNIX paths are capped at ~104-108 bytes on macOS/Linux -- use a
    # short name directly under the system temp dir rather than pytest's
    # (often much longer) tmp_path fixture.
    socket_path = os.path.join(tempfile.gettempdir(), f"bxk-{uuid.uuid4().hex[:8]}.sock")
    server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    server.bind(socket_path)
    try:
        monkeypatch.setattr(sidecar_main, "_DOCKER_SOCKET_PATH", socket_path)

        with caplog.at_level("WARNING"):
            sidecar_main._warn_if_docker_socket_mounted()

        assert "CRITICAL" in caplog.text
        assert "docker.sock" in caplog.text or "HOST-ROOT" in caplog.text
    finally:
        server.close()
        os.unlink(socket_path)
