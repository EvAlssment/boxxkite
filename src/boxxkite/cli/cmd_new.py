"""`boxxkite new` — create a small, runnable starter project."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

import typer

from .errors import CliError

SUPPORTED_LANGUAGES = ("python",)
SUPPORTED_FRAMEWORKS = ("plain",)
SUPPORTED_USE_CASES = ("code-interpreter",)

_PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

_APP_PY = """\
from __future__ import annotations

import os
import sys

from boxxkite_client import BoxxkiteApiError, BoxxkiteClient, BoxxkiteConnectionError


def main() -> int:
    base_url = os.environ.get("BOXXKITE_BASE_URL")
    api_key = os.environ.get("BOXXKITE_API_KEY")
    if not base_url or not api_key:
        print("Set BOXXKITE_BASE_URL and BOXXKITE_API_KEY first.", file=sys.stderr)
        return 1

    try:
        client = BoxxkiteClient(base_url=base_url, api_key=api_key)
        with client.sandbox(label=__PROJECT_LABEL__) as sandbox:
            result = sandbox.exec("python3 -c 'print(1 + 1)'")
    except BoxxkiteConnectionError as exc:
        print(f"boxxkite connection failed: {exc}", file=sys.stderr)
        return 1
    except BoxxkiteApiError as exc:
        print(f"boxxkite request failed: {exc.message} [{exc.code}]", file=sys.stderr)
        return 1

    print(result["stdout"], end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

_README_MD = """\
# __PROJECT_NAME__

A minimal Python project that creates a boxxkite sandbox and runs one command
through the hosted control-plane API.

This starter uses the `python` / `plain` / `code-interpreter` choices. It is a
small starting point: add your agent framework and tools after the first
successful sandbox call.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with the URL and API key for your control-plane, then load it into
the current shell and run the example:

```bash
set -a
source .env
set +a
python app.py
```

The example prints `2` after creating and then cleaning up its sandbox.

See the [boxxkite examples](https://github.com/EvAlssment/boxxkite/tree/main/examples)
for framework-specific integrations and the [Python client README](https://github.com/EvAlssment/boxxkite/tree/main/sdk-python)
for the complete client surface.
"""

_ENV_EXAMPLE = """\
# Copy this file to .env and replace both values before running app.py.
BOXXKITE_BASE_URL=https://your-control-plane.example.com
BOXXKITE_API_KEY=your-api-key-here
"""

_GITIGNORE = """\
.env
.venv/
__pycache__/
*.py[cod]
"""

_STATIC_FILES = {
    ".env.example": _ENV_EXAMPLE,
    ".gitignore": _GITIGNORE,
    "requirements.txt": "boxxkite-client>=0.2.4,<1\n",
}


def _choice(value: str, *, option: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        joined = ", ".join(choices)
        raise CliError(f"Unsupported {option} {value!r}. Choose one of: {joined}.")
    return value


def _destination(path: Path) -> Path:
    destination = path.expanduser()
    name = destination.name
    if not name or name in {".", ".."} or not _PROJECT_NAME.fullmatch(name):
        raise CliError(
            f"Invalid project directory {str(path)!r}. Use a final directory name made of "
            "letters, numbers, dots, hyphens, or underscores."
        )
    if destination.exists() or destination.is_symlink():
        raise CliError(f"Refusing to overwrite existing path: {destination}")
    if destination.parent.exists() and not destination.parent.is_dir():
        raise CliError(f"Project parent is not a directory: {destination.parent}")
    return destination


def _render_files(project_name: str) -> dict[str, str]:
    label = repr(project_name)
    return {
        "app.py": _APP_PY.replace("__PROJECT_LABEL__", label),
        "README.md": _README_MD.replace("__PROJECT_NAME__", project_name),
        **_STATIC_FILES,
    }


def _write_project(destination: Path, files: dict[str, str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        for relative_path, content in files.items():
            output = staging / relative_path
            output.write_text(content)
        staging.rename(destination)
    except FileExistsError as exc:
        raise CliError(f"Refusing to overwrite existing path: {destination}") from exc
    except OSError as exc:
        raise CliError(f"Could not create project at {destination}: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def new(
    project_dir: Path = typer.Argument(..., help="Directory to create for the new project."),
    language: str = typer.Option(
        "python",
        "--language",
        help="Project language. The initial template supports python.",
    ),
    framework: str = typer.Option(
        "plain",
        "--framework",
        help="Integration style. The initial template supports plain.",
    ),
    use_case: str = typer.Option(
        "code-interpreter",
        "--use-case",
        help="Starter use case. The initial template supports code-interpreter.",
    ),
) -> None:
    """Create a minimal project wired to the boxxkite Python client."""
    _choice(language, option="--language", choices=SUPPORTED_LANGUAGES)
    _choice(framework, option="--framework", choices=SUPPORTED_FRAMEWORKS)
    _choice(use_case, option="--use-case", choices=SUPPORTED_USE_CASES)

    destination = _destination(project_dir)
    _write_project(destination, _render_files(destination.name))

    typer.secho(f"Created boxxkite project at {destination}", fg=typer.colors.GREEN)
    typer.echo("Next steps:")
    typer.echo(f"  cd {destination}")
    typer.echo("  cp .env.example .env")
    typer.echo("  edit .env with your control-plane URL and API key")
    typer.echo("  python app.py")
