"""Tests for the sidecar's conditional TLS startup logic
(docs/SIDECAR-TRANSPORT-TLS-DESIGN.md).

Exercises `_sidecar_tls_files_present()` directly rather than actually
spawning uvicorn -- that function is the entire decision point for whether
`if __name__ == "__main__":` serves HTTPS or plain HTTP, so covering it
covers the startup branch without needing a real server process.
"""

import main as sidecar_main


def test_tls_files_present_false_when_no_files_mounted(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_DISABLED", False)
    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_CERT_PATH", str(tmp_path / "tls.crt"))
    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_KEY_PATH", str(tmp_path / "tls.key"))

    assert sidecar_main._sidecar_tls_files_present() is False


def test_tls_files_present_true_when_both_files_exist(tmp_path, monkeypatch):
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.write_text("fake cert")
    key_path.write_text("fake key")

    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_DISABLED", False)
    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_CERT_PATH", str(cert_path))
    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_KEY_PATH", str(key_path))

    assert sidecar_main._sidecar_tls_files_present() is True


def test_tls_files_present_false_when_only_cert_exists(tmp_path, monkeypatch):
    cert_path = tmp_path / "tls.crt"
    cert_path.write_text("fake cert")
    key_path = tmp_path / "tls.key"  # never written

    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_DISABLED", False)
    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_CERT_PATH", str(cert_path))
    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_KEY_PATH", str(key_path))

    assert sidecar_main._sidecar_tls_files_present() is False


def test_tls_files_present_false_when_disabled_even_with_files_mounted(tmp_path, monkeypatch):
    """SIDECAR_TLS_DISABLED=true wins even if a stale cert/key happen to be
    mounted (e.g. a recycled warm pod that used to have TLS enabled)."""
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.write_text("fake cert")
    key_path.write_text("fake key")

    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_DISABLED", True)
    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_CERT_PATH", str(cert_path))
    monkeypatch.setattr(sidecar_main, "SIDECAR_TLS_KEY_PATH", str(key_path))

    assert sidecar_main._sidecar_tls_files_present() is False
