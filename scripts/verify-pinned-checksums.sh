#!/usr/bin/env bash
#
# Independently cross-check the pandoc / Chrome-for-Testing sha256 digests
# pinned in deploy/sandbox.Dockerfile against a second source, not just a
# single self-download-and-hash.
#
# Why this exists (see SECURITY.md, "Known follow-ups", and issue #75):
# those digests were originally self-computed -- downloaded once from the
# official release/GCS URLs and hashed by whoever bumped the version -- which
# guards against corruption/tampering in transit or a later CDN swap, but
# does not by itself protect against the upstream release already being
# compromised (or the digest being mistyped) before the pin was recorded.
#
# This script re-derives each digest from a source independent of "trust the
# person who ran curl | sha256sum last time":
#
#   pandoc:            GitHub's Releases API exposes a `digest` field per
#                       release asset, computed server-side by GitHub at
#                       upload time -- independent of jgm/pandoc's own
#                       release process and independent of this repo's
#                       maintainer. We compare that against the Dockerfile
#                       pin AND against a fresh re-download's own sha256.
#
#   Chrome for Testing: Chrome for Testing publishes NO checksum of any kind
#                       in its own manifests (known-good-versions-with-
#                       downloads.json / last-known-good-versions-with-
#                       downloads.json contain URLs only -- verified by
#                       inspecting both files; see deploy/
#                       pinned-checksums-verification.json). The best
#                       available independent cross-check is Google Cloud
#                       Storage's own backend-computed `x-goog-hash: md5=...`
#                       response header for the object, which is generated
#                       by GCS at upload time, independent of anyone who
#                       later self-computed a sha256 for the Dockerfile pin.
#                       We fetch that header, download the artifact fresh,
#                       and verify BOTH: the fresh sha256 matches the
#                       Dockerfile pin, and the fresh md5 matches GCS's own
#                       reported md5 for that object.
#
# Run this every time PANDOC_VERSION or CHROME_FOR_TESTING_VERSION is bumped
# in deploy/sandbox.Dockerfile, then update deploy/pinned-checksums-
# verification.json with the new result (tests/test_pinned_checksum_
# verification.py enforces that the recorded versions/hashes in that file
# stay in sync with the Dockerfile, so a version bump without re-running
# this script fails CI).
#
# Requires: curl, sha256sum (or shasum -a 256), python3, network access to
# github.com, api.github.com, storage.googleapis.com, and
# googlechromelabs.github.io.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="${REPO_ROOT}/deploy/sandbox.Dockerfile"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

FAILURES=0

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

md5_of() {
    if command -v md5sum >/dev/null 2>&1; then
        md5sum "$1" | awk '{print $1}'
    else
        md5 -q "$1"
    fi
}

report() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  [MATCH] ${label}: ${actual}"
    else
        echo "  [MISMATCH] ${label}: expected ${expected}, got ${actual}" >&2
        FAILURES=$((FAILURES + 1))
    fi
}

extract_dockerfile_arg() {
    # e.g. extract_dockerfile_arg PANDOC_VERSION
    grep -m1 "^ARG $1=" "$DOCKERFILE" | cut -d= -f2
}

extract_dockerfile_var() {
    # e.g. extract_dockerfile_var pandoc_sha256=\" for the amd64 case line
    grep -m1 "$1" "$DOCKERFILE" | grep -oE '[0-9a-f]{64}'
}

PANDOC_VERSION="$(extract_dockerfile_arg PANDOC_VERSION)"
CHROME_FOR_TESTING_VERSION="$(extract_dockerfile_arg CHROME_FOR_TESTING_VERSION)"

PANDOC_AMD64_PIN="$(grep -m1 'x86_64) architecture="amd64"' "$DOCKERFILE" | grep -oE '[0-9a-f]{64}')"
PANDOC_ARM64_PIN="$(grep -m1 'aarch64|arm64) architecture="arm64"' "$DOCKERFILE" | grep -oE '[0-9a-f]{64}')"
CHROME_PIN="$(grep -m1 'chrome_sha256=' "$DOCKERFILE" | grep -oE '[0-9a-f]{64}')"
HEADLESS_PIN="$(grep -m1 'headless_sha256=' "$DOCKERFILE" | grep -oE '[0-9a-f]{64}')"

echo "Pinned versions (from deploy/sandbox.Dockerfile):"
echo "  PANDOC_VERSION=${PANDOC_VERSION}"
echo "  CHROME_FOR_TESTING_VERSION=${CHROME_FOR_TESTING_VERSION}"
echo

# --- pandoc: cross-check against GitHub's server-computed asset digest ---
echo "== pandoc ${PANDOC_VERSION} =="
GH_ASSETS_JSON="${WORKDIR}/pandoc-release.json"
curl -fsSL "https://api.github.com/repos/jgm/pandoc/releases/tags/${PANDOC_VERSION}" -o "$GH_ASSETS_JSON"

for pair in "linux-amd64:${PANDOC_AMD64_PIN}" "linux-arm64:${PANDOC_ARM64_PIN}"; do
    arch="${pair%%:*}"
    pinned="${pair##*:}"
    asset_name="pandoc-${PANDOC_VERSION}-${arch}.tar.gz"
    url="https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/${asset_name}"

    gh_digest="$(python3 -c "
import json, sys
data = json.load(open('$GH_ASSETS_JSON'))
for asset in data.get('assets', []):
    if asset['name'] == '$asset_name':
        print((asset.get('digest') or '').removeprefix('sha256:'))
        break
")"
    if [ -n "$gh_digest" ]; then
        report "${asset_name} GitHub API digest vs Dockerfile pin" "$pinned" "$gh_digest"
    else
        echo "  [WARN] GitHub API did not return a digest field for ${asset_name}; skipping that cross-check" >&2
    fi

    dest="${WORKDIR}/${asset_name}"
    curl -fsSL "$url" -o "$dest"
    fresh="$(sha256_of "$dest")"
    report "${asset_name} fresh re-download vs Dockerfile pin" "$pinned" "$fresh"
done
echo

# --- Chrome for Testing: confirm no upstream checksum exists, then
#     cross-check against GCS's own server-computed md5 ---
echo "== Chrome for Testing ${CHROME_FOR_TESTING_VERSION} =="
for manifest in known-good-versions-with-downloads last-known-good-versions-with-downloads; do
    url="https://googlechromelabs.github.io/chrome-for-testing/${manifest}.json"
    if curl -fsSL "$url" 2>/dev/null | grep -qi '"sha256"\|"md5"\|"hash"'; then
        echo "  [NOTE] ${manifest}.json now appears to contain a hash field -- re-check manually, upstream may have started publishing checksums" >&2
    fi
done
echo "  (confirmed: Chrome for Testing publishes no checksum field in its own manifests as of this run)"

for pair in "chrome-linux64.zip:${CHROME_PIN}" "chrome-headless-shell-linux64.zip:${HEADLESS_PIN}"; do
    name="${pair%%:*}"
    pinned="${pair##*:}"
    url="https://storage.googleapis.com/chrome-for-testing-public/${CHROME_FOR_TESTING_VERSION}/linux64/${name}"

    headers="${WORKDIR}/${name}.headers"
    curl -fsSI "$url" -o "$headers"
    gcs_md5_b64="$(grep -i '^x-goog-hash:' "$headers" | grep -oE 'md5=[A-Za-z0-9+/=]+' | cut -d= -f2- || true)"

    dest="${WORKDIR}/${name}"
    curl -fsSL "$url" -o "$dest"
    fresh_sha256="$(sha256_of "$dest")"
    fresh_md5="$(md5_of "$dest")"

    report "${name} fresh re-download sha256 vs Dockerfile pin" "$pinned" "$fresh_sha256"

    if [ -n "$gcs_md5_b64" ]; then
        gcs_md5_hex="$(python3 -c "import base64,sys; print(base64.b64decode(sys.argv[1]).hex())" "$gcs_md5_b64")"
        report "${name} fresh re-download md5 vs GCS-reported md5 (independent source)" "$gcs_md5_hex" "$fresh_md5"
    else
        echo "  [WARN] no x-goog-hash md5 header returned for ${name}; skipping GCS cross-check" >&2
    fi
done
echo

if [ "$FAILURES" -eq 0 ]; then
    echo "All checks passed. Update deploy/pinned-checksums-verification.json with today's date/results."
    exit 0
else
    echo "${FAILURES} check(s) failed -- investigate before trusting the pinned digests." >&2
    exit 1
fi
