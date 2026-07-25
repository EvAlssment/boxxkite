"""Tests for deploy/pod-security-policy.yaml -- the base ValidatingAdmissionPolicy
closing an RBAC-exposure gap: deploy/rbac.yaml's own comments disclose that
the sandbox-manager-role grants get/list/watch/create/delete on every pod
(and get/create/delete on every secret) in the namespace, not just
sandbox-labeled ones -- so a leaked control-plane credential could
previously submit an arbitrary privileged/hostPath/hostNetwork pod spec
with nothing at the cluster level stopping it. This file backstops that
even if the RBAC-level mitigation (a dedicated namespace) isn't followed.

A sibling deploy/hosted-deployment/pod-security-policy.yaml used to live in
this same repo and this file also asserted parity between the two -- that
manifest is now maintainer-operational content kept in a separate, private
location (see docs/OSS-VS-HOSTED-SPLIT-POSITION.md), not a sibling in this
tree anymore. The parity check is skipped (not deleted) when that path
isn't present, so this file still runs standalone in the public tree
instead of failing with FileNotFoundError.
"""

from pathlib import Path

import pytest
import yaml

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
BASE_POLICY_PATH = DEPLOY_DIR / "pod-security-policy.yaml"
HOSTED_POLICY_PATH = DEPLOY_DIR / "hosted-deployment" / "pod-security-policy.yaml"


def _load_docs(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _policy_and_binding(path: Path) -> tuple[dict, dict]:
    docs = _load_docs(path)
    policy = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicy")
    binding = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding")
    return policy, binding


def _validation_expressions(policy: dict) -> set[str]:
    return {v["expression"] for v in policy["spec"]["validations"]}


def test_base_policy_file_defines_exactly_a_policy_and_a_binding():
    docs = _load_docs(BASE_POLICY_PATH)
    assert len(docs) == 2
    kinds = {d["kind"] for d in docs}
    assert kinds == {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}


def test_base_policy_blocks_all_five_node_compromise_vectors():
    policy, _ = _policy_and_binding(BASE_POLICY_PATH)
    expressions = _validation_expressions(policy)
    assert any("hostNetwork" in e for e in expressions)
    assert any("hostPID" in e for e in expressions)
    assert any("hostIPC" in e for e in expressions)
    assert any("hostPath" in e for e in expressions)
    assert any("privileged" in e for e in expressions)


def test_base_policy_binding_denies_and_fails_closed():
    policy, binding = _policy_and_binding(BASE_POLICY_PATH)
    assert policy["spec"]["failurePolicy"] == "Fail"
    assert binding["spec"]["validationActions"] == ["Deny"]


def test_base_policy_matches_pods_create_and_update():
    policy, _ = _policy_and_binding(BASE_POLICY_PATH)
    resource_rules = policy["spec"]["matchConstraints"]["resourceRules"]
    assert any(
        rule["resources"] == ["pods"] and set(rule["operations"]) >= {"CREATE", "UPDATE"}
        for rule in resource_rules
    )


@pytest.mark.skipif(
    not HOSTED_POLICY_PATH.exists(),
    reason="deploy/hosted-deployment/ is maintainer-operational content, not present in this tree",
)
def test_base_policy_stays_in_parity_with_hosted_deployment_policy():
    """The base and hosted-deployment admission policies protect against
    the same node-compromise vectors -- they must not drift apart, or a
    future hardening added to one silently doesn't apply to the other."""
    base_policy, _ = _policy_and_binding(BASE_POLICY_PATH)
    hosted_policy, _ = _policy_and_binding(HOSTED_POLICY_PATH)
    assert _validation_expressions(base_policy) == _validation_expressions(hosted_policy)


def test_readme_instructs_applying_the_base_pod_security_policy():
    readme = (DEPLOY_DIR.parent / "README.md").read_text()
    assert "pod-security-policy.yaml" in readme
