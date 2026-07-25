"""Kubernetes client configuration for sandbox pod management."""

import asyncio
import copy as _copy
import inspect
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from kubernetes_asyncio import client, config
from kubernetes_asyncio.config.config_exception import ConfigException

logger = logging.getLogger(__name__)


SERVICE_ACCOUNT_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
SERVICE_ACCOUNT_CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

# ── Self-rotation for the external (non-in-cluster) auth path ──────────────
#
# SECURITY CONTEXT (GitHub issue #9): a control-plane running OUTSIDE the
# cluster (e.g. on a managed serverless platform) can't get the
# kubelet-managed, auto-rotated projected ServiceAccount token that an
# in-cluster pod gets for free (see _load_projected_service_account_config
# below) -- it has to bootstrap from a static, long-lived
# `kubernetes.io/service-account-token` Secret instead, which never expires
# on its own and has no rotation cadence. This is a real, accepted risk for
# any out-of-cluster deployment of the control-plane; if you deploy this
# way, treat that static Secret's exposure as equivalent to full API access
# on the sandbox namespace until it's rotated.
#
# This section closes that gap WITHOUT needing cloud-specific infrastructure
# (Workload Identity Federation remains the more complete fix, tracked
# separately): once bootstrapped with ANY valid token (the static Secret is
# now only needed for the first few seconds after a cold start), the
# control-plane mints its OWN short-lived token via the Kubernetes
# TokenRequest API and continuously re-mints a fresh one shortly before each
# expiry -- using the current (possibly already self-minted) token to
# authenticate each mint, forming a rotating chain that stops depending on
# the original static value after the very first successful rotation.
#
# This does NOT eliminate the risk of a live process compromise (an attacker
# with RCE in the running container still has whatever the container
# currently holds, same as any credential design) -- it eliminates the risk
# of a *separately* exfiltrated copy of the original static value (e.g. from
# a Secret Manager access, a backup, or a log) being usable indefinitely,
# since the running app stops relying on that value within one rotation
# cycle. Operators should still delete/rotate the original bootstrap Secret
# after confirming self-rotation is working, and treat any bootstrap value
# as sensitive only for the brief window it's actually needed.
CONTROL_PLANE_SERVICE_ACCOUNT_NAME_ENV = "CONTROL_PLANE_SERVICE_ACCOUNT_NAME"
DEFAULT_CONTROL_PLANE_SERVICE_ACCOUNT_NAME = "boxkite-control-plane"

CONTROL_PLANE_TOKEN_ROTATION_ENABLED_ENV = "CONTROL_PLANE_TOKEN_ROTATION_ENABLED"
CONTROL_PLANE_TOKEN_ROTATION_EXPIRATION_SECONDS_ENV = "CONTROL_PLANE_TOKEN_ROTATION_EXPIRATION_SECONDS"
# Kubernetes enforces a server-side minimum of 600s (10 minutes) on
# TokenRequest regardless of what's requested here; 3600s (1 hour) is a
# conservative default that still meaningfully bounds exposure of any one
# minted token compared to a token that never expires at all.
DEFAULT_CONTROL_PLANE_TOKEN_ROTATION_EXPIRATION_SECONDS = 3600
# Refresh this far before the current token's actual expiry, so a slow
# TokenRequest round-trip (or a transient API server hiccup, retried by
# _RotatingServiceAccountToken) never risks the in-flight request itself
# using an expired token.
_REFRESH_MARGIN = timedelta(minutes=5)


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _control_plane_service_account_name() -> str:
    return os.environ.get(
        CONTROL_PLANE_SERVICE_ACCOUNT_NAME_ENV, DEFAULT_CONTROL_PLANE_SERVICE_ACCOUNT_NAME
    )


def _control_plane_token_rotation_enabled() -> bool:
    return _env_flag(CONTROL_PLANE_TOKEN_ROTATION_ENABLED_ENV, "true")


def _control_plane_token_rotation_expiration_seconds() -> int:
    raw = os.environ.get(CONTROL_PLANE_TOKEN_ROTATION_EXPIRATION_SECONDS_ENV, "").strip()
    if not raw:
        return DEFAULT_CONTROL_PLANE_TOKEN_ROTATION_EXPIRATION_SECONDS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_CONTROL_PLANE_TOKEN_ROTATION_EXPIRATION_SECONDS


class RotatingServiceAccountToken:
    """An async `refresh_api_key_hook` that keeps a Configuration's Bearer
    token fresh via the Kubernetes TokenRequest API.

    kubernetes_asyncio calls `refresh_api_key_hook(configuration)` on every
    single outgoing request (see Configuration.get_api_key_with_prefix) and
    awaits it if it returns a coroutine -- so this refreshes lazily, on
    demand, rather than needing a separately-managed background task/loop.
    """

    # Bounds a single TokenRequest round trip -- without this, a hung API
    # server call would block every outgoing K8s request in the process
    # (they all await this hook) for however long the call takes, with no
    # upper limit.
    _MINT_TIMEOUT_SECONDS = 10.0
    # After a failed mint, don't retry more than once per cooldown window.
    # Without this, every concurrent caller that hits __call__ while
    # _needs_refresh() is still True (nothing changed on failure) queues up
    # on `self._lock` and each attempts its own full mint-with-timeout in
    # turn -- a serialized pile-up of up to _MINT_TIMEOUT_SECONDS-long calls
    # during a real outage, compounding latency across every outgoing K8s
    # call in the process for as long as it lasts.
    _FAILURE_COOLDOWN = timedelta(seconds=15)

    def __init__(
        self,
        *,
        service_account_name: str,
        namespace: str,
        expiration_seconds: int,
    ) -> None:
        self._service_account_name = service_account_name
        self._namespace = namespace
        self._expiration_seconds = max(expiration_seconds, 600)  # server-side floor
        self._expires_at: Optional[datetime] = None
        self._last_failure_at: Optional[datetime] = None
        self._lock = asyncio.Lock()

    @property
    def expires_at(self) -> Optional[datetime]:
        return self._expires_at

    def _needs_refresh(self) -> bool:
        if self._expires_at is None:
            return True
        return datetime.now(timezone.utc) >= (self._expires_at - _REFRESH_MARGIN)

    def _in_failure_cooldown(self) -> bool:
        return (
            self._last_failure_at is not None
            and datetime.now(timezone.utc) < self._last_failure_at + self._FAILURE_COOLDOWN
        )

    async def __call__(self, configuration: client.Configuration) -> None:
        if not self._needs_refresh():
            return
        if self._in_failure_cooldown():
            return
        async with self._lock:
            # Re-check after acquiring the lock: a concurrent request may
            # have already refreshed (or failed and started a cooldown)
            # while this one was waiting.
            if not self._needs_refresh() or self._in_failure_cooldown():
                return
            await self._mint_and_apply(configuration)

    async def _mint_and_apply(self, configuration: client.Configuration) -> None:
        # Deliberately a hook-less shallow copy for this one-off call, NOT
        # `configuration` itself: kubernetes_asyncio invokes
        # refresh_api_key_hook on every outgoing request (including this
        # TokenRequest call), and `self` is already inside `self._lock`
        # (held by __call__ above) -- reusing `configuration` unmodified
        # would re-enter __call__, try to re-acquire the same non-reentrant
        # asyncio.Lock, and deadlock. The still-current token (valid for at
        # least _REFRESH_MARGIN longer) is enough to authenticate this call.
        refresh_config = _copy.copy(configuration)
        refresh_config.refresh_api_key_hook = None

        api_client = client.ApiClient(refresh_config)
        try:
            core_api = client.CoreV1Api(api_client)
            body = client.AuthenticationV1TokenRequest(
                spec=client.V1TokenRequestSpec(expiration_seconds=self._expiration_seconds)
            )
            result = await asyncio.wait_for(
                core_api.create_namespaced_service_account_token(
                    name=self._service_account_name,
                    namespace=self._namespace,
                    body=body,
                ),
                timeout=self._MINT_TIMEOUT_SECONDS,
            )
        except Exception:
            # Keep using whatever token is already set (it may still be
            # valid for a few more minutes, per _REFRESH_MARGIN) rather than
            # crashing the whole process on a transient TokenRequest failure
            # -- and back off for _FAILURE_COOLDOWN so a sustained outage
            # doesn't turn into every subsequent caller serially retrying
            # (and each waiting out _MINT_TIMEOUT_SECONDS) until it recovers.
            self._last_failure_at = datetime.now(timezone.utc)
            logger.exception(
                "[k8s_auth] Failed to self-mint a rotated ServiceAccount token for %s/%s; "
                "continuing with the current token until it actually expires "
                "(next retry in at least %ss).",
                self._namespace,
                self._service_account_name,
                self._FAILURE_COOLDOWN.total_seconds(),
            )
            return
        finally:
            await api_client.close()

        configuration.api_key["BearerToken"] = result.status.token
        configuration.api_key_prefix["BearerToken"] = "Bearer"
        self._expires_at = result.status.expiration_timestamp or (
            datetime.now(timezone.utc) + timedelta(seconds=self._expiration_seconds)
        )
        self._last_failure_at = None
        logger.info(
            "[k8s_auth] Self-minted a rotated ServiceAccount token for %s/%s, expires %s",
            self._namespace,
            self._service_account_name,
            self._expires_at,
        )


def _refresh_projected_service_account_token(configuration: client.Configuration) -> None:
    try:
        token = SERVICE_ACCOUNT_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigException("Service account token file could not be read.") from exc

    if not token:
        raise ConfigException("Service account token file exists but is empty.")

    configuration.api_key["BearerToken"] = token
    configuration.api_key_prefix["BearerToken"] = "Bearer"


def _load_projected_service_account_config() -> None:
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise ConfigException("KUBERNETES_SERVICE_HOST is not set.")
    if not SERVICE_ACCOUNT_TOKEN_PATH.exists():
        raise ConfigException("Service account token file is missing.")
    if not SERVICE_ACCOUNT_CA_PATH.exists():
        raise ConfigException("Service account CA file is missing.")

    configuration = client.Configuration()
    configuration.host = f"https://{host}:{port}"
    configuration.ssl_ca_cert = str(SERVICE_ACCOUNT_CA_PATH)
    _refresh_projected_service_account_token(configuration)
    configuration.refresh_api_key_hook = _refresh_projected_service_account_token
    client.Configuration.set_default(configuration)


def _enable_external_token_rotation(namespace: str) -> None:
    """Wire self-rotation onto the just-loaded external (kubeconfig-based)
    default Configuration. In-cluster mode is deliberately excluded -- it
    already gets kubelet-managed, auto-rotated projected tokens for free via
    _load_projected_service_account_config above, so this would be
    redundant there."""
    if not _control_plane_token_rotation_enabled():
        logger.warning(
            "[k8s_auth] %s=false -- running with a static, non-rotating "
            "kubeconfig token. See k8s_auth.py's module docstring for why "
            "this is not recommended outside local dev.",
            CONTROL_PLANE_TOKEN_ROTATION_ENABLED_ENV,
        )
        return

    configuration = client.Configuration.get_default()
    configuration.refresh_api_key_hook = RotatingServiceAccountToken(
        service_account_name=_control_plane_service_account_name(),
        namespace=namespace,
        expiration_seconds=_control_plane_token_rotation_expiration_seconds(),
    )


async def load_kubernetes_config() -> str:
    """Load in-cluster config when available; otherwise load local kubeconfig."""
    try:
        _load_projected_service_account_config()
        return "in-cluster"
    except ConfigException:
        maybe_config = config.load_kube_config()
        if inspect.isawaitable(maybe_config):
            await maybe_config
        _enable_external_token_rotation(os.environ.get("SANDBOX_NAMESPACE", "default"))
        return "kubeconfig"


def build_kubernetes_api_client() -> client.ApiClient:
    return client.ApiClient(client.Configuration.get_default_copy())
