#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


EXPECTED_TAGS = (
    "boxxkite-sandbox-minimal:bundle",
    "boxxkite-sidecar:bundle",
    "minio/minio:latest",
    "minio/mc:latest",
    "hashicorp/vault:1.16",
)
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class IntegrityError(Exception):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as archive:
        for chunk in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"Cannot read bundle manifest {path}: {exc}") from exc

    if not isinstance(data, dict) or data.get("version") != 1:
        raise IntegrityError("Unsupported or invalid bundle manifest version")

    archive = data.get("archive")
    if not isinstance(archive, dict):
        raise IntegrityError("Bundle manifest is missing archive metadata")
    filename = archive.get("filename")
    checksum = archive.get("sha256")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise IntegrityError("Bundle manifest has an invalid archive filename")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise IntegrityError("Bundle manifest has an invalid archive SHA-256")

    images = data.get("images")
    if not isinstance(images, list):
        raise IntegrityError("Bundle manifest images must be a list")
    by_tag = {}
    for image in images:
        if not isinstance(image, dict):
            raise IntegrityError("Bundle manifest contains an invalid image entry")
        tag = image.get("tag")
        image_id = image.get("image_id")
        size = image.get("size_bytes")
        if not isinstance(tag, str) or tag in by_tag:
            raise IntegrityError("Bundle manifest contains a missing or duplicate image tag")
        if not isinstance(image_id, str) or not IMAGE_ID_PATTERN.fullmatch(image_id):
            raise IntegrityError(f"Bundle manifest has an invalid image ID for {tag}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise IntegrityError(f"Bundle manifest has an invalid image size for {tag}")
        by_tag[tag] = image

    if set(by_tag) != set(EXPECTED_TAGS):
        missing = sorted(set(EXPECTED_TAGS) - set(by_tag))
        extra = sorted(set(by_tag) - set(EXPECTED_TAGS))
        raise IntegrityError(f"Bundle manifest image tags differ from this Compose bundle (missing={missing}, extra={extra})")
    return data


def create_manifest(archive_path, manifest_path, tags):
    if tuple(tags) != EXPECTED_TAGS:
        raise IntegrityError("Requested image tags do not match the Compose bundle")
    archive_path = Path(archive_path)
    images = []
    for tag in tags:
        image = {}
        for field, docker_field in (("image_id", "Id"), ("size_bytes", "Size")):
            try:
                result = subprocess.run(
                    ["docker", "inspect", "--format", f"{{{{.{docker_field}}}}}", tag],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise IntegrityError(f"Cannot inspect Docker image {tag}: {exc}") from exc
            value = result.stdout.strip()
            image[field] = int(value) if field == "size_bytes" else value
        images.append({"tag": tag, **image})

    manifest = {
        "version": 1,
        "archive": {"filename": archive_path.name, "sha256": sha256_file(archive_path)},
        "images": images,
    }
    manifest_path = Path(manifest_path)
    temporary_path = manifest_path.with_name(f"{manifest_path.name}.tmp")
    try:
        temporary_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_path, manifest_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def verify_archive(archive_path, manifest_path):
    archive_path = Path(archive_path)
    manifest = read_manifest(manifest_path)
    expected_filename = manifest["archive"]["filename"]
    if archive_path.name != expected_filename:
        raise IntegrityError(
            f"Archive filename does not match manifest (expected {expected_filename}, got {archive_path.name})"
        )
    try:
        actual_checksum = sha256_file(archive_path)
    except OSError as exc:
        raise IntegrityError(f"Cannot read bundle archive {archive_path}: {exc}") from exc
    if actual_checksum != manifest["archive"]["sha256"]:
        raise IntegrityError("Bundle archive SHA-256 mismatch; transfer may be incomplete or corrupted")
    return manifest


def verify_images(manifest_path):
    manifest = read_manifest(manifest_path)
    for image in manifest["images"]:
        tag = image["tag"]
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise IntegrityError(f"Cannot inspect loaded image {tag}: {exc}") from exc
        actual_id = result.stdout.strip()
        if actual_id != image["image_id"]:
            raise IntegrityError(
                f"Loaded image ID mismatch for {tag}: expected {image['image_id']}, got {actual_id or '<empty>'}"
            )
        print(f"  OK   {tag} ({actual_id})")


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("--archive", required=True)
    create.add_argument("--manifest", required=True)
    create.add_argument("tags", nargs="+")

    verify_archive_parser = commands.add_parser("verify-archive")
    verify_archive_parser.add_argument("--archive", required=True)
    verify_archive_parser.add_argument("--manifest", required=True)

    verify_images_parser = commands.add_parser("verify-images")
    verify_images_parser.add_argument("--manifest", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            create_manifest(args.archive, args.manifest, args.tags)
            print(f"Manifest written: {args.manifest}")
        elif args.command == "verify-archive":
            verify_archive(args.archive, args.manifest)
            print("Archive SHA-256 verified before load.")
        else:
            verify_images(args.manifest)
    except (IntegrityError, OSError) as exc:
        print(f"Bundle verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
