#!/bin/sh
set -eu

backup_dir="/backups/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$backup_dir"

for volume in uploads outputs skills workspace tmp minio-data; do
  tar -czf "$backup_dir/$volume.tar.gz" -C "/source/$volume" .
done

if [ -n "${DATABASE_URL:-}" ]; then
  pg_dump "$DATABASE_URL" | gzip > "$backup_dir/postgres.sql.gz"
else
  echo "DATABASE_URL is not set; skipping Postgres dump." >&2
fi

retention="${BACKUP_RETENTION_DAYS:-14}"
find /backups -mindepth 1 -maxdepth 1 -type d -mtime "+$retention" -exec rm -rf {} +
echo "Backup written to $backup_dir"
