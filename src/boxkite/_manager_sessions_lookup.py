"""Session resolution and cached-endpoint lookup for SandboxManager."""

from ._manager_config import *  # noqa: F401,F403

class SessionLookupMixin:
    async def _resolve_session(self, session_id: str) -> tuple[str, str]:
        """
        Resolve session_id to (pod_name, pod_ip) via K8s pod labels.

        For compose mode, returns ('compose-sandbox', 'localhost').
        Raises ValueError if no running pod found for the session.
        """
        started_at = time.perf_counter()
        if self._use_docker_compose:
            if session_id not in self._compose_sessions:
                raise ValueError(f"Session {session_id} not found")
            logger.info(
                "[Timing] SandboxManager.resolve_session.total: %dms (session_id=%s source=compose)",
                round((time.perf_counter() - started_at) * 1000),
                session_id,
            )
            return ("compose-sandbox", "localhost")

        cached = self._get_cached_session_endpoint(session_id)
        if cached is not None:
            cached_pod_name, _cached_pod_ip = cached
            validate_started_at = time.perf_counter()
            validated = await self._validate_cached_session_endpoint(
                session_id,
                cached_pod_name,
            )
            logger.info(
                "[Timing] SandboxManager.resolve_session.validate_cached: %dms (session_id=%s cache_hit=%s)",
                round((time.perf_counter() - validate_started_at) * 1000),
                session_id,
                "yes" if validated is not None else "stale",
            )
            if validated is not None:
                logger.info(
                    "[Timing] SandboxManager.resolve_session.total: %dms (session_id=%s source=cache)",
                    round((time.perf_counter() - started_at) * 1000),
                    session_id,
                )
                return validated

        await self._init_k8s()
        if not self._k8s_core_api:
            raise RuntimeError("K8s API not initialized")

        session_label_value = _to_k8s_label_value(session_id, prefix="session")
        core_api = self._k8s_core_api
        if core_api is None:
            raise RuntimeError("K8s API not initialized")
        # Retry once with a throwaway ApiClient because transient TLS/connector
        # failures can happen even when the shared client and sandbox pod are both healthy.
        list_started_at = time.perf_counter()
        pods = await self._call_k8s_api_with_retry(
            operation=f"resolve session {session_id}",
            core_api=core_api,
            request_fn=lambda api: api.list_namespaced_pod(
                namespace=SANDBOX_NAMESPACE,
                label_selector=(
                    "app=sandbox,"
                    "sandbox.boxkite.dev/status=claimed,"
                    f"session-id={session_label_value}"
                ),
            ),
        )
        logger.info(
            "[Timing] SandboxManager.resolve_session.k8s_lookup: %dms (session_id=%s pods=%d)",
            round((time.perf_counter() - list_started_at) * 1000),
            session_id,
            len(pods.items),
        )
        for pod in pods.items:
            if self._is_running_claimed_session_pod(pod, session_id):
                self._cache_session_endpoint(session_id, pod.metadata.name, pod.status.pod_ip)
                await self._ensure_pod_auth_token_cached(pod.metadata.name)
                await self._ensure_pod_tls_cert_cached(pod.metadata.name)
                logger.info(
                    "[Timing] SandboxManager.resolve_session.total: %dms (session_id=%s source=k8s)",
                    round((time.perf_counter() - started_at) * 1000),
                    session_id,
                )
                return (pod.metadata.name, pod.status.pod_ip)
        raise ValueError(f"No running pod found for session {session_id}")

    async def _validate_cached_session_endpoint(
        self,
        session_id: str,
        pod_name: str,
    ) -> Optional[tuple[str, str]]:
        """
        Revalidate a cached endpoint against live pod ownership.

        K8s metadata remains the source of truth across worker processes. A
        cache hit is only reusable if the pod is still running, still claimed,
        and still owned by the same session.
        """
        await self._init_k8s()
        if not self._k8s_core_api:
            return None

        core_api = self._k8s_core_api
        if core_api is None:
            return None

        try:
            pod = await self._call_k8s_api_with_retry(
                operation=f"validate cached session {session_id}",
                core_api=core_api,
                request_fn=lambda api: api.read_namespaced_pod(
                    name=pod_name,
                    namespace=SANDBOX_NAMESPACE,
                ),
            )
        except ApiException as exc:
            if exc.status != 404:
                logger.warning(
                    f"[SandboxManager] Failed validating cached endpoint for session {session_id}: {exc}"
                )
            self._invalidate_session_endpoint(session_id)
            return None
        except Exception as exc:
            logger.warning(
                f"[SandboxManager] Failed validating cached endpoint for session {session_id}: {exc}"
            )
            self._invalidate_session_endpoint(session_id)
            return None

        if not self._is_running_claimed_session_pod(pod, session_id):
            self._invalidate_session_endpoint(session_id)
            return None

        self._cache_session_endpoint(session_id, pod.metadata.name, pod.status.pod_ip)
        await self._ensure_pod_auth_token_cached(pod.metadata.name)
        await self._ensure_pod_tls_cert_cached(pod.metadata.name)
        return (pod.metadata.name, pod.status.pod_ip)

    async def _get_session_metadata(self, session_id: str) -> Optional[dict]:
        """Read full session metadata from K8s pod annotations (or compose dict)."""
        if self._use_docker_compose:
            return self._compose_sessions.get(session_id)

        await self._init_k8s()
        if not self._k8s_core_api:
            return None

        session_label_value = _to_k8s_label_value(session_id, prefix="session")
        core_api = self._k8s_core_api
        if core_api is None:
            return None
        try:
            pods = await self._call_k8s_api_with_retry(
                operation=f"get session metadata {session_id}",
                core_api=core_api,
                request_fn=lambda api: api.list_namespaced_pod(
                    namespace=SANDBOX_NAMESPACE,
                    label_selector=(
                        "app=sandbox,"
                        "sandbox.boxkite.dev/status=claimed,"
                        f"session-id={session_label_value}"
                    ),
                ),
            )
        except Exception as e:
            logger.warning(f"[SandboxManager] Failed to query session metadata: {e}")
            return None

        for pod in pods.items:
            labels = pod.metadata.labels or {}
            annotations = pod.metadata.annotations or {}
            if not self._metadata_matches_session_id(labels, annotations, session_id):
                continue
            _, org_id_str, work_item_id_str = self._identity_from_metadata(labels, annotations)

            try:
                upload_file_ids = json.loads(
                    annotations.get("sandbox.boxkite.dev/upload-file-ids", "[]")
                )
            except (json.JSONDecodeError, TypeError):
                upload_file_ids = []

            return {
                "pod_name": pod.metadata.name,
                "pod_ip": pod.status.pod_ip,
                "organization_id": self._parse_uuid(org_id_str),
                "work_item_id": self._parse_uuid(work_item_id_str),
                "upload_file_ids": upload_file_ids,
                "storage_prefix": annotations.get("sandbox.boxkite.dev/storage-prefix", ""),
            }
        return None

