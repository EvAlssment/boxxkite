"""boxxkite_client — a Python client for a hosted boxxkite control-plane.

    from boxxkite_client import BoxxkiteClient

    client = BoxxkiteClient(base_url="https://your-control-plane", api_key="bxk_live_...")
    with client.sandbox(label="demo") as sb:
        result = sb.exec("echo hello")
        print(result["stdout"])

See README.md for the async client and LangChain tool factory.
"""

from .client import (
    AsyncBoxxkiteClient,
    AsyncSandboxSession,
    BoxxkiteClient,
    RetryConfig,
    SandboxSession,
)
from .exceptions import (
    BoxxkiteApiError, BoxxkiteCapabilityDeniedError, BoxxkiteConnectionError,
    BoxxkiteEgressDeniedError, BoxxkiteError, BoxxkiteQuotaExceededError,
    BoxxkiteReadonlyFilesystemError, BoxxkiteSandboxCrashedError,
    BoxxkiteSandboxNotReadyError, BoxxkiteServiceUnavailableError,
)

__all__ = [
    "AsyncBoxxkiteClient",
    "AsyncSandboxSession",
    "BoxxkiteApiError",
    "BoxxkiteClient",
    "BoxxkiteConnectionError",
    "BoxxkiteCapabilityDeniedError",
    "BoxxkiteEgressDeniedError",
    "BoxxkiteError",
    "BoxxkiteQuotaExceededError",
    "BoxxkiteReadonlyFilesystemError",
    "BoxxkiteSandboxCrashedError",
    "BoxxkiteSandboxNotReadyError",
    "BoxxkiteServiceUnavailableError",
    "RetryConfig",
    "SandboxSession",
]
