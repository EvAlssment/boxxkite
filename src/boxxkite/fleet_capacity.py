"""Validated, process-local fleet capacity configuration.

The fleet file selects one cluster for the current process. Routing between
clusters belongs to a separate provider layer; this module only makes the
selected cluster's pod and warm-pool settings declarative and bounded.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

FLEET_CAPACITY_CONFIG_ENV = "BOXXKITE_FLEET_CONFIG"
FLEET_CLUSTER_NAME_ENV = "BOXXKITE_CLUSTER_NAME"
FLEET_API_VERSION = "boxxkite.dev/v1alpha1"
FLEET_KIND = "FleetCapacity"
SIZE_CLASSES = ("small", "medium", "large")
_CLUSTER_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_TOLERATION_KEYS = {"key", "operator", "value", "effect", "tolerationSeconds"}
_TOLERATION_OPERATORS = {"Equal", "Exists"}
_TOLERATION_EFFECTS = {"NoSchedule", "PreferNoSchedule", "NoExecute"}


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.Node, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class FleetCapacityConfigError(ValueError):
    """Raised when a fleet capacity file is invalid or cannot be selected."""


@dataclass(frozen=True)
class ClusterCapacity:
    name: str
    region: str
    targets: dict[str, int]
    max_size: int
    images: dict[str, str]
    node_selector: dict[str, str]
    tolerations: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class FleetCapacityConfig:
    api_version: str
    kind: str
    clusters: tuple[ClusterCapacity, ...]

    def select(self, cluster_name: str | None = None) -> ClusterCapacity:
        if cluster_name:
            for cluster in self.clusters:
                if cluster.name == cluster_name:
                    return cluster
            raise FleetCapacityConfigError(
                f"{FLEET_CLUSTER_NAME_ENV}={cluster_name!r} does not match any configured cluster"
            )
        if len(self.clusters) == 1:
            return self.clusters[0]
        raise FleetCapacityConfigError(
            f"{FLEET_CLUSTER_NAME_ENV} is required when {len(self.clusters)} clusters are configured"
        )


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise FleetCapacityConfigError(f"{path} must be a mapping")
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise FleetCapacityConfigError(f"{path} contains unsupported field(s): {', '.join(unknown)}")


def _nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FleetCapacityConfigError(f"{path} must be a non-negative integer")
    return value


def _string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        detail = "non-empty string" if nonempty else "string"
        raise FleetCapacityConfigError(f"{path} must be a {detail}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise FleetCapacityConfigError(f"{path} contains a control character")
    return value


def _parse_targets(value: Any, path: str) -> dict[str, int]:
    raw = _mapping(value, path)
    _reject_unknown(raw, set(SIZE_CLASSES), path)
    return {size: _nonnegative_int(raw.get(size, 0), f"{path}.{size}") for size in SIZE_CLASSES}


def _parse_images(value: Any, path: str) -> dict[str, str]:
    if value is None:
        return {}
    raw = _mapping(value, path)
    _reject_unknown(raw, {"sandbox", "sidecar"}, path)
    return {name: _string(image, f"{path}.{name}") for name, image in raw.items()}


def _parse_node_selector(value: Any, path: str) -> dict[str, str]:
    if value is None:
        return {}
    raw = _mapping(value, path)
    return {
        _string(key, f"{path} key"): _string(item, f"{path}.{key}")
        for key, item in raw.items()
    }


def _parse_tolerations(value: Any, path: str) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise FleetCapacityConfigError(f"{path} must be a list")
    parsed: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        raw = _mapping(item, item_path)
        _reject_unknown(raw, _TOLERATION_KEYS, item_path)
        operator = raw.get("operator", "Equal")
        if operator not in _TOLERATION_OPERATORS:
            raise FleetCapacityConfigError(
                f"{item_path}.operator must be one of {sorted(_TOLERATION_OPERATORS)}"
            )
        effect = raw.get("effect", "")
        if effect and effect not in _TOLERATION_EFFECTS:
            raise FleetCapacityConfigError(
                f"{item_path}.effect must be one of {sorted(_TOLERATION_EFFECTS)}"
            )
        parsed_item: dict[str, Any] = {"operator": operator}
        for yaml_key, k8s_key in (("key", "key"), ("value", "value"), ("effect", "effect")):
            if yaml_key in raw:
                parsed_item[k8s_key] = _string(raw[yaml_key], f"{item_path}.{yaml_key}", nonempty=False)
        if "tolerationSeconds" in raw:
            parsed_item["toleration_seconds"] = _nonnegative_int(
                raw["tolerationSeconds"], f"{item_path}.tolerationSeconds"
            )
        parsed.append(parsed_item)
    return tuple(parsed)


def _parse_cluster(value: Any, index: int) -> ClusterCapacity:
    path = f"clusters[{index}]"
    raw = _mapping(value, path)
    _reject_unknown(raw, {"name", "region", "warmPool", "images", "nodeSelector", "tolerations"}, path)
    name = _string(raw.get("name"), f"{path}.name")
    if not _CLUSTER_NAME_RE.fullmatch(name) or len(name) > 63:
        raise FleetCapacityConfigError(
            f"{path}.name must be a DNS-label style name of at most 63 characters"
        )
    region = _string(raw.get("region", ""), f"{path}.region", nonempty=False)
    warm_pool = _mapping(raw.get("warmPool"), f"{path}.warmPool")
    _reject_unknown(warm_pool, {"targets", "maxSize"}, f"{path}.warmPool")
    targets = _parse_targets(warm_pool.get("targets"), f"{path}.warmPool.targets")
    max_size = _nonnegative_int(warm_pool.get("maxSize"), f"{path}.warmPool.maxSize")
    target_total = sum(targets.values())
    if target_total > max_size:
        raise FleetCapacityConfigError(
            f"{path}.warmPool.targets total ({target_total}) exceeds "
            f"{path}.warmPool.maxSize ({max_size})"
        )
    return ClusterCapacity(
        name=name,
        region=region,
        targets=targets,
        max_size=max_size,
        images=_parse_images(raw.get("images"), f"{path}.images"),
        node_selector=_parse_node_selector(raw.get("nodeSelector"), f"{path}.nodeSelector"),
        tolerations=_parse_tolerations(raw.get("tolerations"), f"{path}.tolerations"),
    )


def load_fleet_capacity_config(path: str | os.PathLike[str]) -> FleetCapacityConfig:
    """Load and validate a fleet capacity YAML file."""
    config_path = Path(path)
    try:
        document = yaml.load(
            config_path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader
        )
    except OSError as exc:
        raise FleetCapacityConfigError(f"could not read fleet config {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise FleetCapacityConfigError(f"could not parse fleet config {config_path}: {exc}") from exc

    raw = _mapping(document, "fleet config")
    _reject_unknown(raw, {"apiVersion", "kind", "clusters"}, "fleet config")
    if raw.get("apiVersion") != FLEET_API_VERSION:
        raise FleetCapacityConfigError(
            f"fleet config.apiVersion must be {FLEET_API_VERSION!r}"
        )
    if raw.get("kind") != FLEET_KIND:
        raise FleetCapacityConfigError(f"fleet config.kind must be {FLEET_KIND!r}")
    clusters = raw.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        raise FleetCapacityConfigError("fleet config.clusters must be a non-empty list")
    parsed = tuple(_parse_cluster(item, index) for index, item in enumerate(clusters))
    names = [cluster.name for cluster in parsed]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise FleetCapacityConfigError(
            f"fleet config contains duplicate cluster name(s): {', '.join(duplicates)}"
        )
    return FleetCapacityConfig(
        api_version=FLEET_API_VERSION,
        kind=FLEET_KIND,
        clusters=parsed,
    )


def load_active_cluster_capacity() -> ClusterCapacity | None:
    """Load the configured cluster once, or preserve legacy env behavior."""
    path = os.environ.get(FLEET_CAPACITY_CONFIG_ENV, "").strip()
    if not path:
        return None
    config = load_fleet_capacity_config(path)
    cluster_name = os.environ.get(FLEET_CLUSTER_NAME_ENV, "").strip() or None
    return config.select(cluster_name)


ACTIVE_CLUSTER_CAPACITY = load_active_cluster_capacity()
