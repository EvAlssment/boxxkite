"""Per-session NetworkPolicy scoped to browser-enabled sessions
(docs/BROWSER-EXEC-DESIGN.md §3, GitHub issue #119).

Background: the headless-browser tool (sidecar/sidecar_browser.py) needs
the browser's driver subprocess to have REAL, non-enumerable egress -- the
entire point of `browser_navigate` is going to a URL nobody enumerated in
advance, so (unlike `src/boxkite/secrets_network_policy.py`'s per-secret
`allowed_hosts`) there is no finite host list to pin by resolved IP here.
This is a genuinely different, and genuinely riskier, trust boundary than
every other opt-in tool this repo ships: the design doc's own framing is
"broad HTTPS/DNS egress, minus a short explicit denylist" -- a real,
disclosed widening, not a narrowed allowlist dressed up as one.

This module builds one dynamically-provisioned `NetworkPolicy` object per
session (mirroring secrets_network_policy.py's exact pattern: one object
per pod, scoped by `podSelector` to exactly that session's own pod, built
fresh at session-configure time, deleted at session-end), except the
egress rule itself allows broad public HTTPS instead of a per-secret
allowlist.

THE SECURITY-CRITICAL PART (docs/BROWSER-EXEC-DESIGN.md §3, point 2):
allowing broad egress is not itself the risk -- failing to unconditionally
carve out link-local (especially the cloud metadata endpoint,
169.254.169.254), RFC1918, and loopback ranges FROM that broad allow is.
Standard `networking.k8s.io/v1` NetworkPolicy has no "deny" rule type at
all (every NetworkPolicy object only ever grants additional, additive
permission -- see this module's own limitation-1 disclosure below); the
ONLY portable mechanism for "allow broad X, except these ranges" is
`V1IPBlock`'s own `except` field on a single peer, which is evaluated as a
genuine set-subtraction from that peer's `cidr` -- an address inside
`except` is never reachable via that rule, full stop, regardless of how
broad `cidr` is. That is what "unconditional, higher-priority deny ahead
of the broad allow" cashes out to in a real, vanilla NetworkPolicy object:
not a second, separately-ordered rule (rule order across a NetworkPolicy's
own `egress` list has no such priority semantics either -- rules are
OR'd), but the SAME rule's own `except` list, which this module always
builds before ever constructing the surrounding "allow" peer (see
`_DENIED_IPV4_CIDRS`/`_DENIED_IPV6_CIDRS` and `build_browser_egress_network_policy`
below) so that carve-out can never be accidentally omitted or overridden
by a broader peer added later.

Two disclosed, load-bearing limitations, not silently assumed away (same
posture secrets_network_policy.py's own docstring takes):

1. **NetworkPolicy objects with overlapping podSelectors are additive
   (unioned), never intersected.** If an operator has ALSO widened
   `deploy/network-policy.yaml`'s static egress rule to `0.0.0.0/0:443`,
   this per-session policy's own `except` carve-out provides NO protection
   -- the union of "0.0.0.0/0 except link-local/RFC1918/loopback" OR
   "0.0.0.0/0" is still unconditionally "0.0.0.0/0". This mechanism only
   provides real protection when the static manifest is left at its
   storage-only default egress rule.
2. **NetworkPolicy enforcement is CNI-dependent, and `ipBlock.except` in
   particular is a real-world CNI-conformance risk, not just a boilerplate
   caveat** -- some CNI/NetworkPolicy implementations have historically had
   bugs or partial support for `except` specifically (as opposed to a bare
   `cidr` with no `except`). This module cannot verify, from inside a
   Python process building an API object, that the cluster's actual CNI
   enforces `except` correctly. Verify directly against a real cluster
   before trusting this for a browser-enabled session, the same way
   `deploy/network-policy.yaml`'s own header instructs for its IMDS-via-
   omission claim: `curl -m 3 http://169.254.169.254/` from inside a
   browser-enabled pod must time out, not just "the generated policy object
   looks correct in a unit test."
"""

from __future__ import annotations

import logging
from typing import Optional

from kubernetes_asyncio import client

logger = logging.getLogger(__name__)

BROWSER_EGRESS_POLICY_NAME_PREFIX = "sandbox-browser-egress-"
BROWSER_EGRESS_HTTPS_PORT = 443
BROWSER_EGRESS_DNS_PORT = 53
_K8S_NAME_MAX_LENGTH = 253

# Deliberately ordered (see this module's docstring): the single most
# attractive SSRF target in a browser-driven session -- the cloud instance
# metadata endpoint -- is named explicitly and FIRST, ahead of the broader
# range it's already a subset of. Listing it separately is redundant for
# `except`'s actual set-subtraction semantics (a /16 carve-out already
# excludes this /32), but it is not redundant for a HUMAN reviewing this
# policy or this module's own tests: it makes "IMDS is blocked" verifiable
# by name, not merely implied by a broader range a reviewer has to expand
# by hand to confirm.
_DENIED_IPV4_CIDRS: tuple[str, ...] = (
    "169.254.169.254/32",  # cloud IMDS -- AWS/GCP/Azure/DigitalOcean, see design doc §3
    "169.254.0.0/16",  # link-local (RFC 3927), IMDS's broader range
    "127.0.0.0/8",  # loopback
    "10.0.0.0/8",  # RFC1918 private
    "172.16.0.0/12",  # RFC1918 private
    "192.168.0.0/16",  # RFC1918 private
)

# IPv6 equivalents, for dual-stack clusters -- a browser can resolve/connect
# over IPv6 just as easily as IPv4, and there is no reason the IMDS/private/
# loopback carve-out should only apply to one address family.
_DENIED_IPV6_CIDRS: tuple[str, ...] = (
    "::1/128",  # loopback
    "fe80::/10",  # link-local
    "fc00::/7",  # unique local (RFC4193) -- the IPv6 analog of RFC1918
)


def denied_ipv4_cidrs() -> tuple[str, ...]:
    """The ordered IPv4 deny list every broad-allow egress rule this module
    builds carves out via `V1IPBlock.except`. Exposed as its own function
    (rather than only as the private module constant) so tests -- and any
    future caller needing to reason about "what does this policy forbid"
    without constructing a full NetworkPolicy object -- have a stable,
    public entry point."""
    return _DENIED_IPV4_CIDRS


def denied_ipv6_cidrs() -> tuple[str, ...]:
    """IPv6 counterpart to denied_ipv4_cidrs()."""
    return _DENIED_IPV6_CIDRS


def browser_egress_policy_name(pod_name: str) -> str:
    """Deterministic per-pod NetworkPolicy name -- one object per pod,
    looked up/replaced/deleted by name alone, same 1:1 lifecycle pattern as
    `secrets_egress_policy_name(pod_name)`. Truncated defensively at K8s's
    253-char object-name limit, same rationale as that function."""
    return f"{BROWSER_EGRESS_POLICY_NAME_PREFIX}{pod_name}"[:_K8S_NAME_MAX_LENGTH]


def _broad_allow_minus_denied_peers() -> list["client.V1NetworkPolicyPeer"]:
    """The core security-critical construction: one `V1NetworkPolicyPeer`
    per IP family, each an `ipBlock` whose `cidr` is "the whole address
    family" and whose `except` is this module's own deny list -- built
    from that deny list FIRST (see this module's docstring) so the
    resulting peer can never represent "allow everything" without also
    representing "except the denied ranges" in the exact same object.
    """
    return [
        client.V1NetworkPolicyPeer(
            ip_block=client.V1IPBlock(cidr="0.0.0.0/0", _except=list(_DENIED_IPV4_CIDRS))
        ),
        client.V1NetworkPolicyPeer(
            ip_block=client.V1IPBlock(cidr="::/0", _except=list(_DENIED_IPV6_CIDRS))
        ),
    ]


def build_browser_egress_network_policy(
    *,
    pod_name: str,
    namespace: str,
    session_label_value: str,
    browser_enabled: bool,
) -> Optional["client.V1NetworkPolicy"]:
    """Build the per-session NetworkPolicy scoped to exactly this session's
    pod, granting the broad-but-carved-out egress a browser-enabled
    session needs.

    Returns None when `browser_enabled` is False -- callers must treat
    None as "no browser egress rule needed for this session" (delete any
    existing one instead, e.g. because a recycled pod's PREVIOUS tenant
    had the browser tool enabled and this one doesn't), not as an error.

    `podSelector` matches this pod's own `app: sandbox` + `session-id:
    <session_label_value>` labels (`SandboxManager._identity_labels_and_annotations`),
    exactly the same selector shape `build_secrets_egress_network_policy`
    uses and for the same reason: that label value is unique to one pod at
    a time (cleared to None on recycle-to-warm), so this selector cannot
    match a different tenant's pod even though NetworkPolicy selectors are
    label-based rather than pod-identity-based.

    DNS (port 53) is granted via a `podSelector`/`namespaceSelector` peer
    matching `k8s-app: kube-dns` -- the SAME shape
    `deploy/network-policy.yaml`'s own static DNS rule already uses, and
    deliberately NOT an `ipBlock` peer: CoreDNS's ClusterIP commonly lives
    inside the cluster's Service CIDR, which is itself frequently a
    RFC1918 range (e.g. 10.96.0.0/12) -- if DNS were folded into the same
    broad-ipBlock-minus-RFC1918 rule HTTPS uses below, denying RFC1918
    would silently break the session's own DNS resolution. Keeping DNS on
    a pod/namespace selector sidesteps that conflict entirely: selector-based
    peers are matched by label, never by the peer pod's IP falling inside
    an excluded CIDR.
    """
    if not browser_enabled:
        return None

    dns_peer = client.V1NetworkPolicyPeer(
        namespace_selector=client.V1LabelSelector(),
        pod_selector=client.V1LabelSelector(match_labels={"k8s-app": "kube-dns"}),
    )
    dns_rule = client.V1NetworkPolicyEgressRule(
        to=[dns_peer],
        ports=[
            client.V1NetworkPolicyPort(protocol="UDP", port=BROWSER_EGRESS_DNS_PORT),
            client.V1NetworkPolicyPort(protocol="TCP", port=BROWSER_EGRESS_DNS_PORT),
        ],
    )

    https_rule = client.V1NetworkPolicyEgressRule(
        to=_broad_allow_minus_denied_peers(),
        ports=[client.V1NetworkPolicyPort(protocol="TCP", port=BROWSER_EGRESS_HTTPS_PORT)],
    )

    return client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name=browser_egress_policy_name(pod_name),
            namespace=namespace,
            labels={
                "app": "sandbox",
                "sandbox.boxkite.dev/browser-egress": "true",
            },
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(
                match_labels={"app": "sandbox", "session-id": session_label_value}
            ),
            policy_types=["Egress"],
            egress=[dns_rule, https_rule],
        ),
    )
