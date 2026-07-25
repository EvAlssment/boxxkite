"""A single exception type for all handled API errors, mapped to a
consistent JSON envelope: {"error": {"code", "message", "details"?}}.

Error messages are validated at review time to never mention a dollar
amount or a plan/tier name (see LimitExceededError) — usage limits are
communicated purely as configurable fair-use caps.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


class LimitExceededError(ApiError):
    """A fair-use limit was hit. Always a 429; message never mentions
    money, dollar amounts, or plan/tier names — only the limit and unit."""

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(429, code, message, details)


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    body: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details is not None:
        body["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content={"error": body})
