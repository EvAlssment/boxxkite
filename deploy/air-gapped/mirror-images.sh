#!/usr/bin/env bash
# Builds/pulls every image docker-compose.airgapped.yml references and saves
# them into a single tarball an operator carries across the air gap.
#
# MUST be run on a machine with live internet access (this is the only step
# in the whole air-gapped workflow that needs one). See README.md in this
# directory for the full walkthrough, real timings, and what this bundle
# does/doesn't cover.
#
# Usage: ./mirror-images.sh [output-dir]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${1:-$REPO_ROOT/deploy/air-gapped/bundle}"
mkdir -p "$OUT_DIR"

# Kept in sync by hand with docker-compose.airgapped.yml's `image:` lines —
# there is no automated drift check for this (unlike the pod-template.yaml
# parity test CLAUDE.md describes for K8s manifests); if you add a service
# to that compose file, add its image here too.
SANDBOX_TAG="boxkite-sandbox-minimal:bundle"
SIDECAR_TAG="boxkite-sidecar:bundle"
MINIO_TAG="minio/minio:latest"
MC_TAG="minio/mc:latest"
VAULT_TAG="hashicorp/vault:1.16"

echo "== [1/5] Building sandbox image (deploy/sandbox-minimal.Dockerfile) =="
# Uses sandbox-minimal, not sandbox.Dockerfile, deliberately — see README.md
# "Why sandbox-minimal instead of the default sandbox image" for why.
time docker build -f "$REPO_ROOT/deploy/sandbox-minimal.Dockerfile" -t "$SANDBOX_TAG" "$REPO_ROOT"

echo "== [2/5] Building sidecar image (deploy/sidecar.Dockerfile) =="
time docker build -f "$REPO_ROOT/deploy/sidecar.Dockerfile" -t "$SIDECAR_TAG" "$REPO_ROOT"

echo "== [3/5] Pulling already-published upstream images (minio, mc, vault) =="
time docker pull "$MINIO_TAG"
time docker pull "$MC_TAG"
time docker pull "$VAULT_TAG"

TARBALL="$OUT_DIR/boxkite-airgapped-bundle.tar"
echo "== [4/5] Saving all 5 images to $TARBALL =="
time docker save -o "$TARBALL" "$SANDBOX_TAG" "$SIDECAR_TAG" "$MINIO_TAG" "$MC_TAG" "$VAULT_TAG"

echo "== [5/5] Compressing bundle =="
time gzip -f "$TARBALL"
TARBALL="$TARBALL.gz"

MANIFEST="$OUT_DIR/bundle-manifest.json"
python3 - "$MANIFEST" "$SANDBOX_TAG" "$SIDECAR_TAG" "$MINIO_TAG" "$MC_TAG" "$VAULT_TAG" <<'PYEOF'
import json
import subprocess
import sys

manifest_path = sys.argv[1]
tags = sys.argv[2:]
images = []
for tag in tags:
    image_id = subprocess.check_output(["docker", "inspect", "--format", "{{.Id}}", tag]).decode().strip()
    size = subprocess.check_output(["docker", "inspect", "--format", "{{.Size}}", tag]).decode().strip()
    images.append({"tag": tag, "image_id": image_id, "size_bytes": int(size)})

with open(manifest_path, "w") as fh:
    json.dump({"images": images}, fh, indent=2)
    fh.write("\n")
PYEOF

echo
echo "Bundle:   $TARBALL"
echo "Manifest: $MANIFEST"
ls -lh "$TARBALL"
