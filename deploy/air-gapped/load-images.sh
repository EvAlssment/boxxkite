#!/usr/bin/env bash
# Loads the mirror-images.sh bundle with ZERO network calls. Run this on the
# air-gapped side, after carrying the tarball across the gap.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="${1:-$SCRIPT_DIR/bundle/boxxkite-airgapped-bundle.tar.gz}"

if [ ! -f "$BUNDLE" ]; then
  echo "Bundle not found: $BUNDLE" >&2
  echo "Run mirror-images.sh on a machine with internet access first, then" >&2
  echo "carry the resulting tarball here." >&2
  exit 1
fi

MANIFEST="$(dirname "$BUNDLE")/bundle-manifest.json"
python3 "$SCRIPT_DIR/bundle_integrity.py" verify-archive \
  --archive "$BUNDLE" \
  --manifest "$MANIFEST"

echo "== Loading $BUNDLE (no network access required or used) =="
time gunzip -c "$BUNDLE" | docker load

echo "== Verifying loaded image IDs against the bundle manifest =="
python3 "$SCRIPT_DIR/bundle_integrity.py" verify-images --manifest "$MANIFEST"

echo "Load verified — archive checksum and all bundled image IDs match; no registry contacted."
