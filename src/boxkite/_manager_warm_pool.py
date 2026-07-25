"""Warm-pod claim and pod recycling for SandboxManager."""

from ._manager_config import *  # noqa: F401,F403
import boxkite.manager as _manager_pkg
from .warm_pool_sizing import CLAIM_RATE_TRACKER

# How many stale/lost ready-index entries the fast-claim path will pop-and-CAS
# before giving up and falling back to the list-based path. Bounds the work a
# poisoned index (e.g. all entries just claimed by another instance) can add.
FAST_CLAIM_MAX_CAS_ATTEMPTS = 3


class WarmPoolMixin:
    async def _claim_warm_pod_via_k8s(self, size: str = DEFAULT_SANDBOX_SIZE) -> Optional[tuple[str, str]]:
        """
        Claim a warm pod directly via K8s API.

        Used by backend request paths to avoid any in-memory warm-pool dependency.
        Uses label patching as the claim mechanism.

        size selects which per-size warm sub-pool to claim from (see
        warm_pool.py's WARM_POOL_SIZE_TARGETS, which is what actually creates
        pods at non-default sizes). Pods created before the size label
        existed have none -- those are treated as DEFAULT_SANDBOX_SIZE, same
        as warm_pool.py's own scan logic, so the two sides can't disagree
        about a given pod's size.
        """
        import time as _time
        _t0 = _time.monotonic()
        max_claimable_age = compute_max_claimable_age_seconds(
            active_deadline_seconds=SANDBOX_ACTIVE_DEADLINE_SECONDS,
            claim_age_buffer_seconds=SANDBOX_WARM_CLAIM_AGE_BUFFER_SECONDS,
            min_remaining_lifetime_seconds=SANDBOX_WARM_MIN_REMAINING_LIFETIME_SECONDS,
        )
        # Opt-in fast path (BOXKITE_FAST_CLAIM_ENABLED): pop a ready pod from
        # the warm pool's in-memory index and claim it without a per-request
        # pod LIST or Secret READ. Returns None -- and falls through to the
        # unchanged list-based path below -- whenever the index is empty, the
        # candidates all lost the compare-and-swap race, or anything errors,
        # so the fast path is never worse than the default. Flag OFF makes
        # this branch vanish and the claim path stays byte-identical to
        # before.
        if fast_claim_enabled():
            fast_result = await self._fast_claim_warm_pod(size, _t0)
            if fast_result is not None:
                return fast_result
        try:
            pods = await self._k8s_core_api.list_namespaced_pod(
                namespace=SANDBOX_NAMESPACE,
                label_selector="app=sandbox,pool=warm,sandbox.boxkite.dev/status=warm",
            )
            _t1 = _time.monotonic()
            logger.info(f"[TIMING] k8s_list_warm_pods: {(_t1 - _t0)*1000:.0f}ms ({len(pods.items)} found)")
            for pod in pods.items:
                if pod.status.phase != "Running":
                    continue
                pod_size = (pod.metadata.labels or {}).get(SANDBOX_SIZE_LABEL) or DEFAULT_SANDBOX_SIZE
                if pod_size != size:
                    continue
                # Check all containers are ready
                if not pod.status.container_statuses or not all(
                    cs.ready for cs in pod.status.container_statuses
                ):
                    continue
                # Skip pods too close to activeDeadlineSeconds (24h backstop).
                # A pod at 23h58m would get killed mid-tool-call.
                age_seconds = pod_age_seconds(pod.metadata.creation_timestamp)
                if age_seconds is not None and age_seconds >= max_claimable_age:
                    logger.info(
                        f"[SandboxManager] Skipping warm pod {pod.metadata.name}; "
                        f"too old ({age_seconds:.0f}s / {SANDBOX_ACTIVE_DEADLINE_SECONDS}s deadline)"
                    )
                    continue
                pod_name = pod.metadata.name
                pod_ip = pod.status.pod_ip
                resource_version = pod.metadata.resource_version

                # Atomic claim via compare-and-swap:
                # only transition warm->claimed if resourceVersion and labels still match.
                if not resource_version:
                    logger.warning(
                        f"[SandboxManager] Missing resourceVersion for warm pod {pod_name}; skipping"
                    )
                    continue
                try:
                    _t_patch0 = _time.monotonic()
                    await self._k8s_core_api.patch_namespaced_pod(
                        name=pod_name,
                        namespace=SANDBOX_NAMESPACE,
                        body=[
                            {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
                            {"op": "test", "path": "/metadata/labels/pool", "value": "warm"},
                            {
                                "op": "test",
                                "path": "/metadata/labels/sandbox.boxkite.dev~1status",
                                "value": "warm",
                            },
                            {"op": "replace", "path": "/metadata/labels/pool", "value": "claimed"},
                            {
                                "op": "replace",
                                "path": "/metadata/labels/sandbox.boxkite.dev~1status",
                                "value": "claimed",
                            },
                        ],
                    )
                    async def _mark_pod_non_evictable(pod_name: str) -> None:
                        try:
                            await self._k8s_core_api.patch_namespaced_pod(
                                name=pod_name,
                                namespace=SANDBOX_NAMESPACE,
                                body={
                                    "metadata": {
                                        "annotations": {
                                            SAFE_TO_EVICT_ANNOTATION: "false",
                                        }
                                    }
                                },
                            )
                        except Exception as annotation_error:
                            logger.warning(
                                f"[SandboxManager] Failed to mark claimed pod {pod_name} "
                                f"as non-evictable: {annotation_error}"
                            )

                    asyncio.create_task(_mark_pod_non_evictable(pod_name))
                    logger.info(f"[TIMING] k8s_patch_claim: {(_time.monotonic() - _t_patch0)*1000:.0f}ms")
                    logger.info(f"[SandboxManager] Claimed warm pod via K8s labels: {pod_name}")
                    # Recover the sidecar auth token WarmPoolManager generated
                    # when it created this pod (this process didn't create it)
                    # from its Secret.
                    _t_secret0 = _time.monotonic()
                    await self._ensure_pod_secret_cached(pod_name)
                    logger.info(f"[TIMING] k8s_read_secret: {(_time.monotonic() - _t_secret0)*1000:.0f}ms")
                    # Feeds warm_pool.py's opt-in adaptive sizer (issue #156)
                    # -- this is the real production claim path (see this
                    # method's own docstring), so the rolling claim-rate
                    # signal it drives has to be recorded HERE, not on
                    # WarmPoolManager.claim_pod, which nothing currently
                    # calls.
                    CLAIM_RATE_TRACKER.record_claim(size)
                    return (pod_name, pod_ip)
                except ApiException as e:
                    if e.status in {409, 422}:
                        logger.info(
                            f"[SandboxManager] Warm pod {pod_name} was claimed concurrently ({e.status})"
                        )
                    else:
                        logger.warning(f"[SandboxManager] Failed to patch pod {pod_name}: {e.status}")
                    continue

        except Exception as e:
            logger.warning(
                f"[SandboxManager] K8s warm pod discovery failed; falling back to a "
                f"cold pod create for this session: {e}"
            )

        return None

    async def _mark_pod_non_evictable_async(self, pod_name: str) -> None:
        """Fire-and-forget annotate of a just-claimed pod as non-evictable.
        Mirrors the nested annotate in the list-based claim path; kept as a
        method so the fast path can schedule it via asyncio.create_task
        without duplicating the try/except inline."""
        try:
            await self._k8s_core_api.patch_namespaced_pod(
                name=pod_name,
                namespace=SANDBOX_NAMESPACE,
                body={"metadata": {"annotations": {SAFE_TO_EVICT_ANNOTATION: "false"}}},
            )
        except Exception as annotation_error:
            logger.warning(
                f"[SandboxManager] Failed to mark claimed pod {pod_name} "
                f"as non-evictable: {annotation_error}"
            )

    async def _fast_claim_warm_pod(
        self, size: str, _t0: float
    ) -> Optional[tuple[str, str]]:
        """Fast warm-pod claim off the WarmPoolManager's in-memory ready
        index (opt-in, BOXKITE_FAST_CLAIM_ENABLED). See
        _claim_warm_pod_via_k8s for how this fits in.

        Removes the two per-request K8s round-trips the list-based path pays
        -- the pod LIST and the sidecar-Secret READ -- while KEEPING the
        compare-and-swap label patch as the correctness arbiter: a pod is
        never returned without a successful CAS, so a stale index entry (lost
        the race, already claimed, resourceVersion moved on) just fails the
        CAS and we try the next candidate. Returns None on an empty index, on
        exhausting the retry budget, or on any error -- the caller then falls
        back to the unchanged list-based path.
        """
        import time as _time

        from .warm_pool import get_warm_pool

        try:
            warm_pool = await get_warm_pool()
        except Exception as e:
            logger.warning(
                f"[SandboxManager] fast-claim: get_warm_pool failed; "
                f"falling back to list-based claim: {e}"
            )
            return None
        if warm_pool is None:
            return None

        attempts = 0
        while attempts < FAST_CLAIM_MAX_CAS_ATTEMPTS:
            try:
                ready = await warm_pool.pop_ready_pod(size)
            except Exception as e:
                logger.warning(
                    f"[SandboxManager] fast-claim: ready-index pop failed; "
                    f"falling back to list-based claim: {e}"
                )
                return None
            if ready is None:
                return None
            attempts += 1
            if not ready.resource_version:
                continue
            try:
                await self._k8s_core_api.patch_namespaced_pod(
                    name=ready.pod_name,
                    namespace=SANDBOX_NAMESPACE,
                    body=[
                        {"op": "test", "path": "/metadata/resourceVersion", "value": ready.resource_version},
                        {"op": "test", "path": "/metadata/labels/pool", "value": "warm"},
                        {
                            "op": "test",
                            "path": "/metadata/labels/sandbox.boxkite.dev~1status",
                            "value": "warm",
                        },
                        {"op": "replace", "path": "/metadata/labels/pool", "value": "claimed"},
                        {
                            "op": "replace",
                            "path": "/metadata/labels/sandbox.boxkite.dev~1status",
                            "value": "claimed",
                        },
                    ],
                )
            except ApiException as e:
                if e.status in {409, 422}:
                    logger.info(
                        f"[SandboxManager] fast-claim: warm pod {ready.pod_name} "
                        f"lost CAS ({e.status}); trying next candidate"
                    )
                    continue
                logger.warning(
                    f"[SandboxManager] fast-claim: patch failed for {ready.pod_name} "
                    f"({e.status}); falling back to list-based claim"
                )
                return None
            except Exception as e:
                logger.warning(
                    f"[SandboxManager] fast-claim: unexpected patch error for "
                    f"{ready.pod_name}; falling back to list-based claim: {e}"
                )
                return None

            asyncio.create_task(self._mark_pod_non_evictable_async(ready.pod_name))
            # Precomputed connect info skips the hot-path Secret read. If the
            # index entry somehow lacks the token (edge case), recover it the
            # slow way rather than handing back a pod we can't authenticate to.
            if ready.auth_token:
                self._seed_pod_secret_cache(
                    ready.pod_name, ready.auth_token, ready.tls_cert_pem
                )
            else:
                await self._ensure_pod_secret_cached(ready.pod_name)
            CLAIM_RATE_TRACKER.record_claim(size)
            logger.info(f"[TIMING] fast_claim: {(_time.monotonic() - _t0)*1000:.0f}ms")
            logger.info(
                f"[SandboxManager] Fast-claimed warm pod via ready index: {ready.pod_name}"
            )
            return (ready.pod_name, ready.pod_ip)

        return None

    async def _recycle_pod_via_k8s(self, pod_name: str, pod_ip: str) -> bool:
        """
        Recycle a pod to warm state without relying on in-memory warm-pool state.

        Returns:
            True if recycled to warm, False if caller should delete pod.
        """
        WARM_POOL_RECYCLE = _manager_pkg.WARM_POOL_RECYCLE
        WARM_POOL_MAX = _manager_pkg.WARM_POOL_MAX
        if not WARM_POOL_RECYCLE:
            return False

        await self._init_k8s()
        if not self._k8s_core_api:
            return False

        # SECURITY: kill any background processes before the /configure wipe
        # below. destroy_session() already calls this before invoking
        # _recycle_pod_via_k8s, but repeating it here (idempotent -- an
        # empty registry is a no-op) makes this method safe to call from any
        # future path that doesn't already do so upstream, closing the
        # cross-tenant leak in docs/PROCESS-SESSIONS-DESIGN.md sections 2(b)/5.
        await self._kill_all_processes(pod_name, pod_ip)

        # Best-effort wipe of sidecar session/filesystem state.
        await self._ensure_pod_tls_cert_cached(pod_name)
        try:
            async with httpx.AsyncClient(
                timeout=30,
                headers=self._auth_headers_for_pod(pod_name),
                verify=self._pinned_verify_for_pod(pod_name),
            ) as client:
                response = await client.post(
                    f"{self._build_sidecar_url(pod_name, pod_ip)}/configure",
                    json={
                        "session_id": None,
                        "organization_id": None,
                        "work_item_id": None,
                        "storage_prefix": None,
                    },
                )
                response.raise_for_status()
        except Exception as e:
            logger.warning(f"[SandboxManager] Failed to wipe pod {pod_name} for recycle: {e}")
            return False

        # Enforce max active pod cap to prevent warm-pool growth after traffic spikes.
        try:
            pods = await self._k8s_core_api.list_namespaced_pod(
                namespace=SANDBOX_NAMESPACE,
                label_selector="app=sandbox",
            )
            total_active = sum(
                1 for pod in pods.items if pod.status.phase in {"Pending", "Running"}
            )
            if total_active > WARM_POOL_MAX:
                logger.info(
                    f"[SandboxManager] Pool full ({total_active}/{WARM_POOL_MAX}), "
                    f"deleting {pod_name} instead of recycling"
                )
                return False
        except Exception as e:
            logger.warning(
                f"[SandboxManager] Failed to read pool status while recycling {pod_name}: {e}"
            )
            return False

        # Restore warm labels and clear session metadata.
        try:
            await self._k8s_core_api.patch_namespaced_pod(
                name=pod_name,
                namespace=SANDBOX_NAMESPACE,
                body={"metadata": {
                    "labels": {
                        "pool": "warm",
                        "sandbox.boxkite.dev/status": "warm",
                        "session-id": None,
                        "organization-id": None,
                        "work-item-id": None,
                    },
                    "annotations": {
                        "sandbox.boxkite.dev/storage-prefix": None,
                        "sandbox.boxkite.dev/upload-file-ids": None,
                        SESSION_ID_ANNOTATION: None,
                        ORGANIZATION_ID_ANNOTATION: None,
                        WORK_ITEM_ID_ANNOTATION: None,
                        SAFE_TO_EVICT_ANNOTATION: "true",
                    },
                }},
            )
            logger.info(f"[SandboxManager] Recycled pod {pod_name} to warm state")
            # SECURITY (issue #74): this pod's previous session may have
            # been granted secrets and carried a scoped egress
            # NetworkPolicy for them -- a future session that claims this
            # SAME pod from the warm pool must never inherit it. Torn down
            # here, at session-end, not left as a standing rule for
            # whichever tenant claims this pod next; the claiming session's
            # own _sync_secrets_egress_network_policy call (in
            # _create_k8s_session) provisions a fresh one if that NEW
            # session itself was granted secrets.
            await self._delete_secrets_egress_network_policy(pod_name)
            return True
        except Exception as e:
            logger.warning(f"[SandboxManager] Failed to patch pod {pod_name} to warm state: {e}")
            return False

