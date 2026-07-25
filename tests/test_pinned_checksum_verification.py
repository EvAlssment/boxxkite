"""Regression tests for issue #75: deploy/sandbox.Dockerfile's pinned
pandoc / Chrome-for-Testing sha256 digests were originally self-computed
(downloaded once and hashed by whoever bumped the version), with no
independent cross-check. scripts/verify-pinned-checksums.sh re-derives each
pinned digest from a second, independent source (GitHub's server-computed
release-asset `digest` field for pandoc; GCS's server-computed
`x-goog-hash` md5 for Chrome for Testing) and
deploy/pinned-checksums-verification.json records the dated result of the
last run.

These tests are deliberately offline (no network access) so they run in
every CI invocation, not just when someone remembers to re-run the live
script. They guard against the two ways this protection can silently rot:

1. The verification record drifting out of sync with the Dockerfile (e.g. a
   future version bump that doesn't re-run the script and update the
   record) -- caught by comparing the pinned versions/hashes in both files.
2. The documentation of the residual risk being quietly deleted from
   SECURITY.md or the Dockerfile comments.

They do NOT re-run the live network verification themselves -- that's
scripts/verify-pinned-checksums.sh's job, run by hand (or in a scheduled CI
job with network egress) whenever PANDOC_VERSION/CHROME_FOR_TESTING_VERSION
is bumped.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE_PATH = REPO_ROOT / "deploy" / "sandbox.Dockerfile"
SECURITY_MD_PATH = REPO_ROOT / "SECURITY.md"
VERIFICATION_RECORD_PATH = REPO_ROOT / "deploy" / "pinned-checksums-verification.json"
VERIFY_SCRIPT_PATH = REPO_ROOT / "scripts" / "verify-pinned-checksums.sh"

SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _dockerfile_text() -> str:
    return DOCKERFILE_PATH.read_text()


def _extract_arg(name: str, text: str) -> str:
    match = re.search(rf"^ARG {re.escape(name)}=(.+)$", text, re.MULTILINE)
    assert match, f"Could not find ARG {name}= in {DOCKERFILE_PATH}"
    return match.group(1).strip()


def _extract_pandoc_pins(text: str) -> dict[str, str]:
    amd64 = re.search(r'x86_64\) architecture="amd64"; pandoc_sha256="([0-9a-f]{64})"', text)
    arm64 = re.search(
        r'aarch64\|arm64\) architecture="arm64"; pandoc_sha256="([0-9a-f]{64})"', text
    )
    assert amd64 and arm64, "Could not find pandoc sha256 pins in sandbox.Dockerfile"
    return {"linux-amd64": amd64.group(1), "linux-arm64": arm64.group(1)}


def _extract_chrome_pins(text: str) -> dict[str, str]:
    chrome = re.search(r'chrome_sha256="([0-9a-f]{64})"', text)
    headless = re.search(r'headless_sha256="([0-9a-f]{64})"', text)
    assert chrome and headless, "Could not find Chrome for Testing sha256 pins in sandbox.Dockerfile"
    return {
        "chrome-linux64.zip": chrome.group(1),
        "chrome-headless-shell-linux64.zip": headless.group(1),
    }


def _verification_record() -> dict:
    return json.loads(VERIFICATION_RECORD_PATH.read_text())


def test_verification_script_exists_and_is_executable():
    assert VERIFY_SCRIPT_PATH.is_file()
    assert VERIFY_SCRIPT_PATH.stat().st_mode & 0o111, "verify-pinned-checksums.sh must be executable"


def test_verification_script_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(VERIFY_SCRIPT_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_verification_script_reads_versions_from_dockerfile_not_hardcoded():
    script_text = VERIFY_SCRIPT_PATH.read_text()
    # The script must derive PANDOC_VERSION / CHROME_FOR_TESTING_VERSION (and
    # the pinned hashes) from the Dockerfile itself, not a hardcoded copy --
    # otherwise it would silently check stale values forever instead of
    # whatever is actually currently pinned.
    assert "extract_dockerfile_arg PANDOC_VERSION" in script_text
    assert "extract_dockerfile_arg CHROME_FOR_TESTING_VERSION" in script_text
    assert not re.search(r'PANDOC_VERSION="[0-9]', script_text)
    assert not re.search(r'CHROME_FOR_TESTING_VERSION="[0-9]', script_text)


def test_verification_record_version_matches_dockerfile_pin():
    text = _dockerfile_text()
    record = _verification_record()

    pandoc_version = _extract_arg("PANDOC_VERSION", text)
    chrome_version = _extract_arg("CHROME_FOR_TESTING_VERSION", text)

    assert record["pandoc"]["version"] == pandoc_version, (
        "deploy/pinned-checksums-verification.json's recorded pandoc version "
        f"({record['pandoc']['version']}) no longer matches the Dockerfile's "
        f"PANDOC_VERSION ({pandoc_version}) -- re-run "
        "scripts/verify-pinned-checksums.sh and update the record."
    )
    assert record["chrome_for_testing"]["version"] == chrome_version, (
        "deploy/pinned-checksums-verification.json's recorded Chrome for "
        f"Testing version ({record['chrome_for_testing']['version']}) no "
        f"longer matches the Dockerfile's CHROME_FOR_TESTING_VERSION "
        f"({chrome_version}) -- re-run scripts/verify-pinned-checksums.sh "
        "and update the record."
    )


def test_verification_record_hashes_match_dockerfile_pins():
    text = _dockerfile_text()
    record = _verification_record()

    pandoc_pins = _extract_pandoc_pins(text)
    for arch, pinned_sha256 in pandoc_pins.items():
        asset_name = f"pandoc-{record['pandoc']['version']}-{arch}.tar.gz"
        artifact = record["pandoc"]["artifacts"].get(asset_name)
        assert artifact is not None, (
            f"No verification record for {asset_name} -- re-run "
            "scripts/verify-pinned-checksums.sh and update the record."
        )
        assert artifact["dockerfile_pinned_sha256"] == pinned_sha256
        assert artifact["fresh_download_sha256"] == pinned_sha256
        assert artifact["result"] == "match"

    chrome_pins = _extract_chrome_pins(text)
    for name, pinned_sha256 in chrome_pins.items():
        artifact = record["chrome_for_testing"]["artifacts"].get(name)
        assert artifact is not None, (
            f"No verification record for {name} -- re-run "
            "scripts/verify-pinned-checksums.sh and update the record."
        )
        assert artifact["dockerfile_pinned_sha256"] == pinned_sha256
        assert artifact["fresh_download_sha256"] == pinned_sha256
        assert artifact["result"] == "match"


def test_verification_record_hashes_are_well_formed_sha256():
    record = _verification_record()
    for artifact in record["pandoc"]["artifacts"].values():
        assert SHA256_RE.fullmatch(artifact["dockerfile_pinned_sha256"])
        assert SHA256_RE.fullmatch(artifact["github_api_digest_sha256"])
        assert SHA256_RE.fullmatch(artifact["fresh_download_sha256"])
    for artifact in record["chrome_for_testing"]["artifacts"].values():
        assert SHA256_RE.fullmatch(artifact["dockerfile_pinned_sha256"])
        assert SHA256_RE.fullmatch(artifact["fresh_download_sha256"])


def test_security_md_documents_the_independent_cross_check():
    text = SECURITY_MD_PATH.read_text()
    assert "issue #75" in text
    assert "independently cross-checked" in text
    assert "scripts/verify-pinned-checksums.sh" in text
    assert "pinned-checksums-verification.json" in text
    # The residual-risk framing must survive: neither dependency has a
    # same-algorithm, upstream-signed checksum manifest, so this is a
    # narrowing of the gap, not a full close. Collapse whitespace before
    # matching since the source wraps prose across lines.
    normalized = " ".join(text.split())
    assert "does not exist for either dependency" in normalized


def test_dockerfile_comments_reference_the_verification_script_and_record():
    text = _dockerfile_text()
    assert text.count("scripts/verify-pinned-checksums.sh") == 2
    assert text.count("deploy/pinned-checksums-verification.json") == 2
