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
