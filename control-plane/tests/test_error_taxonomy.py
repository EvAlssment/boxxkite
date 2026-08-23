"""The control-plane error envelope is the contract shared by every SDK."""

from control_plane.errors import ApiError, error_metadata


def test_taxonomy_metadata_marks_quota_retryability_and_remediation():
    metadata = error_metadata("global_capacity_reached")
    assert metadata.retryable is True
    assert metadata.remediation


def test_api_error_exposes_actionable_wire_fields():
    error = ApiError(403, "egress_denied", "Network egress was denied")
    assert error.retryable is False
    assert "egress policy" in error.remediation


# ── _sandbox_operation_error classification (issue #94 follow-up) ────────


def test_transport_failures_are_retryable_502_not_egress_denied():
    """"connection refused" / "network is unreachable" are the control-plane
    failing to reach the sidecar. Classifying them as egress_denied made a
    transient blip look like a permanent 403 policy violation that no SDK
    would retry."""
    from control_plane.routers.sandboxes import _sandbox_operation_error

    for message in ("Connection refused", "Network is unreachable", "connect call failed"):
        err = _sandbox_operation_error("exec", RuntimeError(message))
        assert err.status_code == 502, message
        assert err.code == "sandbox_operation_failed", message
        assert err.retryable is True, message


def test_a_real_egress_denial_is_still_classified():
    from control_plane.routers.sandboxes import _sandbox_operation_error

    err = _sandbox_operation_error("exec", RuntimeError("egress denied by policy"))
    assert err.status_code == 403
    assert err.code == "egress_denied"


def test_not_ready_still_maps_to_503():
    from control_plane.routers.sandboxes import _sandbox_operation_error

    err = _sandbox_operation_error("exec", RuntimeError("no running pod for session"))
    assert err.status_code == 503
    assert err.code == "sandbox_not_ready"
