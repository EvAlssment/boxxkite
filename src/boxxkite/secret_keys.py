"""Secret data-key resolution for dynamically created sandbox pods."""

from __future__ import annotations

import os


AWS_ACCESS_KEY_ID_SECRET_KEY_ENV = "AWS_ACCESS_KEY_ID_SECRET_KEY"
AWS_SECRET_ACCESS_KEY_SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY_SECRET_KEY"
AWS_SESSION_TOKEN_SECRET_KEY_ENV = "AWS_SESSION_TOKEN_SECRET_KEY"
STORAGE_S3_ACCESS_KEY_SECRET_KEY_ENV = "STORAGE_S3_ACCESS_KEY_SECRET_KEY"
STORAGE_S3_SECRET_KEY_SECRET_KEY_ENV = "STORAGE_S3_SECRET_KEY_SECRET_KEY"
STORAGE_S3_SESSION_TOKEN_SECRET_KEY_ENV = "STORAGE_S3_SESSION_TOKEN_SECRET_KEY"
STORAGE_AZURE_CONNECTION_STRING_SECRET_KEY_ENV = "STORAGE_AZURE_CONNECTION_STRING_SECRET_KEY"
STORAGE_AZURE_ACCOUNT_KEY_SECRET_KEY_ENV = "STORAGE_AZURE_ACCOUNT_KEY_SECRET_KEY"
STORAGE_AZURE_SAS_TOKEN_SECRET_KEY_ENV = "STORAGE_AZURE_SAS_TOKEN_SECRET_KEY"

DEFAULT_AWS_ACCESS_KEY_ID_SECRET_KEY = "aws-access-key-id"
DEFAULT_AWS_SECRET_ACCESS_KEY_SECRET_KEY = "aws-secret-access-key"
DEFAULT_AWS_SESSION_TOKEN_SECRET_KEY = "aws-session-token"
DEFAULT_STORAGE_AZURE_CONNECTION_STRING_SECRET_KEY = "storage-azure-connection-string"
DEFAULT_STORAGE_AZURE_ACCOUNT_KEY_SECRET_KEY = "storage-azure-account-key"
DEFAULT_STORAGE_AZURE_SAS_TOKEN_SECRET_KEY = "storage-azure-sas-token"


def _secret_data_key(env_name: str, default: str) -> str:
    configured = os.environ.get(env_name, "").strip()
    return configured or default


def get_aws_access_key_id_secret_key() -> str:
    return _secret_data_key(
        AWS_ACCESS_KEY_ID_SECRET_KEY_ENV,
        DEFAULT_AWS_ACCESS_KEY_ID_SECRET_KEY,
    )


def get_aws_secret_access_key_secret_key() -> str:
    return _secret_data_key(
        AWS_SECRET_ACCESS_KEY_SECRET_KEY_ENV,
        DEFAULT_AWS_SECRET_ACCESS_KEY_SECRET_KEY,
    )


def get_aws_session_token_secret_key() -> str:
    return _secret_data_key(
        AWS_SESSION_TOKEN_SECRET_KEY_ENV,
        DEFAULT_AWS_SESSION_TOKEN_SECRET_KEY,
    )


def get_storage_s3_access_key_secret_key() -> str:
    configured = os.environ.get(STORAGE_S3_ACCESS_KEY_SECRET_KEY_ENV, "").strip()
    if configured:
        return configured
    return get_aws_access_key_id_secret_key()


def get_storage_s3_secret_key_secret_key() -> str:
    configured = os.environ.get(STORAGE_S3_SECRET_KEY_SECRET_KEY_ENV, "").strip()
    if configured:
        return configured
    return get_aws_secret_access_key_secret_key()


def get_storage_s3_session_token_secret_key() -> str:
    configured = os.environ.get(STORAGE_S3_SESSION_TOKEN_SECRET_KEY_ENV, "").strip()
    if configured:
        return configured
    return get_aws_session_token_secret_key()


def get_storage_azure_connection_string_secret_key() -> str:
    return _secret_data_key(
        STORAGE_AZURE_CONNECTION_STRING_SECRET_KEY_ENV,
        DEFAULT_STORAGE_AZURE_CONNECTION_STRING_SECRET_KEY,
    )


def get_storage_azure_account_key_secret_key() -> str:
    return _secret_data_key(
        STORAGE_AZURE_ACCOUNT_KEY_SECRET_KEY_ENV,
        DEFAULT_STORAGE_AZURE_ACCOUNT_KEY_SECRET_KEY,
    )


def get_storage_azure_sas_token_secret_key() -> str:
    return _secret_data_key(
        STORAGE_AZURE_SAS_TOKEN_SECRET_KEY_ENV,
        DEFAULT_STORAGE_AZURE_SAS_TOKEN_SECRET_KEY,
    )
