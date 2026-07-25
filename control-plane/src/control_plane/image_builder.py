"""Declarative-builder build execution — docs/DECLARATIVE-BUILDER-DESIGN.md.

**Security boundary (read this before touching anything in this module):**

This is the ONLY place in boxkite that ever executes package-install code
(`pip`/`apk`/`apt` resolving and running third-party install-time code).
Every other trust boundary in this project keeps a live session pod's
`sandbox` container free of any package manager, by design (see
`deploy/sandbox.Dockerfile`'s "pip is REMOVED after installation" comment).
This module does not change that: it never runs inside, or alongside, a
live session pod. It is a strictly separate, one-shot, torn-down-immediately
build execution — see `ImageBuildRunner`/`KanikoJobBuildRunner` below.

Concretely, the isolation this module is built around:

1. **A one-shot builder Job, not a live pod.** `KanikoJobBuildRunner` builds
   a Kubernetes `Job` spec (see `deploy/image-builder-job.yaml` for the
   reference manifest kept in parity with this code) running rootless
   Kaniko — never Docker-in-Docker, never a mounted `docker.sock`. A live
   `docker.sock` connection is a full host-root escape primitive
   (`docker run --privileged -v /:/host`); this module never creates that
   primitive. The Job runs once, to completion or failure, and is deleted
   immediately after — there is no standing builder infrastructure an agent
   or attacker could ever reach mid-build.
2. **A separate NetworkPolicy scoped to package-registry egress only**
   (`deploy/image-builder-network-policy.yaml`), applied to the builder
   Job's pods specifically — never the default-deny posture every session
   sandbox pod gets, and never general internet egress. A live session pod
   never gains this egress; it only ever exists for the lifetime of a build
   Job's pod.
3. **No ambient credentials beyond a narrowly-scoped registry-push
   credential**, namespaced per account
   (`{BOXKITE_IMAGE_REGISTRY_PREFIX}/{account_id}/{image_id}`) — never the
   control plane's own database credentials, never a session pod's sidecar
   auth token, never cluster-admin RBAC. See
   `deploy/image-builder-rbac.yaml`.
4. **Every produced image is referenced by immutable digest, never a
   mutable tag** (`SandboxImage.digest`/`registry_ref`) — a pod spec
   created from a tag-only reference could have its target silently
   swapped after this module's scan gate already passed it. See
   `boxkite.manager._validate_image_ref`, which refuses anything that isn't
   `repo@sha256:<64-hex>`.
5. **A build's failed vulnerability scan is `rejected`, never silently
   promoted to `completed`.** `_scan_gate` below is the single place that
   decision is made; nothing else in this module or the routers layer is
   allowed to set `status="completed"` without going through it first. The
   scan itself is a real `trivy image` invocation against the just-pushed,
   digest-referenced image (`_run_trivy_scan`, invoked from
   `KanikoJobBuildRunner._collect_success`) -- not a placeholder. If the
   scanner itself can't run (binary missing, timeout, malformed output),
   `BOXKITE_IMAGE_SCAN_REQUIRED` (default `True`) decides whether that
   fails the build closed or is logged as a loud warning and let through
   unscanned -- see that setting's docstring in `config.py`.
6. **The pod's security context is never a function of the image.**
   `boxkite.manager._create_pod` applies identical `security_context`,
   resource limits, and network policy regardless of which image is
   referenced — this module has no ability to influence any of that; it
   only ever produces a `registry_ref` string.

This entire feature is off by default (`BOXKITE_IMAGE_BUILDER_ENABLED`) —
an operator must explicitly opt in.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from boxkite.k8s_auth import build_kubernetes_api_client, load_kubernetes_config
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from .config import settings
from .schemas import _NPM_PINNED_PACKAGE_RE, _PINNED_PACKAGE_RE

logger = logging.getLogger(__name__)

# Kaniko's `--digest-file=/dev/termination-log` writes exactly the
# produced image's digest (nothing else) -- validated with the same
# `sha256:<64-hex>` shape `boxkite.manager._validate_image_ref` requires
# for the `repo@sha256:<64-hex>` reference it's later embedded into, so a
# truncated/corrupted termination message can never silently become a
# "completed" build with a garbage digest.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Kaniko's build-context Dockerfile is delivered to the builder Job pod as
# a ConfigMap volume (see deploy/image-builder-job.yaml) mounted at
# `/build-context` -- the key below becomes the file name
# `--dockerfile=/build-context/Dockerfile` in build_job_spec's Kaniko args
# expects.
_DOCKERFILE_CONFIGMAP_KEY = "Dockerfile"


class UnknownBaseError(ValueError):
    """Raised when a `base` value has no entry in
    `settings.BOXKITE_BASE_IMAGE_REFS` -- guards against the schemas.py
    `Literal` and this config dict drifting out of sync (e.g. a new base
    added to one but not the other), rather than silently building `FROM`
    nothing or an empty string."""


def render_dockerfile(
    *,
    base: str,
    python_packages: list[str],
    apt_packages: list[str],
    npm_packages: list[str] | None = None,
) -> str:
    """Generates the actual build-context Dockerfile for a declarative-
    builder request -- the piece `deploy/image-builder-job.yaml` calls
    "an operational choice outside this reference manifest's scope." This
    is that choice, made concretely: layer the caller's pinned package
    lists on top of a pre-approved `base`'s own digest-pinned image.

    Re-validates every package spec against schemas.py's exact-version-pin
    regexes (`_PINNED_PACKAGE_RE`, `_NPM_PINNED_PACKAGE_RE`) even though
    `SandboxImageBuildRequest` already enforces this at the API boundary --
    this function is the one place that turns package names into a shell
    command, so it does not trust "already validated somewhere upstream"
    for something that becomes a `RUN` line.

    Both the default and minimal bases keep `deploy/sandbox.Dockerfile`'s
    "no package manager in the final image" invariant: `pip`/`apk`/`npm`
    are reinstalled only for the duration of this one layer, then removed
    again in the same `RUN`, exactly like the base images' own build
    already does for their own preinstalled packages. `npm_packages`
    defaults to `None` (treated as empty) rather than requiring every
    caller to pass `[]` -- this function predates the field and several
    call sites/tests still call it with just base/python_packages/apt_packages.
    """
    npm_packages = npm_packages or []
    base_image_ref = settings.BOXKITE_BASE_IMAGE_REFS.get(base)
    if base_image_ref is None:
        raise UnknownBaseError(
            f"No image reference configured for base {base!r} -- check "
            "BOXKITE_BASE_IMAGE_REFS_RAW/schemas.py's base Literal are in sync"
        )

    for pkg in (*python_packages, *apt_packages):
        if not _PINNED_PACKAGE_RE.match(pkg):
            raise ValueError(f"Package {pkg!r} is not exact-version pinned; refusing to template it into a RUN line")
    for pkg in npm_packages:
        if not _NPM_PINNED_PACKAGE_RE.match(pkg):
            raise ValueError(f"Package {pkg!r} is not exact-version pinned; refusing to template it into a RUN line")

    lines = [f"FROM {base_image_ref}", "USER root"]

    install_steps = []
    if apt_packages:
        # apt_packages is validated against _PINNED_PACKAGE_RE, the same
        # pip-style "name==version" pattern python_packages uses -- but
        # Alpine's `apk` pins versions with a single `=` ("name=version"),
        # not `==`. Templating the pip-style spec in verbatim would pass an
        # apk atom it can't resolve (a build failure, not a silent
        # floating-version install, but still not the pin it looks like).
        # Convert the separator here, once, in the one place that turns
        # these strings into a shell command.
        apk_atoms = [pkg.replace("==", "=", 1) for pkg in sorted(apt_packages)]
        install_steps.append("apk add --no-cache " + " ".join(apk_atoms))
    if python_packages:
        install_steps.append("apk add --no-cache py3.11-pip")
        install_steps.append(
            "python -m pip install --break-system-packages --no-cache-dir " + " ".join(sorted(python_packages))
        )
        install_steps.append("apk del py3.11-pip")
        install_steps.append("rm -rf /root/.cache/pip")
    if npm_packages:
        # Both bases keep Node.js itself in the runtime image (only npm is
        # removed) -- see deploy/sandbox.Dockerfile / sandbox-minimal.Dockerfile's
        # own "npm is REMOVED after installation; runtime only needs node"
        # comment. Global-install syntax matches
        # deploy/sandbox-claude-code.Dockerfile's hand-maintained equivalent.
        install_steps.append("apk add --no-cache npm")
        install_steps.append("npm install -g " + " ".join(sorted(npm_packages)))
        install_steps.append("apk del npm node-gyp || true")
        install_steps.append("rm -rf /root/.npm")

    if install_steps:
        lines.append("RUN set -eux; " + "; \\\n    ".join(install_steps))

    lines.append("USER sandbox")
    return "\n".join(lines) + "\n"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _job_name_for(image_id: str) -> str:
    """Deterministic Job name for a build. Uses the FULL `image_id` (a
    `uuid4()` string, per `routers/images.py`), not just a short prefix --
    a Job's pod gets named `<job-name>-<5-char-suffix>` and that generated
    pod name is also used as a `job-name` LABEL value, which Kubernetes
    caps at 63 characters, so this can't just be truncated to 8 characters
    the way build_job_spec's job_name used to be before run_build actually
    submitted anything to a real cluster -- two concurrent builds whose
    image_ids happened to share an 8-character prefix would have collided
    on Job name and 409'd. `image-build-` (12 chars) + a uuid4 (36 chars)
    = 48 chars, comfortably under the 63-char label-value limit with room
    left for the pod-name suffix."""
    return f"image-build-{image_id}"[:63].rstrip("-")


def _configmap_name_for(image_id: str) -> str:
    """ConfigMap holding the generated build-context Dockerfile for one
    build. Not label-length-constrained the way _job_name_for is (a
    ConfigMap name is never used to derive a pod name), so no truncation
    concern beyond the general 253-char Kubernetes object-name limit."""
    return f"{_job_name_for(image_id)}-dockerfile"[:253]


def build_configmap_spec(*, image_id: str, account_id: str, dockerfile_content: str) -> dict:
    """Returns a plain-dict ConfigMap spec holding the generated Dockerfile
    content, mirroring build_job_spec's/build_pvc_spec's "plain dict, not
    a `kubernetes.client` object" shape for the same reason: directly
    unit-testable without a live cluster. `KanikoJobBuildRunner.run_build`
    creates this BEFORE the Job (the Job's `build-context` volume mounts
    it by name -- see build_job_spec) and deletes it again once the build
    finishes, success or failure (see run_build's cleanup)."""
    configmap_name = _configmap_name_for(image_id)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": configmap_name,
            "labels": {
                "app": "boxkite-image-builder",
                "boxkite.dev/account-id": account_id,
                "boxkite.dev/image-id": image_id,
            },
        },
        "data": {_DOCKERFILE_CONFIGMAP_KEY: dockerfile_content},
        "_boxkite_configmap_name": configmap_name,
    }


def _strip_internal_keys(spec: dict) -> dict:
    """`build_job_spec`/`build_configmap_spec` embed extra top-level
    `_boxkite_*` keys for unit-test introspection (see their docstrings) --
    not part of the actual Kubernetes object schema. Strip them before
    handing the spec to a real K8s API call; sending them would rely on
    the API server silently pruning unrecognized top-level fields rather
    than this code being explicit about what it actually submits."""
    return {k: v for k, v in spec.items() if not k.startswith("_boxkite_")}


def cache_key_for(
    *, base: str, python_packages: list[str], apt_packages: list[str], npm_packages: list[str] | None = None
) -> str:
    """Deterministic cache key for a build spec -- sorted so package order
    in the request never affects cache hits, per the design doc's 24h
    build-cache requirement. Scoped to the requesting account by the
    repository layer (`SandboxImageRepository.find_cached_completed`), not
    here -- this function alone says nothing about WHO may reuse a hit."""
    payload = {
        "base": base,
        "python_packages": sorted(python_packages),
        "apt_packages": sorted(apt_packages),
        "npm_packages": sorted(npm_packages or []),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def cache_window_start(*, now: datetime | None = None) -> datetime:
    now = now or _utcnow()
    return now - timedelta(hours=settings.BOXKITE_IMAGE_BUILD_CACHE_HOURS)


@dataclass(frozen=True)
class BuildOutcome:
    """Result of one build+scan attempt. `status` is one of "completed",
    "failed", or "rejected" (never "queued"/"building"/"scanning" -- those
    are only ever intermediate states the caller records before/while
    awaiting this)."""

    status: str
    digest: str | None = None
    registry_ref: str | None = None
    scan_result: dict = field(default_factory=dict)
    failure_reason: str | None = None


class ImageBuildRunner(Protocol):
    """Backend-agnostic interface the build dispatcher depends on. Concrete
    implementations own the actual isolation boundary described in this
    module's docstring -- the dispatcher itself only sequences status
    updates around whatever this returns."""

    async def run_build(
        self,
        *,
        image_id: str,
        account_id: str,
        base: str,
        python_packages: list[str],
        apt_packages: list[str],
        npm_packages: list[str] | None = None,
    ) -> BuildOutcome: ...


def _registry_ref_for(*, account_id: str, image_id: str, digest: str) -> str:
    return f"{settings.BOXKITE_IMAGE_REGISTRY_PREFIX}/{account_id}/{image_id}@{digest}"


def _scan_gate(scan_result: dict) -> tuple[bool, str | None]:
    """The single decision point for "did this build pass its
    vulnerability-scan gate" -- see this module's docstring point 5. Blocks
    on any severity in BOXKITE_IMAGE_SCAN_BLOCK_SEVERITIES being present
    with a non-zero count. Returns (passed, reason_if_not)."""
    blocked = settings.BOXKITE_IMAGE_SCAN_BLOCK_SEVERITIES
    for severity in blocked:
        if int(scan_result.get(severity, 0) or 0) > 0:
            return False, f"vulnerability scan found {scan_result.get(severity)} {severity}-severity issue(s)"
    return True, None


class ImageScanError(RuntimeError):
    """Base class for "the scanner itself did not produce a usable result" --
    distinct from "the scanner ran and found vulnerabilities" (that's a
    normal, successful `scan_result` with non-zero severity counts, handled
    entirely by `_scan_gate`). Every subclass here means `_run_trivy_scan`
    could not tell us anything about the image at all."""


class ImageScanUnavailableError(ImageScanError):
    """The `trivy` binary is missing, or failed to execute at all (e.g. not
    on PATH, or exited non-zero for a reason unrelated to the image itself --
    a registry-auth failure, a corrupt local vulnerability DB, etc.)."""


class ImageScanTimeoutError(ImageScanError):
    """The scan subprocess did not finish within BOXKITE_IMAGE_SCAN_TIMEOUT_SECONDS."""


class ImageScanOutputError(ImageScanError):
    """The scanner exited zero but its stdout wasn't the JSON shape this
    module knows how to summarize -- a Trivy version/output-format drift, not
    an image-specific finding."""


# Every severity trivy can report, lowercased -- used both as the fixed
# `--severity` argument (we always want full counts back, independent of
# which of these BOXKITE_IMAGE_SCAN_BLOCK_SEVERITIES actually blocks a
# build) and as the key set _scan_gate's severity lookups index into.
_TRIVY_SEVERITIES = ("critical", "high", "medium", "low", "unknown")

# Cap on how many individual CVE findings get embedded in the persisted
# scan_result/API response -- an image with hundreds of low-severity findings
# shouldn't balloon the DB row or the response payload; the severity counts
# already carry the actionable "should this build be rejected" signal.
_MAX_SURFACED_SCAN_FINDINGS = 50


def _summarize_trivy_results(payload: dict) -> dict:
    """Reduces a full `trivy image --format json` payload down to the shape
    `_scan_gate` expects (lowercase severity name -> count) plus a bounded
    list of individual findings for display -- never the raw payload
    verbatim, which can be megabytes for an image with a large OS package
    set and isn't a shape any caller of `_scan_gate` should need to know
    about."""
    counts = {severity: 0 for severity in _TRIVY_SEVERITIES}
    findings: list[dict] = []
    for result in payload.get("Results") or []:
        for vuln in result.get("Vulnerabilities") or []:
            severity = str(vuln.get("Severity") or "unknown").lower()
            if severity not in counts:
                severity = "unknown"
            counts[severity] += 1
            if len(findings) < _MAX_SURFACED_SCAN_FINDINGS:
                findings.append(
                    {
                        "id": vuln.get("VulnerabilityID"),
                        "severity": severity,
                        "package": vuln.get("PkgName"),
                        "installed_version": vuln.get("InstalledVersion"),
                        "fixed_version": vuln.get("FixedVersion") or None,
                    }
                )
    return {
        **counts,
        "total": sum(counts.values()),
        "scanner": "trivy",
        "scanned": True,
        "findings": findings,
    }


async def _run_trivy_scan(image_ref: str, *, timeout_seconds: float) -> dict:
    """Runs `trivy image --format json` against `image_ref` (the just-pushed,
    digest-referenced image) and returns a `_scan_gate`-shaped summary.
    Raises one of the `ImageScanError` subclasses above -- never returns a
    partial/best-effort dict -- if the scanner itself couldn't produce a
    trustworthy result; the caller (`KanikoJobBuildRunner._collect_success`)
    is the one place that decides what an unusable scan means for the build
    (BOXKITE_IMAGE_SCAN_REQUIRED)."""
    trivy_path = shutil.which("trivy")
    if trivy_path is None:
        raise ImageScanUnavailableError("trivy binary not found on PATH")

    cmd = [
        trivy_path,
        "image",
        "--quiet",
        "--format",
        "json",
        "--severity",
        ",".join(s.upper() for s in _TRIVY_SEVERITIES),
        "--timeout",
        f"{max(1, int(timeout_seconds))}s",
        image_ref,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ImageScanUnavailableError(f"failed to execute trivy: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ImageScanTimeoutError(f"trivy scan of {image_ref!r} exceeded {timeout_seconds:.0f}s") from exc

    if proc.returncode != 0:
        stderr_tail = stderr.decode("utf-8", "replace")[-2000:]
        raise ImageScanUnavailableError(f"trivy exited {proc.returncode} scanning {image_ref!r}: {stderr_tail}")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ImageScanOutputError(f"trivy produced non-JSON output for {image_ref!r}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ImageScanOutputError(f"trivy produced a non-object JSON top level for {image_ref!r}")

    return _summarize_trivy_results(payload)


class KanikoJobBuildRunner:
    """Real build execution: a rootless-Kaniko Kubernetes Job per build,
    scoped registry-push credential, package-registry-only egress. See this
    module's top docstring for the full isolation model and
    `deploy/image-builder-job.yaml` / `deploy/image-builder-network-policy.yaml`
    for the reference manifests this class's Job spec must stay in parity
    with (mirroring how `boxkite.manager`/`deploy/pod-template.yaml` stay in
    parity today, per `tests/test_pod_template_parity.py`).

    `run_build` (submit Job + ConfigMap, poll to completion/failure/timeout,
    extract the produced digest, surface logs on failure, and always clean
    up the Job/ConfigMap afterwards) is exercised in this repo's test suite
    ONLY against a mocked `BatchV1Api`/`CoreV1Api` (`test_image_builder_run_build.py`)
    -- there is no live Kubernetes API available in this environment, so
    the real create-Job/poll/read-log/delete-Job round trip against an
    actual cluster has NOT been verified here. `k8s_batch_api`/
    `k8s_core_api` are the seam those tests inject fakes through; in
    production (`RUNTIME_MODE=k8s`, via `deps.get_image_build_runner`) they
    are left `None` and lazily initialized from the ambient cluster config
    the first time `run_build` runs, reusing `boxkite.k8s_auth` (the same
    in-cluster/kubeconfig loading `SandboxManager` itself uses).

    `scan_image` is the same kind of seam for the vulnerability scan
    (`_collect_success` calls it with the just-pushed `registry_ref`) -- left
    `None` in production so it lazily defaults to a real `_run_trivy_scan`
    call, and injected directly by tests
    (`test_image_builder_run_build.py`'s scan-gate tests) so they don't need
    a real `trivy` binary or a reachable registry to exercise the
    completed/rejected/scan-error branches.
    """

    def __init__(self, k8s_batch_api=None, k8s_core_api=None, scan_image=None):
        self._k8s_batch_api = k8s_batch_api
        self._k8s_core_api = k8s_core_api
        self._init_lock = asyncio.Lock()
        self._scan_image: Callable[[str], Awaitable[dict]] = scan_image or self._default_scan_image

    async def _default_scan_image(self, image_ref: str) -> dict:
        return await _run_trivy_scan(image_ref, timeout_seconds=settings.BOXKITE_IMAGE_SCAN_TIMEOUT_SECONDS)

    async def _ensure_k8s_clients(self) -> tuple["k8s_client.BatchV1Api", "k8s_client.CoreV1Api"]:
        """Lazily initializes real API clients from ambient cluster config
        the first time this runner is actually used -- mirrors
        `SandboxManager._init_k8s`'s lazy-init-under-lock shape. Tests (and
        any caller that constructs this with explicit fakes) skip this
        entirely: both attributes are already set, so this returns them
        immediately without ever touching `boxkite.k8s_auth`."""
        if self._k8s_batch_api is not None and self._k8s_core_api is not None:
            return self._k8s_batch_api, self._k8s_core_api
        async with self._init_lock:
            if self._k8s_batch_api is not None and self._k8s_core_api is not None:
                return self._k8s_batch_api, self._k8s_core_api
            config_source = await load_kubernetes_config()
            logger.info(f"[image_builder] Using {config_source} K8s config for KanikoJobBuildRunner")
            api_client = build_kubernetes_api_client()
            self._k8s_batch_api = k8s_client.BatchV1Api(api_client)
            self._k8s_core_api = k8s_client.CoreV1Api(api_client)
            return self._k8s_batch_api, self._k8s_core_api

    def build_job_spec(
        self,
        *,
        image_id: str,
        account_id: str,
        base: str,
        python_packages: list[str],
        apt_packages: list[str],
        npm_packages: list[str] | None = None,
    ) -> dict:
        """Returns a plain-dict Job spec (not a `kubernetes.client` object)
        so this method — and therefore the Job shape itself — is directly
        unit-testable without a `kubernetes` client installed/mocked. The
        real `run_build` below would feed this to
        `BatchV1Api.create_namespaced_job`.

        Load-bearing shape decisions, each mirrored in
        `deploy/image-builder-job.yaml`:
        - `restartPolicy: Never`, `backoffLimit: 0` -- a build either
          succeeds once or is reported failed; it is never silently retried
          with a different, unreviewed package resolution.
        - `automountServiceAccountToken: false` plus a dedicated,
          narrowly-scoped `serviceAccountName` -- no ambient cluster-admin
          or control-plane credentials reach this pod.
        - `securityContext.runAsNonRoot: true` -- rootless Kaniko, never a
          privileged builder or a mounted `docker.sock`.
        - No `hostNetwork`/`hostPID`/`hostIPC`, no privileged escalation.
        """
        job_name = _job_name_for(image_id)
        configmap_name = _configmap_name_for(image_id)
        registry_ref = f"{settings.BOXKITE_IMAGE_REGISTRY_PREFIX}/{account_id}/{image_id}"
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "labels": {
                    "app": "boxkite-image-builder",
                    "boxkite.dev/account-id": account_id,
                    "boxkite.dev/image-id": image_id,
                },
            },
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 300,
                # Wall-clock cap on the whole build -- a pathological
                # package's build-time hook has no other bound once resource
                # limits alone don't stop a CPU/IO-bound (not memory-bound)
                # hang. Kubernetes kills the pod once this elapses regardless
                # of the container's own resource usage.
                "activeDeadlineSeconds": settings.BOXKITE_IMAGE_BUILD_TIMEOUT_SECONDS,
                "template": {
                    "metadata": {
                        "labels": {"app": "boxkite-image-builder"},
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "serviceAccountName": "boxkite-image-builder",
                        "containers": [
                            {
                                "name": "kaniko",
                                # Digest-pinned, not a mutable tag -- see this
                                # module's docstring point 4 and SECURITY.md's
                                # pandoc/Chrome-for-Testing digest-pinning
                                # discipline, now applied here too. Re-derive
                                # with `docker buildx imagetools inspect
                                # gcr.io/kaniko-project/executor:<new-tag>`
                                # when bumping the Kaniko version.
                                "image": (
                                    "gcr.io/kaniko-project/executor:latest"
                                    "@sha256:4e7a52dd1f14872430652bb3b027405b8dfd17c4538751c620ac005741ef9698"
                                ),
                                "args": [
                                    "--dockerfile=/build-context/Dockerfile",
                                    "--context=dir:///build-context",
                                    f"--destination={registry_ref}",
                                    "--digest-file=/dev/termination-log",
                                ],
                                "securityContext": {
                                    "runAsNonRoot": True,
                                    "runAsUser": 1000,
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                # A build runs untrusted caller-supplied
                                # package names/versions through a real
                                # container build -- the same request/limit
                                # discipline resource_config.py applies to
                                # every sandbox/sidecar container, so a
                                # pathological build can't exhaust node
                                # CPU/memory the way an unbounded builder
                                # container could.
                                "resources": {
                                    "requests": {
                                        "cpu": settings.BOXKITE_IMAGE_BUILD_CPU_REQUEST,
                                        "memory": settings.BOXKITE_IMAGE_BUILD_MEMORY_REQUEST,
                                    },
                                    "limits": {
                                        "cpu": settings.BOXKITE_IMAGE_BUILD_CPU_LIMIT,
                                        "memory": settings.BOXKITE_IMAGE_BUILD_MEMORY_LIMIT,
                                    },
                                },
                                # The build-context Dockerfile is delivered as a
                                # ConfigMap volume (created by run_build BEFORE
                                # this Job -- see build_configmap_spec), never a
                                # writable emptyDir a compromised build step
                                # could tamper with. readOnly: true so Kaniko
                                # itself can't be tricked into rewriting its own
                                # instructions mid-build.
                                "volumeMounts": [
                                    {
                                        "name": "build-context",
                                        "mountPath": "/build-context",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "build-context",
                                "configMap": {"name": configmap_name},
                            }
                        ],
                    },
                },
            },
            "_boxkite_build_spec": {
                "base": base,
                "python_packages": sorted(python_packages),
                "apt_packages": sorted(apt_packages),
                "npm_packages": sorted(npm_packages or []),
            },
            # The actual content for the `/build-context/Dockerfile` this
            # Job's Kaniko args reference -- see render_dockerfile's
            # docstring. deploy/image-builder-job.yaml's build-context
            # volume comment calls populating this "an operational choice
            # outside this reference manifest's scope"; this is that
            # generation step, made concrete and unit-testable here rather
            # than left as a TODO.
            "_boxkite_generated_dockerfile": render_dockerfile(
                base=base, python_packages=python_packages, apt_packages=apt_packages, npm_packages=npm_packages
            ),
        }

    async def run_build(
        self,
        *,
        image_id: str,
        account_id: str,
        base: str,
        python_packages: list[str],
        apt_packages: list[str],
        npm_packages: list[str] | None = None,
    ) -> BuildOutcome:
        """Submits the build-context ConfigMap + Job, polls until the Job
        succeeds/fails/times out, extracts the produced digest (success) or
        the tail of the Kaniko container's logs (failure), and ALWAYS
        deletes both the Job and the ConfigMap afterwards (success,
        failure, or an unexpected exception) -- there is no standing
        builder infrastructure left behind for any one build, matching
        this module's docstring point 1.

        NOTE on vulnerability scanning: `_collect_success` (below) runs a
        real `trivy image` scan against the just-pushed, digest-referenced
        image before deciding `completed` vs `rejected` -- see
        `_run_trivy_scan`/`self._scan_image` and `_scan_gate`.
        """
        batch_api, core_api = await self._ensure_k8s_clients()
        namespace = settings.SANDBOX_NAMESPACE

        job_spec = self.build_job_spec(
            image_id=image_id,
            account_id=account_id,
            base=base,
            python_packages=python_packages,
            apt_packages=apt_packages,
            npm_packages=npm_packages,
        )
        dockerfile_content = job_spec["_boxkite_generated_dockerfile"]
        configmap_spec = build_configmap_spec(
            image_id=image_id, account_id=account_id, dockerfile_content=dockerfile_content
        )
        job_name = job_spec["metadata"]["name"]
        configmap_name = configmap_spec["_boxkite_configmap_name"]

        try:
            await core_api.create_namespaced_config_map(
                namespace=namespace, body=_strip_internal_keys(configmap_spec)
            )
        except ApiException as exc:
            logger.error(f"[image_builder] failed to create build-context ConfigMap for {image_id}: {exc}")
            return BuildOutcome(
                status="failed",
                failure_reason=f"Failed to create build-context ConfigMap: {exc.reason or exc}",
            )

        try:
            try:
                await batch_api.create_namespaced_job(namespace=namespace, body=_strip_internal_keys(job_spec))
            except ApiException as exc:
                logger.error(f"[image_builder] failed to create build Job for {image_id}: {exc}")
                return BuildOutcome(
                    status="failed",
                    failure_reason=f"Failed to create build Job: {exc.reason or exc}",
                )

            return await self._poll_job(
                batch_api=batch_api,
                core_api=core_api,
                namespace=namespace,
                job_name=job_name,
                image_id=image_id,
                account_id=account_id,
            )
        finally:
            await self._cleanup(
                batch_api=batch_api,
                core_api=core_api,
                namespace=namespace,
                job_name=job_name,
                configmap_name=configmap_name,
            )

    async def _poll_job(
        self,
        *,
        batch_api,
        core_api,
        namespace: str,
        job_name: str,
        image_id: str,
        account_id: str,
    ) -> BuildOutcome:
        """Polls `read_namespaced_job` (not `read_namespaced_job_status` --
        deliberately, so this only needs RBAC on the `jobs` resource, not
        the separate `jobs/status` subresource) until `status.succeeded`,
        `status.failed`, or `BOXKITE_IMAGE_BUILD_WAIT_TIMEOUT_SECONDS` elapses.
        `backoffLimit: 0` on the Job (build_job_spec) means `status.failed`
        is set after exactly one failed attempt, never a retried one."""
        deadline = _utcnow() + timedelta(seconds=settings.BOXKITE_IMAGE_BUILD_WAIT_TIMEOUT_SECONDS)

        while True:
            try:
                job = await batch_api.read_namespaced_job(name=job_name, namespace=namespace)
            except ApiException as exc:
                logger.error(f"[image_builder] failed to read build Job status for {image_id}: {exc}")
                return BuildOutcome(
                    status="failed",
                    failure_reason=f"Failed to read build Job status: {exc.reason or exc}",
                )

            status = getattr(job, "status", None)
            if status is not None and (status.succeeded or 0) > 0:
                return await self._collect_success(
                    core_api=core_api,
                    namespace=namespace,
                    job_name=job_name,
                    image_id=image_id,
                    account_id=account_id,
                )
            if status is not None and (status.failed or 0) > 0:
                return await self._collect_failure(
                    core_api=core_api,
                    namespace=namespace,
                    job_name=job_name,
                    reason="Build Job failed",
                )

            if _utcnow() >= deadline:
                logger.error(
                    f"[image_builder] build {image_id} timed out after "
                    f"{settings.BOXKITE_IMAGE_BUILD_WAIT_TIMEOUT_SECONDS:.0f}s"
                )
                return await self._collect_failure(
                    core_api=core_api,
                    namespace=namespace,
                    job_name=job_name,
                    reason=f"Build timed out after {settings.BOXKITE_IMAGE_BUILD_WAIT_TIMEOUT_SECONDS:.0f}s",
                )

            await asyncio.sleep(settings.BOXKITE_IMAGE_BUILD_POLL_INTERVAL_SECONDS)

    async def _find_job_pod(self, *, core_api, namespace: str, job_name: str):
        """Kubernetes labels every pod a Job creates with `job-name=<job
        name>` automatically -- no need for this code to compute or track
        the generated pod name itself."""
        try:
            pods = await core_api.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={job_name}")
        except ApiException as exc:
            logger.warning(f"[image_builder] failed to list pods for build Job {job_name}: {exc}")
            return None
        items = getattr(pods, "items", None) or []
        return items[0] if items else None

    async def _collect_success(
        self,
        *,
        core_api,
        namespace: str,
        job_name: str,
        image_id: str,
        account_id: str,
    ) -> BuildOutcome:
        pod = await self._find_job_pod(core_api=core_api, namespace=namespace, job_name=job_name)
        digest = _extract_digest_from_pod(pod) if pod is not None else None
        if not digest:
            logger.error(
                f"[image_builder] build Job for {image_id} succeeded but no valid digest was "
                "found in its termination message"
            )
            return BuildOutcome(
                status="failed",
                failure_reason=(
                    "Build Job reported success but no valid image digest was found in its "
                    "termination log"
                ),
            )

        registry_ref = _registry_ref_for(account_id=account_id, image_id=image_id, digest=digest)
        scan_result, scan_failure_reason = await self._scan_built_image(registry_ref=registry_ref, image_id=image_id)
        if scan_failure_reason is not None:
            return BuildOutcome(status="failed", failure_reason=scan_failure_reason)

        passed, reason = _scan_gate(scan_result)
        if not passed:
            return BuildOutcome(status="rejected", scan_result=scan_result, failure_reason=reason)

        return BuildOutcome(status="completed", digest=digest, registry_ref=registry_ref, scan_result=scan_result)

    async def _scan_built_image(self, *, registry_ref: str, image_id: str) -> tuple[dict, str | None]:
        """Runs the vulnerability scan against the just-pushed image.
        Returns `(scan_result, None)` on a usable result -- either a real
        scan or, if the scanner couldn't run and
        `BOXKITE_IMAGE_SCAN_REQUIRED` is `False`, an explicitly
        `"scanned": False` placeholder that still passes through
        `_scan_gate` (deliberately, since it carries no severity counts).
        Returns `(_, failure_reason)` when `BOXKITE_IMAGE_SCAN_REQUIRED` is
        `True` (the default) and the scanner itself failed -- see that
        setting's docstring in config.py for why fail-CLOSED is the
        default: a scanner that silently can't run is exactly the
        "`scan_result: dict = {}` always trivially passes" gap this whole
        change exists to close."""
        try:
            return await self._scan_image(registry_ref), None
        except ImageScanError as exc:
            if settings.BOXKITE_IMAGE_SCAN_REQUIRED:
                logger.error(
                    f"[image_builder] vulnerability scan failed for build {image_id} ({registry_ref}): "
                    f"{exc}; failing the build closed (BOXKITE_IMAGE_SCAN_REQUIRED=true)"
                )
                return {}, f"Vulnerability scan could not be completed: {exc}"
            logger.warning(
                f"[image_builder] vulnerability scan failed for build {image_id} ({registry_ref}): "
                f"{exc}; BOXKITE_IMAGE_SCAN_REQUIRED=false, so this build proceeds UNSCANNED -- "
                "its vulnerability content has NOT been verified"
            )
            return {"scanned": False, "scanner": "trivy", "error": str(exc)}, None

    async def _collect_failure(self, *, core_api, namespace: str, job_name: str, reason: str) -> BuildOutcome:
        logs_tail = await self._read_build_logs(core_api=core_api, namespace=namespace, job_name=job_name)
        failure_reason = reason if not logs_tail else f"{reason}. Last build output:\n{logs_tail}"
        return BuildOutcome(status="failed", failure_reason=failure_reason)

    async def _read_build_logs(
        self, *, core_api, namespace: str, job_name: str, tail_lines: int = 200
    ) -> str | None:
        pod = await self._find_job_pod(core_api=core_api, namespace=namespace, job_name=job_name)
        if pod is None or pod.metadata is None:
            return None
        try:
            logs = await core_api.read_namespaced_pod_log(
                name=pod.metadata.name,
                namespace=namespace,
                container="kaniko",
                tail_lines=tail_lines,
            )
        except ApiException as exc:
            logger.warning(f"[image_builder] failed to read build logs for Job {job_name}: {exc}")
            return None
        if not logs:
            return None
        # Cap the amount of raw build log embedded in a DB-persisted
        # failure_reason -- a verbose package-manager failure could
        # otherwise dump an unbounded amount of text into the row.
        return logs[-4000:]

    async def _cleanup(self, *, batch_api, core_api, namespace: str, job_name: str, configmap_name: str) -> None:
        """Always runs (called from run_build's `finally`) -- this is the
        "deprovision" half of this class: no builder Job, its pod, or its
        ConfigMap outlives one build, success or failure. `propagation_policy
        ="Foreground"` on the Job delete also removes its pod; the Job's own
        `ttlSecondsAfterFinished` (build_job_spec) would eventually garbage
        collect it anyway, but this makes cleanup immediate rather than
        relying on that background GC, and it's the only thing that ever
        cleans up the ConfigMap (Job GC does not cascade to it)."""
        try:
            await batch_api.delete_namespaced_job(
                name=job_name,
                namespace=namespace,
                body=k8s_client.V1DeleteOptions(propagation_policy="Foreground"),
            )
        except ApiException as exc:
            if exc.status != 404:
                logger.warning(f"[image_builder] failed to delete build Job {job_name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - cleanup must never raise past run_build
            logger.warning(f"[image_builder] failed to delete build Job {job_name}: {exc}")

        try:
            await core_api.delete_namespaced_config_map(name=configmap_name, namespace=namespace)
        except ApiException as exc:
            if exc.status != 404:
                logger.warning(f"[image_builder] failed to delete ConfigMap {configmap_name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - cleanup must never raise past run_build
            logger.warning(f"[image_builder] failed to delete ConfigMap {configmap_name}: {exc}")


def _extract_digest_from_pod(pod) -> str | None:
    """Kaniko's `--digest-file=/dev/termination-log` writes the produced
    digest as the container's termination message (Kubernetes surfaces
    `/dev/termination-log`'s content as
    `pod.status.container_statuses[i].state.terminated.message` when
    `terminationMessagePolicy` is the default `File`). Returns `None`
    (never a partial/garbage string) unless the message matches
    `_DIGEST_RE` exactly."""
    status = getattr(pod, "status", None)
    container_statuses = getattr(status, "container_statuses", None) if status is not None else None
    if not container_statuses:
        return None
    for container_status in container_statuses:
        state = getattr(container_status, "state", None)
        terminated = getattr(state, "terminated", None) if state is not None else None
        message = getattr(terminated, "message", None) if terminated is not None else None
        if not message:
            continue
        candidate = message.strip()
        if _DIGEST_RE.match(candidate):
            return candidate
    return None


class FakeImageBuildRunner:
    """Deterministic in-process stand-in for tests and for
    RUNTIME_MODE=compose (no cluster to run a real builder Job against).
    Simulates a successful build with a synthetic digest, and reproduces
    the scan-gate policy exactly (so a package list containing the literal
    string "malware" fails the scan gate in tests, exercising the
    "rejected" path without needing a real scanner)."""

    async def run_build(
        self,
        *,
        image_id: str,
        account_id: str,
        base: str,
        python_packages: list[str],
        apt_packages: list[str],
        npm_packages: list[str] | None = None,
    ) -> BuildOutcome:
        all_packages = [*python_packages, *apt_packages, *(npm_packages or [])]
        scan_result = {
            "critical": sum(1 for p in all_packages if "malware" in p.lower()),
            "high": 0,
            "policy": "trivy-equivalent",
        }
        passed, reason = _scan_gate(scan_result)
        if not passed:
            return BuildOutcome(status="rejected", scan_result=scan_result, failure_reason=reason)

        digest_source = cache_key_for(
            base=base, python_packages=python_packages, apt_packages=apt_packages, npm_packages=npm_packages
        )
        digest = "sha256:" + hashlib.sha256(f"{image_id}:{digest_source}".encode()).hexdigest()
        registry_ref = _registry_ref_for(account_id=account_id, image_id=image_id, digest=digest)
        return BuildOutcome(status="completed", digest=digest, registry_ref=registry_ref, scan_result=scan_result)


async def dispatch_build(
    *,
    repo,
    runner: ImageBuildRunner,
    image_id: str,
    account_id: str,
    base: str,
    python_packages: list[str],
    apt_packages: list[str],
    npm_packages: list[str] | None = None,
) -> None:
    """Drives one image row through queued -> building -> scanning ->
    completed/failed/rejected, calling `runner.run_build` for the actual
    isolated build+scan work. Never sets `completed` directly -- only
    `BuildOutcome.status == "completed"` (already scan-gated inside the
    runner) does that.

    `repo` is a `SandboxImageRepository` bound to its own DB session --
    callers (`routers/images.py`) construct this dispatch to run as a
    background task with a session independent of the request's own, so a
    build in flight isn't tied to the lifetime of the HTTP request that
    triggered it (per the design doc's "builds are asynchronous" requirement).
    """
    try:
        await repo.mark_building(image_id=image_id)
        outcome = await runner.run_build(
            image_id=image_id,
            account_id=account_id,
            base=base,
            python_packages=python_packages,
            apt_packages=apt_packages,
            npm_packages=npm_packages,
        )
        await repo.mark_scanning(image_id=image_id)

        if outcome.status == "completed":
            assert outcome.digest and outcome.registry_ref
            await repo.mark_completed(
                image_id=image_id,
                digest=outcome.digest,
                registry_ref=outcome.registry_ref,
                scan_result=outcome.scan_result,
            )
        elif outcome.status == "rejected":
            await repo.mark_rejected(
                image_id=image_id,
                failure_reason=outcome.failure_reason or "Image build rejected by scan gate",
                scan_result=outcome.scan_result,
            )
        else:
            await repo.mark_failed(
                image_id=image_id, failure_reason=outcome.failure_reason or "Image build failed"
            )
    except Exception as exc:  # noqa: BLE001 - this is a background task; must not raise into the event loop
        logger.error("[image_builder] build %s failed with an unexpected error: %s", image_id, exc)
        try:
            await repo.mark_failed(image_id=image_id, failure_reason=f"Unexpected build error: {exc}")
        except Exception:
            logger.error("[image_builder] failed to even record build failure for %s", image_id)
