"""Build a quant-research sandbox image through the hosted control-plane's
declarative builder (`POST /v1/images`) -- see GitHub issue #135 and
`docs/DECLARATIVE-BUILDER-DESIGN.md` for the full API design.

boxkite's default sandbox image already ships pandas/numpy/polars/
scikit-learn (see the "self-hosted quant research agent for banks" blog
post, `site/app/blog/self-hosted-quant-research-agent-for-banks/page.tsx`)
-- real quant desks additionally lean on vectorbt (vectorized backtesting),
backtrader (event-driven, broker-realistic backtesting), TA-Lib (technical
indicators), QuantLib (derivatives/fixed-income pricing), and quantstats
(portfolio tear sheets), none of which are preinstalled. This script builds
those five, exact-version-pinned, on top of "boxkite-default" instead of
hand-maintaining a new Dockerfile for this one vertical.

Follows `../hosted_control_plane/hosted_flow.py`'s signup -> API key ->
sandbox pattern and `../claude_code_declarative_builder/build_claude_code_image.py`'s
build -> poll -> create-sandbox-from-image pattern (same HTTP client, same
`_raise_for_status` helper) -- this script's only new piece is the
`python_packages` list and the smoke-test script it runs once the image is
built.

IMPORTANT -- read before running this against anything but a local dev
instance:
  - The declarative builder is OFF by default
    (`BOXKITE_IMAGE_BUILDER_ENABLED=false`); every /v1/images route 404s
    until an operator opts in.
  - Per docs/DECLARATIVE-BUILDER-DESIGN.md's own status note, the real
    Kubernetes build path (`KanikoJobBuildRunner.run_build`) has not been
    exercised against a live cluster. Every non-k8s RUNTIME_MODE (local
    dev, docker-compose) gets a deterministic in-process
    `FakeImageBuildRunner` instead, which fabricates a digest and runs the
    same scan-gate logic without ever invoking a real container build.
    See this directory's README for exactly what was and wasn't verified.

Prerequisites:
  - A running control-plane instance reachable at CONTROL_PLANE_URL with
    BOXKITE_IMAGE_BUILDER_ENABLED=true (see this directory's README).
  - `pip install httpx`

Run:
    export CONTROL_PLANE_URL=http://localhost:8090
    python build_quant_research_image.py
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

import httpx

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8090").rstrip("/")
TIMEOUT = 30.0
POLL_INTERVAL_SECONDS = 1.0
POLL_TIMEOUT_SECONDS = 120.0

# Exact-version pins -- see this directory's README for how each was
# checked against the live PyPI index (real releases, real wheels for the
# base image's Python 3.11 / glibc combination) rather than guessed.
# Layered on "boxkite-default" (the full pandas/numpy/polars/scikit-learn
# stack the blog post above describes) since a quant workload wants both
# the existing data-science stack and these five additions, not a leaner
# base with the existing stack re-declared on top of it.
PYTHON_PACKAGES = [
    "vectorbt==0.28.5",
    "backtrader==1.9.78.123",
    "TA-Lib==0.7.0",
    "QuantLib==1.42.1",
    "quantstats==0.0.81",
]
TERMINAL_STATUSES = {"completed", "failed", "rejected"}
SMOKE_TEST_SCRIPT = (Path(__file__).parent / "quant_smoke_test.py").read_text()


def _raise_for_status(resp: httpx.Response, step: str) -> None:
    if resp.status_code >= 400:
        print(f"[{step}] HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()


def _signup_and_get_api_key() -> str:
    email = f"quant-research-example-{secrets.token_hex(4)}@example.com"
    password = secrets.token_urlsafe(16)

    print(f"== POST /v1/auth/signup ({email}) ==")
    signup_resp = httpx.post(
        f"{CONTROL_PLANE_URL}/v1/auth/signup",
        json={"email": email, "password": password},
        timeout=TIMEOUT,
    )
    _raise_for_status(signup_resp, "signup")
    dashboard_token = signup_resp.json()["access_token"]
    print(f"Account created: {signup_resp.json()['account']['id']}")

    print("\n== POST /v1/api-keys ==")
    key_resp = httpx.post(
        f"{CONTROL_PLANE_URL}/v1/api-keys",
        json={"name": "quant-research-example"},
        headers={"Authorization": f"Bearer {dashboard_token}"},
        timeout=TIMEOUT,
    )
    _raise_for_status(key_resp, "create api key")
    api_key = key_resp.json()["key"]
    print(f"API key created: {key_resp.json()['prefix']}...")
    return api_key


def _build_image(api_headers: dict[str, str]) -> dict:
    print("\n== POST /v1/images (build request) ==")
    build_resp = httpx.post(
        f"{CONTROL_PLANE_URL}/v1/images",
        json={
            "label": "quant-research",
            "base": "boxkite-default",
            "python_packages": PYTHON_PACKAGES,
        },
        headers=api_headers,
        timeout=TIMEOUT,
    )
    _raise_for_status(build_resp, "build image")
    accepted = build_resp.json()
    image_id = accepted["id"]
    print(f"Image {image_id} queued (status={accepted['status']})")

    print(f"\n== GET /v1/images/{image_id} (poll) ==")
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    row: dict = {}
    while time.monotonic() < deadline:
        status_resp = httpx.get(
            f"{CONTROL_PLANE_URL}/v1/images/{image_id}",
            headers=api_headers,
            timeout=TIMEOUT,
        )
        _raise_for_status(status_resp, "poll image")
        row = status_resp.json()
        print(f"  status={row['status']}")
        if row["status"] in TERMINAL_STATUSES:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    else:
        raise TimeoutError(f"Image {image_id} did not reach a terminal status within {POLL_TIMEOUT_SECONDS}s")

    if row["status"] != "completed":
        raise RuntimeError(f"Image build did not complete: {row}")

    print(f"Image built: digest={row['digest']} registry_ref={row['registry_ref']}")
    return row


def _run_smoke_test_in_sandbox(api_headers: dict[str, str], image_id: str) -> None:
    print("\n== POST /v1/sandboxes (create session from custom image) ==")
    create_resp = httpx.post(
        f"{CONTROL_PLANE_URL}/v1/sandboxes",
        json={"label": "quant-research", "image_id": image_id},
        headers=api_headers,
        timeout=TIMEOUT,
    )
    _raise_for_status(create_resp, "create sandbox")
    session = create_resp.json()
    session_id = session["id"]
    print(f"Session {session_id} created (status={session['status']})")

    try:
        print(f"\n== POST /v1/sandboxes/{session_id}/files (write quant_smoke_test.py) ==")
        file_resp = httpx.post(
            f"{CONTROL_PLANE_URL}/v1/sandboxes/{session_id}/files",
            json={"path": "/workspace/quant_smoke_test.py", "content": SMOKE_TEST_SCRIPT},
            headers=api_headers,
            timeout=TIMEOUT,
        )
        _raise_for_status(file_resp, "write smoke test file")

        print(f"\n== POST /v1/sandboxes/{session_id}/exec (run quant_smoke_test.py) ==")
        exec_resp = httpx.post(
            f"{CONTROL_PLANE_URL}/v1/sandboxes/{session_id}/exec",
            json={"command": "python3 /workspace/quant_smoke_test.py", "timeout": 60},
            headers=api_headers,
            timeout=TIMEOUT,
        )
        _raise_for_status(exec_resp, "exec")
        exec_body = exec_resp.json()
        print(f"exit_code={exec_body['exit_code']}")
        print(exec_body["stdout"])
        if exec_body["stderr"]:
            print(f"stderr: {exec_body['stderr']}", file=sys.stderr)
    finally:
        print(f"\n== DELETE /v1/sandboxes/{session_id} (teardown) ==")
        delete_resp = httpx.delete(
            f"{CONTROL_PLANE_URL}/v1/sandboxes/{session_id}",
            headers=api_headers,
            timeout=TIMEOUT,
        )
        if delete_resp.status_code >= 400:
            print(f"Teardown returned HTTP {delete_resp.status_code}: {delete_resp.text}", file=sys.stderr)
        else:
            print(f"Session {session_id} destroyed.")


def main() -> None:
    api_key = _signup_and_get_api_key()
    api_headers = {"Authorization": f"Bearer {api_key}"}

    image_row = _build_image(api_headers)
    _run_smoke_test_in_sandbox(api_headers, image_row["id"])

    print("\nDone.")


if __name__ == "__main__":
    main()
