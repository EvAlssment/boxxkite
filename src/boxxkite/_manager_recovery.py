"""Locks, K8s/sidecar retry, and mid-message session recovery for SandboxManager."""

from ._manager_config import *  # noqa: F401,F403

class RecoveryMixin:
    async def get_session(self, session_id: str) -> Optional[dict]:
        """Get an active session by ID. Returns basic info dict or None."""
        try:
            pod_name, pod_ip = await self._resolve_session(session_id)
            return {"pod_name": pod_name, "pod_ip": pod_ip}
        except (ValueError, RuntimeError):
            return None

    async def get_sidecar_pty_target(self, session_id: str) -> dict:
        """Resolve session_id to the sidecar's `/pty` WebSocket endpoint, for
        the control-plane's `WS /v1/sandboxes/{id}/takeover` proxy (see
        `docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 3). Reuses the exact
        same session -> pod resolution `execute()`/`file_create()`/etc.
        already use (`_resolve_session`) and the exact same sidecar auth
        token every other sidecar call already attaches
        (`_auth_headers_for_pod`'s token source) -- this is not a new
        privilege or a new auth mechanism, just the one public accessor the
        existing HTTP-only helpers (`_get_http_client`/`_build_sidecar_url`)
        don't cover, since a WebSocket proxy can't reuse the pooled httpx
        client those build for request/response calls.

        Raises ValueError/RuntimeError exactly like `_resolve_session` for
        an unknown or unreachable session -- callers translate that into a
        404/502 the same way every other route in sandboxes.py already does
        for those exceptions.
        """
        pod_name, pod_ip = await self._resolve_session(session_id)
        if self._use_docker_compose:
            token = self._compose_auth_token
            ws_url = self._compose_url.replace("http://", "ws://").replace("https://", "wss://") + "/pty"
        else:
            token = await self._ensure_pod_auth_token_cached(pod_name)
            # NOTE: unlike _build_sidecar_url's HTTP path, this always
            # connects directly to the pod IP, even when
            # SANDBOX_USE_K8S_PROXY is set -- the K8s API proxy path used
            # for HTTP calls is an HTTPS reverse proxy that does not
            # reliably support WebSocket upgrades. Takeover in
            # SANDBOX_USE_K8S_PROXY dev setups is a known, disclosed gap
            # (see docs/SANDBOX-OBSERVABILITY-DESIGN.md) -- direct pod-IP
            # connectivity (the normal in-cluster path) is unaffected.
            ws_url = f"ws://{pod_ip}:{SIDECAR_PORT}/pty"
        return {
            "pod_name": pod_name,
            "pod_ip": pod_ip,
            "ws_url": ws_url,
            "auth_header": SIDECAR_AUTH_HEADER,
            "auth_token": token,
        }

    async def get_sidecar_desktop_target(self, session_id: str) -> dict:
        """Resolve session_id to the sidecar's `/desktop` WebSocket endpoint,
        for the control-plane's `WS /v1/sandboxes/{id}/desktop` proxy
        (GitHub issue #184, docs/GUI-COMPUTER-USE-SCOPING.md). Copied
        verbatim from get_sidecar_pty_target above except the URL suffix --
        same pod resolution, same auth token source, same compose-vs-k8s
        branching, same disclosed SANDBOX_USE_K8S_PROXY WebSocket-upgrade
        caveat."""
        pod_name, pod_ip = await self._resolve_session(session_id)
        if self._use_docker_compose:
            token = self._compose_auth_token
            ws_url = self._compose_url.replace("http://", "ws://").replace("https://", "wss://") + "/desktop"
        else:
            token = await self._ensure_pod_auth_token_cached(pod_name)
            ws_url = f"ws://{pod_ip}:{SIDECAR_PORT}/desktop"
        return {
            "pod_name": pod_name,
            "pod_ip": pod_ip,
            "ws_url": ws_url,
            "auth_header": SIDECAR_AUTH_HEADER,
            "auth_token": token,
        }

    def _get_recovery_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._recovery_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._recovery_locks[session_id] = lock
            self._trim_recovery_lock_cache(preserve_session_id=session_id)
        self._recovery_locks.move_to_end(session_id)
        return lock

    def _trim_recovery_lock_cache(self, preserve_session_id: Optional[str] = None) -> None:
        """Best-effort cap for recovery locks; never evict an active lock."""
        while len(self._recovery_locks) > SANDBOX_RECOVERY_LOCK_CACHE_MAX_ENTRIES:
            evicted = False
            for stale_session_id, stale_lock in list(self._recovery_locks.items()):
                if preserve_session_id is not None and stale_session_id == preserve_session_id:
                    continue
                if stale_lock.locked():
                    continue
                self._recovery_locks.pop(stale_session_id, None)
                evicted = True
                break
            if not evicted:
                break

    def _release_recovery_lock_if_idle(self, session_id: str) -> None:
        """Drop a recovery lock only when no caller holds or waits on it."""
        lock = self._recovery_locks.get(session_id)
        if lock is None:
            return
        if lock.locked():
            return
        # asyncio.Lock has no public waiter count. A released lock can still
        # have queued waiters that have not resumed yet; removing it then lets a
        # later caller create a second lock and breaks recovery single-flight.
        if getattr(lock, "_waiters", None):
            return
        self._recovery_locks.pop(session_id, None)

    def _get_session_create_lock(self, session_id: str) -> asyncio.Lock:
        # Keep one lock per session_id so all callers share the same critical section.
        lock = self._session_create_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_create_locks[session_id] = lock
            self._trim_session_create_lock_cache(preserve_session_id=session_id)
        self._session_create_locks.move_to_end(session_id)
        return lock

    def _trim_session_create_lock_cache(self, preserve_session_id: Optional[str] = None) -> None:
        """Best-effort cap for session-create locks; never evict an active lock."""
        while len(self._session_create_locks) > SANDBOX_CREATE_LOCK_CACHE_MAX_ENTRIES:
            evicted = False
            for stale_session_id, stale_lock in list(self._session_create_locks.items()):
                if preserve_session_id is not None and stale_session_id == preserve_session_id:
                    continue
                if stale_lock.locked():
                    continue
                self._session_create_locks.pop(stale_session_id, None)
                evicted = True
                break
            if not evicted:
                break

    def _cache_session_skills(self, session_id: str, skills: list[dict]) -> None:
        """LRU-bound cache for per-session skills payloads."""
        self._session_skills[session_id] = skills
        self._session_skills.move_to_end(session_id)
        while len(self._session_skills) > SANDBOX_SKILLS_CACHE_MAX_ENTRIES:
            self._session_skills.popitem(last=False)

    async def _create_retry_k8s_core_api(self) -> tuple[client.ApiClient, client.CoreV1Api]:
        # Reuse already-loaded cluster config, but isolate the retry onto a fresh
        # ApiClient so one failed handshake does not disrupt the shared singleton client.
        await self._init_k8s()
        if not self._k8s_initialized:
            raise RuntimeError("K8s API not initialized")
        api_client = build_kubernetes_api_client()
        return api_client, client.CoreV1Api(api_client)

    @staticmethod
    def _is_retryable_k8s_error(error: Exception) -> bool:
        if isinstance(error, ApiException):
            return error.status in {429, 500, 502, 503, 504}
        return isinstance(
            error,
            (
                ClientConnectionError,
                ConnectionError,
                asyncio.TimeoutError,
                TimeoutError,
            ),
        )

    async def _call_k8s_api_with_retry(
        self,
        *,
        operation: str,
        core_api: client.CoreV1Api,
        request_fn: Callable[[client.CoreV1Api], Awaitable[_T]],
    ) -> _T:
        try:
            return await request_fn(core_api)
        except Exception as first_error:
            if not self._is_retryable_k8s_error(first_error):
                raise

            # Retry on an isolated ApiClient; the failure we saw was on connection
            # setup, and resetting the shared singleton can disrupt other callers.
            logger.warning(
                f"[SandboxManager] K8s API {operation} failed; retrying once with a fresh ApiClient: "
                f"{first_error}"
            )
            retry_api_client, retry_core_api = await self._create_retry_k8s_core_api()
            try:
                return await request_fn(retry_core_api)
            finally:
                try:
                    await retry_api_client.close()
                except Exception as close_error:
                    logger.warning(
                        "[SandboxManager] Error closing retry Kubernetes ApiClient: "
                        f"{close_error}"
                    )

    @staticmethod
    def _is_retryable_sidecar_error(error: Exception) -> bool:
        if isinstance(error, FileNotFoundError):
            return False
        if isinstance(error, httpx.HTTPStatusError):
            return error.response is not None and error.response.status_code in {502, 503, 504}
        if isinstance(error, ValueError) and (
            "No running pod found" in str(error)
            or ("Session" in str(error) and "not found" in str(error))
        ):
            return True
        return isinstance(
            error,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.RemoteProtocolError,
                httpx.WriteError,
                httpx.ReadError,
                httpx.PoolTimeout,
            ),
        )

    async def _session_sidecar_available(self, session_id: str) -> bool:
        """
        Check whether the current session sidecar is reachable.

        Used to avoid duplicate recoveries when multiple concurrent calls hit the
        same failure and wait on the per-session recovery lock.
        """
        try:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.get("/health", timeout=5)
            return response.status_code == 200
        except Exception:
            return False

    async def _recover_session_after_sidecar_error(self, session_id: str, error: Exception) -> None:
        # Prefer pod metadata first, then reconstruct from DB if the pod is gone.
        metadata = await self._get_session_metadata(session_id)
        if not metadata or not metadata.get("organization_id"):
            metadata = await self._reconstruct_session_metadata_from_db(session_id)
            if metadata:
                logger.warning(
                    f"[SandboxManager] Session {session_id} missing complete pod metadata; "
                    "using DB reconstruction fallback"
                )
        if not metadata:
            raise ValueError(f"Session {session_id} not found for recovery")

        org_id = metadata.get("organization_id")
        if not org_id:
            raise RuntimeError(
                f"Session {session_id} has no organization_id; cannot perform automatic recovery"
            )

        logger.warning(
            f"[SandboxManager] Recovering session {session_id} after sidecar transport error: {error}"
        )
        # create_session can destroy/recreate a stale session underneath us. Keep
        # a local copy so skills are not lost before the recovery replay step.
        cached_skills = self._session_skills.get(session_id)
        self._invalidate_session_endpoint(session_id)
        await self.create_session(
            organization_id=org_id,
            session_id=session_id,
            work_item_id=metadata.get("work_item_id"),
            upload_file_ids=metadata.get("upload_file_ids"),
        )
        if cached_skills and session_id not in self._session_skills:
            self._cache_session_skills(session_id, cached_skills)

    async def _replay_skills_after_recovery(self, session_id: str) -> dict:
        """Replay cached skills onto a newly recovered sidecar."""
        skills = self._session_skills.get(session_id)
        if not skills:
            return {}

        pod_name, pod_ip = await self._resolve_session(session_id)
        http_client = self._get_http_client(pod_name, pod_ip)
        response = await http_client.post(
            "/ensure-skills",
            json={
                "skills": skills,
                "skills_rev": self._compute_skills_rev(skills),
            },
        )
        response.raise_for_status()
        logger.info(f"[SandboxManager] Replayed skills for session {session_id} after recovery")
        return response.json()

    async def _call_sidecar_with_recovery(
        self,
        *,
        session_id: str,
        operation: str,
        request_fn: Callable[[], Awaitable[_T]],
    ) -> _T:
        try:
            return await request_fn()
        except Exception as first_error:
            if not self._is_retryable_sidecar_error(first_error):
                raise

            # TEMPORARY FIX:
            # We do one best-effort session recreation + retry when transport errors happen
            # mid-run. A robust fix still requires explicit orchestration for live migration
            # and stronger durability guarantees around in-flight filesystem changes.
            logger.warning(
                f"[SandboxManager] Temporary sidecar recovery path for '{operation}' "
                f"(session={session_id}): {first_error}"
            )
            lock = self._get_recovery_lock(session_id)
            async with lock:
                # Another concurrent caller may have already recovered this session
                # while we were waiting for the lock.
                if not await self._session_sidecar_available(session_id):
                    await self._recover_session_after_sidecar_error(session_id, first_error)
                else:
                    logger.info(
                        f"[SandboxManager] Skipping duplicate recovery for session {session_id}; "
                        "sidecar is already healthy"
                    )

                # Keep recovery replay single-flight per session. The sidecar
                # materializes skill files under shared session paths, so overlapping
                # /ensure-skills rebuilds from concurrent waiters can corrupt the
                # recovered pod before tool calls resume.
                replay_result: Optional[dict] = None
                if self._session_skills.get(session_id):
                    try:
                        replay_result = await self._replay_skills_after_recovery(session_id)
                    except Exception as replay_error:
                        logger.warning(
                            f"[SandboxManager] Skills replay failed after recovery for session "
                            f"{session_id}; retrying recovery once: {replay_error}"
                        )
                        if not self._is_retryable_sidecar_error(replay_error):
                            raise RuntimeError(
                                f"Failed to replay skills after recovery for session {session_id}"
                            ) from replay_error

                        if not await self._session_sidecar_available(session_id):
                            await self._recover_session_after_sidecar_error(session_id, replay_error)

                        try:
                            replay_result = await self._replay_skills_after_recovery(session_id)
                        except Exception as second_replay_error:
                            raise RuntimeError(
                                f"Failed to replay skills after recovery for session {session_id}"
                            ) from second_replay_error

                if operation == "ensure_skills" and replay_result is not None:
                    return cast(_T, replay_result)

            return await request_fn()
        finally:
            self._release_recovery_lock_if_idle(session_id)

    # =========================================================================
    # Tool Operations (HTTP routing to sidecar)
    # =========================================================================

