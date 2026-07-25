"""Per-session NetworkPolicy scoped to a session's granted secrets-broker
destination hosts (docs/SECRETS-DESIGN.md, SECURITY.md's "secrets broker
egress widening" follow-up -- GitHub issue #74).

Background: `/http-request` needs the sidecar to reach whatever HTTPS
destinations that session's granted secrets' `allowed_hosts` name -- a set
defined per-secret, per-tenant, at session-create time, not something
`deploy/network-policy.yaml` (a static, operator-authored manifest) can
express. Previously the only documented option was widening that manifest's
egress rule to `0.0.0.0/0:443`, which made "the secrets broker is enabled"
and "the sidecar has unrestricted internet egress" equivalent for every
session, regardless of which secrets it was actually granted.

This module builds one dynamically-provisioned `NetworkPolicy` object per
session, scoped by `podSelector` to exactly that session's own pod (matching
its `app: sandbox` + `session-id: <label>` labels --
`src/boxkite/manager.py`'s `_identity_labels_and_annotations` is the only
place that label is ever set, and it is unique to one pod at a time), with
one egress rule per distinct resolved IP address behind that session's
granted secrets' allowed hosts. `src/boxkite/manager.py` creates this
alongside the pod at session-configure time and deletes it at session-end
(recycle-to-warm-pool or hard delete) -- see `_sync_secrets_egress_network_policy`
/ `_delete_secrets_egress_network_policy` there.

Two disclosed, load-bearing limitations, not silently assumed away:

1. **NetworkPolicy objects with overlapping podSelectors are additive
   (unioned), never intersected.** If an operator has ALSO widened
   `deploy/network-policy.yaml`'s static egress rule (the old workaround
   this feature replaces), this per-session policy adds nothing -- the
   union of "narrow list" OR "0.0.0.0/0" is still "0.0.0.0/0". This
   mechanism only provides real scoping when the static manifest is left at
   its storage-only default; see the updated guidance in
   `deploy/network-policy.yaml` and `docs/SECRETS-DESIGN.md`.
2. **Standard `networking.k8s.io/v1` NetworkPolicy has no hostname/FQDN
   egress primitive** (unlike Cilium's `toFQDNs` or Calico's DNS policy
   extension) -- `ipBlock` is the only portable option, so each allowed
   host is resolved via DNS once, at provisioning time, and pinned by IP.
   If that hostname's DNS record changes afterward, this NetworkPolicy does
   NOT track it until the next session re-provisions it. This is the same
   class of residual risk already disclosed for
   `control_plane.host_safety`'s creation-time-only check --
   `sidecar/main.py`'s own per-request, DNS-rebinding-safe re-resolution
   (`_resolve_and_validate_destination`) remains the actual authoritative
   security boundary. This NetworkPolicy is a coarser, network-layer
   narrowing on top of that, not a replacement for it.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Callable, Optional

from kubernetes_asyncio import client

logger = logging.getLogger(__name__)

SECRETS_EGRESS_POLICY_NAME_PREFIX = "sandbox-secrets-egress-"
SECRETS_EGRESS_POLICY_PORT = 443
_K8S_NAME_MAX_LENGTH = 253

# Mirrors sidecar/main.py's _is_disallowed_destination_ip and
# control-plane's host_safety.py -- same CGNAT/NAT64-aware deny-list,
# duplicated rather than imported because this module lives in the
# separately-packaged src/boxkite core, not the sidecar or control-plane
# services (the same cross-service boundary every other duplicated security
# check in this codebase crosses; see SECURITY.md's disclosure of the
# original gap this closed). Used here purely as defense in depth so a
# secret's allowed_hosts can never result in an explicit NetworkPolicy ALLOW
# rule naming a private/link-local/CGNAT/metadata address -- the sidecar's
# own per-request check remains the authoritative enforcement point.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(addr: "ipaddress.IPv6Address") -> Optional["ipaddress.IPv4Address"]:
    """Return the IPv4 address embedded in an IPv4-mapped (::ffff:0:0/96) or
    NAT64 (64:ff9b::/96) IPv6 address, else None."""
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr in _NAT64_NETWORK:
        return ipaddress.IPv4Address(addr.packed[12:])
    return None


def _is_disallowed_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True

    if isinstance(addr, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(addr)
        if embedded is not None:
            addr = embedded

    return bool(
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_unspecified
        or addr.is_reserved
        or addr.is_multicast
        or (isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_NETWORK)
    )


def secrets_egress_policy_name(pod_name: str) -> str:
    """Deterministic per-pod NetworkPolicy name -- one object per pod,
    looked up/replaced/deleted by name alone, same 1:1 lifecycle pattern as
    `sidecar_auth_secret_name(pod_name)`. Truncated defensively at K8s's
    253-char object-name limit; no pod name in this codebase gets remotely
    close, but silently raising a 422 here would be a worse failure mode
    than a truncated (still-unique-in-practice) name."""
    return f"{SECRETS_EGRESS_POLICY_NAME_PREFIX}{pod_name}"[:_K8S_NAME_MAX_LENGTH]


def collect_allowed_hosts(secret_grants: Optional[list[dict]]) -> list[str]:
    """Union of every granted secret's allowed_hosts for this session,
    deduplicated and sorted so the generated policy is deterministic across
    repeated calls for the same grants (avoids spurious
    replace_namespaced_network_policy churn and makes this trivial to
    test)."""
    hosts: set[str] = set()
    for grant in secret_grants or []:
        for host in grant.get("allowed_hosts") or []:
            normalized = (host or "").strip().lower()
            if normalized:
                hosts.add(normalized)
    return sorted(hosts)


HostResolver = Callable[[str], list[str]]


def default_resolve_host_ips(host: str) -> list[str]:
    """Best-effort DNS resolution of an allowed host to its current IP
    address(es), for the ipBlock-based egress rule this module builds. See
    this module's docstring (limitation 2) for why this is a one-time,
    provisioning-time resolution rather than a live-tracking mechanism.

    Returns [] on any resolution failure (unresolvable host, DNS outage at
    provisioning time) rather than raising -- a session whose secret host
    can't be pre-resolved should still have session creation succeed (the
    sidecar's own allowed_hosts check still gates the actual
    `/http-request` call; this is a supplementary network-layer allowlist,
    not the only enforcement point), just without that host's egress rule
    until the next resolution succeeds.
    """
    try:
        infos = socket.getaddrinfo(host, SECRETS_EGRESS_POLICY_PORT, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as e:
        logger.warning(
            "[secrets_network_policy] Could not resolve secrets-broker host %r: %s",
            host, e,
        )
        return []

    resolved: set[str] = set()
    for _family, _type, _proto, _canonname, sockaddr in infos:
        resolved.add(sockaddr[0])
    return sorted(resolved)


def _ip_block_cidr(ip_str: str) -> str:
    return f"{ip_str}/128" if ":" in ip_str else f"{ip_str}/32"


def build_secrets_egress_network_policy(
    *,
    pod_name: str,
    namespace: str,
    session_label_value: str,
    secret_grants: Optional[list[dict]],
    resolve_host_ips: Optional[HostResolver] = None,
) -> Optional["client.V1NetworkPolicy"]:
    """Build the per-session NetworkPolicy scoped to exactly this session's
    granted secrets' destination hosts.

    Returns None if the session was granted no secrets, or none of its
    allowed hosts could be resolved to a permitted IP -- callers must treat
    None as "no additional egress rule needed for this session" (delete any
    existing one instead), not as an error.

    `podSelector` matches this pod's own `app: sandbox` + `session-id:
    <session_label_value>` labels
    (`SandboxManager._identity_labels_and_annotations`) -- that label value
    is unique to one pod at a time (cleared to None on recycle-to-warm), so
    this selector cannot match a different tenant's pod even though
    NetworkPolicy selectors are label-based rather than pod-identity-based.
    """
    hosts = collect_allowed_hosts(secret_grants)
    if not hosts:
        return None

    # Resolved lazily (module attribute lookup, not a bound default
    # parameter) so callers -- and tests -- can monkeypatch
    # `default_resolve_host_ips` at the module level and have it apply even
    # when `resolve_host_ips` is omitted entirely.
    resolver = resolve_host_ips or default_resolve_host_ips

    egress_rules: list["client.V1NetworkPolicyEgressRule"] = []
    for host in hosts:
        ips = [ip for ip in resolver(host) if not _is_disallowed_ip(ip)]
        if not ips:
            logger.warning(
                "[secrets_network_policy] No permitted resolved address for "
                "secrets-broker host %r (pod %s) -- omitting it from this "
                "session's egress rule. The sidecar's own per-request "
                "allowed_hosts check still governs actual http_request "
                "calls; this only affects the supplementary network-layer "
                "allowlist.",
                host, pod_name,
            )
            continue
        egress_rules.append(
            client.V1NetworkPolicyEgressRule(
                to=[
                    client.V1NetworkPolicyPeer(ip_block=client.V1IPBlock(cidr=_ip_block_cidr(ip)))
                    for ip in ips
                ],
                ports=[
                    client.V1NetworkPolicyPort(
                        protocol="TCP", port=SECRETS_EGRESS_POLICY_PORT
                    )
                ],
            )
        )

    if not egress_rules:
        return None

    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name=secrets_egress_policy_name(pod_name),
            namespace=namespace,
            labels={
                "app": "sandbox",
                "sandbox.boxkite.dev/secrets-egress": "true",
            },
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(
                match_labels={"app": "sandbox", "session-id": session_label_value}
            ),
            policy_types=["Egress"],
            egress=egress_rules,
        ),
    )
