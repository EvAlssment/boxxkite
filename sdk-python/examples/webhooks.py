"""Runnable end-to-end example against a real hosted control-plane.

    BOXKITE_BASE_URL=https://your-control-plane BOXKITE_API_KEY=bxk_live_... \\
        python examples/webhooks.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time

from boxkite_client import BoxkiteApiError, BoxkiteClient


def verify_signature(
    secret: str, signature_header: str, raw_body: bytes, tolerance_seconds: int = 300
) -> bool:
    """Verify an `X-Boxkite-Webhook-Signature` header, per docs/WEBHOOKS-DESIGN.md §6."""
    parts = dict(p.split("=", 1) for p in signature_header.split(","))
    timestamp, signature = int(parts["t"]), parts["v1"]
    if abs(time.time() - timestamp) > tolerance_seconds:
        return False
    signed_data = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed_data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def main() -> None:
    base_url = os.environ.get("BOXKITE_BASE_URL")
    api_key = os.environ.get("BOXKITE_API_KEY")
    if not base_url or not api_key:
        print("Set BOXKITE_BASE_URL and BOXKITE_API_KEY first.", file=sys.stderr)
        raise SystemExit(1)

    client = BoxkiteClient(base_url=base_url, api_key=api_key)

    try:
        webhook = client.create_webhook(
            url="https://example.com/boxkite-webhook",
            event_types=["sandbox.created", "sandbox.destroyed", "audit_log.entry"],
            description="webhooks example",
        )
        print(f"Created webhook {webhook['id']}")
        print(f"Signing secret (shown once, save it now): {webhook['secret']}")

        # Simulate a delivery to prove verify_signature works, without a real
        # receiver: sign a synthetic payload locally with the just-printed
        # secret, then verify it the same way a receiver would.
        secret = webhook["secret"]
        raw_body = json.dumps({"event_type": "sandbox.created", "event_id": "evt_demo"}).encode()
        timestamp = int(time.time())
        signed_data = f"{timestamp}.".encode() + raw_body
        signature = hmac.new(secret.encode(), signed_data, hashlib.sha256).hexdigest()
        signature_header = f"t={timestamp},v1={signature}"

        is_valid = verify_signature(secret, signature_header, raw_body)
        print(f"Locally signed payload verifies: {is_valid}")

        client.delete_webhook(webhook["id"])
        print("Webhook deleted.")
    except BoxkiteApiError as exc:
        print(f"API error: {exc.message} [{exc.code}]", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
