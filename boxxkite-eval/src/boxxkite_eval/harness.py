from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Protocol


class SandboxClient(Protocol):
    def create_sandbox(self, *, label: str | None = None) -> dict: ...

    def exec(self, session_id: str, command: str, *, timeout: int | None = None) -> dict: ...

    def destroy_sandbox(self, session_id: str) -> None: ...


@dataclass(frozen=True)
class Task:
    name: str
    agent_command: str
    verifier_command: str
    setup_command: str | None = None
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("task name must not be empty")
        if not self.agent_command.strip() or not self.verifier_command.strip():
            raise ValueError("agent and verifier commands must not be empty")
        if self.setup_command is not None and not self.setup_command.strip():
            raise ValueError("setup command must not be empty when provided")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class TrialResult:
    task_name: str
    passed: bool
    duration_seconds: float
    exit_code: int | None
    failed_phase: str | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class SuiteResult:
    trials: tuple[TrialResult, ...]

    @property
    def passed(self) -> int:
        return sum(trial.passed for trial in self.trials)

    @property
    def failed(self) -> int:
        return len(self.trials) - self.passed


class EvaluationError(RuntimeError):
    pass


def run_task(client: SandboxClient, task: Task, *, trial_number: int = 1) -> TrialResult:
    started = monotonic()
    sandbox = client.create_sandbox(label=f"eval-{task.name}-{trial_number}")
    session_id = sandbox.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise EvaluationError("sandbox client returned no session id")

    try:
        phases = (
            ("setup", task.setup_command),
            ("agent", task.agent_command),
            ("verifier", task.verifier_command),
        )
        for phase, command in phases:
            if command is None:
                continue
            result = client.exec(session_id, command, timeout=task.timeout_seconds)
            exit_code = result.get("exit_code")
            if exit_code != 0:
                return TrialResult(
                    task_name=task.name,
                    passed=False,
                    duration_seconds=monotonic() - started,
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                    failed_phase=phase,
                    stdout=str(result.get("stdout", "")),
                    stderr=str(result.get("stderr", "")),
                )
        return TrialResult(task.name, True, monotonic() - started, 0)
    finally:
        client.destroy_sandbox(session_id)


def run_suite(client: SandboxClient, tasks: list[Task], *, trials_per_task: int = 1) -> SuiteResult:
    if trials_per_task < 1:
        raise ValueError("trials_per_task must be positive")
    results = tuple(
        run_task(client, task, trial_number=trial)
        for task in tasks
        for trial in range(1, trials_per_task + 1)
    )
    return SuiteResult(results)
