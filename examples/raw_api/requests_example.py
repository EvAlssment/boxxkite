"""Same walkthrough as curl_examples.sh, in Python using `requests` -- no
LangChain and no boxkite Python package. This is the shape to copy if
you're writing a tool-calling layer for a different agent framework and
just need the wire contract for the sidecar's own HTTP API.

Talks directly to the local docker-compose sidecar. For the hosted
control-plane's session-scoped equivalent, see ../hosted_control_plane.

Prerequisites:
    boxkite up      # from the repo root
    pip install requests

Run:
    python requests_example.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

SIDECAR_URL = os.environ.get("SIDECAR_URL", "http://localhost:8080")


def _load_token() -> str:
    token = os.environ.get("SIDECAR_AUTH_TOKEN", "").strip()
    if token:
        return token

    local_env = Path.home() / ".boxkite" / "local.env"
    if local_env.exists():
        for line in local_env.read_text().splitlines():
            if line.startswith("SIDECAR_AUTH_TOKEN="):
                return line.split("=", 1)[1].strip()

    print(
        "Set SIDECAR_AUTH_TOKEN, or run `boxkite up` first so "
        "~/.boxkite/local.env has one.",
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> None:
    token = _load_token()
    headers = {"X-Sidecar-Auth-Token": token}

    print("== GET /health (no auth required) ==")
    health = requests.get(f"{SIDECAR_URL}/health", timeout=10)
    health.raise_for_status()
    print(health.json())

    print("\n== POST /exec ==")
    exec_resp = requests.post(
        f"{SIDECAR_URL}/exec",
        headers=headers,
        json={"command": "python3 -c \"print(sum(range(101)))\"", "timeout": 30},
        timeout=40,
    )
    exec_resp.raise_for_status()
    exec_body = exec_resp.json()
    print(f"exit_code={exec_body['exit_code']} stdout={exec_body['stdout']!r}")
    assert exec_body["exit_code"] == 0
    assert exec_body["stdout"].strip() == "5050"

    print("\n== POST /file-create ==")
    create_resp = requests.post(
        f"{SIDECAR_URL}/file-create",
        headers=headers,
        json={
            "path": "raw_api_python_hello.txt",
            "content": "hello from the raw_api Python example\n",
        },
        timeout=10,
    )
    create_resp.raise_for_status()
    create_body = create_resp.json()
    print(create_body)
    assert create_body["created"] is True

    print("\n== POST /view ==")
    view_resp = requests.post(
        f"{SIDECAR_URL}/view",
        headers=headers,
        json={"path": "raw_api_python_hello.txt"},
        timeout=10,
    )
    view_resp.raise_for_status()
    view_body = view_resp.json()
    print(view_body)
    assert "hello from the raw_api Python example" in view_body["content"]

    print("\n== POST /str-replace ==")
    replace_resp = requests.post(
        f"{SIDECAR_URL}/str-replace",
        headers=headers,
        json={
            "path": "raw_api_python_hello.txt",
            "old_str": "hello",
            "new_str": "greetings",
        },
        timeout=10,
    )
    replace_resp.raise_for_status()
    print(replace_resp.json())

    print("\nAll raw_api requests succeeded.")


if __name__ == "__main__":
    main()
