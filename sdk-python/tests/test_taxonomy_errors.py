from boxxkite_client import BoxxkiteEgressDeniedError, BoxxkiteQuotaExceededError
from boxxkite_client.client import _raise_for_error

import httpx


def test_egress_error_maps_to_named_exception_and_preserves_remediation():
    response = httpx.Response(
        403,
        json={"error": {"code": "egress_denied", "message": "blocked", "retryable": False, "remediation": "allow the host"}},
    )
    try:
        _raise_for_error(response)
    except BoxxkiteEgressDeniedError as exc:
        assert exc.retryable is False
        assert exc.remediation == "allow the host"
    else:
        raise AssertionError("expected BoxxkiteEgressDeniedError")


def test_quota_error_maps_to_named_exception():
    response = httpx.Response(429, json={"error": {"code": "global_capacity_reached", "message": "full"}})
    try:
        _raise_for_error(response)
    except BoxxkiteQuotaExceededError as exc:
        assert exc.code == "global_capacity_reached"
    else:
        raise AssertionError("expected BoxxkiteQuotaExceededError")
