"""End-to-end walkthrough of boxkite's hosted control-plane API: signup ->
create an API key -> create a sandbox session -> exec a command -> read a
file -> tear the session down.

This is the multi-tenant HTTP API in `control-plane/` -- NOT the local
docker-compose sidecar `../raw_api` talks to directly. The control-plane
sits in front of `SandboxManager` and adds accounts, API keys, per-account
usage/concurrency limits, and session ownership -- see
`control-plane/src/control_plane/routers/sandboxes.py`.

As the main README says: "there is no publicly running boxkite-hosted
service to sign up for" -- `boxkite signup` and this script both assume
*you* have deployed `control-plane/` somewhere (or are running it locally
for this walkthrough; see README.md in this directory for how).

Prerequisites:
  - A running control-plane instance reachable at CONTROL_PLANE_URL, backed
    by a SandboxManager that can actually reach a sandbox runtime (compose
    or K8s) -- see this directory's README for a local dev setup.
  - `pip install httpx`

Run:
    export CONTROL_PLANE_URL=http://localhost:8090
    python hosted_flow.py
"""

from __future__ import annotations

import os
import secrets
import sys

import httpx

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8090").rstrip("/")
TIMEOUT = 30.0


def _raise_for_status(resp: httpx.Response, step: str) -> None:
    if resp.status_code >= 400:
        print(f"[{step}] HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()


def main() -> None:
    email = f"cookbook-example-{secrets.token_hex(4)}@example.com"
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
    # Note: this call is authenticated with the short-lived dashboard JWT
    # from signup, NOT an API key -- you can't create an API key using an
    # API key (see control-plane/src/control_plane/routers/api_keys.py).
    key_resp = httpx.post(
        f"{CONTROL_PLANE_URL}/v1/api-keys",
        json={"name": "cookbook-example"},
        headers={"Authorization": f"Bearer {dashboard_token}"},
        timeout=TIMEOUT,
    )
    _raise_for_status(key_resp, "create api key")
    api_key = key_resp.json()["key"]
    print(f"API key created: {key_resp.json()['prefix']}...")

    # Every /v1/sandboxes/* call below uses this API key, not the dashboard
    # JWT -- the two are never interchangeable.
    api_headers = {"Authorization": f"Bearer {api_key}"}

    print("\n== POST /v1/sandboxes (create session) ==")
    create_resp = httpx.post(
        f"{CONTROL_PLANE_URL}/v1/sandboxes",
        json={"label": "cookbook-example"},
        headers=api_headers,
        timeout=TIMEOUT,
    )
    _raise_for_status(create_resp, "create sandbox")
    session = create_resp.json()
    session_id = session["id"]
    print(f"Session {session_id} created (status={session['status']})")
    print(f"Usage: {session['usage']}")

    try:
        print(f"\n== POST /v1/sandboxes/{session_id}/exec ==")
        exec_resp = httpx.post(
            f"{CONTROL_PLANE_URL}/v1/sandboxes/{session_id}/exec",
            json={"command": "python3 -c \"print('hello from the hosted control-plane')\"", "timeout": 20},
            headers=api_headers,
            timeout=TIMEOUT,
        )
        _raise_for_status(exec_resp, "exec")
        exec_body = exec_resp.json()
        print(f"exit_code={exec_body['exit_code']} stdout={exec_body['stdout']!r}")

        print(f"\n== POST /v1/sandboxes/{session_id}/files (create) ==")
        file_resp = httpx.post(
            f"{CONTROL_PLANE_URL}/v1/sandboxes/{session_id}/files",
            json={"path": "hosted_example.txt", "content": "written via the hosted control-plane API\n"},
            headers=api_headers,
            timeout=TIMEOUT,
        )
        _raise_for_status(file_resp, "file create")
        print(file_resp.json())

        print(f"\n== POST /v1/sandboxes/{session_id}/files/view ==")
        view_resp = httpx.post(
            f"{CONTROL_PLANE_URL}/v1/sandboxes/{session_id}/files/view",
            json={"path": "hosted_example.txt"},
            headers=api_headers,
            timeout=TIMEOUT,
        )
        _raise_for_status(view_resp, "file view")
        print(view_resp.json())

        print("\n== GET /v1/sandboxes (list sessions) ==")
        list_resp = httpx.get(
            f"{CONTROL_PLANE_URL}/v1/sandboxes",
            params={"active_only": "true"},
            headers=api_headers,
            timeout=TIMEOUT,
        )
        _raise_for_status(list_resp, "list sandboxes")
        for row in list_resp.json():
            print(f"  {row['id']}  status={row['status']}  label={row['label']}")

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

    print("\nDone.")


if __name__ == "__main__":
    main()
