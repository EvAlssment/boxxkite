"""Per-pod sidecar auth-token and pinned-TLS-cert handling for SandboxManager."""

from ._manager_config import *  # noqa: F401,F403

class TlsAuthMixin:
    def _get_pod_auth_token(self, pod_name: str) -> str:
        """
        Resolve the sidecar auth token for a pod from what's already cached.

        Order of precedence:
        1. In-memory cache (set when this process created/claimed the pod,
           or via a prior call to `_ensure_pod_auth_token_cached`).
        2. Compose mode's single statically-configured token.

        This method makes no K8s API call, so it stays synchronous for
        `_auth_headers_for_pod`'s callers (constructing an httpx client).
        A caller that might be looking at a pod this process didn't create
        must `await self._ensure_pod_auth_token_cached(pod_name)` first —
        that's the only path that reads the pod's sidecar-auth Secret.
        """
        cached = self._pod_auth_tokens.get(pod_name)
        if cached:
            return cached

        if self._use_docker_compose:
            return self._compose_auth_token

        return ""

    def _cache_pod_auth_token(self, pod_name: str, token: str) -> None:
        if not token:
            return
        self._pod_auth_tokens[pod_name] = token
        self._pod_auth_tokens.move_to_end(pod_name)
        while len(self._pod_auth_tokens) > SANDBOX_HTTP_CLIENT_CACHE_MAX_ENTRIES:
            self._pod_auth_tokens.popitem(last=False)

    @staticmethod
    def _decode_sidecar_auth_token_from_secret(secret) -> str:
        if not secret or not secret.data:
            return ""
        raw = secret.data.get(SIDECAR_AUTH_SECRET_KEY)
        if not raw:
            return ""
        try:
            return base64.b64decode(raw).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError):
            return ""

    @staticmethod
    def _decode_sidecar_tls_cert_from_secret(secret) -> str:
        """Decode the pinned TLS cert PEM from the same per-pod Secret the
        sidecar auth token lives in (see tls.py's SIDECAR_TLS_CERT_SECRET_KEY).
        Returns "" if the Secret predates TLS support (e.g. created before
        this feature existed, or SIDECAR_TLS_DISABLED was set at creation
        time) -- callers must treat an empty result as "fall back to plain
        HTTP for this pod", not as an error."""
        if not secret or not secret.data:
            return ""
        raw = secret.data.get(SIDECAR_TLS_CERT_SECRET_KEY)
        if not raw:
            return ""
        try:
            return base64.b64decode(raw).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError):
            return ""

    async def _create_sidecar_auth_secret(
        self,
        secret_name: str,
        token: str,
        tls_cert_pem: Optional[str] = None,
        tls_key_pem: Optional[str] = None,
    ) -> bool:
        """
        Create the per-pod sidecar-auth Secret referenced by the pod's
        `SIDECAR_AUTH_TOKEN` env var (via secretKeyRef — see _create_pod).

        When `tls_cert_pem`/`tls_key_pem` are given (SIDECAR_TLS_DISABLED is
        not set), the same Secret also carries the pod's pinned TLS
        keypair under tls.py's SIDECAR_TLS_CERT_SECRET_KEY/
        SIDECAR_TLS_KEY_SECRET_KEY -- one Secret, one lifecycle, one
        deletion path (_delete_sidecar_auth_secret already covers it for
        free), not a second Secret object.

        Returns True if this call created the Secret fresh, False if one by
        this name already existed (409). Tolerating 409 matters because the
        deterministic name means a concurrent create_session racing on the
        same computed pod name, or a retry after this process's own earlier
        attempt partially succeeded, can both legitimately hit this without
        it being an error — but the caller's own `token`/cert/key are only
        authoritative when this returns True; on False, whichever value is
        already stored is what actually "won" and the caller must read that
        back instead (see _create_pod's use of _ensure_pod_auth_token_cached
        / _ensure_pod_tls_cert_cached for this).
        """
        if not self._k8s_core_api:
            return False
        string_data = {SIDECAR_AUTH_SECRET_KEY: token}
        if tls_cert_pem and tls_key_pem:
            string_data[SIDECAR_TLS_CERT_SECRET_KEY] = tls_cert_pem
            string_data[SIDECAR_TLS_KEY_SECRET_KEY] = tls_key_pem
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name, namespace=SANDBOX_NAMESPACE),
            string_data=string_data,
            type="Opaque",
        )
        try:
            await self._k8s_core_api.create_namespaced_secret(namespace=SANDBOX_NAMESPACE, body=secret)
            return True
        except ApiException as e:
            if e.status != 409:
                raise
            return False

    async def _delete_sidecar_auth_secret(self, secret_name: str) -> None:
        if not self._k8s_core_api:
            return
        try:
            await self._k8s_core_api.delete_namespaced_secret(name=secret_name, namespace=SANDBOX_NAMESPACE)
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"[SandboxManager] Error deleting sidecar-auth secret {secret_name}: {e}")

    async def _sync_secrets_egress_network_policy(
        self,
        pod_name: str,
        session_label_value: str,
        secret_grants: Optional[list[dict]],
    ) -> None:
        """
        Provision (or refresh) this session's per-pod secrets-egress
        NetworkPolicy (issue #74, src/boxkite/secrets_network_policy.py),
        replacing whatever egress rule this same pod name carried for a
        PREVIOUS session (warm-pool reuse) -- never additive across
        sessions on the same pod.

        No-op entirely when BOXKITE_SECRETS_NETWORK_POLICY_ENABLED is unset
        (the default) -- this method must never be the reason a session
        with no secret_grants pays an extra K8s API round-trip.

        Best-effort: a failure here is logged, not raised. This policy is a
        supplementary, additive-only narrowing on top of
        deploy/network-policy.yaml's restrictive default -- failing to
        create/refresh it fails CLOSED (the session simply can't reach any
        secret's destination host over the network, same as if it had been
        granted no secrets at all), never open, so it must not block
        session creation.
        """
        if not BOXKITE_SECRETS_NETWORK_POLICY_ENABLED:
            return

        await self._init_k8s()
        if not self._k8s_networking_api:
            logger.warning(
                "[SandboxManager] BOXKITE_SECRETS_NETWORK_POLICY_ENABLED is "
                "set but the K8s networking API is unavailable; skipping "
                f"secrets-egress NetworkPolicy sync for pod {pod_name}"
            )
            return

        desired = build_secrets_egress_network_policy(
            pod_name=pod_name,
            namespace=SANDBOX_NAMESPACE,
            session_label_value=session_label_value,
            secret_grants=secret_grants,
        )
        if desired is None:
            await self._delete_secrets_egress_network_policy(pod_name)
            return

        policy_name = secrets_egress_policy_name(pod_name)
        try:
            await self._k8s_networking_api.replace_namespaced_network_policy(
                name=policy_name, namespace=SANDBOX_NAMESPACE, body=desired
            )
            logger.info(f"[SandboxManager] Refreshed secrets-egress NetworkPolicy {policy_name}")
        except ApiException as e:
            if e.status != 404:
                logger.error(
                    f"[SandboxManager] Failed to replace secrets-egress NetworkPolicy "
                    f"{policy_name}: {e}"
                )
                return
            try:
                await self._k8s_networking_api.create_namespaced_network_policy(
                    namespace=SANDBOX_NAMESPACE, body=desired
                )
                logger.info(f"[SandboxManager] Created secrets-egress NetworkPolicy {policy_name}")
            except ApiException as create_err:
                logger.error(
                    f"[SandboxManager] Failed to create secrets-egress NetworkPolicy "
                    f"{policy_name}: {create_err}"
                )

    async def _sync_browser_egress_network_policy(
        self,
        pod_name: str,
        session_label_value: str,
        browser_enabled: bool,
    ) -> None:
        """
        Provision (or refresh) this session's per-pod browser-egress
        NetworkPolicy (docs/BROWSER-EXEC-DESIGN.md §3,
        src/boxkite/browser_network_policy.py), replacing whatever egress
        rule this same pod name carried for a PREVIOUS session (warm-pool
        reuse) -- never additive across sessions on the same pod, same
        posture as _sync_secrets_egress_network_policy above.

        No-op entirely when BOXKITE_BROWSER_NETWORK_POLICY_ENABLED is unset
        (the default) -- this method must never be the reason a session
        pays an extra K8s API round-trip when the operator hasn't opted
        into this mechanism.

        Best-effort: a failure here is logged, not raised. This policy is
        the ONLY thing granting a browser-enabled session's driver process
        real egress (per skip_network_isolation in
        sidecar/sidecar_browser.py) -- failing to create/refresh it fails
        CLOSED (the browser process simply can't reach the network at all,
        same as any other exec'd process), never open, so it must not
        block session creation.
        """
        if not BOXKITE_BROWSER_NETWORK_POLICY_ENABLED:
            return

        await self._init_k8s()
        if not self._k8s_networking_api:
            logger.warning(
                "[SandboxManager] BOXKITE_BROWSER_NETWORK_POLICY_ENABLED is "
                "set but the K8s networking API is unavailable; skipping "
                f"browser-egress NetworkPolicy sync for pod {pod_name}"
            )
            return

        desired = build_browser_egress_network_policy(
            pod_name=pod_name,
            namespace=SANDBOX_NAMESPACE,
            session_label_value=session_label_value,
            browser_enabled=browser_enabled,
        )
        if desired is None:
            await self._delete_browser_egress_network_policy(pod_name)
            return

        policy_name = browser_egress_policy_name(pod_name)
        try:
            await self._k8s_networking_api.replace_namespaced_network_policy(
                name=policy_name, namespace=SANDBOX_NAMESPACE, body=desired
            )
            logger.info(f"[SandboxManager] Refreshed browser-egress NetworkPolicy {policy_name}")
        except ApiException as e:
            if e.status != 404:
                logger.error(
                    f"[SandboxManager] Failed to replace browser-egress NetworkPolicy "
                    f"{policy_name}: {e}"
                )
                return
            try:
                await self._k8s_networking_api.create_namespaced_network_policy(
                    namespace=SANDBOX_NAMESPACE, body=desired
                )
                logger.info(f"[SandboxManager] Created browser-egress NetworkPolicy {policy_name}")
            except ApiException as create_err:
                logger.error(
                    f"[SandboxManager] Failed to create browser-egress NetworkPolicy "
                    f"{policy_name}: {create_err}"
                )

    async def _delete_browser_egress_network_policy(self, pod_name: str) -> None:
        """Tear down this pod's browser-egress NetworkPolicy, if any --
        called at session end (recycle-to-warm or hard delete) so a
        recycled pod claimed by a DIFFERENT tenant's session never inherits
        a stale broad-egress rule. No-op if the feature is disabled or the
        K8s networking API isn't available -- mirrors
        _delete_secrets_egress_network_policy's tolerate-404 posture."""
        if not BOXKITE_BROWSER_NETWORK_POLICY_ENABLED:
            return
        await self._init_k8s()
        if not self._k8s_networking_api:
            return
        policy_name = browser_egress_policy_name(pod_name)
        try:
            await self._k8s_networking_api.delete_namespaced_network_policy(
                name=policy_name, namespace=SANDBOX_NAMESPACE
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(
                    f"[SandboxManager] Error deleting browser-egress NetworkPolicy "
                    f"{policy_name}: {e}"
                )

    async def _delete_secrets_egress_network_policy(self, pod_name: str) -> None:
        """Tear down this pod's secrets-egress NetworkPolicy, if any --
        called at session end (recycle-to-warm or hard delete) so a
        recycled pod claimed by a DIFFERENT tenant's session never inherits
        a stale egress rule (acceptance criteria: "not left as a standing
        wildcard"). No-op if the feature is disabled or the K8s networking
        API isn't available -- mirrors _delete_sidecar_auth_secret's
        tolerate-404 posture."""
        if not BOXKITE_SECRETS_NETWORK_POLICY_ENABLED:
            return
        await self._init_k8s()
        if not self._k8s_networking_api:
            return
        policy_name = secrets_egress_policy_name(pod_name)
        try:
            await self._k8s_networking_api.delete_namespaced_network_policy(
                name=policy_name, namespace=SANDBOX_NAMESPACE
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(
                    f"[SandboxManager] Error deleting secrets-egress NetworkPolicy "
                    f"{policy_name}: {e}"
                )

    async def _ensure_pod_secret_cached(self, pod_name: str) -> tuple[str, str]:
        """
        Ensure both this pod's sidecar auth token AND pinned TLS cert are in
        the in-memory cache, via a single read_namespaced_secret call. Both
        live in the same per-pod Secret (see sidecar_auth_secret_name) --
        fetching them separately was two sequential K8s round trips for one
        response body (issue #178). Returns "" for whichever value the
        Secret doesn't contain (TLS disabled, or a Secret predating TLS
        support) -- same "" == not-available semantics as the two callers
        below already document.

        Short-circuits on `_pod_secrets_fetched`, NOT on token/cert
        presence: `_cache_pod_tls_cert` never stores an empty string, so
        with TLS disabled cluster-wide `_pod_tls_certs.get(pod_name)` is
        always falsy and a presence check alone would re-read the Secret
        on every single call for that pod, never actually caching anything.
        """
        if pod_name in self._pod_secrets_fetched:
            return (
                self._pod_auth_tokens.get(pod_name, ""),
                self._pod_tls_certs.get(pod_name, ""),
            )

        cached_token = self._pod_auth_tokens.get(pod_name)
        cached_cert = self._pod_tls_certs.get(pod_name)

        if not self._k8s_core_api:
            return cached_token or "", cached_cert or ""

        secret_name = sidecar_auth_secret_name(pod_name)
        try:
            secret = await self._k8s_core_api.read_namespaced_secret(
                name=secret_name, namespace=SANDBOX_NAMESPACE
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"[SandboxManager] Error reading sidecar-auth secret {secret_name}: {e}")
            # Not marked as fetched: a 404 means the Secret doesn't exist
            # yet (a real creation race), so a later call should retry
            # rather than permanently cache "not available".
            return cached_token or "", cached_cert or ""

        token = self._decode_sidecar_auth_token_from_secret(secret)
        if token:
            self._cache_pod_auth_token(pod_name, token)
        cert_pem = self._decode_sidecar_tls_cert_from_secret(secret)
        if cert_pem:
            self._cache_pod_tls_cert(pod_name, cert_pem)

        self._pod_secrets_fetched[pod_name] = True
        self._pod_secrets_fetched.move_to_end(pod_name)
        while len(self._pod_secrets_fetched) > SANDBOX_HTTP_CLIENT_CACHE_MAX_ENTRIES:
            self._pod_secrets_fetched.popitem(last=False)

        return token or cached_token or "", cert_pem or cached_cert or ""

    async def _ensure_pod_auth_token_cached(self, pod_name: str) -> str:
        """
        Ensure this pod's sidecar auth token is in the in-memory cache,
        reading it from its Secret if not already cached.

        Call this (not the sync `_get_pod_auth_token` alone) whenever a pod
        is resolved/claimed by a process that may not have created it itself
        — e.g. resolving an existing session, claiming a warm pod, or
        reusing a pod found already Running during a 409-conflict retry.
        """
        token, _ = await self._ensure_pod_secret_cached(pod_name)
        return token

    def _cache_pod_tls_cert(self, pod_name: str, cert_pem: str) -> None:
        if not cert_pem:
            return
        self._pod_tls_certs[pod_name] = cert_pem
        self._pod_tls_certs.move_to_end(pod_name)
        while len(self._pod_tls_certs) > SANDBOX_HTTP_CLIENT_CACHE_MAX_ENTRIES:
            self._pod_tls_certs.popitem(last=False)

    async def _ensure_pod_tls_cert_cached(self, pod_name: str) -> str:
        """
        Ensure this pod's pinned TLS cert PEM is in the in-memory cache,
        reading it from its sidecar-auth Secret if not already cached.

        Mirrors `_ensure_pod_auth_token_cached` exactly -- same Secret read
        (the cert lives alongside the auth token, not in a separate
        Secret), same in-memory cache pattern, same "" == not-available
        (either TLS is disabled cluster-wide, or this Secret predates TLS
        support) semantics. Call this whenever a pod is resolved/claimed by
        a process that may not have created it itself.
        """
        _, cert_pem = await self._ensure_pod_secret_cached(pod_name)
        return cert_pem

    def _seed_pod_secret_cache(self, pod_name: str, token: str, cert_pem: str) -> None:
        """Seed the per-pod auth-token/TLS-cert cache from values already
        read elsewhere (the warm-pool fast-claim ready index), so the claim
        hot path skips the Secret read `_ensure_pod_secret_cached` would
        otherwise do. Marks the pod fetched so a later
        `_ensure_pod_secret_cached` call short-circuits instead of re-reading
        -- same bookkeeping that method itself does on a real read.
        """
        if token:
            self._cache_pod_auth_token(pod_name, token)
        if cert_pem:
            self._cache_pod_tls_cert(pod_name, cert_pem)
        self._pod_secrets_fetched[pod_name] = True
        self._pod_secrets_fetched.move_to_end(pod_name)
        while len(self._pod_secrets_fetched) > SANDBOX_HTTP_CLIENT_CACHE_MAX_ENTRIES:
            self._pod_secrets_fetched.popitem(last=False)

    def _auth_headers_for_pod(self, pod_name: str) -> dict:
        token = self._get_pod_auth_token(pod_name)
        if not token:
            logger.warning(
                f"[SandboxManager] No sidecar auth token available for pod {pod_name}; "
                "request will be rejected by the sidecar unless SIDECAR_AUTH_TOKEN "
                "enforcement is somehow disabled."
            )
            return {}
        return {SIDECAR_AUTH_HEADER: token}

    def _get_pod_tls_cert(self, pod_name: str) -> str:
        """Sync cache lookup, mirroring `_get_pod_auth_token`. Callers that
        might be looking at a pod this process didn't create must
        `await self._ensure_pod_tls_cert_cached(pod_name)` first."""
        return self._pod_tls_certs.get(pod_name, "")

    def _pinned_verify_for_pod(self, pod_name: str) -> bool | ssl.SSLContext:
        """Resolve the `verify=` argument for this pod's httpx client.

        Returns:
        - `True` (httpx's default CA verification) when TLS is disabled
          cluster-wide (SIDECAR_TLS_DISABLED=true) or when routing through
          `kubectl proxy` (SANDBOX_USE_K8S_PROXY=true) -- see
          `_build_sidecar_url`'s docstring for why that mode falls back to
          plain HTTP entirely, making `verify=` moot in that case.
        - A pinned `ssl.SSLContext` trusting only this pod's own cert
          (tls.build_pinned_ssl_context), when TLS is enabled and a cert is
          cached for this pod.
        - `True` as a last-resort fallback if TLS is enabled but no cert is
          cached for this pod yet. This is the SAFE failure mode the design
          doc calls out explicitly: falling back to default CA validation
          against a self-signed, non-publicly-issued cert fails the TLS
          handshake loudly (every request to that pod errors) rather than
          silently downgrading to an unpinned-but-still-"successful"
          connection -- there is no `verify=False` path here at all.
        """
        if sidecar_tls_disabled() or self._use_k8s_proxy:
            return True
        cert_pem = self._get_pod_tls_cert(pod_name)
        if not cert_pem:
            return True
        return build_pinned_ssl_context(cert_pem)

