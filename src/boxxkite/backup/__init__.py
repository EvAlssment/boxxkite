"""Self-hosted control-plane backup/restore (GitHub issue #118).

Creates and restores a versioned archive of Postgres dumps and named volume
directories. Secrets ciphertext may be present inside the Postgres dump; the
archive never embeds raw KMS / local-dev key bytes — only key *references*
in ``key-material.json``.
"""

from .archive import (
    ARCHIVE_FORMAT_VERSION,
    BackupError,
    BackupManifest,
    KeyMaterialRef,
    create_backup,
    inspect_backup,
    restore_backup,
)

__all__ = [
    "ARCHIVE_FORMAT_VERSION",
    "BackupError",
    "BackupManifest",
    "KeyMaterialRef",
    "create_backup",
    "inspect_backup",
    "restore_backup",
]
