#!/usr/bin/env bash
# Loads the mirror-images.sh bundle with ZERO network calls. Run this on the
# air-gapped side, after carrying the tarball across the gap.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="${1:-$SCRIPT_DIR/bundle/boxkite-airgapped-bundle.tar.gz}"

if [ ! -f "$BUNDLE" ]; then
  echo "Bundle not found: $BUNDLE" >&2
  echo "Run mirror-images.sh on a machine with internet access first, then" >&2
  echo "carry the resulting tarball here." >&2
  exit 1
fi

echo "== Loading $BUNDLE (no network access required or used) =="
time gunzip -c "$BUNDLE" | docker load

echo "== Verifying loaded images against expected tags =="
MISSING=0
for tag in \
  "boxkite-sandbox-minimal:bundle" \
  "boxkite-sidecar:bundle" \
  "minio/minio:latest" \
  "minio/mc:latest" \
  "hashicorp/vault:1.16"
do
  if docker image inspect "$tag" >/dev/null 2>&1; then
    echo "  OK   $tag"
  else
    echo "  MISSING $tag"
    MISSING=1
  fi
done

if [ "$MISSING" -ne 0 ]; then
  echo "One or more expected images are missing after load — bundle is incomplete." >&2
  exit 1
fi

echo "Load verified — all bundled images present locally, no registry contacted."
