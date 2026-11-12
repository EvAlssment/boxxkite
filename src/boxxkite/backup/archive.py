"""Versioned backup archive create / inspect / restore.

Archive layout (packed as a single ``.tar.gz``):

- ``manifest.json`` — schema version, timestamps, member digests
- ``key-material.json`` — KMS backend / key-id *references* only (never key bytes)
- ``postgres.sql.gz`` — optional ``pg_dump`` output
- ``volumes/<name>.tar.gz`` — optional named volume trees
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
KEY_MATERIAL_NAME = "key-material.json"
POSTGRES_MEMBER = "postgres.sql.gz"
VOLUMES_DIR = "volumes"
LOCAL_DEV_KEY_ENV = "SECRETS_LOCAL_DEV_KMS_KEY"


class BackupError(RuntimeError):
    """Operator-facing failure while creating, inspecting, or restoring a backup."""


@dataclass(frozen=True)
class KeyMaterialRef:
    """Pointers to the secrets-encryption key — never the key bytes themselves.

    Restoring a dump that contains envelope-encrypted org secrets requires the
    same KMS key (or ``SECRETS_LOCAL_DEV_KMS_KEY`` value) the control-plane was
    using when the backup was taken. That material is stored *outside* this
    archive on purpose.
    """

    secrets_kms_backend: str | None = None
    encryption_key_id: str | None = None
    local_dev_key_env: str = LOCAL_DEV_KEY_ENV
    note: str = (
        "This archive does not contain secrets encryption key bytes. "
        "Before restore, ensure the same SECRETS_KMS_BACKEND / SECRETS_KMS_KEY_ID "
        f"(or {LOCAL_DEV_KEY_ENV} for local backend) is available to the "
        "control-plane that will read the restored database."
    )


@dataclass
class BackupManifest:
    schema_version: int
    created_at_utc: str
    members: dict[str, str] = field(default_factory=dict)
    volumes: list[str] = field(default_factory=list)
    has_postgres: bool = False
    key_material: dict | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict) -> BackupManifest:
        try:
            return cls(
                schema_version=int(data["schema_version"]),
                created_at_utc=str(data["created_at_utc"]),
                members={str(k): str(v) for k, v in dict(data.get("members") or {}).items()},
                volumes=[str(v) for v in list(data.get("volumes") or [])],
                has_postgres=bool(data.get("has_postgres")),
                key_material=data.get("key_material"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupError(f"Invalid backup manifest: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _parse_volume_specs(
    specs: Mapping[str, Path], *, must_exist: bool
) -> dict[str, Path]:
    parsed: dict[str, Path] = {}
    for name, path in specs.items():
        if not name or any(ch in name for ch in "/\\"):
            raise BackupError(f"Invalid volume name {name!r}: use a plain identifier.")
        resolved = Path(path)
        if must_exist and not resolved.is_dir():
            raise BackupError(f"Volume path for {name!r} is not a directory: {resolved}")
        parsed[name] = resolved
    return parsed


def _write_volume_tarball(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() or path.is_symlink():
                archive.add(path, arcname=str(path.relative_to(source)))


def _safe_extractall(archive: tarfile.TarFile, dest: Path) -> None:
    for member in archive.getmembers():
        member_path = Path(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise BackupError(f"Refusing unsafe archive member path: {member.name!r}")
    # filter= is Python 3.12+; omit on 3.11 after the manual path check above.
    try:
        archive.extractall(dest, filter="data")
    except TypeError:
        archive.extractall(dest)


def _extract_volume_tarball(tarball: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as archive:
        _safe_extractall(archive, dest)


def _run_pg_dump(database_url: str, dest: Path, *, pg_dump_bin: str) -> None:
    if shutil.which(pg_dump_bin) is None:
        raise BackupError(
            f"{pg_dump_bin!r} not found on PATH. Install PostgreSQL client tools, "
            "or pass --postgres-dump with a pre-made dump file."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as handle:
            proc = subprocess.run(
                [pg_dump_bin, "--no-password", database_url],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace").strip() or f"exit {proc.returncode}"
                raise BackupError(f"pg_dump failed: {err}")
            # gzip via Python so we don't depend on the gzip binary.
            import gzip

            with gzip.GzipFile(fileobj=handle, mode="wb") as gz:
                gz.write(proc.stdout)
    except OSError as exc:
        raise BackupError(f"Could not write postgres dump to {dest}: {exc}") from exc


def _run_psql_restore(database_url: str, dump_gz: Path, *, psql_bin: str) -> None:
    if shutil.which(psql_bin) is None:
        raise BackupError(
            f"{psql_bin!r} not found on PATH. Install PostgreSQL client tools "
            "before restoring a postgres.sql.gz member."
        )
    import gzip

    try:
        sql = gzip.decompress(dump_gz.read_bytes())
    except OSError as exc:
        raise BackupError(f"Could not read {dump_gz}: {exc}") from exc
    proc = subprocess.run(
        [psql_bin, "--no-password", "-v", "ON_ERROR_STOP=1", database_url],
        input=sql,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip() or f"exit {proc.returncode}"
        raise BackupError(f"psql restore failed: {err}")


def _pack_directory(staging: Path, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise BackupError(f"Refusing to overwrite existing archive: {output}")
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(staging)))
    return output


def _unpack_archive(archive: Path, dest: Path) -> None:
    if not archive.is_file():
        raise BackupError(f"Backup archive not found: {archive}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as handle:
        _safe_extractall(handle, dest)


def _load_manifest(root: Path) -> BackupManifest:
    path = root / MANIFEST_NAME
    if not path.is_file():
        raise BackupError(f"Archive is missing {MANIFEST_NAME}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BackupError(f"{MANIFEST_NAME} must be a JSON object")
    manifest = BackupManifest.from_dict(data)
    if manifest.schema_version != ARCHIVE_FORMAT_VERSION:
        raise BackupError(
            f"Unsupported backup schema_version {manifest.schema_version} "
            f"(this CLI supports {ARCHIVE_FORMAT_VERSION})"
        )
    return manifest


def _verify_member_digests(root: Path, manifest: BackupManifest) -> None:
    for relative, expected in sorted(manifest.members.items()):
        path = root / relative
        if not path.is_file():
            raise BackupError(f"Archive member listed in manifest is missing: {relative}")
        actual = _sha256_file(path)
        if actual != expected:
            raise BackupError(
                f"Checksum mismatch for {relative}: expected {expected}, got {actual}"
            )


def create_backup(
    output: Path,
    *,
    database_url: str | None = None,
    postgres_dump: Path | None = None,
    volume_paths: Mapping[str, Path] | None = None,
    key_material: KeyMaterialRef | None = None,
    pg_dump_bin: str = "pg_dump",
) -> Path:
    """Create a versioned ``.tar.gz`` backup archive at ``output``.

    At least one of ``database_url``, ``postgres_dump``, or ``volume_paths``
    must be provided. ``output`` must end with ``.tar.gz`` and must not already
    exist.
    """
    volumes = _parse_volume_specs(volume_paths or {}, must_exist=True)
    if not database_url and postgres_dump is None and not volumes:
        raise BackupError(
            "Nothing to back up: pass --database-url and/or --volume NAME=PATH "
            "(or --postgres-dump)."
        )
    if database_url and postgres_dump is not None:
        raise BackupError("Pass only one of --database-url or --postgres-dump, not both.")
    if output.suffixes[-2:] != [".tar", ".gz"] and not str(output).endswith(".tar.gz"):
        raise BackupError(f"Output path must end with .tar.gz (got {output})")

    key_ref = key_material or KeyMaterialRef(
        secrets_kms_backend=os.environ.get("SECRETS_KMS_BACKEND") or None,
        encryption_key_id=os.environ.get("SECRETS_KMS_KEY_ID") or None,
    )

    with tempfile.TemporaryDirectory(prefix="boxxkite-backup-") as tmp:
        staging = Path(tmp)
        members: dict[str, str] = {}
        volume_names: list[str] = []
        has_postgres = False

        key_path = staging / KEY_MATERIAL_NAME
        key_path.write_text(json.dumps(asdict(key_ref), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        members[KEY_MATERIAL_NAME] = _sha256_file(key_path)

        if postgres_dump is not None:
            if not postgres_dump.is_file():
                raise BackupError(f"Postgres dump file not found: {postgres_dump}")
            dest = staging / POSTGRES_MEMBER
            # Accept either already-gzipped or raw SQL; normalize to .sql.gz.
            raw = postgres_dump.read_bytes()
            if postgres_dump.name.endswith(".gz"):
                dest.write_bytes(raw)
            else:
                import gzip

                with gzip.open(dest, "wb") as gz:
                    gz.write(raw)
            members[POSTGRES_MEMBER] = _sha256_file(dest)
            has_postgres = True
        elif database_url:
            dest = staging / POSTGRES_MEMBER
            _run_pg_dump(database_url, dest, pg_dump_bin=pg_dump_bin)
            members[POSTGRES_MEMBER] = _sha256_file(dest)
            has_postgres = True

        if volumes:
            volumes_root = staging / VOLUMES_DIR
            volumes_root.mkdir(parents=True, exist_ok=True)
            for name, path in sorted(volumes.items()):
                member_rel = f"{VOLUMES_DIR}/{name}.tar.gz"
                _write_volume_tarball(path, staging / member_rel)
                members[member_rel] = _sha256_file(staging / member_rel)
                volume_names.append(name)

        manifest = BackupManifest(
            schema_version=ARCHIVE_FORMAT_VERSION,
            created_at_utc=_utc_stamp(),
            members=members,
            volumes=volume_names,
            has_postgres=has_postgres,
            key_material=asdict(key_ref),
        )
        (staging / MANIFEST_NAME).write_text(manifest.to_json(), encoding="utf-8")
        return _pack_directory(staging, output)


def inspect_backup(archive: Path) -> BackupManifest:
    """Read and checksum-verify a backup archive; return its manifest."""
    with tempfile.TemporaryDirectory(prefix="boxxkite-backup-inspect-") as tmp:
        root = Path(tmp)
        _unpack_archive(archive, root)
        manifest = _load_manifest(root)
        _verify_member_digests(root, manifest)
        return manifest


def restore_backup(
    archive: Path,
    *,
    database_url: str | None = None,
    volume_paths: Mapping[str, Path] | None = None,
    confirm_key_available: bool = False,
    psql_bin: str = "psql",
) -> BackupManifest:
    """Restore members from ``archive``.

    Requires ``confirm_key_available=True`` whenever the archive includes a
    Postgres dump (secrets ciphertext in that dump is useless without the
    original key material, which is intentionally absent from the archive).
    """
    volumes = _parse_volume_specs(volume_paths or {}, must_exist=False)
    with tempfile.TemporaryDirectory(prefix="boxxkite-backup-restore-") as tmp:
        root = Path(tmp)
        _unpack_archive(archive, root)
        manifest = _load_manifest(root)
        _verify_member_digests(root, manifest)

        if manifest.has_postgres:
            if not confirm_key_available:
                raise BackupError(
                    "Refusing to restore postgres.sql.gz without --confirm-key-available. "
                    "The archive's key-material.json records only key *references*; you must "
                    "separately ensure the control-plane still has the matching "
                    f"{LOCAL_DEV_KEY_ENV} / SECRETS_KMS_KEY_ID before applying the dump."
                )
            if not database_url:
                raise BackupError(
                    "Archive contains postgres.sql.gz but no --database-url was provided."
                )
            _run_psql_restore(database_url, root / POSTGRES_MEMBER, psql_bin=psql_bin)

        if manifest.volumes:
            if not volumes:
                raise BackupError(
                    "Archive contains volume members "
                    f"({', '.join(manifest.volumes)}) but no --volume NAME=PATH was provided."
                )
            missing = [name for name in manifest.volumes if name not in volumes]
            if missing:
                raise BackupError(
                    "Archive volumes missing restore destinations: "
                    + ", ".join(missing)
                    + ". Pass --volume NAME=PATH for each."
                )
            for name in manifest.volumes:
                member = root / VOLUMES_DIR / f"{name}.tar.gz"
                _extract_volume_tarball(member, volumes[name])

        if not manifest.has_postgres and not manifest.volumes:
            raise BackupError("Archive contains nothing to restore.")

        return manifest
