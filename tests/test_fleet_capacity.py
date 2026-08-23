from __future__ import annotations

import textwrap

import pytest

from boxxkite.fleet_capacity import (
    FLEET_CAPACITY_CONFIG_ENV,
    FLEET_CLUSTER_NAME_ENV,
    FleetCapacityConfigError,
    load_active_cluster_capacity,
    load_fleet_capacity_config,
)


def _write_config(tmp_path, body: str):
    path = tmp_path / "fleet.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_loads_per_cluster_targets_images_and_scheduling_constraints(tmp_path):
    path = _write_config(
        tmp_path,
        """
        apiVersion: boxxkite.dev/v1alpha1
        kind: FleetCapacity
        clusters:
          - name: us-east
            region: us-east-1
            warmPool:
              maxSize: 12
              targets: {small: 10, medium: 0, large: 2}
            images:
              sandbox: registry.example/sandbox:v1
              sidecar: registry.example/sidecar:v1
            nodeSelector: {pool: sandbox}
            tolerations:
              - key: dedicated
                operator: Equal
                value: sandbox
                effect: NoSchedule
                tolerationSeconds: 60
        """,
    )

    config = load_fleet_capacity_config(path)
    cluster = config.select("us-east")

    assert cluster.targets == {"small": 10, "medium": 0, "large": 2}
    assert cluster.max_size == 12
    assert cluster.images["sandbox"] == "registry.example/sandbox:v1"
    assert cluster.node_selector == {"pool": "sandbox"}
    assert cluster.tolerations == (
        {
            "key": "dedicated",
            "operator": "Equal",
            "value": "sandbox",
            "effect": "NoSchedule",
            "toleration_seconds": 60,
        },
    )


def test_rejects_duplicate_cluster_names(tmp_path):
    path = _write_config(
        tmp_path,
        """
        apiVersion: boxxkite.dev/v1alpha1
        kind: FleetCapacity
        clusters:
          - name: us-east
            warmPool: {maxSize: 1, targets: {small: 1}}
          - name: us-east
            warmPool: {maxSize: 1, targets: {small: 1}}
        """,
    )

    with pytest.raises(FleetCapacityConfigError, match="duplicate cluster"):
        load_fleet_capacity_config(path)


def test_rejects_duplicate_yaml_keys(tmp_path):
    path = _write_config(
        tmp_path,
        """
        apiVersion: boxxkite.dev/v1alpha1
        kind: FleetCapacity
        clusters:
          - name: us-east
            warmPool: {maxSize: 1, targets: {small: 1}}
            warmPool: {maxSize: 1, targets: {small: 1}}
        """,
    )

    with pytest.raises(FleetCapacityConfigError, match="could not parse"):
        load_fleet_capacity_config(path)


def test_rejects_targets_above_per_cluster_ceiling(tmp_path):
    path = _write_config(
        tmp_path,
        """
        apiVersion: boxxkite.dev/v1alpha1
        kind: FleetCapacity
        clusters:
          - name: us-east
            warmPool: {maxSize: 3, targets: {small: 2, large: 2}}
        """,
    )

    with pytest.raises(FleetCapacityConfigError, match="exceeds"):
        load_fleet_capacity_config(path)


def test_rejects_unknown_size_and_invalid_toleration(tmp_path):
    path = _write_config(
        tmp_path,
        """
        apiVersion: boxxkite.dev/v1alpha1
        kind: FleetCapacity
        clusters:
          - name: us-east
            warmPool: {maxSize: 1, targets: {tiny: 1}}
        """,
    )
    with pytest.raises(FleetCapacityConfigError, match="unsupported field"):
        load_fleet_capacity_config(path)

    path = _write_config(
        tmp_path,
        """
        apiVersion: boxxkite.dev/v1alpha1
        kind: FleetCapacity
        clusters:
          - name: us-east
            warmPool: {maxSize: 1, targets: {small: 1}}
            tolerations: [{operator: Invalid}]
        """,
    )
    with pytest.raises(FleetCapacityConfigError, match="operator"):
        load_fleet_capacity_config(path)


def test_multi_cluster_selection_requires_name_and_unknown_names_fail(tmp_path, monkeypatch):
    path = _write_config(
        tmp_path,
        """
        apiVersion: boxxkite.dev/v1alpha1
        kind: FleetCapacity
        clusters:
          - name: us-east
            warmPool: {maxSize: 1, targets: {small: 1}}
          - name: eu-west
            warmPool: {maxSize: 1, targets: {small: 1}}
        """,
    )
    monkeypatch.setenv(FLEET_CAPACITY_CONFIG_ENV, str(path))
    monkeypatch.delenv(FLEET_CLUSTER_NAME_ENV, raising=False)
    with pytest.raises(FleetCapacityConfigError, match="is required"):
        load_active_cluster_capacity()

    monkeypatch.setenv(FLEET_CLUSTER_NAME_ENV, "ap-south")
    with pytest.raises(FleetCapacityConfigError, match="does not match"):
        load_active_cluster_capacity()

    monkeypatch.setenv(FLEET_CLUSTER_NAME_ENV, "eu-west")
    assert load_active_cluster_capacity().name == "eu-west"


def test_no_fleet_path_preserves_legacy_env_configuration(monkeypatch):
    monkeypatch.delenv(FLEET_CAPACITY_CONFIG_ENV, raising=False)
    monkeypatch.setenv(FLEET_CLUSTER_NAME_ENV, "ignored-without-file")
    assert load_active_cluster_capacity() is None
