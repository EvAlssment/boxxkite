"""``boxxkite backup`` — create / inspect / restore self-hosted control-plane
archives (GitHub issue #118).

Local/operator tooling: talks to Postgres and filesystem paths the operator
points at, not the hosted control-plane API. Complements the Compose
``--profile backup`` job in ``deploy/docker-compose.prod.yaml`` with a
first-party CLI that also records secrets key *references* (never key bytes)
and can restore.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from boxxkite.backup import BackupError, KeyMaterialRef, create_backup, inspect_backup, restore_backup

from .errors import CliError


def _parse_volume_options(values: list[str] | None) -> dict[str, Path]:
    parsed: dict[str, Path] = {}
    for raw in values or []:
        if "=" not in raw:
            raise CliError(f"Invalid --volume {raw!r}: expected NAME=PATH")
        name, path_str = raw.split("=", 1)
        name = name.strip()
        path_str = path_str.strip()
        if not name or not path_str:
            raise CliError(f"Invalid --volume {raw!r}: expected NAME=PATH")
        parsed[name] = Path(path_str)
    return parsed


def create(
    output: Path = typer.Option(
        ...,
        "--output",
        "-o",
        help="Destination archive path (must end with .tar.gz).",
    ),
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="Postgres URL for pg_dump. Defaults to the DATABASE_URL env var when set.",
        envvar="DATABASE_URL",
    ),
    postgres_dump: Path | None = typer.Option(
        None,
        "--postgres-dump",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Use an existing SQL (or .sql.gz) dump instead of running pg_dump.",
    ),
    volume: list[str] | None = typer.Option(
        None,
        "--volume",
        help="Named volume to include, as NAME=/absolute/or/relative/path. Repeatable.",
    ),
    secrets_kms_backend: str | None = typer.Option(
        None,
        "--secrets-kms-backend",
        help="Recorded in key-material.json (not secret). Defaults to SECRETS_KMS_BACKEND.",
        envvar="SECRETS_KMS_BACKEND",
    ),
    secrets_key_id: str | None = typer.Option(
        None,
        "--secrets-key-id",
        help=(
            "Public key id / ARN / URL recorded in key-material.json. "
            "Never pass raw key bytes here. Defaults to SECRETS_KMS_KEY_ID."
        ),
        envvar="SECRETS_KMS_KEY_ID",
    ),
) -> None:
    """Create a versioned backup archive of Postgres and/or named volume dirs."""
    volumes = _parse_volume_options(volume)
    key_material = KeyMaterialRef(
        secrets_kms_backend=secrets_kms_backend or os.environ.get("SECRETS_KMS_BACKEND") or None,
        encryption_key_id=secrets_key_id or os.environ.get("SECRETS_KMS_KEY_ID") or None,
    )
    try:
        archive = create_backup(
            output,
            database_url=database_url,
            postgres_dump=postgres_dump,
            volume_paths=volumes,
            key_material=key_material,
        )
    except BackupError as exc:
        raise CliError(str(exc)) from exc

    typer.echo(f"Backup written to {archive}")
    if key_material.secrets_kms_backend or key_material.encryption_key_id:
        typer.echo(
            "Recorded key-material.json references only "
            f"(backend={key_material.secrets_kms_backend!r}, "
            f"key_id={key_material.encryption_key_id!r}). "
            "Raw key bytes were not written into the archive."
        )
    else:
        typer.echo(
            "Warning: no SECRETS_KMS_BACKEND / SECRETS_KMS_KEY_ID recorded. "
            "If this control-plane encrypts org secrets, set those so restore "
            "operators know which key must still be available.",
            err=True,
        )


def inspect(
    archive: Path = typer.Option(
        ...,
        "--archive",
        "-a",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to a boxxkite backup .tar.gz archive.",
    ),
) -> None:
    """Verify checksums and print archive membership."""
    try:
        manifest = inspect_backup(archive)
    except BackupError as exc:
        raise CliError(str(exc)) from exc

    typer.echo(f"schema_version={manifest.schema_version}")
    typer.echo(f"created_at_utc={manifest.created_at_utc}")
    typer.echo(f"has_postgres={manifest.has_postgres}")
    typer.echo(f"volumes={','.join(manifest.volumes) if manifest.volumes else '-'}")
    if manifest.key_material:
        typer.echo(
            "key_material.backend="
            f"{manifest.key_material.get('secrets_kms_backend')!r} "
            f"key_id={manifest.key_material.get('encryption_key_id')!r}"
        )
    for name, digest in sorted(manifest.members.items()):
        typer.echo(f"member {name} sha256={digest}")


def restore(
    archive: Path = typer.Option(
        ...,
        "--archive",
        "-a",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to a boxxkite backup .tar.gz archive.",
    ),
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="Postgres URL for psql restore. Defaults to DATABASE_URL when set.",
        envvar="DATABASE_URL",
    ),
    volume: list[str] | None = typer.Option(
        None,
        "--volume",
        help="Restore destination for a named volume, as NAME=PATH. Repeatable.",
    ),
    confirm_key_available: bool = typer.Option(
        False,
        "--confirm-key-available",
        help=(
            "Required when the archive includes postgres.sql.gz. Affirms the "
            "operator still has the matching secrets encryption key outside "
            "this archive (see key-material.json)."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive restore confirmation prompt.",
    ),
) -> None:
    """Restore Postgres and/or volume members from a verified archive."""
    volumes = _parse_volume_options(volume)
    if not yes:
        typer.confirm(
            f"Restore from {archive} into the targets above? This can overwrite data.",
            abort=True,
        )
    try:
        manifest = restore_backup(
            archive,
            database_url=database_url,
            volume_paths=volumes,
            confirm_key_available=confirm_key_available,
        )
    except BackupError as exc:
        raise CliError(str(exc)) from exc

    typer.echo(
        f"Restored archive created_at_utc={manifest.created_at_utc} "
        f"(postgres={manifest.has_postgres}, volumes={len(manifest.volumes)})"
    )
