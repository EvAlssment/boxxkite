"""Tests for src/boxkite/browser_network_policy.py -- the per-session
NetworkPolicy scoping mechanism for browser-enabled sessions
(docs/BROWSER-EXEC-DESIGN.md §3, GitHub issue #119).

The most important coverage here is NOT "does the generated policy contain
a deny CIDR and an allow CIDR somewhere" -- it's whether the deny actually
WINS over the broad allow for concrete addresses, the same way a real
CNI's NetworkPolicy engine would evaluate `ipBlock.cidr` + `ipBlock.except`
together. `_simulate_ip_block_peer_permits` below is a small, honest
reimplementation of that exact evaluation rule (an address is reachable
via one ipBlock peer iff it's inside `cidr` AND not inside any `except`
range) -- see test_https_rule_denies_imds_and_private_ranges_but_allows_public_addresses,
which is the test that actually exercises deny-before-allow precedence,
not just presence of both rules.
"""

from __future__ import annotations

import ipaddress

import pytest
from kubernetes_asyncio import client

from boxkite import browser_network_policy as bnp

pytestmark = pytest.mark.pr


def _simulate_ip_block_peer_permits(peer: "client.V1NetworkPolicyPeer", address: str) -> bool:
    """Reimplements real NetworkPolicy `ipBlock` peer evaluation: an address
    is permitted by this peer iff it falls inside `cidr` AND does not fall
    inside any of `except`'s ranges. This is the actual enforcement
    semantics a real CNI applies -- not merely "both a cidr and an except
    list are present somewhere in the object" (a check that would pass even
    if they were unrelated/unwired)."""
    if peer.ip_block is None:
        return False
    addr = ipaddress.ip_address(address)
    try:
        network = ipaddress.ip_network(peer.ip_block.cidr)
    except ValueError:
        return False
    if addr not in network:
        return False
    for excluded_cidr in (peer.ip_block._except or []):
        if addr in ipaddress.ip_network(excluded_cidr):
            return False
    return True


def _https_rule(policy: "client.V1NetworkPolicy"):
    """The broad-HTTPS egress rule -- identified by its 443 port, not by
    list position, so a reordering of `egress` doesn't silently break these
    tests."""
    for rule in policy.spec.egress:
        ports = {(p.protocol, p.port) for p in (rule.ports or [])}
        if ("TCP", 443) in ports:
            return rule
    raise AssertionError("No egress rule grants TCP/443")


def _dns_rule(policy: "client.V1NetworkPolicy"):
    for rule in policy.spec.egress:
        ports = {(p.protocol, p.port) for p in (rule.ports or [])}
        if ("UDP", 53) in ports:
            return rule
    raise AssertionError("No egress rule grants UDP/53")


def _build_policy(**overrides):
    kwargs = dict(
        pod_name="sandbox-abc123-standalone-9f2e",
        namespace="default",
        session_label_value="sess-1",
        browser_enabled=True,
    )
    kwargs.update(overrides)
    return bnp.build_browser_egress_network_policy(**kwargs)


# ---------------------------------------------------------------------------
# Naming / gating
# ---------------------------------------------------------------------------

def test_browser_egress_policy_name_is_deterministic_and_prefixed():
    name = bnp.browser_egress_policy_name("sandbox-abc123-standalone-9f2e")
    assert name == "sandbox-browser-egress-sandbox-abc123-standalone-9f2e"


def test_browser_egress_policy_name_truncates_at_k8s_limit():
    long_pod_name = "sandbox-" + ("x" * 300)
    name = bnp.browser_egress_policy_name(long_pod_name)
    assert len(name) == 253


def test_build_policy_returns_none_when_browser_disabled():
    assert _build_policy(browser_enabled=False) is None


def test_build_policy_returns_a_policy_when_browser_enabled():
    assert _build_policy(browser_enabled=True) is not None


# ---------------------------------------------------------------------------
# podSelector -- must match exactly this session's pod, same shape
# secrets_network_policy.py's own selector uses.
# ---------------------------------------------------------------------------

def test_build_policy_pod_selector_matches_exact_session_pod():
    policy = _build_policy(session_label_value="sess-xyz")
    assert policy.spec.pod_selector.match_labels == {
        "app": "sandbox",
        "session-id": "sess-xyz",
    }


def test_build_policy_name_and_namespace():
    policy = _build_policy(pod_name="sandbox-foo", namespace="my-ns")
    assert policy.metadata.name == "sandbox-browser-egress-sandbox-foo"
    assert policy.metadata.namespace == "my-ns"
    assert policy.spec.policy_types == ["Egress"]


# ---------------------------------------------------------------------------
# DNS rule -- selector-based, NOT ipBlock-based (see module docstring: an
# ipBlock-based DNS rule sharing the RFC1918 carve-out would break DNS
# resolution itself, since CoreDNS's ClusterIP commonly lives in RFC1918
# space).
# ---------------------------------------------------------------------------

def test_dns_rule_targets_kube_dns_by_selector_not_ip_block():
    policy = _build_policy()
    rule = _dns_rule(policy)
    assert len(rule.to) == 1
    peer = rule.to[0]
    assert peer.ip_block is None
    assert peer.pod_selector.match_labels == {"k8s-app": "kube-dns"}
    ports = {(p.protocol, p.port) for p in rule.ports}
    assert ports == {("UDP", 53), ("TCP", 53)}


# ---------------------------------------------------------------------------
# HTTPS rule -- the security-critical part.
# ---------------------------------------------------------------------------

def test_https_rule_grants_only_tcp_443():
    policy = _build_policy()
    rule = _https_rule(policy)
    assert len(rule.ports) == 1
    assert rule.ports[0].protocol == "TCP"
    assert rule.ports[0].port == 443


def test_https_rule_allows_the_whole_address_space_before_carve_outs():
    """The broad-allow half of "broad allow minus a carve-out" -- cidr is
    genuinely 0.0.0.0/0 / ::/0, not some pre-narrowed range. The carve-out
    (verified separately below) is what does the actual narrowing."""
    policy = _build_policy()
    rule = _https_rule(policy)
    cidrs = {peer.ip_block.cidr for peer in rule.to if peer.ip_block}
    assert cidrs == {"0.0.0.0/0", "::/0"}


def test_https_rule_except_lists_match_the_published_deny_lists():
    policy = _build_policy()
    rule = _https_rule(policy)
    ipv4_peer = next(p for p in rule.to if p.ip_block and p.ip_block.cidr == "0.0.0.0/0")
    ipv6_peer = next(p for p in rule.to if p.ip_block and p.ip_block.cidr == "::/0")
    assert list(ipv4_peer.ip_block._except) == list(bnp.denied_ipv4_cidrs())
    assert list(ipv6_peer.ip_block._except) == list(bnp.denied_ipv6_cidrs())


def test_denied_ipv4_cidrs_orders_the_imds_specific_address_before_broader_ranges():
    """docs/BROWSER-EXEC-DESIGN.md §3 singles out 169.254.169.254
    ("especially") ahead of the general link-local/RFC1918/loopback carve-
    out -- verify the published deny list actually reflects that emphasis
    (the single most attractive SSRF target listed first, by name, not
    merely implied by a broader range a reviewer has to expand by hand),
    not just that it's present somewhere in the list."""
    denied = bnp.denied_ipv4_cidrs()
    assert denied[0] == "169.254.169.254/32"
    assert denied.index("169.254.169.254/32") < denied.index("169.254.0.0/16")
    for rfc1918 in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert denied.index("169.254.169.254/32") < denied.index(rfc1918)


def test_denied_ipv4_cidrs_contains_exactly_the_documented_ranges():
    assert set(bnp.denied_ipv4_cidrs()) == {
        "169.254.169.254/32",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    }


def test_denied_ipv6_cidrs_contains_loopback_link_local_and_unique_local():
    assert set(bnp.denied_ipv6_cidrs()) == {"::1/128", "fe80::/10", "fc00::/7"}


# ---------------------------------------------------------------------------
# THE deny-before-allow precedence test: simulate real ipBlock evaluation
# for concrete addresses and confirm the deny actually wins, rather than
# merely coexisting with the allow in the same object.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "denied_address",
    [
        "169.254.169.254",  # cloud IMDS -- THE address this feature exists to protect
        "169.254.1.1",  # other link-local
        "127.0.0.1",  # loopback
        "10.0.0.1",  # RFC1918
        "172.16.5.5",  # RFC1918
        "192.168.1.1",  # RFC1918
    ],
)
def test_https_rule_denies_imds_and_private_ranges_despite_the_broad_allow(denied_address):
    policy = _build_policy()
    rule = _https_rule(policy)
    ipv4_peer = next(p for p in rule.to if p.ip_block and p.ip_block.cidr == "0.0.0.0/0")
    assert _simulate_ip_block_peer_permits(ipv4_peer, denied_address) is False


@pytest.mark.parametrize(
    "public_address",
    ["8.8.8.8", "1.1.1.1", "93.184.216.34"],  # Google DNS, Cloudflare DNS, example.com
)
def test_https_rule_still_allows_ordinary_public_addresses(public_address):
    """The carve-out must be narrow -- it should not accidentally deny
    ordinary public internet addresses a browser session legitimately
    needs to reach."""
    policy = _build_policy()
    rule = _https_rule(policy)
    ipv4_peer = next(p for p in rule.to if p.ip_block and p.ip_block.cidr == "0.0.0.0/0")
    assert _simulate_ip_block_peer_permits(ipv4_peer, public_address) is True


@pytest.mark.parametrize(
    "denied_address_v6",
    ["::1", "fe80::1", "fc00::1"],
)
def test_https_rule_denies_ipv6_loopback_link_local_and_unique_local(denied_address_v6):
    policy = _build_policy()
    rule = _https_rule(policy)
    ipv6_peer = next(p for p in rule.to if p.ip_block and p.ip_block.cidr == "::/0")
    assert _simulate_ip_block_peer_permits(ipv6_peer, denied_address_v6) is False


def test_https_rule_still_allows_a_public_ipv6_address():
    policy = _build_policy()
    rule = _https_rule(policy)
    ipv6_peer = next(p for p in rule.to if p.ip_block and p.ip_block.cidr == "::/0")
    # 2606:4700:4700::1111 -- Cloudflare's public IPv6 DNS resolver.
    assert _simulate_ip_block_peer_permits(ipv6_peer, "2606:4700:4700::1111") is True


def test_https_rule_deny_carve_out_is_not_bypassable_via_the_dns_rule():
    """The DNS rule (selector-based) and the HTTPS rule (ipBlock-based) are
    independent peers -- confirm the DNS rule's selector shape can never be
    satisfied by an attacker-controlled IP address the way an ipBlock rule
    could, i.e. it carries no ip_block at all for a denied range to ever
    apply to."""
    policy = _build_policy()
    rule = _dns_rule(policy)
    assert all(peer.ip_block is None for peer in rule.to)


# ---------------------------------------------------------------------------
# Determinism -- same inputs must produce byte-identical (via to_dict())
# objects, so replace_namespaced_network_policy calls are idempotent and
# don't churn on every session-configure call.
# ---------------------------------------------------------------------------

def test_build_policy_is_deterministic_for_same_inputs():
    first = _build_policy()
    second = _build_policy()
    assert first.to_dict() == second.to_dict()
