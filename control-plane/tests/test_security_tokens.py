"""Unit tests for security.py's generate_secure_token -- the shared
opaque-token primitive behind refresh tokens, password-reset tokens, and
email-verification tokens (issue #79)."""

from __future__ import annotations

from control_plane.security import generate_secure_token, hash_secret


def test_generate_secure_token_returns_raw_and_matching_hash():
    raw, token_hash = generate_secure_token()
    assert raw
    assert token_hash == hash_secret(raw)


def test_generate_secure_token_is_high_entropy_and_unique():
    tokens = {generate_secure_token()[0] for _ in range(50)}
    assert len(tokens) == 50
    for raw in tokens:
        assert len(raw) >= 32


def test_generate_secure_token_hash_never_equals_raw_value():
    raw, token_hash = generate_secure_token()
    assert token_hash != raw
