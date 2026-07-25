"""Build a Claude-Code-ready sandbox image through the hosted control-plane's
declarative builder (`POST /v1/images`), as the runtime-composed alternative
to hand-maintaining `deploy/sandbox-claude-code.Dockerfile`.

See docs/DECLARATIVE-BUILDER-DESIGN.md for the full API design and
`../hosted_control_plane/hosted_flow.py` for the base signup -> API key ->
sandbox pattern this script follows (same HTTP client, same auth flow, same
`_raise_for_status` helper) -- this script only adds the `/v1/images` build
+ poll step in between API key creation and sandbox creation.

What this proves that the Dockerfile can't show by itself: that
`deploy/sandbox-claude-code.Dockerfile`'s content (git + openssh-client on
top of `boxkite-minimal`, Claude Code installed globally via npm, npm
stripped afterwards) can be expressed as a single `POST /v1/images` call
instead of a hand-maintained Dockerfile, now that `npm_packages` exists on
`SandboxImageBuildRequest` (it didn't when that Dockerfile was written --
see its own header comment).

IMPORTANT -- read before running this against anything but a local dev
instance:
  - The declarative builder is OFF by default
    (`BOXKITE_IMAGE_BUILDER_ENABLED=false`); every /v1/images route 404s
    until an operator opts in.
  - Per docs/DECLARATIVE-BUILDER-DESIGN.md's own status note, the real
    Kubernetes build path (`KanikoJobBuildRunner.run_build`) is not
    implemented yet -- RUNTIME_MODE=k8s deployments get a NotImplementedError
    if they actually try to build. Every non-k8s RUNTIME_MODE (local dev,
    docker-compose) gets a deterministic in-process `FakeImageBuildRunner`
    instead, which fabricates a digest and runs the same scan-gate logic
    without ever invoking a real container build. This script's build step
    is only a REAL build end-to-end against a real Kubernetes cluster with
    that runner finished and reviewed -- see this directory's README for
    exactly what was and wasn't verified.

Prerequisites:
  - A running control-plane instance reachable at CONTROL_PLANE_URL with
    BOXKITE_IMAGE_BUILDER_ENABLED=true (see this directory's README).
  - `pip install httpx`

Run:
    export CONTROL_PLANE_URL=http://localhost:8090
    python build_claude_code_image.py
"""

from __future__ import annotations

import os
import secrets
import sys
import time

import httpx

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8090").rstrip("/")
TIMEOUT = 30.0
POLL_INTERVAL_SECONDS = 1.0
POLL_TIMEOUT_SECONDS = 120.0

# The exact-version pins equivalent to deploy/sandbox-claude-code.Dockerfile's
# content, layered on the "boxkite-minimal" base (lean python+node, no
# data-science/document/browser stack -- see schemas.py's `base` field
# description). Every pin below was verified installable against the live
# Wolfi package repo `boxkite-minimal` is built from -- see this directory's
# README for exactly how and when that was checked, and the caveat that
# Wolfi is a rolling-release distro so old package builds get pruned; a pin
# that resolves today is not guaranteed to still resolve months from now.
APT_PACKAGES = [
    "git==2.54.0-r0",
    "openssh-client==10.0_p1-r2",
]
NPM_PACKAGES = [
    "@anthropic-ai/claude-code==2.0.1",
]
TERMINAL_STATUSES = {"completed", "failed", "rejected"}


def _raise_for_status(resp: httpx.Response, step: str) -> None:
    if resp.status_code >= 400:
        print(f"[{step}] HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()


def _signup_and_get_api_key() -> str:
    email = f"declarative-builder-example-{secrets.token_hex(4)}@example.com"
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
        json={"name": "declarative-builder-example"},
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
            "label": "claude-code-declarative",
            "base": "boxkite-minimal",
            "apt_packages": APT_PACKAGES,
            "npm_packages": NPM_PACKAGES,
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


def _run_claude_version_in_sandbox(api_headers: dict[str, str], image_id: str) -> None:
    print("\n== POST /v1/sandboxes (create session from custom image) ==")
    create_resp = httpx.post(
        f"{CONTROL_PLANE_URL}/v1/sandboxes",
        json={"label": "claude-code-declarative", "image_id": image_id},
        headers=api_headers,
        timeout=TIMEOUT,
    )
    _raise_for_status(create_resp, "create sandbox")
    session = create_resp.json()
    session_id = session["id"]
    print(f"Session {session_id} created (status={session['status']})")

    try:
        print(f"\n== POST /v1/sandboxes/{session_id}/exec (claude --version) ==")
        exec_resp = httpx.post(
            f"{CONTROL_PLANE_URL}/v1/sandboxes/{session_id}/exec",
            json={"command": "claude --version", "timeout": 20},
            headers=api_headers,
            timeout=TIMEOUT,
        )
        _raise_for_status(exec_resp, "exec")
        exec_body = exec_resp.json()
        print(f"exit_code={exec_body['exit_code']} stdout={exec_body['stdout']!r}")
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
    _run_claude_version_in_sandbox(api_headers, image_row["id"])

    print("\nDone.")


if __name__ == "__main__":
    main()
