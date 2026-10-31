#!/usr/bin/env python3
"""Validate and run the starter boxxkite-bench task dataset."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

DATASET = Path(__file__).parent / "boxxkite-bench-v1" / "tasks.json"
FORMAT = "boxxkite-bench-v1"
TASK_KEYS = {"id", "title", "category", "prompt", "setup_files", "checks"}


class DatasetError(ValueError):
    pass


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in ("", ".", "..") for part in path.parts)


def load_dataset(path: Path = DATASET) -> list[dict]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(str(exc)) from exc
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise DatasetError(f"dataset format must be {FORMAT!r}")
    if document.get("license") != "Apache-2.0":
        raise DatasetError("dataset license must be Apache-2.0")
    tasks = document.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise DatasetError("tasks must be a non-empty array")
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) != TASK_KEYS:
            raise DatasetError(f"each task must have exactly: {', '.join(sorted(TASK_KEYS))}")
        task_id = task["id"]
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", task_id):
            raise DatasetError("task id must be lowercase kebab-case")
        if task_id in seen:
            raise DatasetError(f"duplicate task id: {task_id}")
        seen.add(task_id)
        for key in ("title", "category", "prompt"):
            if not isinstance(task[key], str) or not task[key].strip():
                raise DatasetError(f"{task_id}: {key} must be non-empty text")
        files = task["setup_files"]
        if not isinstance(files, dict) or not files:
            raise DatasetError(f"{task_id}: setup_files must be a non-empty object")
        if any(not _safe_relative_path(name) or not isinstance(content, str) for name, content in files.items()):
            raise DatasetError(f"{task_id}: setup_files needs safe relative paths and text contents")
        checks = task["checks"]
        if not isinstance(checks, list) or not checks:
            raise DatasetError(f"{task_id}: checks must be a non-empty array")
        for check in checks:
            if (not isinstance(check, dict) or set(check) != {"type", "path", "text"}
                    or check["type"] != "file_contains" or not _safe_relative_path(check["path"])
                    or not isinstance(check["text"], str) or not check["text"]):
                raise DatasetError(f"{task_id}: checks support file_contains with safe path and non-empty text")
    return tasks


def _run_task(task: dict, command: list[str], timeout: int) -> bool:
    with tempfile.TemporaryDirectory(prefix=f"boxxkite-{task['id']}-") as workspace:
        root = Path(workspace)
        for name, content in task["setup_files"].items():
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        try:
            result = subprocess.run(command, cwd=root, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"{task['id']}: command failed: {exc}", file=sys.stderr)
            return False
        passed = result.returncode == 0
        if not passed:
            print(f"{task['id']}: command exited {result.returncode}", file=sys.stderr)
        for check in task["checks"]:
            candidate = root / check["path"]
            try:
                candidate.resolve().relative_to(root.resolve())
                matched = candidate.is_file() and check["text"] in candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError):
                matched = False
            if not matched:
                print(f"{task['id']}: check failed: {check['path']} contains required text", file=sys.stderr)
                passed = False
        print(f"{task['id']}: {'PASS' if passed else 'FAIL'}")
        return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("validate", help="validate the bundled dataset")
    run_parser = subparsers.add_parser("run", help="run a command against task workspaces")
    run_parser.add_argument("--task", help="run only this task id")
    run_parser.add_argument("--timeout", type=int, default=300, help="per-task command timeout in seconds")
    run_parser.add_argument("agent_command", nargs=argparse.REMAINDER, help="command after --, executed in a temporary task workspace")
    args = parser.parse_args()
    try:
        tasks = load_dataset()
    except DatasetError as exc:
        print(f"invalid dataset: {exc}", file=sys.stderr)
        return 2
    if args.action == "validate":
        print(f"valid {FORMAT}: {len(tasks)} tasks")
        return 0
    selected = [task for task in tasks if args.task is None or task["id"] == args.task]
    if not selected:
        print(f"unknown task id: {args.task}", file=sys.stderr)
        return 2
    command = args.agent_command[1:] if args.agent_command[:1] == ["--"] else args.agent_command
    if not command or args.timeout < 1:
        run_parser.error("run requires a command after -- and a positive --timeout")
    return 0 if all(_run_task(task, command, args.timeout) for task in selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
