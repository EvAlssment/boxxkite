"""HTTP client pool, sidecar URL building, and shutdown for SandboxManager."""

from ._manager_config import *  # noqa: F401,F403

class HttpClientMixin:
    def _get_http_client(self, pod_name: str, pod_ip: str) -> httpx.AsyncClient:
        """Get or create HTTP client for a pod (connection pool)."""
        client = self._http_clients.get(pod_name)
        if client and not self._use_docker_compose:
            # Pod names can be reused after recovery; recreate client if endpoint changed.
            expected_url = self._build_sidecar_url(pod_name, pod_ip)
            if str(client.base_url).rstrip("/") != expected_url.rstrip("/"):
                self._http_clients.pop(pod_name, None)
                try:
                    asyncio.create_task(client.aclose())
                except RuntimeError:
                    pass
                client = None
            else:
                self._http_clients.move_to_end(pod_name)

        if client is None:
            if self._use_docker_compose:
                base_url = self._compose_url
                verify: bool | ssl.SSLContext = True
            else:
                base_url = self._build_sidecar_url(pod_name, pod_ip)
                verify = self._pinned_verify_for_pod(pod_name)
            client = httpx.AsyncClient(
                base_url=base_url,
                timeout=REQUEST_TIMEOUT,
                headers=self._auth_headers_for_pod(pod_name),
                verify=verify,
            )
            self._http_clients[pod_name] = client
            self._http_clients.move_to_end(pod_name)
            self._trim_http_client_cache()

        return client

    def _trim_http_client_cache(self) -> None:
        """Bound HTTP client cache size to avoid unbounded growth in long-lived processes."""
        while len(self._http_clients) > SANDBOX_HTTP_CLIENT_CACHE_MAX_ENTRIES:
            stale_pod_name, stale_client = self._http_clients.popitem(last=False)
            logger.warning(
                "[SandboxManager] Evicting stale HTTP client from cache "
                f"(pod={stale_pod_name}, max={SANDBOX_HTTP_CLIENT_CACHE_MAX_ENTRIES})"
            )
            try:
                asyncio.create_task(stale_client.aclose())
            except RuntimeError:
                pass

    def _cache_session_endpoint(self, session_id: str, pod_name: str, pod_ip: str) -> None:
        self._session_endpoints[session_id] = (time.monotonic(), pod_name, pod_ip)
        self._session_endpoints.move_to_end(session_id)
        while len(self._session_endpoints) > SANDBOX_SESSION_ENDPOINT_CACHE_MAX_ENTRIES:
            self._session_endpoints.popitem(last=False)

    def _get_cached_session_endpoint(self, session_id: str) -> Optional[tuple[str, str]]:
        cached = self._session_endpoints.get(session_id)
        if cached is None:
            return None

        cached_at, pod_name, pod_ip = cached
        if (time.monotonic() - cached_at) > SANDBOX_SESSION_ENDPOINT_TTL_SECONDS:
            self._session_endpoints.pop(session_id, None)
            return None

        self._session_endpoints.move_to_end(session_id)
        return pod_name, pod_ip

    def _invalidate_session_endpoint(self, session_id: str) -> None:
        self._session_endpoints.pop(session_id, None)

    @classmethod
    def _is_running_claimed_session_pod(cls, pod: Any, session_id: str) -> bool:
        labels = pod.metadata.labels or {}
        annotations = pod.metadata.annotations or {}
        if labels.get("sandbox.boxkite.dev/status") != "claimed":
            return False
        if not cls._metadata_matches_session_id(labels, annotations, session_id):
            return False
        return pod.status.phase == "Running" and bool(pod.status.pod_ip)

    def _build_sidecar_url(self, pod_name: str, pod_ip: str) -> str:
        """Build the sidecar base URL for a pod.

        When SANDBOX_USE_K8S_PROXY=true, routes HTTP through a local
        ``kubectl proxy`` (default http://localhost:8001) instead of direct
        pod IPs.  This is needed on macOS where pod IPs inside a kind
        cluster are not directly reachable from the host.

        Start the proxy first::

            kubectl proxy --context kind-boxkite-dev &

        DELIBERATE CARVE-OUT (see docs/SIDECAR-TRANSPORT-TLS-DESIGN.md §5):
        this proxy mode always stays plain ``http://``, even when TLS is
        otherwise enabled. Whether the K8s API server's pod-proxy
        subresource cleanly forwards to an HTTPS backend with a
        self-signed, non-cluster-CA-issued cert is unverified against a
        real cluster, and SANDBOX_USE_K8S_PROXY is already documented as a
        macOS/kind local-dev-only convenience (pod IPs inside a kind
        cluster aren't directly reachable from the host at all) -- treating
        it like SIDECAR_TLS_DISABLED=true here is the same pragmatic,
        explicitly-scoped answer the design doc calls out as reasonable,
        made explicit rather than silently assumed.

        Otherwise this is ``https://`` by default (self-signed per-pod cert,
        pinned by the manager — see tls.py) unless SIDECAR_TLS_DISABLED=true,
        in which case it falls back to plain ``http://`` (see SECURITY.md's
        discussion of that escape hatch's own risk).
        """
        if self._use_k8s_proxy:
            return (
                f"{self._k8s_proxy_url}/api/v1/namespaces/{SANDBOX_NAMESPACE}"
                f"/pods/{pod_name}:{SIDECAR_PORT}/proxy"
            )
        scheme = "http" if sidecar_tls_disabled() else "https"
        return f"{scheme}://{pod_ip}:{SIDECAR_PORT}"

    async def _close_http_client(self, pod_name: str) -> None:
        """Close and remove HTTP client for a pod.

        Deliberately does NOT drop the cached sidecar auth token — callers
        may recycle the pod (not delete it) right after closing the client,
        and a recycled pod keeps the same token for its whole lifetime (see
        sidecar_auth.py). Token cache cleanup happens in _delete_pod.
        """
        client = self._http_clients.pop(pod_name, None)
        if client:
            await client.aclose()

    async def _close_k8s_client(self) -> None:
        """Close Kubernetes ApiClient to avoid unclosed aiohttp session warnings."""
        api_client = self._k8s_api_client
        self._k8s_api_client = None
        self._k8s_core_api = None
        self._k8s_networking_api = None
        self._k8s_initialized = False
        if api_client is None:
            return
        try:
            await api_client.close()
        except Exception as e:
            logger.warning(f"[SandboxManager] Error closing Kubernetes ApiClient: {e}")

    async def shutdown(self) -> None:
        """Close pooled HTTP clients and Kubernetes client resources."""
        http_clients = list(self._http_clients.items())
        self._http_clients = OrderedDict()

        if http_clients:
            close_tasks = [client.aclose() for _, client in http_clients]
            results = await asyncio.gather(*close_tasks, return_exceptions=True)
            for (pod_name, _), result in zip(http_clients, results):
                if isinstance(result, Exception):
                    logger.warning(
                        f"[SandboxManager] Error closing HTTP client for {pod_name}: {result}"
                    )

        await self._close_k8s_client()

    # =========================================================================
    # Session Lifecycle
    # =========================================================================

