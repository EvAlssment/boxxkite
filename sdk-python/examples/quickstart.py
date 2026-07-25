"""Runnable end-to-end example against a real hosted control-plane.

    BOXKITE_BASE_URL=https://your-control-plane BOXKITE_API_KEY=bxk_live_... \
        python examples/quickstart.py
"""

from __future__ import annotations

import os
import sys

from boxkite_client import BoxkiteApiError, BoxkiteClient


def main() -> None:
    base_url = os.environ.get("BOXKITE_BASE_URL")
    api_key = os.environ.get("BOXKITE_API_KEY")
    if not base_url or not api_key:
        print("Set BOXKITE_BASE_URL and BOXKITE_API_KEY first.", file=sys.stderr)
        raise SystemExit(1)

    client = BoxkiteClient(base_url=base_url, api_key=api_key)

    account = client.account()
    print(f"Signed in as {account['email']}")

    usage = client.usage()
    print(
        f"Usage: {usage['monthly_sandbox_hours_used']}/{usage['monthly_sandbox_hours_limit']} "
        f"sandbox-hours, {usage['concurrent_sandboxes']}/{usage['concurrent_sandboxes_limit']} concurrent"
    )

    try:
        with client.sandbox(label="sdk-quickstart") as sb:
            print(f"Created sandbox {sb.id}")

            result = sb.exec("python3 -c 'print(1 + 1)'")
            print(f"exec result: {result['stdout'].strip()}")

            sb.file_create("hello.txt", "hello from boxkite-client\n")
            viewed = sb.view("hello.txt")
            print(f"file contents: {viewed['content'].strip()}")
        print("Sandbox destroyed.")
    except BoxkiteApiError as exc:
        print(f"API error: {exc.message} [{exc.code}]", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
