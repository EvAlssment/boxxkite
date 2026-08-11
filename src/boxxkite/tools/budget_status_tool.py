"""
Budget Status Tool - remaining sandbox-hours, concurrency, and rate-limit
headroom for the current hosted account (GitHub issue #75).

Unlike every other tool in this package, this one does not go through
SandboxManager at all: SandboxManager has no account/billing concept (see
control-plane/src/control_plane/usage_policy.py's own module docstring --
that's deliberately a control-plane-level policy layer, not something
SandboxManager itself knows about). Fair-use budget only exists for hosted
accounts, so this tool calls the hosted control-plane's GET /v1/usage
directly with the account's own API key, the same way `boxxkite whoami`
does (see cli/cmd_whoami.py).

For a self-hosted deployment with no hosted API key configured, there is no
account/budget concept to report on at all -- the tool says so plainly
rather than fabricating a number, matching this package's general
"degrade honestly, don't fake it" posture (see docs/handoff-adapters.md).
"""

import logging
from typing import Optional

import httpx

from .types import ToolSpec

logger = logging.getLogger(__name__)

DEFAULT_HOSTED_BASE_URL = "https://api.boxxkite.com"
DEFAULT_TIMEOUT = 10.0

BUDGET_STATUS_DESCRIPTION = """Check the current hosted account's remaining sandbox-hours, concurrent-sandbox headroom, and exec/file-op rate-limit headroom.

Use this before starting a long-running or exec-heavy task to confirm
there's enough budget left this month, or to understand why a previous
call may have been rejected with a usage/rate-limit error.

Only meaningful for hosted-mode accounts -- a self-hosted deployment with
no hosted API key configured has no account/budget concept, and this tool
says so rather than fabricating a number.
"""

BUDGET_STATUS_PARAMETERS = {
    "type": "object",
    "properties": {},
    "required": [],
}


def _format_budget_status(usage: dict) -> str:
    lines = [
        f"Monthly sandbox-hours: {usage['monthly_sandbox_hours_used']:.2f} / "
        f"{usage['monthly_sandbox_hours_limit']:.2f} used",
        f"Concurrent sandboxes: {usage['concurrent_sandboxes']} / {usage['concurrent_sandboxes_limit']}",
        f"Rate-limit headroom (exec/file-ops): {usage['sandbox_ops_rate_limit_remaining']} / "
        f"{usage['sandbox_ops_rate_limit']} remaining this minute",
    ]
    return "\n".join(lines)


def create_budget_status_tool_spec(
    hosted_api_key: Optional[str] = None,
    hosted_base_url: str = DEFAULT_HOSTED_BASE_URL,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for budget_status.

    Args:
        hosted_api_key: The hosted control-plane API key to check budget
            for. When None, the tool's handler returns a message saying
            budget status isn't available, rather than raising -- so
            wiring this tool up unconditionally in a self-hosted deployment
            degrades gracefully instead of breaking tool discovery.
        hosted_base_url: Hosted control-plane base URL. Defaults to the
            public hosted SaaS; override for a self-hosted deployment that
            still runs its own control-plane.

    Returns:
        ToolSpec with a plain async handler() -> str
    """

    async def budget_status() -> str:
        if not hosted_api_key:
            return (
                "Budget status isn't available: no hosted API key is configured. "
                "This is expected for a self-hosted deployment with no hosted "
                "control-plane account -- there's no monthly-hours/concurrency "
                "cap to report on."
            )

        logger.info("[budget_status] Checking usage")
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.get(
                    f"{hosted_base_url.rstrip('/')}/v1/usage",
                    headers={"Authorization": f"Bearer {hosted_api_key}"},
                )
        except Exception as e:
            logger.error(f"[budget_status] Error: {e}", exc_info=True)
            return f"Error checking budget status: {str(e)}"

        if resp.status_code != 200:
            return f"Error checking budget status: HTTP {resp.status_code}"

        return _format_budget_status(resp.json())

    return ToolSpec(
        name="budget_status",
        description=BUDGET_STATUS_DESCRIPTION,
        parameters=BUDGET_STATUS_PARAMETERS,
        handler=budget_status,
    )
