"""Tests for the sidecar's shared-secret HTTP authentication (Critical #1).

Covers:
- Every non-exempt route rejects requests without a valid token (401).
- /health remains reachable without a token (kubelet probes send no headers).
- The sidecar fails CLOSED (503) when no token is configured at all, rather
  than silently running unauthenticated.
- A correct token is accepted.
"""

import main as sidecar_main
from fastapi.testclient import TestClient


# Endpoints kept intentionally reachable without the shared secret. Any
# growth of this set should be a deliberate, reviewed decision — see the
# comment on `_AUTH_EXEMPT_PATHS` in sidecar/main.py.
EXPECTED_EXEMPT_PATHS = {"/health"}


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _all_app_routes() -> list[tuple[str, str]]:
    """Return (method, path) pairs for every real endpoint the app serves.

    Filters out FastAPI's auto-generated docs/openapi routes and the
    implicit HEAD/OPTIONS methods, keeping only the sidecar's own routes so
    this test tracks sidecar/main.py's actual route list rather than
    framework scaffolding.
    """
    pairs: list[tuple[str, str]] = []
    for route in _iter_leaf_routes(sidecar_main.app.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        if path in {"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            pairs.append((method, path))
    return pairs


def _iter_leaf_routes(routes):
    """Yield every leaf route, descending through included/mounted sub-routers.

    FastAPI 0.139's `app.include_router()` doesn't flatten routes into
    `app.routes`; it inserts a lazy `_IncludedRouter` wrapper whose real
    routes live under `.original_router.routes`. Starlette mounts similarly
    nest under `.routes`. A flat walk of `app.routes` therefore sees only the
    handful of routes declared directly with `@app.<method>` (which is why
    this test — and the auth-coverage test below — silently degraded to just
    `/health` + `/metrics`). Recurse so both track the real route surface.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None and getattr(included, "routes", None) is not None:
            yield from _iter_leaf_routes(included.routes)
            continue
        nested = getattr(route, "routes", None)
        if nested and not getattr(route, "path", None):
            yield from _iter_leaf_routes(nested)
            continue
        yield route


def test_route_inventory_matches_known_sidecar_routes():
    """Guard against silently adding a new route without an auth test covering it."""
    known_paths = {
        "/health",
        "/metrics",
        "/exec",
        "/http-request",
        "/interpreter/exec",
        "/interpreter/reset",
        "/interpreter/status",
        "/node-interpreter/exec",
        "/node-interpreter/reset",
        "/node-interpreter/status",
        "/lsp/start",
        "/lsp/{lsp_id}/open",
        "/lsp/{lsp_id}/completion",
        "/lsp/{lsp_id}/stop",
        "/browser/navigate",
        "/browser/exec",
        "/browser/screenshot",
        "/browser/close",
        "/ensure-skills",
        "/inject-skills",
        "/file-create",
        "/view",
        "/read-image",
        "/str-replace",
        "/present-files",
        "/ls",
        "/glob",
        "/grep",
        "/watch",
        "/pty-exec",
        "/mount-bucket",
        "/configure",
        "/prefetch-uploads",
        "/flush",
        "/flush/confirmed",
        "/tool-call",
        "/process/start",
        "/process/{process_id}/output",
        "/process/{process_id}/input",
        "/process/{process_id}/stop",
        "/process",
        "/process/kill-all",
        "/preview/{port}/{path:path}",
    }
    actual_paths = {path for _, path in _all_app_routes()}
    assert actual_paths == known_paths, (
        "sidecar/main.py's route list changed — update EXPECTED_EXEMPT_PATHS "
        "and the auth test coverage in this file, then update known_paths here. "
        f"actual={sorted(actual_paths)}"
    )


def test_every_non_exempt_route_rejects_missing_token(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    protected_routes = [
        (method, path)
        for method, path in _all_app_routes()
        if path not in EXPECTED_EXEMPT_PATHS
    ]
    assert protected_routes, "expected at least one protected route to test"

    for method, path in protected_routes:
        response = client.request(method, path, json={})
        assert response.status_code == 401, (
            f"{method} {path} should reject a request with no auth header, "
            f"got {response.status_code}: {response.text}"
        )
        assert "invalid" in response.json()["detail"].lower() or "missing" in response.json()["detail"].lower()


def test_every_non_exempt_route_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    protected_routes = [
        (method, path)
        for method, path in _all_app_routes()
        if path not in EXPECTED_EXEMPT_PATHS
    ]

    for method, path in protected_routes:
        response = client.request(
            method,
            path,
            json={},
            headers={sidecar_main.SIDECAR_AUTH_HEADER: "totally-wrong-value"},
        )
        assert response.status_code == 401, f"{method} {path} should reject a mismatched token"


def test_configure_is_protected_even_though_its_the_bootstrap_call(monkeypatch):
    """/configure is called once at pod claim time — it still needs the secret.

    The secret is provisioned into the pod BEFORE the manager ever calls
    /configure (SandboxManager/WarmPoolManager inject SIDECAR_AUTH_TOKEN as a
    container env var at pod-creation time, before the pod is claimed), so
    there is no bootstrapping gap: /configure never runs without the sidecar
    already having its token.
    """
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    response = client.post("/configure", json={"session_id": "s1"})
    assert response.status_code == 401


def test_health_reachable_without_token_even_when_configured(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    response = client.get("/health")
    assert response.status_code == 200


def test_health_reachable_without_token_when_unconfigured(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "")
    client = _client()

    response = client.get("/health")
    assert response.status_code == 200


def test_protected_route_fails_closed_when_token_unconfigured(monkeypatch):
    """No SIDECAR_AUTH_TOKEN configured at all -> 503, not silently open."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "")
    client = _client()

    response = client.post("/exec", json={"command": "echo hi"})
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"].lower()


def test_correct_token_is_accepted(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")

    async def _fake_exec_in_sandbox(command, timeout=30, extra_env=None):
        return (0, "hi\n", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox)
    client = _client()

    response = client.post(
        "/exec",
        json={"command": "echo hi"},
        headers={sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"},
    )
    assert response.status_code == 200
    assert response.json()["stdout"] == "hi\n"


def test_token_comparison_is_constant_time(monkeypatch):
    """Auth must use hmac.compare_digest, not `==`, to avoid timing side channels."""
    import inspect

    source = inspect.getsource(sidecar_main.enforce_sidecar_auth)
    assert "compare_digest" in source


def test_normalize_rejects_pod_template_placeholder_as_unconfigured():
    """A self-hoster who copies deploy/pod-template.yaml's literal
    SIDECAR_AUTH_TOKEN value verbatim must get the same fail-closed 503 as
    an unset token, not a plausible-looking-but-shared secret that silently
    works."""
    assert (
        sidecar_main._normalize_sidecar_auth_token(
            sidecar_main.SIDECAR_AUTH_TOKEN_TEMPLATE_PLACEHOLDER
        )
        == ""
    )


def test_normalize_passes_through_a_real_token():
    assert sidecar_main._normalize_sidecar_auth_token("a-real-random-token") == "a-real-random-token"


def test_protected_route_fails_closed_when_token_is_the_template_placeholder(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "")  # what normalization produces
    client = _client()

    response = client.post(
        "/exec",
        json={"command": "echo hi"},
        headers={sidecar_main.SIDECAR_AUTH_HEADER: sidecar_main.SIDECAR_AUTH_TOKEN_TEMPLATE_PLACEHOLDER},
    )
    assert response.status_code == 503
