"""Tests that deploy/rbac.yaml grants the manager's ServiceAccount the RBAC
it needs for the per-session secrets-egress NetworkPolicy mechanism (issue
#74, src/boxkite/secrets_network_policy.py).

A sibling `deploy/hosted-deployment/rbac.yaml` used to live in this same
repo and this file also asserted parity between the two -- that manifest is
now maintainer-operational content kept in a separate, private location
(see docs/OSS-VS-HOSTED-SPLIT-POSITION.md), not a sibling in this tree
anymore. The parity check is skipped (not deleted) when that path isn't
present, so this file still runs standalone in the public tree instead of
failing with FileNotFoundError, and still runs the full parity check in any
tree where both manifests happen to be siblings again.
"""

from pathlib import Path

import pytest
import yaml

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
BASE_RBAC_PATH = DEPLOY_DIR / "rbac.yaml"
HOSTED_RBAC_PATH = DEPLOY_DIR / "hosted-deployment" / "rbac.yaml"


def _load_docs(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _manager_role(path: Path) -> dict:
    docs = _load_docs(path)
    return next(
        d for d in docs
        if d.get("kind") == "Role" and d["metadata"]["name"] == "sandbox-manager-role"
    )


def _network_policy_rule(role: dict) -> dict:
    return next(
        rule for rule in role["rules"]
        if rule.get("apiGroups") == ["networking.k8s.io"]
    )


def test_base_rbac_grants_networkpolicy_verbs():
    role = _manager_role(BASE_RBAC_PATH)
    rule = _network_policy_rule(role)
    assert rule["resources"] == ["networkpolicies"]
    for verb in ("get", "create", "update", "delete"):
        assert verb in rule["verbs"]


@pytest.mark.skipif(
    not HOSTED_RBAC_PATH.exists(),
    reason="deploy/hosted-deployment/ is maintainer-operational content, not present in this tree",
)
def test_hosted_rbac_grants_networkpolicy_verbs():
    role = _manager_role(HOSTED_RBAC_PATH)
    rule = _network_policy_rule(role)
    assert rule["resources"] == ["networkpolicies"]
    for verb in ("get", "create", "update", "delete"):
        assert verb in rule["verbs"]


@pytest.mark.skipif(
    not HOSTED_RBAC_PATH.exists(),
    reason="deploy/hosted-deployment/ is maintainer-operational content, not present in this tree",
)
def test_base_and_hosted_rbac_grant_the_same_networkpolicy_verbs():
    base_rule = _network_policy_rule(_manager_role(BASE_RBAC_PATH))
    hosted_rule = _network_policy_rule(_manager_role(HOSTED_RBAC_PATH))
    assert set(base_rule["verbs"]) == set(hosted_rule["verbs"])


def test_networkpolicy_rbac_does_not_grant_list_or_watch():
    """Same deliberate-narrowing posture as the per-pod Secret rule: the
    manager always computes the exact deterministic policy name
    (secrets_egress_policy_name(pod_name)) rather than enumerating existing
    NetworkPolicy objects."""
    paths = [BASE_RBAC_PATH]
    if HOSTED_RBAC_PATH.exists():
        paths.append(HOSTED_RBAC_PATH)
    for path in paths:
        rule = _network_policy_rule(_manager_role(path))
        assert "list" not in rule["verbs"]
        assert "watch" not in rule["verbs"]
