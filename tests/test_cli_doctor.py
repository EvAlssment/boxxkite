"""Tests for `boxxkite doctor` (GitHub issue #119).

Every check takes its API object as an argument, so these exercise the real
check logic against fakes rather than a live cluster -- including the failure
paths, which is the half that actually matters for a preflight tool.
"""

from types import SimpleNamespace

import pytest

from boxxkite.cli import cmd_doctor
from boxxkite.cli.cmd_doctor import FAIL, PASS, WARN


class _Boom:
    """Any attribute call raises -- stands in for an API the cluster doesn't
    serve, or that the caller has no permission for."""

    def __init__(self, message: str = "nope"):
        self._message = message

    def __getattr__(self, _name):
        async def _raise(*args, **kwargs):
            raise RuntimeError(self._message)

        return _raise


def _version_api(major="1", minor="29"):
    return SimpleNamespace(get_code=_async_return(SimpleNamespace(major=major, minor=minor)))


def _async_return(value):
    async def _call(*args, **kwargs):
        return value

    return _call


def _named_items(names):
    return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name=n)) for n in names])


async def test_version_check_passes_on_a_supported_cluster():
    result = await cmd_doctor.check_server_version(_version_api(minor="29"))
    assert result.status == PASS
    assert "1.29" in result.detail


async def test_version_check_fails_below_the_psa_ga_floor():
    result = await cmd_doctor.check_server_version(_version_api(minor="21"))
    assert result.status == FAIL
    assert "Pod Security Admission" in result.remediation


async def test_version_check_tolerates_a_managed_providers_plus_suffix():
    """GKE/EKS report minor as e.g. "29+", which must not read as unparseable."""
    result = await cmd_doctor.check_server_version(_version_api(minor="29+"))
    assert result.status == PASS


async def test_unreachable_cluster_fails_the_first_check_rather_than_raising():
    result = await cmd_doctor.check_server_version(_Boom("connection refused"))
    assert result.status == FAIL
    assert result.name == "cluster-reachable"
    assert "connection refused" in result.detail


async def test_seccomp_check_fails_on_a_pre_1_19_cluster():
    assert (await cmd_doctor.check_seccomp_support(_version_api(minor="18"))).status == FAIL
    assert (await cmd_doctor.check_seccomp_support(_version_api(minor="25"))).status == PASS


async def test_networkpolicy_api_check_fails_when_the_api_is_absent():
    ok = await cmd_doctor.check_networkpolicy_api(
        SimpleNamespace(list_namespaced_network_policy=_async_return(_named_items([]))), "default"
    )
    assert ok.status == PASS

    absent = await cmd_doctor.check_networkpolicy_api(_Boom("404 not found"), "default")
    assert absent.status == FAIL


async def test_enforcement_check_recognizes_a_known_cni():
    apps = SimpleNamespace(
        list_namespaced_daemon_set=_async_return(_named_items(["kube-proxy", "calico-node"]))
    )
    result = await cmd_doctor.check_networkpolicy_enforcement(apps)
    assert result.status == PASS
    assert "calico-node" in result.detail
    # Never claim more than metadata actually proves.
    assert "not a live traffic test" in result.detail


async def test_enforcement_check_warns_when_no_enforcing_cni_is_found():
    """A CNI that serves the NetworkPolicy API but never enforces it fails
    open silently -- the whole reason this check exists."""
    apps = SimpleNamespace(
        list_namespaced_daemon_set=_async_return(_named_items(["kube-proxy", "kube-flannel-ds"]))
    )
    result = await cmd_doctor.check_networkpolicy_enforcement(apps)
    assert result.status == WARN
    assert "fail open" in result.remediation


async def test_psa_check_passes_on_restricted_and_warns_otherwise():
    def _ns(labels):
        return SimpleNamespace(read_namespace=_async_return(SimpleNamespace(metadata=SimpleNamespace(labels=labels))))

    restricted = await cmd_doctor.check_pod_security_admission(
        _ns({"pod-security.kubernetes.io/enforce": "restricted"}), "boxxkite"
    )
    assert restricted.status == PASS

    privileged = await cmd_doctor.check_pod_security_admission(
        _ns({"pod-security.kubernetes.io/enforce": "privileged"}), "boxxkite"
    )
    assert privileged.status == WARN

    unlabelled = await cmd_doctor.check_pod_security_admission(_ns({}), "boxxkite")
    assert unlabelled.status == WARN
    assert "pod-security-policy.yaml" in unlabelled.remediation


async def test_psa_check_handles_a_namespace_without_labels_at_all():
    no_labels = SimpleNamespace(
        read_namespace=_async_return(SimpleNamespace(metadata=SimpleNamespace(labels=None)))
    )
    result = await cmd_doctor.check_pod_security_admission(no_labels, "boxxkite")
    assert result.status == WARN


async def test_rbac_check_lists_exactly_the_missing_verbs():
    def _authz(allowed_for):
        async def _create(body):
            attrs = body["resource_attributes"]
            key = (attrs["resource"], attrs["verb"])
            return SimpleNamespace(status=SimpleNamespace(allowed=key in allowed_for))

        return SimpleNamespace(create_self_subject_access_review=_create)

    def _factory(*, namespace, resource, verb):
        return {"resource_attributes": {"namespace": namespace, "resource": resource, "verb": verb}}

    all_allowed = await cmd_doctor.check_rbac(
        _authz(set(cmd_doctor.REQUIRED_PERMISSIONS)), "boxxkite", _factory
    )
    assert all_allowed.status == PASS

    partial = await cmd_doctor.check_rbac(
        _authz({("pods", "get"), ("pods", "list")}), "boxxkite", _factory
    )
    assert partial.status == FAIL
    assert "create pods" in partial.detail
    assert "delete pods" in partial.detail
    assert "get pods" not in partial.detail


async def test_cert_manager_is_only_ever_a_warning():
    present = await cmd_doctor.check_cert_manager(
        SimpleNamespace(list_custom_resource_definition=_async_return(_named_items(["certificates.cert-manager.io"])))
    )
    assert present.status == PASS

    absent = await cmd_doctor.check_cert_manager(
        SimpleNamespace(list_custom_resource_definition=_async_return(_named_items(["widgets.example.com"])))
    )
    assert absent.status == WARN

    unreadable = await cmd_doctor.check_cert_manager(_Boom())
    assert unreadable.status == WARN


async def test_run_cluster_checks_reports_every_check_even_when_all_apis_fail():
    """A broken cluster must produce a full report, not a stack trace on the
    first failed call."""
    results = await cmd_doctor.run_cluster_checks(
        version_api=_Boom(),
        core_api=_Boom(),
        networking_api=_Boom(),
        apps_api=_Boom(),
        apiext_api=_Boom(),
        authz_api=_Boom(),
        namespace="boxxkite",
        review_factory=lambda **kw: kw,
    )
    assert len(results) == 7
    assert all(r.status in (PASS, WARN, FAIL) for r in results)
    assert any(r.status == FAIL for r in results)


def test_render_results_prints_remediation_only_for_non_passing_checks(capsys):
    cmd_doctor.render_results(
        [
            cmd_doctor.CheckResult("ok-check", PASS, "fine", "should not be printed"),
            cmd_doctor.CheckResult("bad-check", FAIL, "broken", "do the thing"),
        ]
    )
    out = capsys.readouterr().out
    assert "should not be printed" not in out
    assert "do the thing" in out


@pytest.mark.parametrize("minor,expected", [("29", 29), ("29+", 29), ("", None), ("v", None)])
def test_minor_version_parsing(minor, expected):
    assert cmd_doctor._parse_minor(minor) == expected
