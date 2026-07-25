"""Tests for src/boxkite/secrets_network_policy.py -- the per-session
NetworkPolicy scoping mechanism for the secrets broker (issue #74).
"""

import pytest

from boxkite import secrets_network_policy as snp

pytestmark = pytest.mark.pr


def test_secrets_egress_policy_name_is_deterministic_and_prefixed():
    name = snp.secrets_egress_policy_name("sandbox-abc123-standalone-9f2e")
    assert name == "sandbox-secrets-egress-sandbox-abc123-standalone-9f2e"


def test_secrets_egress_policy_name_truncates_at_k8s_limit():
    long_pod_name = "sandbox-" + ("x" * 300)
    name = snp.secrets_egress_policy_name(long_pod_name)
    assert len(name) == 253


def test_collect_allowed_hosts_dedupes_and_sorts_across_grants():
    grants = [
        {"name": "stripe-key", "allowed_hosts": ["api.stripe.com", "Api.Stripe.com"]},
        {"name": "github-token", "allowed_hosts": ["api.github.com", "api.stripe.com"]},
    ]
    assert snp.collect_allowed_hosts(grants) == ["api.github.com", "api.stripe.com"]


def test_collect_allowed_hosts_ignores_blank_entries():
    grants = [{"name": "x", "allowed_hosts": ["", "  ", "api.example.com"]}]
    assert snp.collect_allowed_hosts(grants) == ["api.example.com"]


def test_collect_allowed_hosts_empty_for_no_grants():
    assert snp.collect_allowed_hosts(None) == []
    assert snp.collect_allowed_hosts([]) == []


@pytest.mark.parametrize(
    "ip_str",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.100.100.200",  # Alibaba Cloud IMDS, CGNAT space
        "224.0.0.1",  # multicast
        "0.0.0.0",
        "not-an-ip",
    ],
)
def test_is_disallowed_ip_blocks_private_and_special_ranges(ip_str):
    assert snp._is_disallowed_ip(ip_str) is True


@pytest.mark.parametrize("ip_str", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
def test_is_disallowed_ip_allows_public_addresses(ip_str):
    assert snp._is_disallowed_ip(ip_str) is False


def test_is_disallowed_ip_unwraps_nat64_embedded_metadata_address():
    # 64:ff9b::/96 + 169.254.169.254 embedded in the low 32 bits.
    nat64_metadata = "64:ff9b::a9fe:a9fe"
    assert snp._is_disallowed_ip(nat64_metadata) is True


def test_is_disallowed_ip_unwraps_ipv4_mapped_public_address():
    mapped_public = "::ffff:8.8.8.8"
    assert snp._is_disallowed_ip(mapped_public) is False


def test_build_policy_returns_none_when_no_secret_grants():
    policy = snp.build_secrets_egress_network_policy(
        pod_name="sandbox-1",
        namespace="default",
        session_label_value="session-abc",
        secret_grants=None,
    )
    assert policy is None


def test_build_policy_returns_none_when_all_hosts_unresolvable():
    policy = snp.build_secrets_egress_network_policy(
        pod_name="sandbox-1",
        namespace="default",
        session_label_value="session-abc",
        secret_grants=[{"name": "s", "allowed_hosts": ["nonexistent.example"]}],
        resolve_host_ips=lambda host: [],
    )
    assert policy is None


def test_build_policy_omits_host_whose_only_resolved_ip_is_disallowed():
    policy = snp.build_secrets_egress_network_policy(
        pod_name="sandbox-1",
        namespace="default",
        session_label_value="session-abc",
        secret_grants=[
            {"name": "metadata-secret", "allowed_hosts": ["metadata.internal"]},
            {"name": "real-secret", "allowed_hosts": ["api.example.com"]},
        ],
        resolve_host_ips=lambda host: (
            ["169.254.169.254"] if host == "metadata.internal" else ["93.184.216.40"]
        ),
    )
    assert policy is not None
    all_cidrs = {
        peer.ip_block.cidr
        for rule in policy.spec.egress
        for peer in rule.to
    }
    assert all_cidrs == {"93.184.216.40/32"}


def test_build_policy_pod_selector_matches_exact_session_pod():
    policy = snp.build_secrets_egress_network_policy(
        pod_name="sandbox-1",
        namespace="ns1",
        session_label_value="session-xyz",
        secret_grants=[{"name": "s", "allowed_hosts": ["api.example.com"]}],
        resolve_host_ips=lambda host: ["93.184.216.40"],
    )
    assert policy.metadata.name == snp.secrets_egress_policy_name("sandbox-1")
    assert policy.metadata.namespace == "ns1"
    assert policy.spec.pod_selector.match_labels == {
        "app": "sandbox",
        "session-id": "session-xyz",
    }
    assert policy.spec.policy_types == ["Egress"]


def test_build_policy_uses_ipv6_128_prefix_for_v6_addresses():
    policy = snp.build_secrets_egress_network_policy(
        pod_name="sandbox-1",
        namespace="default",
        session_label_value="session-abc",
        secret_grants=[{"name": "s", "allowed_hosts": ["api.example.com"]}],
        resolve_host_ips=lambda host: ["2606:4700:4700::1111"],
    )
    cidrs = {peer.ip_block.cidr for rule in policy.spec.egress for peer in rule.to}
    assert cidrs == {"2606:4700:4700::1111/128"}


def test_build_policy_one_egress_rule_per_host_with_all_resolved_ips():
    policy = snp.build_secrets_egress_network_policy(
        pod_name="sandbox-1",
        namespace="default",
        session_label_value="session-abc",
        secret_grants=[{"name": "s", "allowed_hosts": ["api.example.com"]}],
        resolve_host_ips=lambda host: ["93.184.216.40", "93.184.216.41"],
    )
    assert len(policy.spec.egress) == 1
    cidrs = {peer.ip_block.cidr for peer in policy.spec.egress[0].to}
    assert cidrs == {"93.184.216.40/32", "93.184.216.41/32"}
    assert policy.spec.egress[0].ports[0].port == 443
    assert policy.spec.egress[0].ports[0].protocol == "TCP"


def test_build_policy_is_deterministic_for_same_grants(monkeypatch):
    grants = [{"name": "s", "allowed_hosts": ["b.example.com", "a.example.com"]}]

    def fake_resolve(host):
        return ["93.184.216.43"]

    policy1 = snp.build_secrets_egress_network_policy(
        pod_name="p", namespace="default", session_label_value="s",
        secret_grants=grants, resolve_host_ips=fake_resolve,
    )
    policy2 = snp.build_secrets_egress_network_policy(
        pod_name="p", namespace="default", session_label_value="s",
        secret_grants=grants, resolve_host_ips=fake_resolve,
    )
    assert [r.to[0].ip_block.cidr for r in policy1.spec.egress] == [
        r.to[0].ip_block.cidr for r in policy2.spec.egress
    ]


def test_build_policy_uses_module_level_default_resolver_when_omitted(monkeypatch):
    """resolve_host_ips is looked up lazily at call time (not bound at def
    time) so tests -- and any future caller -- can monkeypatch
    default_resolve_host_ips and have it take effect even when the
    resolver argument is omitted entirely (the real call site,
    SandboxManager._sync_secrets_egress_network_policy, never passes one)."""
    monkeypatch.setattr(snp, "default_resolve_host_ips", lambda host: ["93.184.216.42"])
    policy = snp.build_secrets_egress_network_policy(
        pod_name="sandbox-1",
        namespace="default",
        session_label_value="session-abc",
        secret_grants=[{"name": "s", "allowed_hosts": ["api.example.com"]}],
    )
    cidrs = {peer.ip_block.cidr for rule in policy.spec.egress for peer in rule.to}
    assert cidrs == {"93.184.216.42/32"}


def test_default_resolve_host_ips_returns_empty_on_resolution_failure(monkeypatch):
    import socket

    def raise_gaierror(*args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", raise_gaierror)
    assert snp.default_resolve_host_ips("nonexistent.example") == []


def test_default_resolve_host_ips_dedupes_and_sorts(monkeypatch):
    import socket

    def fake_getaddrinfo(host, port, proto=None):
        return [
            (socket.AF_INET, None, None, "", ("93.184.216.43", port)),
            (socket.AF_INET, None, None, "", ("93.184.216.43", port)),
            (socket.AF_INET, None, None, "", ("93.184.216.40", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert snp.default_resolve_host_ips("api.example.com") == [
        "93.184.216.40",
        "93.184.216.43",
    ]
