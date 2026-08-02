"""Per-source-host concurrent connection limiting.

The bastion has no auth-attempt counter of its own to rate-limit (the only
auth method is a single password check against control-plane, see
`bridge.BastionSSHServer.validate_password`), so the resource-exhaustion
vector this guards against is many concurrently *open* TCP connections from
one source holding a slot through the login-timeout window rather than many
failed attempts on one connection. Capping concurrent connections per
source host bounds that regardless of whether each connection ever attempts
auth at all.
"""

from __future__ import annotations


class PerHostConnectionLimiter:
    """Not thread-safe -- only meant to be used from the single asyncio
    event loop `asyncssh.create_server` runs its callbacks on."""

    def __init__(self, max_connections_per_host: int) -> None:
        self._max_connections_per_host = max_connections_per_host
        self._counts: dict[str, int] = {}

    def try_acquire(self, host: str) -> bool:
        current = self._counts.get(host, 0)
        if current >= self._max_connections_per_host:
            return False
        self._counts[host] = current + 1
        return True

    def release(self, host: str) -> None:
        current = self._counts.get(host, 0)
        if current <= 1:
            self._counts.pop(host, None)
        else:
            self._counts[host] = current - 1
