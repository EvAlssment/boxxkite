"""Exception types raised by BoxkiteClient/AsyncBoxkiteClient."""

from __future__ import annotations


class BoxkiteError(Exception):
    """Base class for every error this SDK raises."""


class BoxkiteConnectionError(BoxkiteError):
    """The control-plane could not be reached at all (DNS, TLS, timeout)."""


class BoxkiteApiError(BoxkiteError):
    """The control-plane responded with an error envelope
    (`{"error": {code, message}}`), e.g. a 404, 401, or 429."""

    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{message} [{code}] (HTTP {status_code})")
