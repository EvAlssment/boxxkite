"""Session creation, upload prefetch, and compose-session setup for SandboxManager."""

from ._manager_config import *  # noqa: F401,F403

class CreateSessionMixin:
    async def create_session(
        self,
        organization_id: UUID,
        session_id: str,
        work_item_id: Optional[UUID] = None,
        upload_file_ids: Optional[list[str]] = None,
        size: str = DEFAULT_SANDBOX_SIZE,
        storage_gb: Optional[float] = None,
        lifetime_seconds: Optional[int] = None,
        restore_from_snapshot_id: Optional[str] = None,
        secret_grants: Optional[list[dict]] = None,
        secret_capability_token: Optional[str] = None,
        secrets_control_plane_url: Optional[str] = None,
        image_ref: Optional[str] = None,
        volume_mounts: Optional[list[dict]] = None,
        mcp_connection_grants: Optional[list[dict]] = None,
        browser_enabled: bool = False,
        desktop_enabled: bool = False,
        gpu_count: Optional[int] = None,
    ) -> dict:
        """
        Create a new sandbox session.

        1. Claims a warm pod (if available) or creates a new one
        2. Configures the sidecar with session info
        3. Pre-fetches any existing work item files from S3
        4. Patches pod with session metadata (K8s labels/annotations)

        Args:
            organization_id: Organization ID for S3 path scoping
            session_id: Unique session identifier
            work_item_id: Work item ID for file persistence
            upload_file_ids: Optional file IDs to pre-fetch
            size: Sandbox CPU/memory size preset ("small", "medium", "large").
                Claims from that size's own warm sub-pool if one is
                configured (WARM_POOL_SIZE_MEDIUM/WARM_POOL_SIZE_LARGE,
                both 0 -- i.e. no pre-warmed pods of that size -- by
                default); otherwise a fresh pod is created on demand.
            storage_gb: Overrides the workspace/uploads/outputs/skills volume
                size limit (Gi), bounded by resource_config.max_volume_size_limit_gi().
            lifetime_seconds: Overrides the pod's activeDeadlineSeconds,
                bounded by resource_config.max_active_deadline_seconds().
            restore_from_snapshot_id: Purely observational (see
                RESTORED_FROM_SNAPSHOT_ANNOTATION) -- recorded as a pod
                annotation for audit/debugging. Does NOT change this
                session's storage_prefix or prefetch behavior: the caller
                (control plane) must copy the snapshot's data into this
                session_id's own live storage_prefix *before* calling this
                method, so /configure's normal prefetch path picks it up
                exactly like any other pre-existing session file.
            secret_grants: Optional list of {"name": str, "allowed_hosts":
                list[str]} dicts -- this session's secrets-broker grants
                (docs/SECRETS-DESIGN.md §3/4). Non-sensitive metadata only
                (names + allowlists), never the secret values themselves --
                pushed to the sidecar's /configure so its new POST
                /http-request route knows which {{secret:name}} references
                this session may use and against which destination hosts.
            secret_capability_token: Short-lived, session-bound capability
                token (control_plane.secret_capability) the sidecar uses to
                resolve a granted secret's real value on demand from
                `secrets_control_plane_url`'s internal endpoint. This is the
                ONLY thing that crosses the manager-to-sidecar transport for
                this feature -- never the resolved secret value itself (see
                the design doc §4's transport discussion).
            secrets_control_plane_url: Base URL of the control plane's own
                internal secret-resolve endpoint (settings.SECRETS_CONTROL_PLANE_URL),
                handed to the sidecar so it knows where to redeem
                `secret_capability_token`. Required (non-None) whenever
                `secret_grants` is non-empty.
            image_ref: Optional digest-pinned image reference
                (docs/DECLARATIVE-BUILDER-DESIGN.md), used in place of the
                operator's static SANDBOX_IMAGE for this one pod's `image`
                field ONLY. Nothing else about the pod spec changes --
                capability drops, read-only rootfs, non-root runAsUser,
                resource requests/limits, and network policy are identical
                regardless of which image is referenced (see _create_pod).
                Rejected with ValueError unless it's pinned to an exact
                `repo@sha256:<64-hex>` digest -- a mutable tag here would let
                the referenced image's contents change after the
                control-plane's build/scan gate already approved it.
            volume_mounts: Optional list of {"pvc_name": str, "mount_path": str}
                dicts (docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume
                addendum) -- each becomes a PersistentVolumeClaim volume
                mounted into the `sandbox` container ONLY (bash_tool's
                nsenter-based exec reaches it there; no other container
                field changes). The control-plane router is responsible
                for resolving a caller's `volume_id` to a real, "ready"
                pvc_name before ever reaching this method -- this method
                only validates the shape (_validate_volume_mounts), same
                division of responsibility as image_ref (routers/sandboxes.py
                resolves image_id -> registry_ref; this method validates
                the format). Forces a cold pod create, same as image_ref --
                no pre-warmed pod has any PVC premounted.
            mcp_connection_grants: Optional list of {"name": str,
                "allowed_hosts": [str]} dicts -- this session's outbound-MCP
                connection grants (GitHub issues #116/#117,
                docs/OUTBOUND-MCP-DESIGN.md §3), resolved server-side from a
                caller's `mcp_connection_names` against the curated MCP
                catalog. Exactly the same shape as secret_grants, and
                unioned with it into the same per-session NetworkPolicy
                (_create_k8s_session) -- this is network-layer egress
                scoping ONLY. There is no MCP-proxy transport yet (see the
                design doc's §6 scope boundary), so this does not by itself
                let an agent speak MCP to the granted destination; it only
                widens which hosts the pod may reach at the network layer.
                Ignored in Docker Compose mode (no K8s NetworkPolicy there).
            browser_enabled: Whether this session's tool set includes the
                browser_navigate/browser_exec/browser_screenshot/
                browser_close tools (docs/BROWSER-EXEC-DESIGN.md) --
                purely a signal for provisioning this session's pod's
                browser-egress NetworkPolicy (see
                src/boxkite/browser_network_policy.py); it does NOT itself
                gate the sidecar routes or the tool specs (those are
                BOXKITE_BROWSER_ENABLED and
                create_sandbox_tool_specs(enable_browser_tool=...)
                respectively). Also requires
                BOXKITE_BROWSER_NETWORK_POLICY_ENABLED server-side --
                setting this True alone does not provision anything if
                that operator-level flag is unset. K8s mode only; ignored
                in Docker Compose mode (no NetworkPolicy concept there).
                Full gating picture (four independent knobs, all required
                together, per docs/BROWSER-EXEC-DESIGN.md §5): sidecar's
                BOXKITE_BROWSER_ENABLED, the tool factory's
                create_sandbox_tool_specs(enable_browser_tool=True), the
                operator-level BOXKITE_BROWSER_NETWORK_POLICY_ENABLED, and
                this per-session browser_enabled flag. Any missing knob
                fails closed (browser tool exposed but non-functional, or
                not exposed at all) rather than open -- never respond to a
                failure here by widening the static NetworkPolicy instead
                of setting the actual missing per-session/operator flag.
                Rejected (ValueError) when combined with size='small': a
                headless Chromium process needs 'medium' or 'large'
                (docs/BROWSER-EXEC-DESIGN.md §4). Only takes effect on a
                cold-create or a warm-pod claim -- calling this again for a
                session_id whose pod is already running (the "reuse
                existing session" branch above) does not re-provision or
                update the browser-egress NetworkPolicy for a
                browser_enabled value that differs from what the session
                was originally created with. Fails closed (no egress, not
                broadened egress) if that happens, but is not itself a
                mechanism for changing browser_enabled on a live session.
            desktop_enabled: Whether this session intends to use `WS
                .../desktop` GUI/remote-desktop human takeover (GitHub
                issue #184, docs/GUI-COMPUTER-USE-SCOPING.md). This flag's
                *only* effect in v1 is the resource-floor check below
                (`_validate_desktop_resource_floor`) -- unlike
                browser_enabled, it does not provision any NetworkPolicy
                (the desktop stack needs zero egress: Xvfb/WM/x11vnc are
                all local to the pod) and does not itself gate the sidecar
                route (that's BOXKITE_DESKTOP_ENABLED) or control-plane
                access (that's RBAC + the desktop token). It exists so a
                caller's intent to use desktop takeover is declared and
                validated at session-creation time, the same choke point
                every other per-session capability flag goes through,
                rather than only discovered as a runtime failure the first
                time someone opens `WS /desktop` against a too-small pod.
                Rejected (ValueError) when combined with size='small': the
                Xvfb/WM/x11vnc stack needs 'medium' or 'large'
                (docs/GUI-COMPUTER-USE-SCOPING.md).
            gpu_count: Opt-in, experimental (docs/GPU-SUPPORT-SCOPING.md) --
                requests this many GPUs (BOXKITE_GPU_RESOURCE_NAME,
                "nvidia.com/gpu" by default) as an extended resource limit
                on the sandbox container. Rejected (ValueError) unless
                BOXKITE_GPU_ENABLED is set and the count is within
                BOXKITE_MAX_GPU_COUNT_PER_SESSION -- an operator must
                explicitly opt in and provision a GPU-equipped node pool
                with a device plugin first; this is not verified against
                real GPU hardware in this codebase (see the scoping doc's
                own disclosed cross-tenant VRAM-wipe question). Forces a
                cold pod create, same as image_ref/volume_mounts -- no
                pre-warmed pod has a GPU reserved.

        Returns:
            Dict with 'pod_name' key
        """
        logger.info(f"[SandboxManager] Creating session {session_id} for work_item {work_item_id}")
        normalized_upload_file_ids = self._normalize_upload_file_ids(upload_file_ids)
        size = _validate_sandbox_size(size)
        _validate_browser_resource_floor(size, browser_enabled)
        _validate_desktop_resource_floor(size, desktop_enabled)
        volume_size_limit = _validate_storage_gb(storage_gb)
        lifetime_seconds = _validate_lifetime_seconds(lifetime_seconds)
        image_ref = _validate_image_ref(image_ref)
        volume_mounts = _validate_volume_mounts(volume_mounts)
        gpu_count = _validate_gpu_count(gpu_count)
        uses_custom_sizing = size != DEFAULT_SANDBOX_SIZE or volume_size_limit is not None or lifetime_seconds is not None

        async with self._get_session_create_lock(session_id):
            # Serialize create/reuse for the same session within a worker so retries
            # do not race into duplicate pod creation.
            # Reuse existing session if pod is still running
            try:
                pod_name, pod_ip = await self._resolve_session(session_id)
                logger.info(f"[SandboxManager] Reusing existing session {session_id}")
                http_client = self._get_http_client(pod_name, pod_ip)
                try:
                    # Merge upload_file_ids on the pod annotation
                    if normalized_upload_file_ids:
                        if self._use_docker_compose:
                            session_metadata = self._compose_sessions.get(session_id, {})
                            existing_upload_ids = self._normalize_upload_file_ids(
                                session_metadata.get("upload_file_ids")
                            )
                            merged_upload_file_ids = self._normalize_upload_file_ids(
                                existing_upload_ids + normalized_upload_file_ids
                            )
                            if merged_upload_file_ids != existing_upload_ids:
                                session_metadata["upload_file_ids"] = merged_upload_file_ids
                        else:
                            session_metadata = await self._get_session_metadata(session_id)
                            existing_upload_ids = self._normalize_upload_file_ids(
                                session_metadata.get("upload_file_ids") if session_metadata else None
                            )
                            merged_upload_file_ids = self._normalize_upload_file_ids(
                                existing_upload_ids + normalized_upload_file_ids
                            )
                            if merged_upload_file_ids != existing_upload_ids:
                                try:
                                    await self._k8s_core_api.patch_namespaced_pod(
                                        name=pod_name, namespace=SANDBOX_NAMESPACE,
                                        body={"metadata": {"annotations": {
                                            "sandbox.boxkite.dev/upload-file-ids": json.dumps(
                                                merged_upload_file_ids
                                            ),
                                        }}}
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f"[SandboxManager] Failed to update upload annotations: {e}"
                                    )

                    await self._prefetch_uploads_for_session(
                        http_client=http_client,
                        organization_id=organization_id,
                        upload_file_ids=normalized_upload_file_ids or None,
                    )
                    return {"pod_name": pod_name}
                except Exception as e:
                    logger.warning(
                        f"[SandboxManager] Reuse prefetch failed for session {session_id}; recreating: {e}"
                    )
                    # This is an internal repair path, not an agent/session teardown.
                    # Keep the skills payload cached so the recreated sidecar can
                    # receive /ensure-skills before tool execution resumes.
                    await self.destroy_session(session_id, preserve_cached_skills=True)
            except ValueError:
                pass  # No existing session

            if self._use_docker_compose:
                if uses_custom_sizing:
                    # Compose mode's resources are fixed in deploy/docker-compose.yml
                    # and aren't wired to resource_config.py at all -- there's no
                    # per-container resize lever to pull here yet.
                    logger.warning(
                        f"[SandboxManager] Ignoring custom sandbox size/storage/lifetime "
                        f"for session {session_id}: not supported in Docker Compose mode"
                    )
                if restore_from_snapshot_id:
                    logger.info(
                        f"[SandboxManager] restore_from_snapshot_id={restore_from_snapshot_id} "
                        f"is observational-only in Docker Compose mode (no pod annotations to record it on)"
                    )
                if image_ref:
                    # Compose mode's sandbox container image is fixed in
                    # deploy/docker-compose.yml, same limitation
                    # uses_custom_sizing already logs above -- there's no
                    # per-container image override lever in compose mode.
                    logger.warning(
                        f"[SandboxManager] Ignoring custom image_ref for session {session_id}: "
                        "not supported in Docker Compose mode"
                    )
                if mcp_connection_grants:
                    # Compose mode has no K8s API at all, so there is no
                    # NetworkPolicy to widen -- same "nothing to plug this
                    # into" limitation image_ref/uses_custom_sizing already
                    # log above.
                    logger.warning(
                        f"[SandboxManager] Ignoring mcp_connection_grants for session {session_id}: "
                        "no NetworkPolicy mechanism in Docker Compose mode"
                    )
                if browser_enabled:
                    # Same "no NetworkPolicy mechanism at all" limitation --
                    # the browser tool, if also exposed via
                    # create_sandbox_tool_specs(enable_browser_tool=True),
                    # simply gets no egress in compose mode regardless of
                    # this flag.
                    logger.warning(
                        f"[SandboxManager] Ignoring browser_enabled for session {session_id}: "
                        "no NetworkPolicy mechanism in Docker Compose mode"
                    )
                if gpu_count:
                    # Compose mode's services are fixed in
                    # deploy/docker-compose.yml -- there's no Kubernetes
                    # extended-resource scheduling concept to request a GPU
                    # through at all in this mode.
                    logger.warning(
                        f"[SandboxManager] Ignoring gpu_count for session {session_id}: "
                        "not supported in Docker Compose mode"
                    )
                return await self._create_compose_session(
                    organization_id,
                    session_id,
                    work_item_id,
                    normalized_upload_file_ids or None,
                    secret_grants=secret_grants,
                    secret_capability_token=secret_capability_token,
                    secrets_control_plane_url=secrets_control_plane_url,
                )

            return await self._create_k8s_session(
                organization_id,
                session_id,
                work_item_id,
                normalized_upload_file_ids or None,
                size=size,
                volume_size_limit=volume_size_limit,
                lifetime_seconds=lifetime_seconds,
                restore_from_snapshot_id=restore_from_snapshot_id,
                secret_grants=secret_grants,
                secret_capability_token=secret_capability_token,
                secrets_control_plane_url=secrets_control_plane_url,
                image_ref=image_ref,
                volume_mounts=volume_mounts,
                mcp_connection_grants=mcp_connection_grants,
                browser_enabled=browser_enabled,
                gpu_count=gpu_count,
            )

    async def _prefetch_uploads_for_session(
        self,
        http_client: httpx.AsyncClient,
        organization_id: Optional[UUID],
        upload_file_ids: Optional[list[str]] = None,
    ) -> None:
        """Fetch newly attached uploads for an existing session without reconfigure."""
        response = await http_client.post(
            "/prefetch-uploads",
            json={
                "organization_id": str(organization_id) if organization_id else None,
                "upload_file_ids": upload_file_ids,
            },
        )
        if response.status_code == 404:
            logger.warning(
                "[SandboxManager] Sidecar missing /prefetch-uploads endpoint; skipping upload prefetch"
            )
            return
        response.raise_for_status()
        prefetch_result = response.json()
        logger.info(
            f"[SandboxManager] Session upload prefetch complete, "
            f"{len(prefetch_result.get('prefetched_files', []))} files"
        )

    async def _create_compose_session(
        self,
        organization_id: UUID,
        session_id: str,
        work_item_id: Optional[UUID] = None,
        upload_file_ids: Optional[list[str]] = None,
        secret_grants: Optional[list[dict]] = None,
        secret_capability_token: Optional[str] = None,
        secrets_control_plane_url: Optional[str] = None,
    ) -> dict:
        """Create session in Docker Compose mode (local dev)."""
        s3_prefix = self._build_storage_prefix(organization_id, session_id, work_item_id)
        normalized_upload_file_ids = upload_file_ids or []

        async with httpx.AsyncClient(
            base_url=self._compose_url,
            timeout=30,
            headers=self._auth_headers_for_pod("compose-sandbox"),
        ) as temp_client:
            response = await temp_client.post("/configure", json={
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
            logger.info(f"[SandboxManager] Compose session configured, prefetched {len(config_result.get('prefetched_files', []))} files")

        # Store metadata for compose mode (no K8s to persist on)
        self._compose_sessions[session_id] = {
            "organization_id": organization_id,
            "work_item_id": work_item_id,
            "upload_file_ids": normalized_upload_file_ids,
            "storage_prefix": s3_prefix,
        }
        await self._record_session_metadata(
            session_id,
            organization_id=organization_id,
            work_item_id=work_item_id,
            storage_prefix=s3_prefix,
            upload_file_ids=normalized_upload_file_ids,
        )
        self._invalidate_session_endpoint(session_id)
        return {"pod_name": "compose-sandbox"}

