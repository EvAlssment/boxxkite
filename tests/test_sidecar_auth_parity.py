"""Parity test: src/boxkite/sidecar_auth.py vs sidecar/main.py's duplicated constants.

sidecar/main.py is a separately deployed service that intentionally does not
depend on the `boxkite` package, so it re-declares the env var name and
header name used for sidecar HTTP auth as local constants instead of
importing sidecar_auth.py. This test is the drift guard for that
intentional duplication.
"""

import main as sidecar_main
from boxkite.sidecar_auth import (
    SIDECAR_AUTH_HEADER,
    SIDECAR_AUTH_TOKEN_ENV,
    SIDECAR_AUTH_TOKEN_TEMPLATE_PLACEHOLDER,
)


def test_auth_header_name_matches_between_manager_and_sidecar():
    assert sidecar_main.SIDECAR_AUTH_HEADER == SIDECAR_AUTH_HEADER


def test_auth_token_env_name_matches_between_manager_and_sidecar():
    assert sidecar_main.SIDECAR_AUTH_TOKEN_ENV == SIDECAR_AUTH_TOKEN_ENV


def test_auth_token_template_placeholder_matches_between_manager_and_sidecar():
    assert sidecar_main.SIDECAR_AUTH_TOKEN_TEMPLATE_PLACEHOLDER == SIDECAR_AUTH_TOKEN_TEMPLATE_PLACEHOLDER
