"""A single exception type for all handled API errors, mapped to a
consistent JSON envelope: {"error": {"code", "message", "details"?}}.

Error messages are validated at review time to never mention a dollar
amount or a plan/tier name (see LimitExceededError) — usage limits are
communicated purely as configurable fair-use caps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class ErrorMetadata:
    retryable: bool
    remediation: str


_DEFAULT_METADATA = ErrorMetadata(
    retryable=False,
    remediation="Review the error message and request details before retrying.",
)


# The wire-level taxonomy is deliberately code-first. New server errors must
# choose a code here so every SDK receives the same retry/remediation signal.
ERROR_TAXONOMY: dict[str, ErrorMetadata] = {
    "rate_limited": ErrorMetadata(True, "Wait and retry after the server's Retry-After interval."),
    "global_capacity_reached": ErrorMetadata(True, "Retry later, or reduce concurrent sandbox usage."),
    "concurrent_sandbox_limit_reached": ErrorMetadata(False, "Destroy an unused sandbox before creating another."),
    "sandbox_size_limit_reached": ErrorMetadata(False, "Choose a smaller sandbox size or change the deployment policy."),
    "sandbox_storage_limit_reached": ErrorMetadata(False, "Reduce requested storage or change the deployment policy."),
    "monthly_usage_limit_reached": ErrorMetadata(False, "Wait for the usage window to reset or change the deployment policy."),
    "image_build_limit_reached": ErrorMetadata(False, "Reduce image build frequency or change the deployment policy."),
    "global_build_capacity_reached": ErrorMetadata(True, "Retry later when image-builder capacity is available."),
    "volume_limit_reached": ErrorMetadata(False, "Delete an unused volume or change the deployment policy."),
    "webhook_limit_reached": ErrorMetadata(False, "Delete an unused webhook or change the deployment policy."),
    "snapshot_limit_reached": ErrorMetadata(False, "Delete an unused snapshot or change the deployment policy."),
    "egress_denied": ErrorMetadata(False, "Allow the destination in the sandbox egress policy or use an approved proxy."),
    "capability_denied": ErrorMetadata(False, "Request the capability explicitly and verify the sandbox policy."),
    "readonly_filesystem": ErrorMetadata(False, "Write to the sandbox workspace or mount a writable volume."),
    "sandbox_not_ready": ErrorMetadata(True, "Wait for the sandbox to become ready, then retry the operation."),
    "sandbox_crashed": ErrorMetadata(False, "Inspect sandbox diagnostics for logs, events, and resource failures."),
    "service_unavailable": ErrorMetadata(True, "Retry after the control plane or sandbox runtime becomes available."),
    "storage_error": ErrorMetadata(True, "Retry the operation and check the deployment's object storage configuration."),
    "preview_unreachable": ErrorMetadata(True, "Verify the process is listening on the requested port and retry."),
}


def error_metadata(code: str, *, status_code: int | None = None) -> ErrorMetadata:
    if code in ERROR_TAXONOMY:
        return ERROR_TAXONOMY[code]
    if status_code is not None and status_code >= 500:
        return ErrorMetadata(True, "Retry later and inspect service health if the problem persists.")
    return _DEFAULT_METADATA


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
        *,
        retryable: bool | None = None,
        remediation: str | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        metadata = error_metadata(code, status_code=status_code)
        self.retryable = metadata.retryable if retryable is None else retryable
        self.remediation = metadata.remediation if remediation is None else remediation
        super().__init__(message)


class LimitExceededError(ApiError):
    """A fair-use limit was hit. Always a 429; message never mentions
    money, dollar amounts, or plan/tier names — only the limit and unit."""

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(429, code, message, details)


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    body: dict[str, Any] = {
        "code": exc.code,
        "message": exc.message,
        "retryable": exc.retryable,
        "remediation": exc.remediation,
    }
    if exc.details is not None:
        body["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content={"error": body})
