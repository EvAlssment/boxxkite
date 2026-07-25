"""Structured logging + Prometheus /metrics."""

from __future__ import annotations

import json
import logging

import httpx

from control_plane.config import Settings
from control_plane.observability import JsonLogFormatter, configure_logging


class TestJsonLogFormatter:
    def _record(self, **extra):
        return logging.LogRecord(
            name="boxkite.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        ), extra

    def test_emits_valid_json_with_core_fields(self):
        record, _ = self._record()
        line = JsonLogFormatter().format(record)
        obj = json.loads(line)
        assert obj["level"] == "INFO"
        assert obj["logger"] == "boxkite.test"
        assert obj["message"] == "hello world"
        assert "ts" in obj

    def test_includes_structured_extra_fields(self):
        record = logging.LogRecord(
            "boxkite.access", logging.INFO, __file__, 1, "GET / 200", None, None
        )
        record.request_id = "abc123"
        record.status = 200
        obj = json.loads(JsonLogFormatter().format(record))
        assert obj["request_id"] == "abc123"
        assert obj["status"] == 200


class TestConfigureLogging:
    def test_auto_uses_json_outside_dev(self):
        configure_logging(Settings(ENVIRONMENT="production", JWT_SECRET="x" * 40))
        assert isinstance(logging.getLogger().handlers[0].formatter, JsonLogFormatter)

    def test_auto_uses_text_in_dev(self):
        configure_logging(Settings(ENVIRONMENT="development"))
        assert not isinstance(logging.getLogger().handlers[0].formatter, JsonLogFormatter)

    def test_explicit_flag_overrides_environment(self):
        configure_logging(Settings(ENVIRONMENT="development", BOXKITE_JSON_LOGS=True))
        assert isinstance(logging.getLogger().handlers[0].formatter, JsonLogFormatter)


class TestMetricsEndpoint:
    async def test_metrics_exposition_and_request_counting(self, client: httpx.AsyncClient):
        # Generate a request so at least one metric series exists.
        await client.get("/health/ready")
        # /health is excluded from metrics; hit a real (401) route to record one.
        await client.get("/v1/sandboxes")

        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        body = resp.text
        assert "boxkite_http_requests_total" in body
        assert "boxkite_http_request_duration_seconds" in body
        # The matched route template (not the raw path) is used as a label.
        assert "/v1/sandboxes" in body

    async def test_every_response_carries_a_request_id(self, client: httpx.AsyncClient):
        resp = await client.get("/health")
        assert resp.headers.get("x-request-id")

    async def test_metrics_404_when_disabled(self, client: httpx.AsyncClient, monkeypatch):
        monkeypatch.setattr("control_plane.main.settings.BOXKITE_METRICS_ENABLED", False)
        resp = await client.get("/metrics")
        assert resp.status_code == 404
