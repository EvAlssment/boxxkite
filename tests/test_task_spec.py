from pathlib import Path

import pytest

from evals.task_spec import TaskSpecError, load_task_spec, parse_task_spec


VALID = {
    "spec_version": 1,
    "name": "sample",
    "setup": ["sh", "setup.sh"],
    "entrypoint": ["python", "agent.py"],
    "verifier": ["python", "verify.py"],
    "timeout_seconds": 60,
    "resources": {"cpu_millis": 500, "memory_mb": 512},
    "reward_range": [0, 1],
}


def test_parse_v1_task_spec():
    spec = parse_task_spec(VALID)
    assert spec.spec_version == 1
    assert spec.setup == ("sh", "setup.sh")
    assert spec.entrypoint == ("python", "agent.py")
    assert spec.verifier == ("python", "verify.py")
    assert spec.timeout_seconds == 60
    assert (spec.resources.cpu_millis, spec.resources.memory_mb) == (500, 512)
    assert spec.reward_range == (0.0, 1.0)


@pytest.mark.parametrize(
    "change",
    [
        {"spec_version": 2},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"setup": []},
        {"entrypoint": ["python", ""]},
        {"resources": {"cpu_millis": 0, "memory_mb": 512}},
        {"reward_range": [1, 0]},
        {"reward_range": [float("nan"), 1]},
        {"reward_range": [0, float("inf")]},
        {"reward_range": [float("-inf"), 1]},
        {"unexpected": "field"},
    ],
)
def test_rejects_invalid_or_unknown_fields(change):
    data = {**VALID, **change}
    with pytest.raises(TaskSpecError):
        parse_task_spec(data)


def test_loads_yaml_file():
    path = Path(__file__).parents[1] / "evals" / "task_spec" / "example.yaml"
    assert load_task_spec(path).name == "sample-task"


def test_yaml_loader_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("spec_version: 1\nspec_version: 1\n", encoding="utf-8")
    with pytest.raises(TaskSpecError):
        load_task_spec(path)


@pytest.mark.parametrize("reward_range", ["[.nan, 1]", "[0, .inf]", "[-.inf, 1]"])
def test_yaml_loader_rejects_non_finite_reward_endpoints(tmp_path, reward_range):
    path = tmp_path / "non-finite.yaml"
    path.write_text(
        "spec_version: 1\n"
        "name: sample\n"
        "setup: [sh, setup.sh]\n"
        "entrypoint: [python, agent.py]\n"
        "verifier: [python, verify.py]\n"
        "timeout_seconds: 60\n"
        "resources: {cpu_millis: 500, memory_mb: 512}\n"
        f"reward_range: {reward_range}\n",
        encoding="utf-8",
    )
    with pytest.raises(TaskSpecError, match="finite"):
        load_task_spec(path)
