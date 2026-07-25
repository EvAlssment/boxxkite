"""boxkite_client — a Python client for a hosted boxkite control-plane.

    from boxkite_client import BoxkiteClient

    client = BoxkiteClient(base_url="https://your-control-plane", api_key="bxk_live_...")
    with client.sandbox(label="demo") as sb:
        result = sb.exec("echo hello")
        print(result["stdout"])

See README.md for the async client and LangChain tool factory.
"""

from .client import (
    AsyncBoxkiteClient,
    AsyncSandboxSession,
    BoxkiteClient,
    RetryConfig,
    SandboxSession,
)
from .exceptions import BoxkiteApiError, BoxkiteConnectionError, BoxkiteError

__all__ = [
    "AsyncBoxkiteClient",
    "AsyncSandboxSession",
    "BoxkiteApiError",
    "BoxkiteClient",
    "BoxkiteConnectionError",
    "BoxkiteError",
    "RetryConfig",
    "SandboxSession",
]
