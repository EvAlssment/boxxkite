"""Helpers for sidecar-only Azure storage auth in sandbox pods."""

from __future__ import annotations

import os

from kubernetes_asyncio import client

from .secret_keys import (
    get_storage_azure_account_key_secret_key,
    get_storage_azure_connection_string_secret_key,
    get_storage_azure_sas_token_secret_key,
)


AKS_WORKLOAD_IDENTITY_USE_LABEL = "azure.workload.identity/use"
AKS_SKIP_CONTAINERS_ANNOTATION = "azure.workload.identity/skip-containers"
SANDBOX_AZURE_WORKLOAD_IDENTITY_ENABLED_ENV = "SANDBOX_AZURE_WORKLOAD_IDENTITY_ENABLED"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUE_VALUES


def _env_value(name: str, *fallbacks: str, default: str = "") -> str:
    for key in (name, *fallbacks):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return default


def is_azure_workload_identity_enabled() -> bool:
    return _env_bool(SANDBOX_AZURE_WORKLOAD_IDENTITY_ENABLED_ENV)


def build_azure_workload_identity_pod_labels() -> dict[str, str]:
    if not is_azure_workload_identity_enabled():
        return {}
    return {AKS_WORKLOAD_IDENTITY_USE_LABEL: "true"}


def build_azure_workload_identity_skip_annotations() -> dict[str, str]:
    if not is_azure_workload_identity_enabled():
        return {}
    return {AKS_SKIP_CONTAINERS_ANNOTATION: "sandbox"}


def build_sidecar_azure_storage_env(storage_credentials_secret: str) -> list[client.V1EnvVar]:
    """Return Azure Blob env vars for the sidecar container only."""
    return [
        client.V1EnvVar(
            name="STORAGE_AZURE_CONTAINER",
            value=_env_value("STORAGE_AZURE_CONTAINER", "AZURE_STORAGE_CONTAINER", default="boxkite-storage"),
        ),
        client.V1EnvVar(
            name="STORAGE_AZURE_ACCOUNT_NAME",
            value=_env_value("STORAGE_AZURE_ACCOUNT_NAME", "AZURE_STORAGE_ACCOUNT_NAME"),
        ),
        client.V1EnvVar(
            name="STORAGE_AZURE_ACCOUNT_URL",
            value=_env_value(
                "STORAGE_AZURE_ACCOUNT_URL",
                "AZURE_STORAGE_ACCOUNT_URL",
                "STORAGE_AZURE_BLOB_ENDPOINT",
                "AZURE_STORAGE_BLOB_ENDPOINT",
            ),
        ),
        client.V1EnvVar(
            name="STORAGE_AZURE_AUTH_MODE",
            value=_env_value("STORAGE_AZURE_AUTH_MODE", "AZURE_STORAGE_AUTH_MODE", default="auto"),
        ),
        client.V1EnvVar(
            name="STORAGE_AZURE_CLIENT_ID",
            value=_env_value("STORAGE_AZURE_CLIENT_ID"),
        ),
        client.V1EnvVar(
            name="STORAGE_AZURE_CONNECTION_STRING",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name=storage_credentials_secret,
                    key=get_storage_azure_connection_string_secret_key(),
                    optional=True,
                )
            ),
        ),
        client.V1EnvVar(
            name="STORAGE_AZURE_ACCOUNT_KEY",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name=storage_credentials_secret,
                    key=get_storage_azure_account_key_secret_key(),
                    optional=True,
                )
            ),
        ),
        client.V1EnvVar(
            name="STORAGE_AZURE_SAS_TOKEN",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name=storage_credentials_secret,
                    key=get_storage_azure_sas_token_secret_key(),
                    optional=True,
                )
            ),
        ),
    ]
