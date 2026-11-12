"""Unit tests for boxxkite.backup archive create/inspect/restore."""

from __future__ import annotations

import gzip
import json
import tarfile
from pathlib import Path

import pytest

from boxxkite.backup import BackupError, KeyMaterialRef, create_backup, inspect_backup, restore_backup


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_create_inspect_restore_roundtrip_volumes(tmp_path: Path):
    vol_a = _write_tree(tmp_path / "src-a", {"hello.txt": "hello", "nested/x.txt": "x"})
    vol_b = _write_tree(tmp_path / "src-b", {"only.txt": "b"})
    archive = tmp_path / "backup.tar.gz"

    created = create_backup(
        archive,
        volume_paths={"uploads": vol_a, "workspace": vol_b},
        key_material=KeyMaterialRef(
            secrets_kms_backend="local",
            encryption_key_id=None,
        ),
    )
    assert created == archive
    assert archive.is_file()

    manifest = inspect_backup(archive)
    assert manifest.schema_version == 1
    assert manifest.has_postgres is False
    assert manifest.volumes == ["uploads", "workspace"]
    assert "key-material.json" in manifest.members
    assert "volumes/uploads.tar.gz" in manifest.members

    dest_a = tmp_path / "dest-a"
    dest_b = tmp_path / "dest-b"
    restore_backup(
        archive,
        volume_paths={"uploads": dest_a, "workspace": dest_b},
    )
    assert (dest_a / "hello.txt").read_text(encoding="utf-8") == "hello"
    assert (dest_a / "nested" / "x.txt").read_text(encoding="utf-8") == "x"
    assert (dest_b / "only.txt").read_text(encoding="utf-8") == "b"


def test_create_with_postgres_dump_file(tmp_path: Path):
    dump = tmp_path / "dump.sql"
    dump.write_text("SELECT 1;\n", encoding="utf-8")
    archive = tmp_path / "pg.tar.gz"

    create_backup(
        archive,
        postgres_dump=dump,
        key_material=KeyMaterialRef(secrets_kms_backend="aws", encryption_key_id="arn:aws:kms:test"),
    )
    manifest = inspect_backup(archive)
    assert manifest.has_postgres is True
    assert manifest.key_material["encryption_key_id"] == "arn:aws:kms:test"

    with tarfile.open(archive, "r:gz") as handle:
        key = handle.extractfile("key-material.json")
        assert key is not None
        payload = json.loads(key.read().decode("utf-8"))
    assert "SECRETS_LOCAL_DEV_KMS_KEY" in payload["note"]
    assert "raw" not in json.dumps(payload).lower() or "never" in payload["note"].lower()


def test_restore_postgres_requires_confirm_flag(tmp_path: Path):
    dump = tmp_path / "dump.sql.gz"
    with gzip.open(dump, "wb") as gz:
        gz.write(b"SELECT 1;\n")
    archive = tmp_path / "pg.tar.gz"
    create_backup(archive, postgres_dump=dump)

    with pytest.raises(BackupError, match="confirm-key-available"):
        restore_backup(archive, database_url="postgresql://localhost/boxxkite")


def test_create_requires_something_to_back_up(tmp_path: Path):
    with pytest.raises(BackupError, match="Nothing to back up"):
        create_backup(tmp_path / "empty.tar.gz")


def test_create_rejects_non_targz_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    vol = _write_tree(tmp_path / "vol", {"a.txt": "a"})
    with pytest.raises(BackupError, match=".tar.gz"):
        create_backup(tmp_path / "backup.zip", volume_paths={"v": vol})


def test_create_refuses_overwrite(tmp_path: Path):
    vol = _write_tree(tmp_path / "vol", {"a.txt": "a"})
    archive = tmp_path / "backup.tar.gz"
    create_backup(archive, volume_paths={"v": vol})
    with pytest.raises(BackupError, match="overwrite"):
        create_backup(archive, volume_paths={"v": vol})


def test_checksum_mismatch_detected(tmp_path: Path):
    vol = _write_tree(tmp_path / "vol", {"a.txt": "a"})
    archive = tmp_path / "backup.tar.gz"
    create_backup(archive, volume_paths={"uploads": vol})

    # Tamper with a member inside the archive.
    staging = tmp_path / "evil"
    staging.mkdir()
    with tarfile.open(archive, "r:gz") as handle:
        try:
            handle.extractall(staging, filter="data")
        except TypeError:
            handle.extractall(staging)
    member = staging / "volumes" / "uploads.tar.gz"
    member.write_bytes(member.read_bytes() + b"tamper")
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as handle:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                handle.add(path, arcname=str(path.relative_to(staging)))

    with pytest.raises(BackupError, match="Checksum mismatch"):
        inspect_backup(evil)


def test_rejects_path_traversal_in_volume_tarball(tmp_path: Path):
    # Build a hand-crafted archive with a traversal member under volumes/.
    staging = tmp_path / "staging"
    (staging / "volumes").mkdir(parents=True)
    bad_vol = staging / "volumes" / "uploads.tar.gz"
    with tarfile.open(bad_vol, "w:gz") as handle:
        info = tarfile.TarInfo(name="../escape.txt")
        data = b"nope"
        info.size = len(data)
        import io

        handle.addfile(info, io.BytesIO(data))

    key = {
        "secrets_kms_backend": None,
        "encryption_key_id": None,
        "local_dev_key_env": "SECRETS_LOCAL_DEV_KMS_KEY",
        "note": "test",
    }
    (staging / "key-material.json").write_text(json.dumps(key), encoding="utf-8")
    from boxxkite.backup.archive import _sha256_file

    members = {
        "key-material.json": _sha256_file(staging / "key-material.json"),
        "volumes/uploads.tar.gz": _sha256_file(bad_vol),
    }
    manifest = {
        "schema_version": 1,
        "created_at_utc": "2026-01-01T00-00-00Z",
        "members": members,
        "volumes": ["uploads"],
        "has_postgres": False,
        "key_material": key,
    }
    (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                handle.add(path, arcname=str(path.relative_to(staging)))

    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(BackupError, match="unsafe archive member"):
        restore_backup(archive, volume_paths={"uploads": dest})
