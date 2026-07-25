"""Structured logging + Prometheus metrics for the control-plane.

Two things a production operator needs and the service previously lacked:

- **Structured JSON logs** (`configure_logging`): one JSON object per log line
  (timestamp, level, logger, message, plus any structured `extra=` fields and a
  request id), so logs are queryable in Loki/CloudWatch/Datadog instead of
  regex-scraped. Human-readable text stays the default in dev.
- **Prometheus `/metrics`** (`RequestMetricsMiddleware` + `render_metrics`):
  request count and latency histogram, labeled by method, matched route
  *template* (never the raw path — session ids etc. would explode label
  cardinality), and status. A per-request `X-Request-ID` is added to every
  response and echoed in the access log for correlation.

The middleware is pure-ASGI (it only wraps `send` and reads `scope`), so it
never buffers the response body or interferes with streaming/background tasks.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

# A dedicated registry (not the global default) so importing this module twice
# in a test session can't raise "Duplicated timeseries".
REGISTRY = CollectorRegistry()

REQUEST_COUNT = Counter(
    "boxkite_http_requests_total",
    "Total HTTP requests processed, by method, matched route template, and status.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
REQUEST_LATENCY = Histogram(
    "boxkite_http_request_duration_seconds",
    "HTTP request latency in seconds, by method and matched route template.",
    ["method", "route"],
    registry=REGISTRY,
)

# Paths excluded from metrics recording so probes/scrapes don't inflate counts.
_UNINSTRUMENTED_PREFIXES = ("/metrics", "/health")

_ACCESS_LOGGER = logging.getLogger("boxkite.access")

# LogRecord attributes that are built-in; everything else in a record's __dict__
# came from a structured `extra=` and is worth emitting.
_STD_LOGRECORD_KEYS = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonLogFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object, including `extra=` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_LOGRECORD_KEYS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(cfg) -> None:
    """Install a root log handler. JSON when BOXKITE_JSON_LOGS is true (or unset
    and not a dev/test ENVIRONMENT); human-readable text otherwise."""
    use_json = cfg.BOXKITE_JSON_LOGS
    if use_json is None:
        use_json = not cfg.is_dev_environment

    handler = logging.StreamHandler()
    if use_json:
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def render_metrics() -> tuple[bytes, str]:
    """Return (body, content_type) for the Prometheus exposition endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class RequestMetricsMiddleware:
    """Times each request, records Prometheus metrics against the matched route
    template, stamps an X-Request-ID, and emits a structured access log."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request_id = _header(scope, b"x-request-id") or uuid.uuid4().hex
        status_holder = {"status": 500}
        start = time.perf_counter()

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("latin-1"))
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            path = scope.get("path", "")
            method = scope.get("method", "")
            status = status_holder["status"]
            route = scope.get("route")
            # Matched template (low cardinality) if routing succeeded, else a
            # single "unmatched" bucket so 404-scanning can't explode labels.
            route_label = getattr(route, "path", None) or "unmatched"

            if not path.startswith(_UNINSTRUMENTED_PREFIXES):
                REQUEST_COUNT.labels(method, route_label, str(status)).inc()
                REQUEST_LATENCY.labels(method, route_label).observe(elapsed)

            _ACCESS_LOGGER.info(
                "%s %s %s",
                method,
                path,
                status,
                extra={
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "route": route_label,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
