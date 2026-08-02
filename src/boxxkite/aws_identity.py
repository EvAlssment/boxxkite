"""Helpers for sidecar-only AWS web identity in sandbox pods."""

from __future__ import annotations

import os
from typing import Optional

from kubernetes_asyncio import client

from .azure_identity import AKS_SKIP_CONTAINERS_ANNOTATION
from .secret_keys import (
    get_storage_s3_access_key_secret_key,
    get_storage_s3_secret_key_secret_key,
    get_storage_s3_session_token_secret_key,
)


AWS_WEB_IDENTITY_VOLUME_NAME = "aws-web-identity-token"
AWS_WEB_IDENTITY_TOKEN_FILENAME = "token"
AWS_WEB_IDENTITY_TOKEN_MOUNT_PATH = "/var/run/secrets/eks.amazonaws.com/serviceaccount"
AWS_WEB_IDENTITY_TOKEN_FILE = f"{AWS_WEB_IDENTITY_TOKEN_MOUNT_PATH}/{AWS_WEB_IDENTITY_TOKEN_FILENAME}"
EKS_SKIP_CONTAINERS_ANNOTATION = "eks.amazonaws.com/skip-containers"
SANDBOX_IDENTITY_CONTAINERS = ("sandbox", "sidecar")

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUE_VALUES


def is_aws_web_identity_enabled() -> bool:
    return _env_bool("SANDBOX_AWS_WEB_IDENTITY_ENABLED")


def build_pod_identity_webhook_skip_annotations() -> dict[str, str]:
    """Return cloud webhook skip annotations for manual sidecar-only identity."""
    if not is_aws_web_identity_enabled():
        return {}

    # The pod may run as an annotated service account. Keep provider webhooks
    # from injecting identity into either container; this code explicitly mounts
    # the projected AWS web identity token into the sidecar only. If Azure
    # workload identity is enabled too, its helper narrows the AKS skip list so
    # the sidecar can receive the Azure token.
    return {
        EKS_SKIP_CONTAINERS_ANNOTATION: ",".join(SANDBOX_IDENTITY_CONTAINERS),
        AKS_SKIP_CONTAINERS_ANNOTATION: ";".join(SANDBOX_IDENTITY_CONTAINERS),
    }


def _web_identity_region() -> str:
    return os.environ.get("SANDBOX_AWS_REGION") or os.environ.get("AWS_REGION", "us-east-1")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required when SANDBOX_AWS_WEB_IDENTITY_ENABLED=true")
    return value


def _token_expiration_seconds() -> int:
    raw_value = os.environ.get("SANDBOX_AWS_WEB_IDENTITY_TOKEN_EXPIRATION_SECONDS", "3600")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("SANDBOX_AWS_WEB_IDENTITY_TOKEN_EXPIRATION_SECONDS must be an integer") from exc
    if value <= 0:
        raise ValueError("SANDBOX_AWS_WEB_IDENTITY_TOKEN_EXPIRATION_SECONDS must be positive")
    return value


def build_sidecar_aws_auth_env(storage_credentials_secret: str) -> list[client.V1EnvVar]:
    """Return AWS auth env vars for the sidecar container only."""
    if not is_aws_web_identity_enabled():
        return [
            client.V1EnvVar(
                name="AWS_ACCESS_KEY_ID",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=storage_credentials_secret,
                        key=get_storage_s3_access_key_secret_key(),
                        optional=True,
                    )
                ),
            ),
            client.V1EnvVar(
                name="AWS_SECRET_ACCESS_KEY",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=storage_credentials_secret,
                        key=get_storage_s3_secret_key_secret_key(),
                        optional=True,
                    )
                ),
            ),
            client.V1EnvVar(
                name="AWS_SESSION_TOKEN",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=storage_credentials_secret,
                        key=get_storage_s3_session_token_secret_key(),
                        optional=True,
                    )
                ),
            ),
        ]

    region = _web_identity_region()
    sts_regional_endpoints = os.environ.get("SANDBOX_AWS_STS_REGIONAL_ENDPOINTS", "regional").strip()
    env = [
        client.V1EnvVar(name="AWS_ROLE_ARN", value=_required_env("SANDBOX_AWS_ROLE_ARN")),
        client.V1EnvVar(name="AWS_WEB_IDENTITY_TOKEN_FILE", value=AWS_WEB_IDENTITY_TOKEN_FILE),
        client.V1EnvVar(name="AWS_REGION", value=region),
        client.V1EnvVar(name="AWS_DEFAULT_REGION", value=region),
    ]
    if sts_regional_endpoints:
        env.append(client.V1EnvVar(name="AWS_STS_REGIONAL_ENDPOINTS", value=sts_regional_endpoints))
    return env


def build_sidecar_aws_web_identity_volume_mount() -> Optional[client.V1VolumeMount]:
    if not is_aws_web_identity_enabled():
        return None
    return client.V1VolumeMount(
        name=AWS_WEB_IDENTITY_VOLUME_NAME,
        mount_path=AWS_WEB_IDENTITY_TOKEN_MOUNT_PATH,
        read_only=True,
    )


def build_aws_web_identity_volume() -> Optional[client.V1Volume]:
    if not is_aws_web_identity_enabled():
        return None

    return client.V1Volume(
        name=AWS_WEB_IDENTITY_VOLUME_NAME,
        projected=client.V1ProjectedVolumeSource(
            sources=[
                client.V1VolumeProjection(
                    service_account_token=client.V1ServiceAccountTokenProjection(
                        audience=os.environ.get("SANDBOX_AWS_WEB_IDENTITY_AUDIENCE", "sts.amazonaws.com"),
                        expiration_seconds=_token_expiration_seconds(),
                        path=AWS_WEB_IDENTITY_TOKEN_FILENAME,
                    )
                )
            ]
        ),
    )
