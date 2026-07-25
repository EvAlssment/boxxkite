"""boxkite control-plane: multi-tenant accounts, API keys, and fair-use-limited
sandbox session management on top of SandboxManager/WarmPoolManager.

This is a separate FastAPI service from `sidecar/main.py` (its own
pyproject/requirements/Dockerfile). It authenticates external callers (email
+ password for a future dashboard, long-lived API keys for programmatic
sandbox management) and then delegates all actual pod lifecycle work to the
existing, already-security-reviewed `boxkite.SandboxManager`. It never talks
to a sandbox pod directly and never weakens sidecar auth, NetworkPolicy, or
pod security context — see SECURITY.md and README "Security" at the repo
root for the isolation model this service builds on top of.

No billing or payment concepts exist anywhere in this package. Usage limits
are configurable fair-use caps (see `config.py`), never priced tiers.
"""

__version__ = "0.1.0"
