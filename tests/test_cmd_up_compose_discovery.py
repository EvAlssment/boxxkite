"""`boxkite up` auto-discovers deploy/docker-compose.yml by walking up from
cwd -- an unrelated project that happens to have its own file at that same
relative path would otherwise get silently `docker compose up --build`'d.
These tests cover the content-sanity guard on auto-discovered files."""

from __future__ import annotations

from pathlib import Path

import pytest

from boxkite.cli.cmd_up import _find_compose_file
from boxkite.cli.errors import CliError

_REAL_COMPOSE_CONTENT = """\
services:
  sidecar:
    environment:
      SIDECAR_AUTH_TOKEN: "${SIDECAR_AUTH_TOKEN:?set it}"
"""

_UNRELATED_COMPOSE_CONTENT = """\
services:
  web:
    image: nginx
"""


def test_finds_a_real_boxkite_compose_file_by_walking_up(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    compose_path = deploy_dir / "docker-compose.yml"
    compose_path.write_text(_REAL_COMPOSE_CONTENT)

    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    found = _find_compose_file(None, search_root=nested)
    assert found == compose_path


def test_refuses_an_unrelated_compose_file_found_by_coincidence(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    compose_path = deploy_dir / "docker-compose.yml"
    compose_path.write_text(_UNRELATED_COMPOSE_CONTENT)

    with pytest.raises(CliError, match="doesn't look like a boxkite compose file"):
        _find_compose_file(None, search_root=tmp_path)


def test_explicit_compose_file_bypasses_the_content_check(tmp_path: Path):
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(_UNRELATED_COMPOSE_CONTENT)

    found = _find_compose_file(compose_path, search_root=tmp_path)
    assert found == compose_path


def test_raises_when_no_compose_file_exists_anywhere_up_the_tree(tmp_path: Path):
    nested = tmp_path / "x" / "y"
    nested.mkdir(parents=True)

    with pytest.raises(CliError, match="Could not find deploy/docker-compose.yml"):
        _find_compose_file(None, search_root=nested)
