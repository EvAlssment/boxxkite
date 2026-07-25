"""Parity test: src/boxkite/tls.py vs sidecar/main.py's duplicated constants.

sidecar/main.py is a separately deployed service that intentionally does not
depend on the `boxkite` package (see tests/test_sidecar_auth_parity.py for
the analogous test on the auth-token constants), so it re-declares the TLS
mount path/filenames/env var name as local constants instead of importing
tls.py. This test is the drift guard for that intentional duplication.
"""

import main as sidecar_main

from boxkite.tls import (
    SIDECAR_TLS_CERT_FILENAME,
    SIDECAR_TLS_DISABLED_ENV,
    SIDECAR_TLS_KEY_FILENAME,
    SIDECAR_TLS_MOUNT_PATH,
)


def test_tls_disabled_env_name_matches_between_manager_and_sidecar():
    assert sidecar_main.SIDECAR_TLS_DISABLED_ENV == SIDECAR_TLS_DISABLED_ENV


def test_tls_mount_path_matches_between_manager_and_sidecar():
    assert sidecar_main.SIDECAR_TLS_MOUNT_PATH == SIDECAR_TLS_MOUNT_PATH


def test_tls_cert_filename_matches_between_manager_and_sidecar():
    assert sidecar_main.SIDECAR_TLS_CERT_FILENAME == SIDECAR_TLS_CERT_FILENAME


def test_tls_key_filename_matches_between_manager_and_sidecar():
    assert sidecar_main.SIDECAR_TLS_KEY_FILENAME == SIDECAR_TLS_KEY_FILENAME
