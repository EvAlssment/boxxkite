"""Exception types raised by BoxxkiteClient/AsyncBoxxkiteClient."""

from __future__ import annotations


class BoxxkiteError(Exception):
    """Base class for every error this SDK raises."""


class BoxxkiteConnectionError(BoxxkiteError):
    """The control-plane could not be reached at all (DNS, TLS, timeout)."""


class BoxxkiteApiError(BoxxkiteError):
    """The control-plane responded with an error envelope
    (`{"error": {code, message}}`), e.g. a 404, 401, or 429."""

    def __init__(self, *, status_code: int, code: str, message: str, retryable: bool = False, remediation: str | None = None, details: object | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.remediation = remediation
        self.details = details
        super().__init__(f"{message} [{code}] (HTTP {status_code})")


class BoxxkiteQuotaExceededError(BoxxkiteApiError): pass
class BoxxkiteEgressDeniedError(BoxxkiteApiError): pass
class BoxxkiteSandboxNotReadyError(BoxxkiteApiError): pass
class BoxxkiteCapabilityDeniedError(BoxxkiteApiError): pass
class BoxxkiteReadonlyFilesystemError(BoxxkiteApiError): pass
class BoxxkiteSandboxCrashedError(BoxxkiteApiError): pass
class BoxxkiteServiceUnavailableError(BoxxkiteApiError): pass


def api_error_type(code: str, status_code: int) -> type[BoxxkiteApiError]:
    if code.endswith("_limit_reached") or code.endswith("_capacity_reached"):
        return BoxxkiteQuotaExceededError
    return {
        "egress_denied": BoxxkiteEgressDeniedError,
        "capability_denied": BoxxkiteCapabilityDeniedError,
        "command_not_allowed": BoxxkiteCapabilityDeniedError,
        "readonly_filesystem": BoxxkiteReadonlyFilesystemError,
        "sandbox_not_ready": BoxxkiteSandboxNotReadyError,
        "sandbox_crashed": BoxxkiteSandboxCrashedError,
        "service_unavailable": BoxxkiteServiceUnavailableError,
    }.get(code, BoxxkiteServiceUnavailableError if status_code >= 500 else BoxxkiteApiError)
