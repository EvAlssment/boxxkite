"""verify_startup_config: fail-fast on insecure defaults outside dev/test."""

from __future__ import annotations

import pytest

from control_plane.config import Settings
from control_plane.main import verify_startup_config

_REAL_SECRET = "x" * 40


def _settings(**overrides) -> Settings:
    base = dict(
        ENVIRONMENT="production",
        JWT_SECRET=_REAL_SECRET,
        SECRETS_KMS_BACKEND="aws",
    )
    base.update(overrides)
    return Settings(**base)


class TestJwtSecretGuard:
    def test_placeholder_jwt_secret_blocks_production_startup(self):
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            verify_startup_config(
                _settings(JWT_SECRET="insecure-dev-secret-change-me-32-bytes-minimum")
            )

    def test_placeholder_jwt_secret_only_warns_in_dev(self):
        # Does not raise in a dev environment.
        verify_startup_config(
            _settings(
                ENVIRONMENT="development",
                JWT_SECRET="insecure-dev-secret-change-me-32-bytes-minimum",
            )
        )


class TestLocalKmsGuard:
    def test_local_kms_blocks_production_startup(self):
        with pytest.raises(RuntimeError, match="SECRETS_KMS_BACKEND='local'"):
            verify_startup_config(_settings(SECRETS_KMS_BACKEND="local"))

    def test_local_kms_allowed_in_production_with_explicit_override(self):
        verify_startup_config(
            _settings(
                SECRETS_KMS_BACKEND="local",
                BOXKITE_ALLOW_INSECURE_LOCAL_KMS=True,
            )
        )

    def test_local_kms_fine_in_dev(self):
        verify_startup_config(
            _settings(ENVIRONMENT="test", SECRETS_KMS_BACKEND="local")
        )


def test_fully_configured_production_passes():
    verify_startup_config(_settings())
