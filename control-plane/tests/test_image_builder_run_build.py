"""KanikoJobBuildRunner.run_build against a mocked K8s API -- issue #80.

There is no live Kubernetes cluster available in this environment/CI, so
these tests exercise `run_build`/`_poll_job`/`_collect_success`/
`_collect_failure`/`_cleanup` against small in-memory fakes for
`BatchV1Api`/`CoreV1Api` (mirroring the existing `_FakeCoreApi` pattern
used for `SandboxManager` in `tests/test_manager.py` /
`tests/test_manager_volume_mounts.py`, adapted for the Job/ConfigMap/pod-log
calls this module actually makes). This proves the create -> poll ->
extract-digest-or-logs -> always-cleanup state machine is correct; it does
NOT prove the real Job spec actually builds/pushes an image on a real
cluster -- that still needs live-cluster verification (see
docs/DECLARATIVE-BUILDER-DESIGN.md).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from control_plane.config import settings
from control_plane.image_builder import (
    ImageScanOutputError,
    ImageScanTimeoutError,
    ImageScanUnavailableError,
    KanikoJobBuildRunner,
    _configmap_name_for,
    _extract_digest_from_pod,
    _job_name_for,
)

_VALID_DIGEST = "sha256:" + "a" * 64


async def _clean_scan(image_ref: str) -> dict:
    """Fake `scan_image` callable standing in for a real `trivy` invocation
    -- injected via `KanikoJobBuildRunner`'s `scan_image` seam so these tests
    exercise the create->poll->collect state machine without needing a real
    scanner binary or a reachable registry. Shaped exactly like
    `_summarize_trivy_results`' real return value."""
    return {
        "critical": 0,
        "high": 0,
        "medium": 1,
        "low": 2,
        "unknown": 0,
        "total": 3,
        "scanner": "trivy",
        "scanned": True,
        "findings": [{"id": "CVE-2024-0001", "severity": "medium", "package": "libfoo", "installed_version": "1.0",
                      "fixed_version": "1.1"}],
    }


async def _infected_scan(image_ref: str) -> dict:
    return {
        "critical": 0,
        "high": 2,
        "medium": 0,
        "low": 0,
        "unknown": 0,
        "total": 2,
        "scanner": "trivy",
        "scanned": True,
        "findings": [
            {"id": "CVE-2024-9999", "severity": "high", "package": "libbar", "installed_version": "2.0",
             "fixed_version": "2.1"},
        ],
    }


def _job(*, succeeded=0, failed=0):
    return SimpleNamespace(status=SimpleNamespace(succeeded=succeeded, failed=failed))


def _pod_with_message(message: str | None, *, name: str = "image-build-pod-abcde"):
    if message is None:
        container_status = SimpleNamespace(state=SimpleNamespace(terminated=None))
    else:
        container_status = SimpleNamespace(state=SimpleNamespace(terminated=SimpleNamespace(message=message)))
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(container_statuses=[container_status]),
    )


class _FakeBatchApi:
    def __init__(self, job_responses=None):
        self._job_responses = list(job_responses or [])
        self.create_calls: list[dict] = []
        self.read_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    async def create_namespaced_job(self, *, namespace, body):
        self.create_calls.append({"namespace": namespace, "body": body})

    async def read_namespaced_job(self, *, name, namespace):
        self.read_calls.append({"name": name, "namespace": namespace})
        response = self._job_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def delete_namespaced_job(self, *, name, namespace, body=None):
        self.delete_calls.append({"name": name, "namespace": namespace})


class _FakeCoreApi:
    def __init__(self, *, pod=None, logs=None):
        self._pod = pod
        self._logs = logs
        self.configmap_create_calls: list[dict] = []
        self.configmap_delete_calls: list[dict] = []
        self.list_pod_calls: list[dict] = []
        self.log_calls: list[dict] = []

    async def create_namespaced_config_map(self, *, namespace, body):
        self.configmap_create_calls.append({"namespace": namespace, "body": body})

    async def delete_namespaced_config_map(self, *, name, namespace):
        self.configmap_delete_calls.append({"name": name, "namespace": namespace})

    async def list_namespaced_pod(self, *, namespace, label_selector):
        self.list_pod_calls.append({"namespace": namespace, "label_selector": label_selector})
        return SimpleNamespace(items=[self._pod] if self._pod is not None else [])

    async def read_namespaced_pod_log(self, *, name, namespace, container, tail_lines):
        self.log_calls.append({"name": name, "namespace": namespace, "container": container})
        if self._logs is None:
            raise ApiException(status=404, reason="pod logs not found")
        return self._logs


def _build_kwargs(**overrides):
    kwargs = dict(
        image_id="9f2f5b2a-1111-4b2b-9c2c-abcdefabcdef",
        account_id="acct_1",
        base="boxkite-default",
        python_packages=["polars==1.9.0"],
        apt_packages=[],
    )
    kwargs.update(overrides)
    return kwargs


def test_job_name_and_configmap_name_are_deterministic_and_fit_k8s_label_limits():
    image_id = "9f2f5b2a-1111-4b2b-9c2c-abcdefabcdef"
    job_name = _job_name_for(image_id)
    configmap_name = _configmap_name_for(image_id)

    assert job_name == f"image-build-{image_id}"
    assert len(job_name) <= 63  # DNS-1123 label limit (used as a pod "job-name" label value)
    assert configmap_name == f"{job_name}-dockerfile"
    assert len(configmap_name) <= 253


def test_extract_digest_from_pod_returns_none_when_no_termination_message():
    assert _extract_digest_from_pod(_pod_with_message(None)) is None


def test_extract_digest_from_pod_rejects_malformed_message():
    assert _extract_digest_from_pod(_pod_with_message("not-a-digest")) is None


def test_extract_digest_from_pod_accepts_valid_digest():
    assert _extract_digest_from_pod(_pod_with_message(_VALID_DIGEST)) == _VALID_DIGEST


async def test_run_build_success_returns_completed_outcome_and_cleans_up():
    batch_api = _FakeBatchApi(job_responses=[_job(succeeded=0, failed=0), _job(succeeded=1, failed=0)])
    core_api = _FakeCoreApi(pod=_pod_with_message(_VALID_DIGEST))
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api, scan_image=_clean_scan)

    outcome = await runner.run_build(**_build_kwargs())

    assert outcome.status == "completed"
    assert outcome.digest == _VALID_DIGEST
    assert outcome.registry_ref is not None
    assert outcome.registry_ref.endswith(f"@{_VALID_DIGEST}")
    assert outcome.scan_result["scanned"] is True
    assert outcome.scan_result["critical"] == 0
    assert outcome.scan_result["high"] == 0

    # ConfigMap created before the Job, and both cleaned up unconditionally.
    assert len(core_api.configmap_create_calls) == 1
    assert len(batch_api.create_calls) == 1
    assert len(batch_api.delete_calls) == 1
    assert len(core_api.configmap_delete_calls) == 1

    # The submitted Job/ConfigMap bodies never leak the unit-test-only
    # "_boxkite_*" introspection keys into the real API call.
    job_body = batch_api.create_calls[0]["body"]
    configmap_body = core_api.configmap_create_calls[0]["body"]
    assert not any(k.startswith("_boxkite_") for k in job_body)
    assert not any(k.startswith("_boxkite_") for k in configmap_body)
    assert "polars==1.9.0" in configmap_body["data"]["Dockerfile"]


async def test_run_build_uses_configured_namespace(monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_NAMESPACE", "boxkite-builds")
    batch_api = _FakeBatchApi(job_responses=[_job(succeeded=1)])
    core_api = _FakeCoreApi(pod=_pod_with_message(_VALID_DIGEST))
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api, scan_image=_clean_scan)

    await runner.run_build(**_build_kwargs())

    assert core_api.configmap_create_calls[0]["namespace"] == "boxkite-builds"
    assert batch_api.create_calls[0]["namespace"] == "boxkite-builds"
    assert batch_api.read_calls[0]["namespace"] == "boxkite-builds"


async def test_run_build_job_failure_surfaces_log_tail_in_failure_reason():
    batch_api = _FakeBatchApi(job_responses=[_job(succeeded=0, failed=1)])
    core_api = _FakeCoreApi(pod=_pod_with_message(None), logs="ERROR: could not resolve package foo==1.2.3")
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api)

    outcome = await runner.run_build(**_build_kwargs())

    assert outcome.status == "failed"
    assert outcome.digest is None
    assert "Build Job failed" in outcome.failure_reason
    assert "could not resolve package foo==1.2.3" in outcome.failure_reason
    # Cleanup still ran even though the build failed.
    assert len(batch_api.delete_calls) == 1
    assert len(core_api.configmap_delete_calls) == 1


async def test_run_build_success_with_missing_digest_fails_safely():
    batch_api = _FakeBatchApi(job_responses=[_job(succeeded=1)])
    core_api = _FakeCoreApi(pod=_pod_with_message("garbage-not-a-digest"))
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api)

    outcome = await runner.run_build(**_build_kwargs())

    assert outcome.status == "failed"
    assert outcome.digest is None
    assert "no valid image digest" in outcome.failure_reason


async def test_run_build_times_out_and_cleans_up(monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_IMAGE_BUILD_WAIT_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(settings, "BOXKITE_IMAGE_BUILD_POLL_INTERVAL_SECONDS", 0.001)
    batch_api = _FakeBatchApi(job_responses=[_job(succeeded=0, failed=0)])
    core_api = _FakeCoreApi(pod=None, logs=None)
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api)

    outcome = await runner.run_build(**_build_kwargs())

    assert outcome.status == "failed"
    assert "timed out" in outcome.failure_reason
    assert len(batch_api.delete_calls) == 1
    assert len(core_api.configmap_delete_calls) == 1


async def test_run_build_configmap_creation_failure_never_creates_a_job():
    class _FailingConfigMapCoreApi(_FakeCoreApi):
        async def create_namespaced_config_map(self, *, namespace, body):
            raise ApiException(status=403, reason="forbidden")

    batch_api = _FakeBatchApi()
    core_api = _FailingConfigMapCoreApi()
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api)

    outcome = await runner.run_build(**_build_kwargs())

    assert outcome.status == "failed"
    assert "ConfigMap" in outcome.failure_reason
    assert len(batch_api.create_calls) == 0
    # Nothing to clean up: the ConfigMap create itself failed, and the Job
    # was never created -- cleanup for both is skipped in this path since
    # run_build returns before entering the try/finally cleanup block.
    assert len(batch_api.delete_calls) == 0


async def test_run_build_job_creation_failure_still_cleans_up_the_configmap():
    class _FailingJobBatchApi(_FakeBatchApi):
        async def create_namespaced_job(self, *, namespace, body):
            raise ApiException(status=409, reason="already exists")

    batch_api = _FailingJobBatchApi()
    core_api = _FakeCoreApi()
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api)

    outcome = await runner.run_build(**_build_kwargs())

    assert outcome.status == "failed"
    assert "build Job" in outcome.failure_reason
    assert len(core_api.configmap_create_calls) == 1
    # Cleanup still attempted for both, even though the Job never actually
    # got created (delete_namespaced_job on a nonexistent Job 404s, which
    # _cleanup treats as already-clean, not an error).
    assert len(batch_api.delete_calls) == 1
    assert len(core_api.configmap_delete_calls) == 1


async def test_run_build_cleanup_swallows_delete_errors_without_masking_outcome():
    class _FlakyBatchApi(_FakeBatchApi):
        async def delete_namespaced_job(self, *, name, namespace, body=None):
            raise ApiException(status=500, reason="internal error")

    batch_api = _FlakyBatchApi(job_responses=[_job(succeeded=1)])
    core_api = _FakeCoreApi(pod=_pod_with_message(_VALID_DIGEST))
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api, scan_image=_clean_scan)

    outcome = await runner.run_build(**_build_kwargs())

    # The build itself succeeded; a cleanup-time delete failure must not
    # turn that into a reported build failure.
    assert outcome.status == "completed"
    assert outcome.digest == _VALID_DIGEST


# ── Vulnerability scan gate (issue #150) ─────────────────────────────────
# `run_build`'s `scan_result` used to be hardcoded `{}` unconditionally, so
# every build trivially passed `_scan_gate` regardless of what was actually
# in the image. These tests exercise the real wiring -- `_collect_success`
# calling `self._scan_image` (injected here as a fake standing in for a real
# `trivy` invocation) and feeding its result into `_scan_gate` -- without
# needing a real scanner binary or a reachable registry.


async def test_run_build_rejected_when_scan_finds_high_severity_findings():
    batch_api = _FakeBatchApi(job_responses=[_job(succeeded=1)])
    core_api = _FakeCoreApi(pod=_pod_with_message(_VALID_DIGEST))
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api, scan_image=_infected_scan)

    outcome = await runner.run_build(**_build_kwargs())

    assert outcome.status == "rejected"
    assert outcome.digest is None
    assert outcome.registry_ref is None
    assert "high" in outcome.failure_reason
    assert outcome.scan_result["high"] == 2
    assert outcome.scan_result["findings"][0]["id"] == "CVE-2024-9999"
    # Cleanup still ran even though the build was rejected by the scan gate.
    assert len(batch_api.delete_calls) == 1
    assert len(core_api.configmap_delete_calls) == 1


async def test_run_build_fails_closed_when_scanner_unavailable_and_scan_required(monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_IMAGE_SCAN_REQUIRED", True)

    async def _unavailable_scan(image_ref: str) -> dict:
        raise ImageScanUnavailableError("trivy binary not found on PATH")

    batch_api = _FakeBatchApi(job_responses=[_job(succeeded=1)])
    core_api = _FakeCoreApi(pod=_pod_with_message(_VALID_DIGEST))
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api, scan_image=_unavailable_scan)

    outcome = await runner.run_build(**_build_kwargs())

    assert outcome.status == "failed"
    assert "scan could not be completed" in outcome.failure_reason
    assert outcome.digest is None
    assert outcome.registry_ref is None


async def test_run_build_fails_open_when_scanner_unavailable_and_scan_not_required(monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_IMAGE_SCAN_REQUIRED", False)

    async def _unavailable_scan(image_ref: str) -> dict:
        raise ImageScanUnavailableError("trivy binary not found on PATH")

    batch_api = _FakeBatchApi(job_responses=[_job(succeeded=1)])
    core_api = _FakeCoreApi(pod=_pod_with_message(_VALID_DIGEST))
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api, scan_image=_unavailable_scan)

    outcome = await runner.run_build(**_build_kwargs())

    # Fail OPEN: the build still completes, but the scan_result is
    # explicitly flagged as unscanned rather than looking like a clean pass.
    assert outcome.status == "completed"
    assert outcome.digest == _VALID_DIGEST
    assert outcome.scan_result["scanned"] is False
    assert "trivy binary not found" in outcome.scan_result["error"]


async def test_run_build_fails_closed_on_scan_timeout(monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_IMAGE_SCAN_REQUIRED", True)

    async def _timeout_scan(image_ref: str) -> dict:
        raise ImageScanTimeoutError(f"trivy scan of {image_ref!r} exceeded 300s")

    batch_api = _FakeBatchApi(job_responses=[_job(succeeded=1)])
    core_api = _FakeCoreApi(pod=_pod_with_message(_VALID_DIGEST))
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api, scan_image=_timeout_scan)

    outcome = await runner.run_build(**_build_kwargs())

    assert outcome.status == "failed"
    assert "scan could not be completed" in outcome.failure_reason


async def test_run_build_fails_closed_on_malformed_scanner_output(monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_IMAGE_SCAN_REQUIRED", True)

    async def _malformed_output_scan(image_ref: str) -> dict:
        raise ImageScanOutputError(f"trivy produced non-JSON output for {image_ref!r}")

    batch_api = _FakeBatchApi(job_responses=[_job(succeeded=1)])
    core_api = _FakeCoreApi(pod=_pod_with_message(_VALID_DIGEST))
    runner = KanikoJobBuildRunner(k8s_batch_api=batch_api, k8s_core_api=core_api, scan_image=_malformed_output_scan)

    outcome = await runner.run_build(**_build_kwargs())

    assert outcome.status == "failed"
    assert "scan could not be completed" in outcome.failure_reason


async def test_default_scan_image_invokes_real_trivy_scan(monkeypatch):
    """Unlike every other test in this file (which injects a fake
    `scan_image` to avoid needing a real scanner), this one exercises the
    *actual* default wiring end to end against the real, locally-installed
    `trivy` binary and a real, small public image -- proving the subprocess
    invocation/JSON-parsing path genuinely works, not just that the tests
    mock it correctly. Skipped if `trivy` isn't on PATH (e.g. CI images that
    don't have it installed) rather than failing the suite outright."""
    import shutil

    if shutil.which("trivy") is None:
        pytest.skip("trivy binary not available in this environment")

    runner = KanikoJobBuildRunner(k8s_batch_api=None, k8s_core_api=None)

    # alpine:3.19 is small, public, and (at the time this test was written)
    # has real HIGH-severity CVEs -- exercising both the "scan runs" and
    # "findings get summarized" paths against a real image, not a synthetic
    # empty result.
    result = await runner._default_scan_image("alpine:3.19")

    assert result["scanner"] == "trivy"
    assert result["scanned"] is True
    assert isinstance(result["critical"], int)
    assert isinstance(result["high"], int)
    assert result["total"] == sum(result[s] for s in ("critical", "high", "medium", "low", "unknown"))
    assert isinstance(result["findings"], list)
