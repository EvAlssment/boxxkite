"""Tests for High #3: deploy/network-policy.yaml must not be a blanket allow.

Asserts against the actual parsed YAML content (not just "the file exists")
so this can't silently regress back to an 0.0.0.0/0 egress rule, and checks
that the default policy's podSelector matches labels real pods actually
carry (src/boxkite/manager.py / src/boxkite/warm_pool.py only ever set
`app: sandbox` — never a `component` label).

Also covers the fix for a later finding: the permissive, fully-open-egress
policy used to be a second `---`-separated document in THIS file, so a
plain `kubectl apply -f deploy/network-policy.yaml` silently applied both
policies at once (NetworkPolicies with overlapping podSelectors are
additive). It now lives in its own file,
deploy/network-policy-permissive-optin.yaml, specifically so that can't
happen — see tests below asserting each file defines exactly one policy.
"""

import ipaddress
from pathlib import Path

import yaml

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
NETWORK_POLICY_PATH = DEPLOY_DIR / "network-policy.yaml"
PERMISSIVE_NETWORK_POLICY_PATH = DEPLOY_DIR / "network-policy-permissive-optin.yaml"

CLOUD_METADATA_IP = "169.254.169.254"


def _load_policies(path: Path = NETWORK_POLICY_PATH) -> list[dict]:
    text = path.read_text()
    return [doc for doc in yaml.safe_load_all(text) if doc]


def _egress_rules(policy: dict) -> list[dict]:
    return policy.get("spec", {}).get("egress") or []


def _has_wide_open_ip_block(rule: dict) -> bool:
    for to_entry in rule.get("to", []):
        ip_block = to_entry.get("ipBlock")
        if ip_block and ip_block.get("cidr") == "0.0.0.0/0":
            return True
    return False


def test_network_policy_file_defines_exactly_one_policy():
    """deploy/network-policy.yaml must define ONLY the restrictive default
    policy -- the permissive alternative living in the same file (as a
    second `---` document) is exactly the bug that let a plain
    `kubectl apply -f` silently apply both at once. See
    test_permissive_policy_file_defines_exactly_one_policy below for its
    counterpart."""
    policies = _load_policies()
    assert len(policies) == 1
    assert policies[0]["metadata"]["name"] == "sandbox-network-policy"


def test_permissive_policy_file_defines_exactly_one_policy():
    policies = _load_policies(PERMISSIVE_NETWORK_POLICY_PATH)
    assert len(policies) == 1
    assert policies[0]["metadata"]["name"] == "sandbox-network-policy-permissive"


def test_default_policy_pod_selector_matches_real_pod_labels():
    """
    manager.py/warm_pool.py only ever label live pods `app: sandbox` (no
    `component` label exists anywhere in pod-creation code). A podSelector
    requiring `component: execution` would match zero real pods.
    """
    policies = _load_policies()
    default_policy = next(
        p for p in policies if p["metadata"]["name"] == "sandbox-network-policy"
    )
    selector_labels = default_policy["spec"]["podSelector"]["matchLabels"]
    assert selector_labels == {"app": "sandbox"}


def test_default_policy_has_no_blanket_allow_egress_rule():
    policies = _load_policies()
    default_policy = next(
        p for p in policies if p["metadata"]["name"] == "sandbox-network-policy"
    )
    for rule in _egress_rules(default_policy):
        assert not _has_wide_open_ip_block(rule), (
            "sandbox-network-policy must not contain an ipBlock 0.0.0.0/0 "
            "egress rule — that allows PyPI/npm/GitHub/anything on the "
            "matching port(s), not just object storage."
        )


def test_default_policy_documents_imds_verification_explicitly():
    """The cloud instance metadata endpoint (169.254.169.254 -- typically
    unauthenticated, can hand out the node's own IAM credentials) is only
    blocked by omission from the allowlist, and link-local ranges have a
    history of CNI-specific enforcement gaps on real cloud clusters. This
    must be called out by name as the top verification priority, not left
    as an instance of the generic "verify your CNI enforces this" advice."""
    text = NETWORK_POLICY_PATH.read_text()
    assert CLOUD_METADATA_IP in text


def test_default_policy_never_allows_the_cloud_metadata_ip_via_ipblock():
    """Defense in depth against a future CHANGEME edit (the storage-egress
    ipBlock fallback) accidentally supplying a CIDR wide enough to cover
    the cloud metadata endpoint."""
    policies = _load_policies()
    default_policy = next(
        p for p in policies if p["metadata"]["name"] == "sandbox-network-policy"
    )
    metadata_addr = ipaddress.ip_address(CLOUD_METADATA_IP)
    for rule in _egress_rules(default_policy):
        for to_entry in rule.get("to", []):
            ip_block = to_entry.get("ipBlock")
            if not ip_block:
                continue
            network = ipaddress.ip_network(ip_block["cidr"], strict=False)
            assert metadata_addr not in network, (
                f"egress ipBlock {ip_block['cidr']} allows the cloud metadata "
                f"endpoint {CLOUD_METADATA_IP} -- narrow the CIDR or add an "
                "'except' entry excluding it."
            )


def test_default_policy_still_allows_dns():
    policies = _load_policies()
    default_policy = next(
        p for p in policies if p["metadata"]["name"] == "sandbox-network-policy"
    )
    dns_rules = [
        rule
        for rule in _egress_rules(default_policy)
        if any(
            (to_entry.get("podSelector", {}).get("matchLabels", {}).get("k8s-app") == "kube-dns")
            for to_entry in rule.get("to", [])
        )
    ]
    assert dns_rules, "default policy must still allow DNS resolution"


def test_permissive_policy_is_separately_named_labeled_and_filed():
    policies = _load_policies(PERMISSIVE_NETWORK_POLICY_PATH)
    permissive = [
        p
        for p in policies
        if p["metadata"].get("labels", {}).get("boxkite.dev/policy-mode") == "permissive-opt-in"
    ]
    assert len(permissive) == 1
    assert permissive[0]["metadata"]["name"] != "sandbox-network-policy"
    assert permissive[0]["metadata"]["name"] == "sandbox-network-policy-permissive"
    # And it must NOT also appear in the default policy's file -- that's the
    # exact bug this split fixes.
    assert not any(
        p["metadata"].get("labels", {}).get("boxkite.dev/policy-mode") == "permissive-opt-in"
        for p in _load_policies(NETWORK_POLICY_PATH)
    )


def test_permissive_policy_is_the_only_place_a_blanket_egress_appears():
    """Wide-open egress is fine in the explicitly-labeled permissive policy —
    just not silently present in the default one (covered above)."""
    policies = _load_policies(PERMISSIVE_NETWORK_POLICY_PATH)
    permissive = next(
        p
        for p in policies
        if p["metadata"].get("labels", {}).get("boxkite.dev/policy-mode") == "permissive-opt-in"
    )
    egress = _egress_rules(permissive)
    assert egress == [{}]
