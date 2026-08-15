"""``boxxkite doctor`` -- pre-install cluster compatibility checks (GitHub
issue #119).

boxxkite's isolation model assumes things about the target cluster that
``helm install`` does not verify: a NetworkPolicy-enforcing CNI, Pod Security
Admission (or an equivalent admission controller), seccomp support, and RBAC
wide enough for the manager to create/delete sandbox pods. When one of those
is missing today, the first sign is a confusing pod-scheduling or admission
error deep in Kubernetes output -- or, worse for a security product, nothing
at all, because a policy that isn't enforced fails open and quietly.

This command turns that into an upfront table.

**What it can and cannot prove.** Every check here reads cluster metadata; none
of them exercise a real workload. In particular `networkpolicy-enforcement` is
a *heuristic*: it confirms the NetworkPolicy API is served and looks for a
known enforcing CNI in kube-system, because actually proving enforcement means
scheduling two pods, pulling images, and attempting live traffic -- far too
slow and intrusive for a preflight, and it needs the very permissions this
command is checking for. A PASS here means "nothing looks wrong"; it is not a
substitute for verifying isolation against your own cluster (see
docs/../blog "Verifying network-dark isolation against your own CNI").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import typer

logger = logging.getLogger(__name__)

# PSA reached GA in 1.25; below that its namespace labels are ignored (or
# only honored as beta), so the isolation model's admission side isn't
# actually enforced by the API server.
MIN_RECOMMENDED_MINOR = 25

# seccompProfile: RuntimeDefault in a pod spec is GA from 1.19.
MIN_SECCOMP_MINOR = 19

# DaemonSet name substrings for CNIs that actually implement NetworkPolicy.
# Deliberately a denylist-free allowlist of the common ones: an unknown CNI
# gets a WARN telling the operator to verify it themselves, never a silent
# PASS.
ENFORCING_CNI_MARKERS = ("calico", "cilium", "weave", "antrea", "kube-router", "ovn-kubernetes")

PASS = "pass"
WARN = "warn"
FAIL = "fail"

# Verbs the sandbox manager actually needs on sandbox pods -- see
# deploy/rbac.yaml. Kept in the same shape a SelfSubjectAccessReview wants.
REQUIRED_PERMISSIONS = (
    ("pods", "create"),
    ("pods", "get"),
    ("pods", "list"),
    ("pods", "delete"),
    ("pods/exec", "create"),
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    remediation: str = ""


def _parse_minor(minor: str) -> Optional[int]:
    """K8s reports minor as e.g. "29", or "29+" on some managed providers."""
    digits = "".join(c for c in str(minor) if c.isdigit())
    return int(digits) if digits else None


async def check_server_version(version_api: Any) -> CheckResult:
    try:
        info = await version_api.get_code()
    except Exception as exc:
        return CheckResult(
            "cluster-reachable",
            FAIL,
            f"could not read cluster version: {exc}",
            "Check your kubeconfig context and that the API server is reachable.",
        )

    version = f"{info.major}.{info.minor}"
    minor = _parse_minor(info.minor)
    if minor is None:
        return CheckResult(
            "kubernetes-version",
            WARN,
            f"server reports an unparseable minor version ({info.minor!r})",
            "Verify the version manually with `kubectl version`.",
        )
    if minor < MIN_RECOMMENDED_MINOR:
        return CheckResult(
            "kubernetes-version",
            FAIL,
            f"v{version} is below the v1.{MIN_RECOMMENDED_MINOR} boxxkite expects",
            f"Pod Security Admission is GA from v1.{MIN_RECOMMENDED_MINOR}; below that the "
            "namespace labels boxxkite relies on are not enforced by the API server.",
        )
    return CheckResult("kubernetes-version", PASS, f"v{version}")


async def check_seccomp_support(version_api: Any) -> CheckResult:
    try:
        info = await version_api.get_code()
        minor = _parse_minor(info.minor)
    except Exception as exc:
        return CheckResult("seccomp-runtimedefault", WARN, f"version unavailable: {exc}")

    if minor is None or minor < MIN_SECCOMP_MINOR:
        return CheckResult(
            "seccomp-runtimedefault",
            FAIL,
            f"seccompProfile: RuntimeDefault needs v1.{MIN_SECCOMP_MINOR}+",
            "Upgrade the cluster; boxxkite sets RuntimeDefault on both the sandbox "
            "and sidecar containers at runtime.",
        )
    return CheckResult(
        "seccomp-runtimedefault",
        PASS,
        "supported (boxxkite sets it per-pod, so the SeccompDefault feature gate isn't required)",
    )


async def check_networkpolicy_api(networking_api: Any, namespace: str) -> CheckResult:
    try:
        await networking_api.list_namespaced_network_policy(namespace)
    except Exception as exc:
        return CheckResult(
            "networkpolicy-api",
            FAIL,
            f"networking.k8s.io/v1 NetworkPolicy unavailable: {exc}",
            "boxxkite's default-deny egress policy cannot be applied without it.",
        )
    return CheckResult("networkpolicy-api", PASS, "networking.k8s.io/v1 served")


async def check_networkpolicy_enforcement(apps_api: Any) -> CheckResult:
    """Heuristic -- see this module's docstring for why this deliberately
    doesn't run a live traffic test."""
    try:
        daemonsets = await apps_api.list_namespaced_daemon_set("kube-system")
    except Exception as exc:
        return CheckResult(
            "networkpolicy-enforcement",
            WARN,
            f"could not list kube-system DaemonSets to identify the CNI: {exc}",
            "Confirm your CNI enforces NetworkPolicy yourself -- an unenforced policy fails open.",
        )

    names = [ds.metadata.name for ds in getattr(daemonsets, "items", []) if getattr(ds, "metadata", None)]
    matched = [n for n in names for marker in ENFORCING_CNI_MARKERS if marker in n.lower()]
    if matched:
        return CheckResult(
            "networkpolicy-enforcement",
            PASS,
            f"found a NetworkPolicy-enforcing CNI ({matched[0]}) -- metadata only, not a live traffic test",
        )
    return CheckResult(
        "networkpolicy-enforcement",
        WARN,
        "no known NetworkPolicy-enforcing CNI found in kube-system",
        "Some CNIs (notably plain flannel) serve the NetworkPolicy API but never enforce it, "
        "so policies silently fail open. Verify enforcement against your own cluster before "
        "trusting the isolation model.",
    )


async def check_pod_security_admission(core_api: Any, namespace: str) -> CheckResult:
    try:
        ns = await core_api.read_namespace(namespace)
    except Exception as exc:
        return CheckResult(
            "pod-security-admission",
            WARN,
            f"namespace {namespace!r} not readable: {exc}",
            "Create it first, or pass --namespace for the namespace you'll install into.",
        )

    labels = (getattr(ns.metadata, "labels", None) or {}) if getattr(ns, "metadata", None) else {}
    enforce = labels.get("pod-security.kubernetes.io/enforce")
    if enforce in ("restricted", "baseline"):
        return CheckResult("pod-security-admission", PASS, f"enforce={enforce}")
    if enforce:
        return CheckResult(
            "pod-security-admission",
            WARN,
            f"enforce={enforce}",
            "boxxkite's pods are written for the `restricted` profile; a weaker level "
            "admits more than the isolation model assumes.",
        )
    return CheckResult(
        "pod-security-admission",
        WARN,
        f"namespace {namespace!r} has no pod-security.kubernetes.io/enforce label",
        "Apply deploy/pod-security-policy.yaml (or label the namespace "
        "`pod-security.kubernetes.io/enforce=restricted`) so the API server rejects a "
        "pod spec that drifts from the isolation model.",
    )


async def check_rbac(authz_api: Any, namespace: str, review_factory: Any) -> CheckResult:
    missing = []
    for resource, verb in REQUIRED_PERMISSIONS:
        try:
            review = await authz_api.create_self_subject_access_review(
                review_factory(namespace=namespace, resource=resource, verb=verb)
            )
            allowed = bool(getattr(getattr(review, "status", None), "allowed", False))
        except Exception as exc:
            return CheckResult(
                "rbac",
                WARN,
                f"could not run a SelfSubjectAccessReview: {exc}",
                "Check the permissions in deploy/rbac.yaml by hand instead.",
            )
        if not allowed:
            missing.append(f"{verb} {resource}")

    if missing:
        return CheckResult(
            "rbac",
            FAIL,
            f"missing: {', '.join(missing)}",
            f"Apply deploy/rbac.yaml (or grant the equivalent) in namespace {namespace!r}.",
        )
    return CheckResult("rbac", PASS, f"all {len(REQUIRED_PERMISSIONS)} required verbs allowed")


async def check_cert_manager(apiext_api: Any) -> CheckResult:
    """Only relevant when TLS automation is used for manager<->sidecar certs;
    a WARN, never a FAIL, since boxxkite also accepts certs provisioned any
    other way."""
    try:
        crds = await apiext_api.list_custom_resource_definition()
    except Exception as exc:
        return CheckResult("cert-manager", WARN, f"could not list CRDs: {exc}")

    names = [c.metadata.name for c in getattr(crds, "items", []) if getattr(c, "metadata", None)]
    if any(n.endswith("cert-manager.io") for n in names):
        return CheckResult("cert-manager", PASS, "cert-manager CRDs present")
    return CheckResult(
        "cert-manager",
        WARN,
        "no cert-manager CRDs found",
        "Only needed if you want automated manager<->sidecar TLS certs; provisioning them "
        "another way is fine (see SECURITY.md on the pinned-cert transport).",
    )


def _review_body(*, namespace: str, resource: str, verb: str):
    """Built lazily so importing this module doesn't require kubernetes_asyncio
    -- the CLI imports every cmd_* module at startup, and `boxxkite doctor` is
    the only command that needs a cluster client at all."""
    from kubernetes_asyncio import client

    return client.V1SelfSubjectAccessReview(
        spec=client.V1SelfSubjectAccessReviewSpec(
            resource_attributes=client.V1ResourceAttributes(
                namespace=namespace, resource=resource, verb=verb
            )
        )
    )


async def run_cluster_checks(
    *,
    version_api: Any,
    core_api: Any,
    networking_api: Any,
    apps_api: Any,
    apiext_api: Any,
    authz_api: Any,
    namespace: str,
    review_factory: Any = _review_body,
) -> list[CheckResult]:
    """Every check, in report order. Takes already-constructed API objects so
    this is directly unit-testable without a cluster."""
    return [
        await check_server_version(version_api),
        await check_seccomp_support(version_api),
        await check_networkpolicy_api(networking_api, namespace),
        await check_networkpolicy_enforcement(apps_api),
        await check_pod_security_admission(core_api, namespace),
        await check_rbac(authz_api, namespace, review_factory),
        await check_cert_manager(apiext_api),
    ]


_STATUS_STYLE = {
    PASS: ("PASS", typer.colors.GREEN),
    WARN: ("WARN", typer.colors.YELLOW),
    FAIL: ("FAIL", typer.colors.RED),
}


def render_results(results: list[CheckResult]) -> None:
    width = max((len(r.name) for r in results), default=0)
    for r in results:
        label, color = _STATUS_STYLE.get(r.status, (r.status.upper(), None))
        typer.secho(f"  {label}  ", fg=color, nl=False, bold=True)
        typer.echo(f"{r.name.ljust(width)}  {r.detail}")
        if r.remediation and r.status != PASS:
            typer.echo(f"        {' ' * width}  → {r.remediation}")


def doctor(
    namespace: str = typer.Option(
        "default",
        "--namespace",
        "-n",
        help="Namespace boxxkite's sandbox pods will run in.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit non-zero on any check that isn't PASS, not just on failures.",
    ),
) -> None:
    """Check whether the current kubeconfig context's cluster can actually
    enforce boxxkite's isolation model, before you `helm install`."""
    import asyncio

    asyncio.run(_run(namespace=namespace, strict=strict))


async def _run(*, namespace: str, strict: bool) -> None:
    from kubernetes_asyncio import client

    from ..k8s_auth import build_kubernetes_api_client, load_kubernetes_config
    from .errors import CliError

    try:
        source = await load_kubernetes_config()
    except Exception as exc:
        raise CliError(
            f"Could not load a Kubernetes config ({exc}). `boxxkite doctor` checks a real "
            "cluster -- set a kubeconfig context, or run it inside the cluster."
        ) from exc

    typer.echo(f"Checking cluster from {source}, namespace {namespace!r}\n")

    api = build_kubernetes_api_client()
    try:
        results = await run_cluster_checks(
            version_api=client.VersionApi(api),
            core_api=client.CoreV1Api(api),
            networking_api=client.NetworkingV1Api(api),
            apps_api=client.AppsV1Api(api),
            apiext_api=client.ApiextensionsV1Api(api),
            authz_api=client.AuthorizationV1Api(api),
            namespace=namespace,
        )
    finally:
        await api.close()

    render_results(results)

    failures = [r for r in results if r.status == FAIL]
    warnings = [r for r in results if r.status == WARN]
    typer.echo(
        f"\n{len(results) - len(failures) - len(warnings)} passed, "
        f"{len(warnings)} warning(s), {len(failures)} failure(s)."
    )

    if failures or (strict and warnings):
        raise CliError(
            "Cluster is not ready for boxxkite; fix the items above before installing."
            if failures
            else "--strict: warnings above are being treated as failures."
        )
