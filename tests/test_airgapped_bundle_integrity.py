import importlib.util
import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = Path(__file__).parents[1] / "deploy" / "air-gapped" / "bundle_integrity.py"
SPEC = importlib.util.spec_from_file_location("bundle_integrity", MODULE_PATH)
bundle_integrity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bundle_integrity
SPEC.loader.exec_module(bundle_integrity)

IntegrityError = bundle_integrity.IntegrityError
IMAGE_ID = "sha256:" + "a" * 64


def write_bundle(tmp_path):
    archive = tmp_path / "boxxkite-airgapped-bundle.tar.gz"
    archive.write_bytes(b"deterministic archive bytes")
    manifest = tmp_path / "bundle-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "archive": {"filename": archive.name, "sha256": sha256(archive.read_bytes()).hexdigest()},
                "images": [
                    {"tag": tag, "image_id": IMAGE_ID, "size_bytes": 123}
                    for tag in bundle_integrity.EXPECTED_TAGS
                ],
            }
        ),
        encoding="utf-8",
    )
    return archive, manifest


def test_create_manifest_records_archive_checksum_and_docker_image_ids(tmp_path):
    archive = tmp_path / "boxxkite-airgapped-bundle.tar.gz"
    archive.write_bytes(b"bundle")
    manifest_path = tmp_path / "bundle-manifest.json"
    outputs = iter([IMAGE_ID, "123"] * len(bundle_integrity.EXPECTED_TAGS))

    with patch.object(
        bundle_integrity.subprocess,
        "run",
        side_effect=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, next(outputs), ""),
    ) as docker_run:
        bundle_integrity.create_manifest(archive, manifest_path, bundle_integrity.EXPECTED_TAGS)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["archive"] == {
        "filename": archive.name,
        "sha256": sha256(b"bundle").hexdigest(),
    }
    assert [image["tag"] for image in manifest["images"]] == list(bundle_integrity.EXPECTED_TAGS)
    assert all(image["image_id"] == IMAGE_ID and image["size_bytes"] == 123 for image in manifest["images"])
    assert docker_run.call_count == len(bundle_integrity.EXPECTED_TAGS) * 2


def test_verify_archive_accepts_matching_colocated_manifest(tmp_path):
    archive, manifest = write_bundle(tmp_path)

    result = bundle_integrity.verify_archive(archive, manifest)

    assert result["archive"]["filename"] == archive.name


@pytest.mark.parametrize("corruption", [b"truncated", b"tampered"])
def test_verify_archive_rejects_partial_or_changed_transfer(tmp_path, corruption):
    archive, manifest = write_bundle(tmp_path)
    archive.write_bytes(corruption)

    with pytest.raises(IntegrityError, match="SHA-256 mismatch"):
        bundle_integrity.verify_archive(archive, manifest)


def test_verify_archive_rejects_missing_manifest_before_load(tmp_path):
    archive = tmp_path / "boxxkite-airgapped-bundle.tar.gz"
    archive.write_bytes(b"bundle")

    with pytest.raises(IntegrityError, match="Cannot read bundle manifest"):
        bundle_integrity.verify_archive(archive, tmp_path / "missing.json")


def test_verify_archive_rejects_manifest_for_another_archive_name(tmp_path):
    archive, manifest = write_bundle(tmp_path)
    renamed = tmp_path / "renamed.tar.gz"
    renamed.write_bytes(archive.read_bytes())

    with pytest.raises(IntegrityError, match="filename does not match"):
        bundle_integrity.verify_archive(renamed, manifest)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda data: data["images"].pop(), "image tags differ"),
        (lambda data: data["images"].append(data["images"][0]), "duplicate image tag"),
        (lambda data: data["images"][0].update(image_id="sha256:bad"), "invalid image ID"),
        (lambda data: data["archive"].update(sha256="bad"), "invalid archive SHA-256"),
    ],
)
def test_verify_archive_rejects_invalid_manifest_entries(tmp_path, mutate, match):
    archive, manifest_path = write_bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(IntegrityError, match=match):
        bundle_integrity.verify_archive(archive, manifest_path)


def test_verify_images_compares_loaded_ids_to_manifest(tmp_path):
    _, manifest = write_bundle(tmp_path)
    with patch.object(
        bundle_integrity.subprocess,
        "run",
        side_effect=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, IMAGE_ID, ""),
    ) as docker_run:
        bundle_integrity.verify_images(manifest)

    assert docker_run.call_count == len(bundle_integrity.EXPECTED_TAGS)
    assert all(call.args[0][1:3] == ["image", "inspect"] for call in docker_run.call_args_list)


def test_verify_images_fails_when_loaded_id_differs(tmp_path):
    _, manifest = write_bundle(tmp_path)
    different_id = "sha256:" + "b" * 64
    with patch.object(
        bundle_integrity.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(["docker"], 0, different_id, ""),
    ):
        with pytest.raises(IntegrityError, match="Loaded image ID mismatch"):
            bundle_integrity.verify_images(manifest)


def test_offline_loader_verifies_archive_before_docker_load_and_never_pulls():
    loader = (MODULE_PATH.parent / "load-images.sh").read_text(encoding="utf-8")
    compose = (MODULE_PATH.parent / "docker-compose.airgapped.yml").read_text(encoding="utf-8")

    assert loader.index("verify-archive") < loader.index("docker load")
    assert "docker pull" not in loader
    assert "pull_policy: never" in compose
