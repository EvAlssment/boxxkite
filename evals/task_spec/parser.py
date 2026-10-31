from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class TaskSpecError(ValueError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass(frozen=True)
class ResourceConstraints:
    cpu_millis: int
    memory_mb: int


@dataclass(frozen=True)
class TaskSpec:
    spec_version: int
    name: str
    setup: tuple[str, ...]
    entrypoint: tuple[str, ...]
    verifier: tuple[str, ...]
    timeout_seconds: int
    resources: ResourceConstraints
    reward_range: tuple[float, float]


def _mapping(value: Any, field: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskSpecError(f"{field} must be a mapping")
    extra = value.keys() - keys
    missing = keys - value.keys()
    if extra or missing:
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if extra:
            details.append(f"unknown {', '.join(sorted(extra))}")
        raise TaskSpecError(f"{field}: {'; '.join(details)}")
    return value


def _command(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(arg, str) or not arg for arg in value):
        raise TaskSpecError(f"{field} must be a non-empty list of non-empty strings")
    return tuple(value)


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise TaskSpecError(f"{field} must be a positive integer")
    return value


def parse_task_spec(data: Any) -> TaskSpec:
    root = _mapping(
        data,
        "task spec",
        {"spec_version", "name", "setup", "entrypoint", "verifier", "timeout_seconds", "resources", "reward_range"},
    )
    if type(root["spec_version"]) is not int or root["spec_version"] != 1:
        raise TaskSpecError("spec_version must be 1")
    if not isinstance(root["name"], str) or not root["name"].strip():
        raise TaskSpecError("name must be a non-empty string")
    resources = _mapping(root["resources"], "resources", {"cpu_millis", "memory_mb"})
    reward = root["reward_range"]
    if (
        not isinstance(reward, list)
        or len(reward) != 2
        or any(type(value) not in (int, float) for value in reward)
        or reward[0] > reward[1]
    ):
        raise TaskSpecError("reward_range must contain two ordered numbers")
    return TaskSpec(
        spec_version=1,
        name=root["name"].strip(),
        setup=_command(root["setup"], "setup"),
        entrypoint=_command(root["entrypoint"], "entrypoint"),
        verifier=_command(root["verifier"], "verifier"),
        timeout_seconds=_positive_int(root["timeout_seconds"], "timeout_seconds"),
        resources=ResourceConstraints(
            cpu_millis=_positive_int(resources["cpu_millis"], "resources.cpu_millis"),
            memory_mb=_positive_int(resources["memory_mb"], "resources.memory_mb"),
        ),
        reward_range=(float(reward[0]), float(reward[1])),
    )


def load_task_spec(path: str | Path) -> TaskSpec:
    try:
        with Path(path).open(encoding="utf-8") as stream:
            data = yaml.load(stream, Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise TaskSpecError(f"unable to load task spec: {exc}") from exc
    return parse_task_spec(data)
