# Backup and restore a self-hosted control-plane

Self-hosted operators can snapshot control-plane Postgres data and named
storage volume directories into a single versioned archive with
`boxxkite backup`, then restore them later. This is the first-party path for
[GitHub issue #118](https://github.com/EvAlssment/boxxkite/issues/118).

It complements — but does not replace — the optional Compose backup job in
[`deploy/PRODUCTION-COMPOSE.md`](../../deploy/PRODUCTION-COMPOSE.md)
(`docker compose ... --profile backup`). The CLI adds:

- a portable `.tar.gz` archive with a checksummed `manifest.json`
- explicit secrets **key references** in `key-material.json` (never raw key bytes)
- a matching `restore` command with preflight checks

## Archive layout

```text
backup-YYYY....tar.gz
├── manifest.json          # schema_version, member sha256 digests
├── key-material.json      # SECRETS_KMS_BACKEND / SECRETS_KMS_KEY_ID references only
├── postgres.sql.gz        # optional pg_dump
└── volumes/
    └── <name>.tar.gz      # optional named volume trees
```

Schema version is currently `1`.

## Create a backup

```bash
# Volumes only (useful for Compose data dirs or copied volume mounts)
boxxkite backup create \
  --output ./backups/boxxkite-$(date -u +%Y%m%dT%H%M%SZ).tar.gz \
  --volume uploads=/var/lib/boxxkite/uploads \
  --volume workspace=/var/lib/boxxkite/workspace \
  --secrets-kms-backend "$SECRETS_KMS_BACKEND" \
  --secrets-key-id "$SECRETS_KMS_KEY_ID"

# Postgres via pg_dump (requires PostgreSQL client tools on PATH)
export DATABASE_URL='postgresql://user:password@127.0.0.1:5432/boxxkite'
boxxkite backup create \
  --output ./backups/cp.tar.gz \
  --database-url "$DATABASE_URL" \
  --secrets-kms-backend aws \
  --secrets-key-id "$SECRETS_KMS_KEY_ID"

# Or feed a dump you already took
pg_dump "$DATABASE_URL" > /tmp/boxxkite.sql
boxxkite backup create --output ./backups/cp.tar.gz --postgres-dump /tmp/boxxkite.sql
```

`DATABASE_URL`, `SECRETS_KMS_BACKEND`, and `SECRETS_KMS_KEY_ID` are also read
from the environment when the matching flags are omitted.

## Inspect

```bash
boxxkite backup inspect --archive ./backups/cp.tar.gz
```

Verifies every member digest in `manifest.json` before printing membership.

## Restore

```bash
# Volume restore
boxxkite backup restore \
  --archive ./backups/cp.tar.gz \
  --volume uploads=/var/lib/boxxkite/uploads \
  --volume workspace=/var/lib/boxxkite/workspace \
  --yes

# Postgres restore — requires an explicit key-availability acknowledgement
boxxkite backup restore \
  --archive ./backups/cp.tar.gz \
  --database-url "$DATABASE_URL" \
  --confirm-key-available \
  --yes
```

### Why `--confirm-key-available`?

Org secrets in the control-plane database are stored as ciphertext wrapped by
the configured secrets KMS backend (`SECRETS_KMS_*` /
`SECRETS_LOCAL_DEV_KMS_KEY`). The backup archive **must not** embed those key
bytes — otherwise a stolen backup file would include everything needed to
decrypt secrets offline.

`key-material.json` only records which backend / key id was in use. Restore
therefore requires you to affirm that the same key material is still available
to the control-plane that will open the restored database.

## Relation to Compose `--profile backup`

The production Compose overlay still ships `deploy/backup.sh` for an in-stack
job that tars mounted volumes into the `backups` Docker volume. Prefer
`boxxkite backup` when you want:

- a single downloadable archive with integrity digests
- recorded key-material references
- a first-party restore path

Keep the Compose job for cron-on-the-VM workflows that already mount those
volumes, then optionally wrap its output with `boxxkite backup create
--postgres-dump ... --volume ...` if you want the versioned archive format.

## Out of scope (v1)

- Automatic S3/object-storage upload (planned follow-up / CronJob mode)
- Cloud CSI volume-snapshot orchestration across every provider
- Embedding or exporting raw `SECRETS_LOCAL_DEV_KMS_KEY` / KMS plaintext keys
