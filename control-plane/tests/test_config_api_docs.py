"""Tests for the ENABLE_API_DOCS / api_docs_enabled gating logic (config.py).

FastAPI's docs_url/redoc_url/openapi_url are fixed at app-construction time
(import time), so these tests exercise the Settings property directly rather
than re-importing control_plane.main under different env vars.
"""

from __future__ import annotations

import httpx

from control_plane.config import Settings


def test_docs_enabled_by_default_in_development() -> None:
    settings = Settings(ENVIRONMENT="development", ENABLE_API_DOCS=None)
    assert settings.is_dev_environment is True
    assert settings.api_docs_enabled is True


def test_docs_disabled_by_default_in_production() -> None:
    settings = Settings(ENVIRONMENT="production", ENABLE_API_DOCS=None)
    assert settings.is_dev_environment is False
    assert settings.api_docs_enabled is False


def test_explicit_true_overrides_production_default() -> None:
    settings = Settings(ENVIRONMENT="production", ENABLE_API_DOCS=True)
    assert settings.api_docs_enabled is True


def test_explicit_false_overrides_development_default() -> None:
    settings = Settings(ENVIRONMENT="development", ENABLE_API_DOCS=False)
    assert settings.api_docs_enabled is False


async def test_docs_reachable_in_default_test_app(client: httpx.AsyncClient) -> None:
    # The shared `client` fixture runs against the real module-level `app`,
    # constructed at import time with ENVIRONMENT defaulting to
    # "development" (no env override in the test process) -- so docs should
    # be reachable here, exercising the actual main.py wiring once.
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    resp = await client.get("/docs")
    assert resp.status_code == 200
