"""Phase-1 scaffolding for a unified per-session capability policy
(docs/UNIFIED-CAPABILITY-POLICY-SCOPING.md, GitHub issue #155).

STATUS: additive, construction-and-observability only. This module is a
pure data type plus pure construction/validation functions -- no I/O, no
side effects. As of #155's second pass, `usage_policy.py`'s
`create_session` DOES call `build_session_capability_policy` +
`assert_policy_invariants` on every real session, purely to log and
invariant-check the assembled policy -- it does NOT replace any of the
7 existing enforcement call sites (`command_whitelist.py`'s 4 SDK-tool
call sites, plus `routers/sandboxes.py` x2 and `hosted_mcp.py`'s live
`account.custom_allowed_commands` reads). Those still run exactly as
before. This module is still NOT called from `manager.py` or
`sidecar_secrets.py`. Building/wiring it this far does not change any
enforcement behavior.

WHY THIS EXISTS: boxkite has three independently-built allowlist
mechanisms today -- the bash-tool command allowlist
(`command_whitelist.py`), secrets' `allowed_hosts` egress scoping
(`Secret.allowed_hosts`, enforced in `sidecar/sidecar_secrets.py`), and MCP
catalog grants (`mcp_catalog.py`, issue #117). Each has its own shape and
is resolved independently at each of several call sites. This module
defines, in one place, a `SessionCapabilityPolicy` type that can represent
all three grants as they exist *today* -- it does not invent a new
capability model, and it does not attempt to unify how these three are
actually enforced (see the scoping doc's §3 for why that's a separate,
harder, not-yet-scoped problem: the three mechanisms are enforced in three
different processes with different trust boundaries and different
failure semantics).

Read docs/UNIFIED-CAPABILITY-POLICY-SCOPING.md before wiring this into
anything. Enforcement unification requires a maintainer-approved design
doc first, per this project's own security review conventions
(SECURITY.md's "known follow-ups", CLAUDE.md's design-review-before-build
convention already used for #49/#50).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

NetworkGrantKind = Literal["secret", "mcp_connection"]


@dataclass(frozen=True)
class CommandRule:
    """One entry from the existing `allowed_commands` shape
    (`command_whitelist.py`'s `_normalize_rules` input) -- either a bare
    program name or a program name plus regex arg constraints. This
    mirrors that module's accepted shape; it does not reimplement its
    parsing or matching logic."""

    command: str
    args_allow: tuple[str, ...] = ()
    args_deny: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecPolicy:
    """The command-allowlist half of a session's capability policy.

    `rules is None` means "unrestricted" -- the same semantics
    `validate_command_whitelist(command, allowed_commands=None)` already
    has (`command_whitelist.py:385-386`). This is deliberately preserved,
    not normalized away: unlike secrets' `allowed_hosts` (non-nullable by
    design), an absent command allowlist is a real, intentional
    "no constraint" state today.
    """

    rules: tuple[CommandRule, ...] | None = None


@dataclass(frozen=True)
class NetworkGrant:
    """One resolved secret or MCP-connection grant, in the shared
    `{"name", "allowed_hosts"}` shape `usage_policy.py`'s
    `_resolve_secret_grants`/`_resolve_mcp_connection_grants` already
    produce.

    `kind` is preserved rather than erased because, as of this writing,
    secret grants and MCP-connection grants are NOT equivalently
    enforced: secrets have a sidecar-side, per-request, identity-scoped
    check (`sidecar/sidecar_secrets.py`'s `/http-request` handler);
    MCP-connection grants only widen the per-pod NetworkPolicy egress
    allowlist (no MCP-proxy transport exists yet to enforce an
    identity-scoped check against). Collapsing this distinction would
    hide a real, currently-open gap rather than unify it -- see
    docs/UNIFIED-CAPABILITY-POLICY-SCOPING.md §3.
    """

    name: str
    allowed_hosts: tuple[str, ...]
    kind: NetworkGrantKind
    capability_token: str | None = None


@dataclass(frozen=True)
class SessionCapabilityPolicy:
    """A single object representing all three of a session's existing
    grants, assembled from their current independent sources. Additive
    only -- see this module's docstring."""

    account_id: str
    session_id: str
    exec: ExecPolicy = field(default_factory=ExecPolicy)
    network_grants: tuple[NetworkGrant, ...] = ()

    def all_allowed_hosts(self) -> tuple[str, ...]:
        """The union of every grant's `allowed_hosts`, de-duplicated but
        order-preserving. This mirrors what
        `secrets_network_policy.collect_allowed_hosts` already computes
        for the per-pod NetworkPolicy (which unions secrets + MCP grants
        together) -- it does NOT mirror the sidecar's per-request check,
        which deliberately looks at one grant's own `allowed_hosts` in
        isolation, never a union. Do not use this method as a substitute
        for that per-grant check."""
        seen: dict[str, None] = {}
        for grant in self.network_grants:
            for host in grant.allowed_hosts:
                seen.setdefault(host, None)
        return tuple(seen.keys())

    def network_grant_by_name(self, name: str) -> NetworkGrant | None:
        for grant in self.network_grants:
            if grant.name == name:
                return grant
        return None


def command_rules_from_allowed_commands(
    allowed_commands: list | None,
) -> ExecPolicy:
    """Wrap the existing `allowed_commands` shape (whatever
    `command_whitelist.py` already accepts: `None`, bare strings, or
    `{command, args_allow?, args_deny?}` dicts) into an `ExecPolicy`.
    Pure/no-op on `None` or an empty list -- both mean "unrestricted",
    matching `validate_command_whitelist`'s own semantics."""
    if not allowed_commands:
        return ExecPolicy(rules=None)

    rules: list[CommandRule] = []
    for entry in allowed_commands:
        if isinstance(entry, str):
            rules.append(CommandRule(command=entry))
        elif isinstance(entry, dict):
            rules.append(
                CommandRule(
                    command=entry["command"],
                    args_allow=tuple(entry.get("args_allow") or ()),
                    args_deny=tuple(entry.get("args_deny") or ()),
                )
            )
        else:
            raise TypeError(
                f"Unsupported allowed_commands entry type: {type(entry).__name__}"
            )
    return ExecPolicy(rules=tuple(rules))


def network_grant_from_secret(name: str, allowed_hosts: list[str]) -> NetworkGrant:
    """Build a `NetworkGrant` from one entry of the list
    `usage_policy.py`'s `_resolve_secret_grants` already produces --
    `{"name": name, "allowed_hosts": [...]}` -- plus the shared
    `secret_capability_token` issued for the whole session."""
    return NetworkGrant(name=name, allowed_hosts=tuple(allowed_hosts), kind="secret")


def network_grant_from_mcp_connection(name: str, host: str) -> NetworkGrant:
    """Build a `NetworkGrant` from one entry of the list
    `usage_policy.py`'s `_resolve_mcp_connection_grants` already produces
    -- `{"name": name, "allowed_hosts": [host]}`. MCP connections have no
    per-session capability token today (`usage_policy.py`'s own docstring
    on `_resolve_mcp_connection_grants` explains why: no MCP-proxy
    transport exists yet to use one with)."""
    return NetworkGrant(name=name, allowed_hosts=(host,), kind="mcp_connection")


def assert_policy_invariants(policy: SessionCapabilityPolicy) -> None:
    """Raise ValueError if either of #155's two cross-mechanism security
    invariants is violated:

    1. No `NetworkGrant.name` appears under two different `kind`s -- a
       name must resolve to exactly one grant, never let a caller-facing
       name be ambiguous between (e.g.) a secret and an MCP connection.
    2. No `kind="mcp_connection"` grant carries a `capability_token` --
       there is no MCP-proxy transport yet for a token to be presented to
       (issue #116), so one showing up here would mean something built it
       under a mistaken assumption that one exists.

    Pure and side-effect-free, like the rest of this module: callers
    decide what to do with the raised error (log-and-continue vs. hard
    fail) rather than this function deciding for them.
    """
    kind_by_name: dict[str, NetworkGrantKind] = {}
    for grant in policy.network_grants:
        existing_kind = kind_by_name.get(grant.name)
        if existing_kind is not None and existing_kind != grant.kind:
            raise ValueError(
                f"NetworkGrant name {grant.name!r} appears under two different "
                f"kinds: {existing_kind!r} and {grant.kind!r}"
            )
        kind_by_name[grant.name] = grant.kind

        if grant.kind == "mcp_connection" and grant.capability_token is not None:
            raise ValueError(
                f"mcp_connection grant {grant.name!r} carries a capability_token, "
                "but no MCP-proxy transport exists yet to use one with (issue #116)"
            )


def build_session_capability_policy(
    *,
    account_id: str,
    session_id: str,
    allowed_commands: list | None = None,
    secret_grants: list[dict] | None = None,
    mcp_connection_grants: list[dict] | None = None,
    secret_capability_token: str | None = None,
) -> SessionCapabilityPolicy:
    """Assemble a `SessionCapabilityPolicy` from the three sources exactly
    as they exist today:

    - `allowed_commands`: whatever shape `command_whitelist.py` accepts.
    - `secret_grants`: the list `usage_policy.py`'s `_resolve_secret_grants`
      returns -- `[{"name": ..., "allowed_hosts": [...]}, ...]`.
    - `mcp_connection_grants`: the list
      `_resolve_mcp_connection_grants` returns, same shape.

    Pure construction only -- does not call either resolver, does not
    touch a database, and does not validate that these inputs came from a
    real account/session. Callers are responsible for having already
    resolved these three lists (e.g. via `UsagePolicy.create_session`)
    before calling this function.
    """
    exec_policy = command_rules_from_allowed_commands(allowed_commands)

    network_grants: list[NetworkGrant] = []
    for grant in secret_grants or []:
        network_grants.append(
            NetworkGrant(
                name=grant["name"],
                allowed_hosts=tuple(grant["allowed_hosts"]),
                kind="secret",
                capability_token=secret_capability_token,
            )
        )
    for grant in mcp_connection_grants or []:
        network_grants.append(
            NetworkGrant(
                name=grant["name"],
                allowed_hosts=tuple(grant["allowed_hosts"]),
                kind="mcp_connection",
            )
        )

    return SessionCapabilityPolicy(
        account_id=account_id,
        session_id=session_id,
        exec=exec_policy,
        network_grants=tuple(network_grants),
    )
