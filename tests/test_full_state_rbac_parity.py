"""Tests for deploy/full-state-snapshot-rbac-optin.yaml (issue #149).

Mirrors test_kata_template_parity.py's drift-guard shape: a reference
manifest that isn't wired into any runtime path (`create_all`, a
deployment script, or any other test) has already drifted from the code
that depends on it twice in this repo's history (see CLAUDE.md's
"Reference manifests ... must match the actual runtime code" note) --
`deploy/full-state-snapshot-rbac-optin.yaml` backs the single most
privilege-broad opt-in grant in the repo (`nodes/proxy` create/get) and
had zero test coverage before this file.

Does NOT (and cannot, in this environment) verify anything against a
live cluster -- see checkpoint_backend.py's own module docstring for the
disclosed, still-unverified risk (the ContainerCheckpoint feature gate /
CRI-level checkpoint support). This only guards the two things that are
checkable from source: (1) the ServiceAccount this manifest binds to is
the same one the rest of this repo's RBAC/deployment code actually uses,
and (2) checkpoint_backend.py's real kubelet API calls stay within (never
broader than) the resource/verb scope this manifest's ClusterRole grants.
"""

from pathlib import Path

import yaml

from boxkite import k8s_auth

REPO_ROOT = Path(__file__).resolve().parent.parent
OPTIN_MANIFEST_PATH = REPO_ROOT / "deploy" / "full-state-snapshot-rbac-optin.yaml"
DEFAULT_RBAC_PATH = REPO_ROOT / "deploy" / "rbac.yaml"
CHECKPOINT_BACKEND_PATH = REPO_ROOT / "src" / "boxkite" / "checkpoint_backend.py"


def _load_docs(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _find(docs: list[dict], kind: str, name: str) -> dict:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    raise AssertionError(f"No {kind} named {name!r} found")


def _optin_docs() -> list[dict]:
    return _load_docs(OPTIN_MANIFEST_PATH)


def _default_rbac_docs() -> list[dict]:
    return _load_docs(DEFAULT_RBAC_PATH)


# ── ServiceAccount identity parity ──────────────────────────────────────


def test_optin_manifest_has_exactly_one_clusterrole_and_one_clusterrolebinding():
    docs = _optin_docs()
    kinds = [doc.get("kind") for doc in docs]
    assert kinds.count("ClusterRole") == 1
    assert kinds.count("ClusterRoleBinding") == 1


def test_optin_binding_subject_matches_default_rbac_service_account():
    """The opt-in ClusterRoleBinding must bind the SAME ServiceAccount
    identity (name + namespace) that deploy/rbac.yaml's RoleBinding grants
    the manager's normal permissions to -- otherwise enabling this feature
    grants nodes/proxy to an identity that never actually runs the
    control-plane, or (worse) silently fails to grant it to the one that
    does."""
    optin_binding = _find(_optin_docs(), "ClusterRoleBinding", "boxkite-full-state-checkpoint-optin-binding")
    default_binding = _find(_default_rbac_docs(), "RoleBinding", "sandbox-manager-binding")

    optin_subject = optin_binding["subjects"][0]
    default_subject = default_binding["subjects"][0]

    assert optin_subject["kind"] == "ServiceAccount"
    assert optin_subject["name"] == default_subject["name"]
    assert optin_subject["namespace"] == default_subject["namespace"]


def test_optin_binding_subject_matches_k8s_auth_default_service_account_name():
    """Ties the manifest back to the actual runtime default
    (k8s_auth.DEFAULT_CONTROL_PLANE_SERVICE_ACCOUNT_NAME), not just to
    another manifest -- this is the "real control-plane deployment" side
    of the parity check, since a manifest-to-manifest match alone can't
    catch both drifting together away from the code."""
    optin_binding = _find(_optin_docs(), "ClusterRoleBinding", "boxkite-full-state-checkpoint-optin-binding")
    optin_subject = optin_binding["subjects"][0]
    assert optin_subject["name"] == k8s_auth.DEFAULT_CONTROL_PLANE_SERVICE_ACCOUNT_NAME


def test_optin_binding_role_ref_matches_the_clusterrole_defined_in_same_file():
    docs = _optin_docs()
    cluster_role = _find(docs, "ClusterRole", "boxkite-full-state-checkpoint-optin")
    binding = _find(docs, "ClusterRoleBinding", "boxkite-full-state-checkpoint-optin-binding")
    assert binding["roleRef"]["kind"] == "ClusterRole"
    assert binding["roleRef"]["name"] == cluster_role["metadata"]["name"]


# ── RBAC scope vs. checkpoint_backend.py's real API calls ──────────────


def _nodes_proxy_rule() -> dict:
    cluster_role = _find(_optin_docs(), "ClusterRole", "boxkite-full-state-checkpoint-optin")
    for rule in cluster_role["rules"]:
        if rule.get("apiGroups") == [""] and rule.get("resources") == ["nodes/proxy"]:
            return rule
    raise AssertionError("No nodes/proxy rule found in boxkite-full-state-checkpoint-optin ClusterRole")


def test_nodes_proxy_rule_grants_exactly_create_and_get():
    """The manifest's own header comments claim this grants only `create`
    and `get` on nodes/proxy (not e.g. delete/update/patch, which nodes/
    proxy doesn't even support, but which would be a much larger and
    unjustified blast-radius increase if ever added by mistake)."""
    rule = _nodes_proxy_rule()
    assert set(rule["verbs"]) == {"create", "get"}


def test_checkpoint_backend_uses_only_post_and_get_node_proxy_calls():
    """checkpoint_backend.py must never call a node-proxy method whose
    HTTP verb requires more than what the ClusterRole grants. The
    Kubernetes API maps POST -> the `create` verb and GET -> the `get`
    verb on the nodes/proxy subresource; connect_post_node_proxy_with_path
    (checkpoint()) and connect_get_node_proxy_with_path
    (probe_checkpoint_support()) are the only two call sites in this
    module, and both map to verbs already covered by
    test_nodes_proxy_rule_grants_exactly_create_and_get above. This test
    fails if a future change adds a call requiring a verb (e.g. `update`,
    `delete`, `patch`) this manifest doesn't grant, or if the manifest's
    verb set is ever narrowed below what this module actually calls."""
    source = CHECKPOINT_BACKEND_PATH.read_text()
    node_proxy_calls = {
        line.split("core_api.")[1].split("(")[0].strip()
        for line in source.splitlines()
        if "core_api." in line and "node_proxy" in line
    }
    assert node_proxy_calls, "Expected at least one *_node_proxy_with_path call in checkpoint_backend.py"

    granted_verbs = set(_nodes_proxy_rule()["verbs"])
    call_to_verb = {
        "connect_post_node_proxy_with_path": "create",
        "connect_get_node_proxy_with_path": "get",
    }
    for call in node_proxy_calls:
        assert call in call_to_verb, (
            f"Unrecognized node-proxy call {call!r} -- add its required RBAC verb to "
            "call_to_verb above and confirm the manifest grants it before allowing this call."
        )
        required_verb = call_to_verb[call]
        assert required_verb in granted_verbs, (
            f"{call} requires the {required_verb!r} verb on nodes/proxy, which "
            "deploy/full-state-snapshot-rbac-optin.yaml does not grant."
        )


def test_checkpoint_backend_never_calls_a_broader_node_proxy_verb():
    """Positive-scope check complementing the two tests above: explicitly
    assert the module contains no delete/patch/put node-proxy call, so a
    future addition can't silently rely on the manifest being widened
    without this test catching the new, broader dependency first."""
    source = CHECKPOINT_BACKEND_PATH.read_text()
    for forbidden in ("connect_delete_node_proxy", "connect_patch_node_proxy", "connect_put_node_proxy"):
        assert forbidden not in source


def test_optin_manifest_also_grants_pods_get_for_node_resolution():
    """_manager_checkpoint.py resolves the pod's node_name via
    read_namespaced_pod (a `get` on the namespaced `pods` resource)
    before calling the node-proxy checkpoint endpoint -- this manifest
    includes that rule for completeness (per its own comment) in case it
    is ever applied to a ServiceAccount that doesn't already have
    deploy/rbac.yaml's grants."""
    cluster_role = _find(_optin_docs(), "ClusterRole", "boxkite-full-state-checkpoint-optin")
    pods_rules = [
        rule for rule in cluster_role["rules"]
        if rule.get("apiGroups") == [""] and rule.get("resources") == ["pods"]
    ]
    assert len(pods_rules) == 1
    assert "get" in pods_rules[0]["verbs"]
