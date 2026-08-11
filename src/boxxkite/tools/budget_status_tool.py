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
    """Formats whatever fields are actually present.

    Uses .get(), not usage[...]: this package ships on PyPI independently
    of the hosted control-plane deployment, so a caller could be talking
    to a control-plane that predates sandbox_ops_rate_limit* (added after
    the rest of this response shape) or, in principle, an even older one
    missing other fields. Missing pieces are omitted from the summary
    rather than raising -- consistent with this tool degrading to a
    plain message instead of erroring when hosted_api_key itself is
    unset.
    """
    lines = []
    hours_used = usage.get("monthly_sandbox_hours_used")
    hours_limit = usage.get("monthly_sandbox_hours_limit")
    if hours_used is not None and hours_limit is not None:
        lines.append(f"Monthly sandbox-hours: {hours_used:.2f} / {hours_limit:.2f} used")

    concurrent = usage.get("concurrent_sandboxes")
    concurrent_limit = usage.get("concurrent_sandboxes_limit")
    if concurrent is not None and concurrent_limit is not None:
        lines.append(f"Concurrent sandboxes: {concurrent} / {concurrent_limit}")

    rate_remaining = usage.get("sandbox_ops_rate_limit_remaining")
    rate_limit = usage.get("sandbox_ops_rate_limit")
    if rate_remaining is not None and rate_limit is not None:
        lines.append(
            f"Rate-limit headroom (exec/file-ops): {rate_remaining} / {rate_limit} remaining this minute"
        )

    if not lines:
        return "Budget status response didn't contain any recognized usage fields."
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
            if resp.status_code != 200:
                return f"Error checking budget status: HTTP {resp.status_code}"
            return _format_budget_status(resp.json())
        except Exception as e:
            # Covers the request itself, a non-JSON body, and anything
            # _format_budget_status doesn't already tolerate -- this tool
            # promises to degrade to a message, never raise.
            logger.error(f"[budget_status] Error: {e}", exc_info=True)
            return f"Error checking budget status: {str(e)}"

    return ToolSpec(
        name="budget_status",
        description=BUDGET_STATUS_DESCRIPTION,
        parameters=BUDGET_STATUS_PARAMETERS,
        handler=budget_status,
    )
