"""
Sandbox Manager - K8s Pod Lifecycle & HTTP Routing

This is the main entry point for sandbox operations in the new architecture.
Manages K8s pod lifecycle and routes all tool calls to the sidecar HTTP API.

Architecture:
    Backend (SandboxManager)
        -> HTTP :8080 (sidecar)
            -> nsenter/docker-exec (bash_tool only)
            -> Direct file I/O (file_create, view, read_image, str_replace)
            -> S3 sync (present_files, flush)

Key Features:
    - Pod lifecycle management (create, configure, destroy)
    - HTTP-based tool execution (no kubectl exec)
    - Warm pool integration for fast session startup
    - Work item-scoped file persistence via S3
    - K8s pod labels/annotations as session store (survives backend restarts)
"""

from ._manager_config import *  # noqa: F401,F403  (re-export module-level config/constants/helpers)

from ._manager_metadata import MetadataMixin
from ._manager_sessions_lookup import SessionLookupMixin
from ._manager_tls_auth import TlsAuthMixin
from ._manager_http_client import HttpClientMixin
from ._manager_create import CreateSessionMixin
from ._manager_warm_pool import WarmPoolMixin
from ._manager_recovery import RecoveryMixin
from ._manager_proxy import SidecarProxyMixin
from ._manager_files_skills import FilesSkillsMixin
from ._manager_checkpoint import FullStateCheckpointMixin


# PodLifecycleMixin is defined inline (not extracted to a sibling module)
# because tests/test_pod_template_parity.py reads this file's source text
# and regexes for the sidecar V1Capabilities(add=[...], drop=[...]) block
# built in _create_pod; the pod-spec code must physically live here.
class PodLifecycleMixin:
    async def _create_k8s_session(
        self,
        organization_id: UUID,
        session_id: str,
        work_item_id: Optional[UUID] = None,
        upload_file_ids: Optional[list[str]] = None,
        size: str = DEFAULT_SANDBOX_SIZE,
        volume_size_limit: Optional[str] = None,
        lifetime_seconds: Optional[int] = None,
        restore_from_snapshot_id: Optional[str] = None,
        secret_grants: Optional[list[dict]] = None,
        secret_capability_token: Optional[str] = None,
        secrets_control_plane_url: Optional[str] = None,
        image_ref: Optional[str] = None,
        volume_mounts: Optional[list[dict]] = None,
        mcp_connection_grants: Optional[list[dict]] = None,
        browser_enabled: bool = False,
        gpu_count: Optional[int] = None,
    ) -> dict:
        """Create session in K8s mode."""
        import time as _time
        import uuid as _uuid
        _t0 = _time.monotonic()

        await self._init_k8s()

        pod_name: Optional[str] = None
        pod_ip: Optional[str] = None
        # Storage/lifetime can't be pre-warmed (an emptyDir's sizeLimit can't be
        # resized on a running pod), so those always force a fresh pod. Size
        # CAN be pre-warmed -- warm_pool.py maintains a separate warm sub-pool
        # per size (see WARM_POOL_SIZE_TARGETS) -- so a non-default size still
        # tries a claim first, just from that size's sub-pool.
        # image_ref also forces a cold create: per
        # docs/DECLARATIVE-BUILDER-DESIGN.md section 4's recommendation (a),
        # WarmPoolManager only ever pre-warms pods from the operator's
        # default SANDBOX_IMAGE -- there is no pre-created pod to claim for
        # any custom image, so claiming a warm pod here would silently
        # ignore the caller's requested image_ref and hand back a pod
        # running the wrong image entirely.
        forces_cold_create = (
            volume_size_limit is not None
            or lifetime_seconds is not None
            or image_ref is not None
            or bool(volume_mounts)
            # No pre-warmed pod has a GPU resource reserved
            # (docs/GPU-SUPPORT-SCOPING.md) -- claiming one here would
            # silently hand back a pod with no GPU at all.
            or gpu_count is not None
        )

        # Claim a warm pod via K8s API.
        if not pod_name and not forces_cold_create:
            claimed = await self._claim_warm_pod_via_k8s(size=size)
            if claimed:
                pod_name, pod_ip = claimed
                logger.info(f"[SandboxManager] Claimed warm pod via K8s: {pod_name}")

        _t_claimed = _time.monotonic()
        logger.info(f"[TIMING] sandbox_pod_claim: {(_t_claimed - _t0)*1000:.0f}ms (pod={pod_name or 'none'})")

        # Create new pod if no warm pod available
        if not pod_name:
            # Handle None work_item_id (e.g., chat without work item)
            work_item_suffix = work_item_id.hex[:8] if work_item_id else "standalone"
            # Add a random suffix to avoid name collisions with pods that are still terminating.
            pod_name = f"sandbox-{session_id[:8]}-{work_item_suffix}-{_uuid.uuid4().hex[:4]}"
            pod_ip = await self._create_pod(
                pod_name,
                session_id,
                organization_id,
                work_item_id,
                size=size,
                volume_size_limit=volume_size_limit,
                active_deadline_seconds=lifetime_seconds,
                image_ref=image_ref,
                volume_mounts=volume_mounts,
                gpu_count=gpu_count,
            )
            logger.info(f"[SandboxManager] Created new pod {pod_name}")
            _t_pod_created = _time.monotonic()
            logger.info(f"[TIMING] sandbox_pod_create: {(_t_pod_created - _t_claimed)*1000:.0f}ms")

        # This create either drained a warm pod (claim) or found none (cold).
        # Either way the warm pool is now below target, so nudge a top-up from
        # the request path -- the timer-driven replenish loop stalls under a
        # CPU-throttling serverless runtime (see schedule_replenish), leaving
        # the pool permanently drained and every subsequent create cold. Best
        # effort and non-blocking: it must never add latency to this create or
        # fail it.
        try:
            from .warm_pool import get_warm_pool

            warm_pool = await get_warm_pool()
            if warm_pool is not None:
                warm_pool.schedule_replenish()
        except Exception as e:
            logger.warning(f"[SandboxManager] Warm-pool replenish nudge failed (non-fatal): {e}")

        s3_prefix = self._build_storage_prefix(organization_id, session_id, work_item_id)
        normalized_upload_file_ids = upload_file_ids or []

        http_client = self._get_http_client(pod_name, pod_ip)
        _t_pre_configure = _time.monotonic()
        response = await http_client.post("/configure", json={
            "session_id": session_id,
            "organization_id": str(organization_id) if organization_id else None,
            "work_item_id": str(work_item_id) if work_item_id else None,
            "storage_prefix": s3_prefix,
            "upload_file_ids": normalized_upload_file_ids or None,
            **_secret_configure_fields(
                secret_grants, secret_capability_token, secrets_control_plane_url
            ),
        })
        response.raise_for_status()
        config_result = response.json()
        _t_configured = _time.monotonic()
        logger.info(f"[TIMING] sidecar_configure: {(_t_configured - _t_pre_configure)*1000:.0f}ms")

        identity_labels, identity_annotations = self._identity_labels_and_annotations(
            session_id=session_id,
            organization_id=organization_id,
            work_item_id=work_item_id,
        )

        # Patch pod with session metadata (K8s as source of truth)
        try:
            await self._k8s_core_api.patch_namespaced_pod(
                name=pod_name, namespace=SANDBOX_NAMESPACE,
                body={"metadata": {
                    "labels": {
                        "pool": "claimed",
                        "sandbox.boxkite.dev/status": "claimed",
                        **identity_labels,
                    },
                    "annotations": {
                        "sandbox.boxkite.dev/storage-prefix": s3_prefix,
                        "sandbox.boxkite.dev/upload-file-ids": json.dumps(normalized_upload_file_ids),
                        SAFE_TO_EVICT_ANNOTATION: "false",
                        RESTORED_FROM_SNAPSHOT_ANNOTATION: restore_from_snapshot_id or "",
                        **identity_annotations,
                    },
                }}
            )
        except Exception as e:
            logger.error(
                f"[SandboxManager] Failed to patch session metadata for {session_id} "
                f"on pod {pod_name}; cleaning up pod"
            )
            await self._close_http_client(pod_name)
            try:
                await self._delete_pod(pod_name)
            except Exception as cleanup_error:
                logger.error(
                    f"[SandboxManager] Cleanup failed after metadata patch error "
                    f"for pod {pod_name}: {cleanup_error}"
                )
            raise RuntimeError(
                f"Failed to persist session metadata for session {session_id}"
            ) from e

        # Provision/refresh this session's secrets-egress NetworkPolicy
        # (issue #74) -- no-op unless BOXKITE_SECRETS_NETWORK_POLICY_ENABLED
        # is set. Must run AFTER the label patch above (identity_labels
        # carries the exact "session-id" label value the policy's
        # podSelector matches) and covers both the cold-create and
        # warm-pod-claim paths, since both fall through to this same point.
        #
        # mcp_connection_grants (issues #116/#117, docs/OUTBOUND-MCP-DESIGN.md
        # §3) is the exact same {"name", "allowed_hosts"} shape as
        # secret_grants -- unioned into one list here rather than given its
        # own NetworkPolicy or its own sync/delete lifecycle. It is
        # deliberately NOT part of the /configure payload above: there is no
        # MCP-proxy transport yet for the sidecar to use it with (see the
        # design doc's §6 scope boundary), so this list only ever widens the
        # session's per-pod network-layer egress allowlist.
        await self._sync_secrets_egress_network_policy(
            pod_name,
            identity_labels["session-id"],
            [*(secret_grants or []), *(mcp_connection_grants or [])] or None,
        )

        # Provision/refresh this session's browser-egress NetworkPolicy
        # (docs/BROWSER-EXEC-DESIGN.md §3) -- no-op unless
        # BOXKITE_BROWSER_NETWORK_POLICY_ENABLED is set. Same ordering
        # requirement as the secrets-egress sync above (must run AFTER the
        # label patch, for the same podSelector-label reason), and always
        # called (even when browser_enabled=False) so a warm-pool-claimed
        # pod that previously had it enabled for a DIFFERENT tenant gets
        # that stale broad-egress rule deleted, not left standing.
        _warn_if_browser_enabled_without_network_policy(
            session_id, browser_enabled, BOXKITE_BROWSER_NETWORK_POLICY_ENABLED
        )
        await self._sync_browser_egress_network_policy(
            pod_name, identity_labels["session-id"], browser_enabled
        )

        logger.info(
            f"[SandboxManager] Session {session_id} configured, "
            f"prefetched {len(config_result.get('prefetched_files', []))} files"
        )
        logger.info(f"[TIMING] sandbox_create_session_total: {(_t_configured - _t0)*1000:.0f}ms")
        if pod_name and pod_ip:
            self._cache_session_endpoint(session_id, pod_name, pod_ip)
        await self._record_session_metadata(
            session_id,
            organization_id=organization_id,
            work_item_id=work_item_id,
            storage_prefix=s3_prefix,
            upload_file_ids=normalized_upload_file_ids,
        )

        return {"pod_name": pod_name}

    async def _create_pod(
        self,
        pod_name: str,
        session_id: str,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        size: str = DEFAULT_SANDBOX_SIZE,
        volume_size_limit: Optional[str] = None,
        active_deadline_seconds: Optional[int] = None,
        image_ref: Optional[str] = None,
        volume_mounts: Optional[list[dict]] = None,
        gpu_count: Optional[int] = None,
    ) -> str:
        """Create a new sandbox pod and wait for it to be ready.

        image_ref, if given, replaces SANDBOX_IMAGE for the `sandbox`
        container's `image` field ONLY -- see
        docs/DECLARATIVE-BUILDER-DESIGN.md section 5 ("the pod's security
        context must never be a function of the referenced image"). Every
        other field below (security_context, resources, volume mounts) is
        built identically regardless of image_ref; a future change that
        makes any of them conditional on the image is a bug, not a feature.

        volume_mounts, if given, adds one PersistentVolumeClaim-backed
        V1Volume/V1VolumeMount pair per entry to the pod and the `sandbox`
        container ONLY (docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume
        addendum) -- never the sidecar container, so this cannot be used to
        smuggle a volume into the sidecar's own privileged filesystem view.
        """
        if not self._k8s_core_api:
            raise RuntimeError("K8s API not initialized")

        identity_labels, identity_annotations = self._identity_labels_and_annotations(
            session_id=session_id,
            organization_id=organization_id,
            work_item_id=work_item_id,
        )
        webhook_skip_annotations = {
            **build_pod_identity_webhook_skip_annotations(),
            **build_azure_workload_identity_skip_annotations(),
        }

        # SECURITY: fresh, unguessable per-pod secret for the sidecar's HTTP
        # API (defense in depth on top of NetworkPolicy — see sidecar_auth.py).
        # Stored in its own Kubernetes Secret (not a pod annotation — a plain
        # annotation is readable by anything with mere `pods: get/list` RBAC,
        # which is a much lower bar than reading a Secret requires) so any
        # manager process can recover it later via `secrets: get` (e.g. after
        # a restart, or a warm pod claimed by a different worker).
        sidecar_auth_token = generate_sidecar_auth_token()
        sidecar_auth_secret = sidecar_auth_secret_name(pod_name)

        # SECURITY: fresh, short-lived, self-signed TLS keypair for the
        # sidecar's HTTP API, pinned by this manager instead of validated
        # against a public CA (see tls.py, docs/SIDECAR-TRANSPORT-TLS-DESIGN.md).
        # Generated alongside the auth token above and stored in the SAME
        # per-pod Secret, not a second one. Skipped entirely when
        # SIDECAR_TLS_DISABLED=true (operators running their own mesh mTLS).
        tls_cert_pem = tls_key_pem = ""
        if not sidecar_tls_disabled():
            tls_cert_pem, tls_key_pem = generate_pod_self_signed_cert(pod_name)

        created_secret_fresh = await self._create_sidecar_auth_secret(
            sidecar_auth_secret, sidecar_auth_token, tls_cert_pem, tls_key_pem
        )
        if not created_secret_fresh:
            # A Secret by this deterministic name already existed (e.g. a
            # concurrent create for the same computed pod name won the
            # race) -- our locally generated token/cert aren't necessarily
            # what's actually stored. Read back the real values so the
            # cache below and the pod's secretKeyRef/volume (which
            # reference this Secret's name, not our local variables) stay
            # consistent.
            recovered_token, recovered_cert = await self._ensure_pod_secret_cached(pod_name)
            sidecar_auth_token = recovered_token or sidecar_auth_token
            tls_cert_pem = recovered_cert or tls_cert_pem

        # Build labels, handling None values
        labels = {
            "app": "sandbox",
            "pool": "claimed",
            "sandbox.boxkite.dev/status": "claimed",
            SANDBOX_SIZE_LABEL: size,
            **build_azure_workload_identity_pod_labels(),
            **identity_labels,
        }
        tls_enabled = bool(tls_cert_pem and tls_key_pem)
        sidecar_volume_mounts = [
            client.V1VolumeMount(name="workspace", mount_path="/workspace"),
            client.V1VolumeMount(name="uploads", mount_path="/mnt/user-data/uploads"),
            client.V1VolumeMount(name="outputs", mount_path="/mnt/user-data/outputs"),
            client.V1VolumeMount(name="skills", mount_path="/mnt/skills"),
            client.V1VolumeMount(name="tmp", mount_path="/tmp"),  # Shared /tmp for ephemeral files
        ]
        aws_web_identity_mount = build_sidecar_aws_web_identity_volume_mount()
        if aws_web_identity_mount:
            sidecar_volume_mounts.append(aws_web_identity_mount)

        pod_volumes = build_sandbox_pod_volumes(volume_size_limit=volume_size_limit)
        aws_web_identity_volume = build_aws_web_identity_volume()
        if aws_web_identity_volume:
            pod_volumes.append(aws_web_identity_volume)

        # docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum -- one
        # PVC-backed V1Volume/V1VolumeMount pair per entry, sandbox
        # container only (see this method's own docstring for why never
        # the sidecar).
        extra_sandbox_volume_mounts = []
        for i, entry in enumerate(volume_mounts or []):
            volume_name = f"boxkite-vol-{i}"
            pod_volumes.append(
                client.V1Volume(
                    name=volume_name,
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=entry["pvc_name"]
                    ),
                )
            )
            extra_sandbox_volume_mounts.append(
                client.V1VolumeMount(name=volume_name, mount_path=entry["mount_path"])
            )

        if tls_enabled:
            # Cert/key must be a mounted file, not an env var -- uvicorn's
            # ssl_certfile/ssl_keyfile take filesystem paths (see sidecar/
            # main.py). Backed by the same per-pod Secret the auth token
            # lives in, not a new Secret object.
            sidecar_volume_mounts.append(
                client.V1VolumeMount(
                    name="sidecar-tls", mount_path=SIDECAR_TLS_MOUNT_PATH, read_only=True
                )
            )
            pod_volumes.append(
                client.V1Volume(
                    name="sidecar-tls",
                    secret=client.V1SecretVolumeSource(
                        secret_name=sidecar_auth_secret,
                        items=[
                            client.V1KeyToPath(
                                key=SIDECAR_TLS_CERT_SECRET_KEY, path=SIDECAR_TLS_CERT_FILENAME
                            ),
                            client.V1KeyToPath(
                                key=SIDECAR_TLS_KEY_SECRET_KEY, path=SIDECAR_TLS_KEY_FILENAME
                            ),
                        ],
                    ),
                )
            )

        # Build pod spec (matching pod-template.yaml)
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=SANDBOX_NAMESPACE,
                labels=labels,
                annotations={
                    SAFE_TO_EVICT_ANNOTATION: "false",
                    **webhook_skip_annotations,
                    **identity_annotations,
                },
            ),
            spec=client.V1PodSpec(
                # SECURITY: share_process_namespace is required for nsenter to work.
                # The sidecar uses nsenter to execute commands in the sandbox's
                # mount/PID namespace. However, the sidecar MUST drop privileges
                # to UID 1001 via --setuid/--setgid before executing any agent code.
                # This prevents agents from reading /proc/<sidecar_pid>/environ
                # (kernel enforces UID checks - sidecar runs as root, agent as 1001).
                share_process_namespace=True,
                # Opt-in, off by default (docs/KATA-CONTAINERS-SCOPING.md).
                # None omits the field entirely -- ordinary runc, no behavior
                # change. Experimental when set: see kata_runtime_class_name's
                # own docstring for the unverified emptyDir.sizeLimit risk
                # that must be confirmed against a real cluster before this
                # is a supported configuration rather than an opt-in one.
                runtime_class_name=kata_runtime_class_name(),
                automount_service_account_token=False,
                service_account_name=SANDBOX_SERVICE_ACCOUNT_NAME,
                restart_policy="Never",
                # Disable K8s service discovery env vars (security: don't expose cluster info)
                enable_service_links=False,
                # Auto-terminate after 30 minutes to prevent orphaned pods
                active_deadline_seconds=active_deadline_seconds or SANDBOX_ACTIVE_DEADLINE_SECONDS,
                priority_class_name=SANDBOX_CLAIMED_PRIORITY_CLASS or None,
                containers=[
                    # Sandbox container
                    client.V1Container(
                        name="sandbox",
                        image=image_ref or SANDBOX_IMAGE,
                        command=["tail", "-f", "/dev/null"],
                        security_context=client.V1SecurityContext(
                            run_as_user=1001,
                            run_as_non_root=True,
                            allow_privilege_escalation=False,
                            capabilities=client.V1Capabilities(drop=["ALL"]),
                            read_only_root_filesystem=True,
                            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                        ),
                        # The sandbox container provides the filesystem/tooling
                        # image and namespaces. Runtime CPU/memory is lower here
                        # because user commands are currently launched by the
                        # sidecar; see resource_config.py for the accounting
                        # details behind the default split.
                        resources=build_sandbox_container_resources(size=size, gpu_count=gpu_count),
                        volume_mounts=[
                            client.V1VolumeMount(name="workspace", mount_path="/workspace"),
                            client.V1VolumeMount(
                                name="uploads",
                                mount_path="/mnt/user-data/uploads",
                                read_only=True,
                            ),
                            client.V1VolumeMount(
                                name="outputs",
                                mount_path="/mnt/user-data/outputs",
                            ),
                            client.V1VolumeMount(
                                name="skills",
                                mount_path="/mnt/skills",
                                read_only=True,
                            ),
                            client.V1VolumeMount(name="tmp", mount_path="/tmp"),
                            *extra_sandbox_volume_mounts,
                        ],
                        env=[
                            client.V1EnvVar(name="PATH", value="/usr/local/bin:/usr/bin:/bin"),
                            client.V1EnvVar(name="HOME", value="/workspace"),
                            client.V1EnvVar(name="LANG", value="C.UTF-8"),
                            client.V1EnvVar(name="PYTHONUNBUFFERED", value="1"),
                        ],
                    ),
                    # Sidecar container
                    # SECURITY: The sidecar runs as root with SYS_PTRACE/SYS_ADMIN
                    # to enable nsenter. However, it ALWAYS drops privileges to
                    # UID 1001 before executing any agent code (via --setuid/--setgid).
                    # Additionally, environment variables are sanitized - no Azure/S3
                    # credentials are passed to the subprocess.
                    client.V1Container(
                        name="sidecar",
                        image=SIDECAR_IMAGE,
                        ports=[client.V1ContainerPort(container_port=SIDECAR_PORT)],
                        security_context=client.V1SecurityContext(
                            run_as_user=0,  # Root required for nsenter namespace entry
                            # SYS_PTRACE: Required for nsenter to attach to sandbox process
                            # SYS_ADMIN: Required for namespace operations (mount, pid)
                            # CHOWN: Required by /configure (sidecar_sync.py) to chown
                            # /workspace and /outputs to SANDBOX_UID/GID on every session
                            # setup -- dropped by mistake in 4cabac3's "drop ALL, add back
                            # only what's needed" pass, which broke every sandbox create
                            # with PermissionError until caught and re-added here.
                            # SYS_CHROOT: util-linux 2.42's nsenter chroot()s internally
                            # when switching mount namespaces (to re-root the process so
                            # its cwd/root make sense in the new mount tree) -- without
                            # this, entering the sandbox container's MOUNT namespace
                            # specifically fails with EPERM ("reassociate to namespaces
                            # failed"), even though entering its PID namespace and every
                            # other part of nsenter's job works fine on SYS_ADMIN alone.
                            # Confirmed by bisecting the full capability set against a
                            # live repro pod until only this one bit made the difference.
                            # SETUID/SETGID: nsenter's own --setuid/--setgid privilege
                            # drop (build_k8s_exec_command) calls setuid()/setgid() to
                            # become SANDBOX_UID/GID before exec -- with capabilities
                            # otherwise dropped to ALL, root no longer gets these for
                            # free; without them nsenter fails with
                            # "setgid() failed: Operation not permitted" after
                            # successfully entering the target namespaces.
                            # NOTE: These capabilities are NOT inherited by agent code
                            # because nsenter drops to UID 1001 before exec.
                            capabilities=client.V1Capabilities(add=["SYS_PTRACE", "SYS_ADMIN", "CHOWN", "SYS_CHROOT", "SETUID", "SETGID"], drop=["ALL"]),
                            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                        ),
                        # The sidecar owns the execution cgroup today, so it
                        # intentionally receives the larger default budget.
                        resources=build_sidecar_container_resources(size=size),
                        volume_mounts=sidecar_volume_mounts,
                        env=[
                            client.V1EnvVar(name="RUNTIME_MODE", value="k8s"),
                            # SECURITY: shared secret the sidecar requires on every
                            # HTTP request except /health (see sidecar_auth.py).
                            # Sourced from the Secret created above via
                            # secretKeyRef, NOT a literal value -- a literal
                            # `value:` here would be readable by anything with
                            # mere `pods: get` RBAC (pod spec, same as an
                            # annotation), defeating the point of moving this
                            # off the annotation in the first place.
                            client.V1EnvVar(
                                name=SIDECAR_AUTH_TOKEN_ENV,
                                value_from=client.V1EnvVarSource(
                                    secret_key_ref=client.V1SecretKeySelector(
                                        name=sidecar_auth_secret,
                                        key=SIDECAR_AUTH_SECRET_KEY,
                                    )
                                ),
                            ),
                            # Storage backend selection (azure or s3)
                            client.V1EnvVar(name="STORAGE_BACKEND", value=STORAGE_BACKEND),
                            client.V1EnvVar(name="S3_BUCKET", value=S3_BUCKET),
                            client.V1EnvVar(name="STORAGE_S3_REGION", value=STORAGE_S3_REGION),
                            client.V1EnvVar(name="STORAGE_S3_ENDPOINT", value=STORAGE_S3_ENDPOINT),
                            client.V1EnvVar(name="STORAGE_S3_KMS_KEY_ID", value=STORAGE_S3_KMS_KEY_ID),
                            client.V1EnvVar(
                                name="STORAGE_S3_BUCKET_KEY_ENABLED",
                                value=STORAGE_S3_BUCKET_KEY_ENABLED,
                            ),
                            build_sidecar_exec_network_isolation_env(),
                            *build_sidecar_aws_auth_env(STORAGE_CREDENTIALS_SECRET),
                            # Azure Blob Storage configuration
                            *build_sidecar_azure_storage_env(STORAGE_CREDENTIALS_SECRET),
                            build_sidecar_tls_env(tls_enabled),
                        ],
                        readiness_probe=client.V1Probe(
                            http_get=client.V1HTTPGetAction(
                                path="/health",
                                port=SIDECAR_PORT,
                                scheme="HTTPS" if tls_enabled else "HTTP",
                            ),
                            initial_delay_seconds=2,
                            period_seconds=5,
                        ),
                    ),
                ],
                volumes=pod_volumes,
            ),
        )

        # Cache the token/cert now so the very first HTTP call this process
        # makes to this pod (e.g. /configure right after create) is
        # authenticated and TLS-pinned.
        self._cache_pod_auth_token(pod_name, sidecar_auth_token)
        self._cache_pod_tls_cert(pod_name, tls_cert_pem)

        # Create pod (handle conflict if pod already exists)
        import time as _time
        _t_create0 = _time.monotonic()
        try:
            await self._k8s_core_api.create_namespaced_pod(namespace=SANDBOX_NAMESPACE, body=pod)
        except ApiException as e:
            if e.status == 409:
                # Pod already exists - check if it's usable or needs replacement
                logger.warning(f"[SandboxManager] Pod {pod_name} already exists, checking status")
                try:
                    existing_pod = await self._k8s_core_api.read_namespaced_pod(
                        name=pod_name, namespace=SANDBOX_NAMESPACE
                    )
                    # If pod is running and has IP, try to reuse it
                    if existing_pod.status.phase == "Running" and existing_pod.status.pod_ip:
                        logger.info(f"[SandboxManager] Reusing existing pod {pod_name}")
                        # The existing pod was never given our freshly generated
                        # token/cert (create_namespaced_pod failed before it
                        # applied) — recover the real values from its Secret
                        # instead.
                        self._pod_auth_tokens.pop(pod_name, None)
                        self._pod_tls_certs.pop(pod_name, None)
                        self._pod_secrets_fetched.pop(pod_name, None)
                        await self._ensure_pod_secret_cached(pod_name)
                        return existing_pod.status.pod_ip
                    # If pod is pending/stuck, delete and recreate
                    logger.info(f"[SandboxManager] Deleting stuck pod {pod_name} (phase: {existing_pod.status.phase})")
                    await self._k8s_core_api.delete_namespaced_pod(
                        name=pod_name,
                        namespace=SANDBOX_NAMESPACE,
                        body=client.V1DeleteOptions(grace_period_seconds=0),
                    )
                    # Wait briefly for deletion
                    await asyncio.sleep(2)
                    # Retry creation
                    await self._k8s_core_api.create_namespaced_pod(namespace=SANDBOX_NAMESPACE, body=pod)
                except ApiException as inner_e:
                    logger.error(f"[SandboxManager] Failed to handle existing pod {pod_name}: {inner_e}")
                    raise
            else:
                raise

        # Wait for pod to be ready. On failure (no schedulable node, image
        # pull failure, the pod getting evicted before it ever goes Running,
        # etc.) the pod above was already created -- clean it up (pod +
        # sidecar-auth Secret) before propagating, the same way
        # WarmPoolManager._create_warm_pod already does for pre-warmed pods.
        # Without this, a cold-create that fails this way leaks a stuck pod
        # forever: it's invisible to both the idle reaper (requires
        # status=claimed AND Running with a pod_ip) and WarmPoolManager's
        # own stale-pod scan (only reaps pods it manages itself).
        logger.info(f"[TIMING] k8s_create_pod_api: {(_time.monotonic() - _t_create0)*1000:.0f}ms")
        # Readiness wait isolates node scheduling + image-pull cost — the
        # dominant, cluster-scale-dependent term the warm pool exists to
        # amortize (issue #178). On a small dev cluster with images already
        # cached this is cheap, which is exactly why cold-start there can look
        # deceptively close to a warm claim; on a production-scale cluster
        # paying real scheduling/image-pull cost it is where the warm pool's
        # advantage actually shows up.
        _t_ready0 = _time.monotonic()
        try:
            pod_ip = await self._wait_for_pod_ready(pod_name)
        except Exception:
            logger.error(f"[SandboxManager] Pod {pod_name} failed to become ready; cleaning up")
            await self._delete_pod(pod_name)
            raise
        logger.info(f"[TIMING] k8s_pod_readiness_wait: {(_time.monotonic() - _t_ready0)*1000:.0f}ms")
        return pod_ip

    async def _wait_for_pod_ready(
        self, pod_name: str, timeout: int = SANDBOX_POD_READY_TIMEOUT_SECONDS
    ) -> str:
        """Wait for pod to be ready and return its IP."""
        if not self._k8s_core_api:
            raise RuntimeError("K8s API not initialized")

        start_time = asyncio.get_event_loop().time()
        while True:
            try:
                pod = await self._k8s_core_api.read_namespaced_pod(
                    name=pod_name,
                    namespace=SANDBOX_NAMESPACE,
                )

                # Check if pod is ready
                if pod.status.phase == "Running" and pod.status.pod_ip:
                    # Check container readiness
                    if pod.status.container_statuses:
                        all_ready = all(cs.ready for cs in pod.status.container_statuses)
                        if all_ready:
                            return pod.status.pod_ip

                # Check for failure
                if pod.status.phase in ("Failed", "Succeeded"):
                    raise RuntimeError(f"Pod {pod_name} terminated with phase {pod.status.phase}")

            except ApiException as e:
                if e.status != 404:
                    raise

            # Check timeout
            if asyncio.get_event_loop().time() - start_time > timeout:
                raise TimeoutError(f"Pod {pod_name} not ready after {timeout}s")

            await asyncio.sleep(1)

    async def destroy_session(
        self,
        session_id: str,
        *,
        preserve_cached_skills: bool = False,
    ) -> None:
        """
        Destroy a sandbox session.

        1. Flushes any pending outputs to S3
        2. Recycles pod to warm pool (or deletes it)
        3. Cleans up HTTP client and session metadata

        preserve_cached_skills keeps the per-session skills payload for
        recovery-driven recreates. Normal teardown should clear it.
        """
        if self._use_docker_compose:
            meta = self._compose_sessions.pop(session_id, None)
            if not meta:
                logger.warning(f"[SandboxManager] Session {session_id} not found")
                self._invalidate_session_endpoint(session_id)
                if not preserve_cached_skills:
                    self._session_skills.pop(session_id, None)
                self._release_recovery_lock_if_idle(session_id)
                return
            logger.info(f"[SandboxManager] Destroying compose session {session_id}")
            # Kill any background processes before flushing/tearing down --
            # see _kill_all_processes()'s docstring.
            await self._kill_all_processes("compose-sandbox", "localhost")
            try:
                http_client = self._get_http_client("compose-sandbox", "localhost")
                await http_client.post("/flush")
            except Exception as e:
                logger.error(f"[SandboxManager] Error flushing outputs: {e}")
            self._invalidate_session_endpoint(session_id)
            if not preserve_cached_skills:
                self._session_skills.pop(session_id, None)
                await self._forget_session_metadata(session_id)
            self._release_recovery_lock_if_idle(session_id)
            return  # Don't delete compose sidecar

        # K8s mode: resolve session pod
        try:
            pod_name, pod_ip = await self._resolve_session(session_id)
        except ValueError:
            logger.warning(f"[SandboxManager] Session {session_id} not found")
            self._invalidate_session_endpoint(session_id)
            if not preserve_cached_skills:
                self._session_skills.pop(session_id, None)
            self._release_recovery_lock_if_idle(session_id)
            return

        logger.info(f"[SandboxManager] Destroying session {session_id}")

        # Kill any background processes *before* flushing/recycling -- see
        # _kill_all_processes()'s docstring. Ordered first so a process
        # mid-write to disk doesn't race the flush below.
        await self._kill_all_processes(pod_name, pod_ip)

        try:
            # Flush outputs to S3
            http_client = self._get_http_client(pod_name, pod_ip)
            await http_client.post("/flush")
        except Exception as e:
            logger.error(f"[SandboxManager] Error flushing outputs: {e}")

        # Clean up caches
        await self._close_http_client(pod_name)
        self._invalidate_session_endpoint(session_id)
        if not preserve_cached_skills:
            self._session_skills.pop(session_id, None)
            await self._forget_session_metadata(session_id)
        self._release_recovery_lock_if_idle(session_id)

        # Recycle or delete pod via K8s APIs (no in-memory warm-pool coupling).
        recycled = await self._recycle_pod_via_k8s(pod_name, pod_ip)
        if not recycled:
            await self._delete_pod(pod_name)

    async def _delete_pod(self, pod_name: str) -> None:
        """Delete a K8s pod and its companion sidecar-auth Secret."""
        self._pod_auth_tokens.pop(pod_name, None)
        self._pod_tls_certs.pop(pod_name, None)
        self._pod_secrets_fetched.pop(pod_name, None)

        if not self._k8s_core_api:
            return

        try:
            await self._k8s_core_api.delete_namespaced_pod(
                name=pod_name,
                namespace=SANDBOX_NAMESPACE,
                body=client.V1DeleteOptions(grace_period_seconds=5),
            )
            logger.info(f"[SandboxManager] Deleted pod {pod_name}")
        except ApiException as e:
            if e.status != 404:
                logger.error(f"[SandboxManager] Error deleting pod {pod_name}: {e}")

        await self._delete_sidecar_auth_secret(sidecar_auth_secret_name(pod_name))
        # SECURITY (issue #74): a hard-deleted pod's secrets-egress
        # NetworkPolicy is otherwise orphaned garbage (K8s doesn't cascade-
        # delete NetworkPolicy objects when their selected pod disappears)
        # -- clean it up explicitly, same as the sidecar-auth Secret above.
        await self._delete_secrets_egress_network_policy(pod_name)
        # Same orphaned-garbage cleanup for the browser-egress NetworkPolicy
        # (docs/BROWSER-EXEC-DESIGN.md §3), if this pod ever had one.
        await self._delete_browser_egress_network_policy(pod_name)


class SandboxManager(
    MetadataMixin,
    SessionLookupMixin,
    TlsAuthMixin,
    HttpClientMixin,
    CreateSessionMixin,
    WarmPoolMixin,
    PodLifecycleMixin,
    RecoveryMixin,
    SidecarProxyMixin,
    FilesSkillsMixin,
    FullStateCheckpointMixin,
):
    """
    Manages sandbox pod lifecycle and routes tool calls to sidecar.

    Session state is stored on K8s pod labels/annotations (not in-memory),
    so sessions survive backend restarts and work across multiple processes.
    For Docker Compose (local dev), a lightweight in-memory dict is used instead.

    Usage:
        manager = SandboxManager()
        session_info = await manager.create_session(org_id, session_id, work_item_id)

        # Execute commands
        result = await manager.execute(session_id, "python3 -c 'print(1+1)'")

        # File operations
        await manager.file_create(session_id, "hello.txt", "Hello World")
        content = await manager.view(session_id, "hello.txt")
        image = await manager.read_image(session_id, "uploads/screenshot.png")
        await manager.str_replace(session_id, "hello.txt", "Hello", "Hi")

        # Generate download URLs
        files = await manager.present_files(session_id, ["hello.txt"])

        # Cleanup
        await manager.destroy_session(session_id)
    """

    def __init__(self, *, session_metadata_store: Optional[SessionMetadataStore] = None):
        """
        Initialize SandboxManager.

        Args:
            session_metadata_store: Optional hook used only when a sidecar
                transport error needs to recover a session whose K8s pod is
                already gone (see session_store.py). Defaults to a no-op that
                simply fails recovery with "session not found" — the same
                behavior as before this hook existed. Most callers don't need
                this; K8s pod labels/annotations are the primary source of
                truth and cover the common recovery path already.
        """
        self._session_metadata_store: SessionMetadataStore = (
            session_metadata_store or NoOpSessionMetadataStore()
        )
        self._k8s_core_api: Optional[client.CoreV1Api] = None
        self._k8s_networking_api: Optional[client.NetworkingV1Api] = None
        self._k8s_api_client: Optional[client.ApiClient] = None
        self._k8s_initialized = False
        self._k8s_init_lock = asyncio.Lock()
        self._use_docker_compose = os.environ.get("RUNTIME_MODE") == "compose"
        # Proxy mode: on macOS + kind, pod IPs are inside the Docker VM and
        # unreachable from the host.  Setting SANDBOX_USE_K8S_PROXY=true routes
        # all sidecar HTTP through `kubectl proxy` (default :8001) instead.
        self._use_k8s_proxy = os.environ.get("SANDBOX_USE_K8S_PROXY", "").lower() == "true"
        self._k8s_proxy_url = os.environ.get("SANDBOX_K8S_PROXY_URL", "http://localhost:8001").rstrip("/")
        self._recovery_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._session_create_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()

        # HTTP connection pool: pod_name -> client (transport-level, not session state).
        # Clients are created lazily and cleaned up when pods are destroyed.
        self._http_clients: OrderedDict[str, httpx.AsyncClient] = OrderedDict()

        # Per-pod sidecar auth tokens (see sidecar_auth.py). Populated when this
        # process creates a pod, or lazily recovered (via _ensure_pod_auth_token_cached
        # reading the pod's per-pod Secret, sidecar_auth_secret_name(pod_name))
        # when a pod was claimed or created by a different process (e.g.
        # WarmPoolManager, or another backend worker after a restart).
        self._pod_auth_tokens: OrderedDict[str, str] = OrderedDict()
        # Compose mode has a single, statically configured sidecar — its
        # token comes from this process's own environment, matching whatever
        # docker-compose.yml injects into the sidecar container.
        self._compose_auth_token = os.environ.get(SIDECAR_AUTH_TOKEN_ENV, "").strip()

        # Per-pod pinned TLS cert PEM (see tls.py). Mirrors _pod_auth_tokens
        # exactly: populated when this process creates a pod, lazily
        # recovered via _ensure_pod_tls_cert_cached() otherwise, and keyed
        # by pod *name* (never pod IP -- pod IPs are reused across pod
        # creations, so an IP-keyed cache could pin a stale cert against a
        # brand new pod that happens to reuse a freed IP).
        self._pod_tls_certs: OrderedDict[str, str] = OrderedDict()

        # Tracks which pod names have already had a real (successful)
        # Secret read via _ensure_pod_secret_cached, independent of whether
        # the token/cert values it found were non-empty. Needed because
        # _cache_pod_tls_cert never stores an empty string -- with TLS
        # disabled cluster-wide, _pod_tls_certs.get(pod_name) is always
        # falsy, so a token/cert-presence check alone can never short-circuit
        # and would re-read the Secret on every single call for that pod.
        self._pod_secrets_fetched: OrderedDict[str, bool] = OrderedDict()

        # Compose mode: lightweight in-memory session metadata (no K8s to store on).
        self._compose_sessions: dict[str, dict] = {}

        # Skills cache: session_id -> skills payload. Same-process, same-message scope.
        # Used to replay skills during mid-message recovery after pod recreation.
        self._session_skills: OrderedDict[str, list[dict]] = OrderedDict()
        self._session_endpoints: OrderedDict[str, tuple[float, str, str]] = OrderedDict()

        # Docker Compose mode - single shared sidecar
        if self._use_docker_compose:
            env_url = os.environ.get("SIDECAR_URL")
            # `boxkite up` persists the sidecar token + URL to
            # ~/.boxkite/local.env; load them here so library users and example
            # scripts don't have to re-export SIDECAR_AUTH_TOKEN/SIDECAR_URL by
            # hand after `boxkite up`. Explicit env vars always win.
            if not self._compose_auth_token or not env_url:
                from .local_env import read_local_env_credentials

                creds = read_local_env_credentials()
                if creds is not None:
                    file_url, file_token = creds
                    if not self._compose_auth_token:
                        self._compose_auth_token = file_token
                    if not env_url:
                        env_url = file_url
            self._compose_url = env_url or "http://localhost:8080"
            logger.info(f"[SandboxManager] Running in Docker Compose mode, sidecar at {self._compose_url}")



# Singleton instance (optional, for simple usage)
_default_manager: Optional[SandboxManager] = None


def get_sandbox_manager() -> SandboxManager:
    """Get or create the default SandboxManager instance."""
    global _default_manager
    if _default_manager is None:
        _default_manager = SandboxManager()
    return _default_manager


async def close_sandbox_manager() -> None:
    """Close and reset the default SandboxManager singleton."""
    global _default_manager
    manager = _default_manager
    _default_manager = None
    if manager is not None:
        await manager.shutdown()
