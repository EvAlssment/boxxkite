"""Regression test: deploy/docker-compose.yml's MinIO service previously
bound its S3 API (9000) and admin Console (9001) ports to all interfaces
("9000:9000"), while .env.example's own comment claimed MinIO was "never
exposed outside the compose network" -- a self-hoster running `docker
compose up` on any host with a permissive firewall/security group exposed
an S3-compatible store (with the well-known minioadmin/minioadmin
credential) to the whole network. Both ports must stay bound to
127.0.0.1 only.
"""

from __future__ import annotations

from pathlib import Path

import yaml

DOCKER_COMPOSE_PATH = Path(__file__).resolve().parent.parent / "deploy" / "docker-compose.yml"


def _minio_service() -> dict:
    doc = yaml.safe_load(DOCKER_COMPOSE_PATH.read_text())
    return doc["services"]["minio"]


def test_minio_ports_are_bound_to_localhost_only():
    ports = _minio_service()["ports"]
    assert len(ports) == 2
    for port_mapping in ports:
        assert str(port_mapping).startswith("127.0.0.1:"), (
            f"MinIO port mapping {port_mapping!r} is not bound to 127.0.0.1 -- "
            "this exposes MinIO's well-known minioadmin/minioadmin credential "
            "beyond the local machine on any host with a permissive firewall."
        )
