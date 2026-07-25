"""Tests for opt-in GPU support (docs/GPU-SUPPORT-SCOPING.md).

Covers resource_config.py's flag/validation helpers and
build_sandbox_container_resources' gpu_count wiring. Does NOT (and cannot,
in this environment) verify anything against a live GPU-equipped cluster
or a real device plugin -- see the scoping doc's own disclosed,
unverified cross-tenant VRAM-wipe question.
"""

from pathlib import Path

import pytest

from boxkite import resource_config
from boxkite._manager_config import _validate_gpu_count


@pytest.fixture(autouse=True)
def _clean_gpu_env(monkeypatch):
    monkeypatch.delenv(resource_config.BOXKITE_GPU_ENABLED_ENV, raising=False)
    monkeypatch.delenv(resource_config.GPU_RESOURCE_NAME_ENV, raising=False)
    monkeypatch.delenv(resource_config.BOXKITE_MAX_GPU_COUNT_PER_SESSION_ENV, raising=False)


def test_gpu_disabled_by_default():
    assert resource_config.gpu_enabled() is False


def test_gpu_resource_name_defaults_to_nvidia():
    assert resource_config.gpu_resource_name() == "nvidia.com/gpu"


def test_gpu_resource_name_is_operator_configurable(monkeypatch):
    monkeypatch.setenv(resource_config.GPU_RESOURCE_NAME_ENV, "amd.com/gpu")
    assert resource_config.gpu_resource_name() == "amd.com/gpu"


def test_max_gpu_count_per_session_defaults_to_one():
    assert resource_config.max_gpu_count_per_session() == 1


def test_build_sandbox_container_resources_omits_gpu_limit_by_default():
    resources = resource_config.build_sandbox_container_resources()
    assert "nvidia.com/gpu" not in resources.limits


def test_build_sandbox_container_resources_adds_gpu_limit_when_requested():
    resources = resource_config.build_sandbox_container_resources(gpu_count=2)
    assert resources.limits["nvidia.com/gpu"] == "2"
    # No fractional/request half for an extended resource -- the scheduler
    # auto-fills an equal request when a limit is set with none given.
    assert "nvidia.com/gpu" not in resources.requests


def test_build_sandbox_container_resources_respects_custom_gpu_resource_name(monkeypatch):
    monkeypatch.setenv(resource_config.GPU_RESOURCE_NAME_ENV, "amd.com/gpu")
    resources = resource_config.build_sandbox_container_resources(gpu_count=1)
    assert resources.limits["amd.com/gpu"] == "1"
    assert "nvidia.com/gpu" not in resources.limits


# ── _validate_gpu_count (manager-level gate) ────────────────────────────


def test_validate_gpu_count_none_is_a_no_op():
    assert _validate_gpu_count(None) is None


def test_validate_gpu_count_rejected_when_disabled():
    with pytest.raises(ValueError, match="BOXKITE_GPU_ENABLED"):
        _validate_gpu_count(1)


def test_validate_gpu_count_accepted_when_enabled_and_within_ceiling(monkeypatch):
    monkeypatch.setenv(resource_config.BOXKITE_GPU_ENABLED_ENV, "true")
    assert _validate_gpu_count(1) == 1


def test_validate_gpu_count_rejected_above_ceiling(monkeypatch):
    monkeypatch.setenv(resource_config.BOXKITE_GPU_ENABLED_ENV, "true")
    monkeypatch.setenv(resource_config.BOXKITE_MAX_GPU_COUNT_PER_SESSION_ENV, "2")
    with pytest.raises(ValueError, match="at most 2"):
        _validate_gpu_count(3)


def test_validate_gpu_count_rejected_when_zero_or_negative(monkeypatch):
    monkeypatch.setenv(resource_config.BOXKITE_GPU_ENABLED_ENV, "true")
    with pytest.raises(ValueError):
        _validate_gpu_count(0)


# ── manager.py/warm_pool.py stay in sync with the opt-in flag ───────────


def test_manager_py_forces_cold_create_and_wires_gpu_count():
    source = (Path(__file__).resolve().parent.parent / "src" / "boxkite" / "manager.py").read_text()
    assert "gpu_count is not None" in source
    assert "build_sandbox_container_resources(size=size, gpu_count=gpu_count)" in source
