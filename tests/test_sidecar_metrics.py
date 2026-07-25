"""Tests for the sidecar's Prometheus-style /metrics endpoint.

docs/E2B-COMPARISON.md's gap table named "OpenTelemetry & Metrics" as a
low-fit gap -- this is the pull-only, dependency-free counterpart (see
main.py's own module docstring on why not a full OTel SDK/exporter: that
would need outbound egress, which conflicts with the sidecar's default-deny
posture).

Covers:
- /metrics requires the same sidecar auth as every other route (not exempt).
- Route labels collapse path params (e.g. a process id) so cardinality
  stays bounded instead of growing per unique id.
- /exec increments the exec counters, and a failing command increments the
  error counter too.
"""

import main as sidecar_main
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def _reset_metrics_state(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "_metrics_request_counts", {})
    monkeypatch.setattr(sidecar_main, "_metrics_request_errors", {})
    monkeypatch.setattr(sidecar_main, "_metrics_exec_count", 0)
    monkeypatch.setattr(sidecar_main, "_metrics_exec_errors", 0)


def test_metrics_requires_auth_like_every_other_route(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    response = client.get("/metrics")

    assert response.status_code == 401


def test_metrics_reports_request_counts(monkeypatch):
    _reset_metrics_state(monkeypatch)
    client = _client()

    client.get("/health")
    client.get("/health")

    response = client.get("/metrics", headers=_auth_headers())

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert 'boxkite_sidecar_requests_total{route="/health"} 2' in body


def test_metrics_collapses_path_params_into_a_stable_label():
    assert sidecar_main._metrics_route_label("/process/abc-123/output") == "/process/{id}/output"
    assert sidecar_main._metrics_route_label("/process/xyz-789/output") == "/process/{id}/output"
    assert sidecar_main._metrics_route_label("/health") == "/health"
    assert sidecar_main._metrics_route_label("/") == "/"


def test_exec_increments_exec_counters(monkeypatch):
    _reset_metrics_state(monkeypatch)

    async def _fake_exec(command, timeout, extra_env=None):
        return (0, "ok", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec)
    client = _client()

    client.post("/exec", json={"command": "echo hi", "timeout": 5}, headers=_auth_headers())

    response = client.get("/metrics", headers=_auth_headers())
    body = response.text
    assert "boxkite_sidecar_exec_total 1" in body
    assert "boxkite_sidecar_exec_errors_total 0" in body


def test_exec_failure_increments_the_error_counter(monkeypatch):
    _reset_metrics_state(monkeypatch)

    async def _fake_exec(command, timeout, extra_env=None):
        return (1, "", "boom")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec)
    client = _client()

    client.post("/exec", json={"command": "false", "timeout": 5}, headers=_auth_headers())

    response = client.get("/metrics", headers=_auth_headers())
    body = response.text
    assert "boxkite_sidecar_exec_total 1" in body
    assert "boxkite_sidecar_exec_errors_total 1" in body
