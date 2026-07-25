"""render_dockerfile / multi-base support -- docs/DECLARATIVE-BUILDER-DESIGN.md.

Covers the Dockerfile-generation seam added for the `boxkite-minimal` base
variant: each pre-approved `base` resolves to its own digest/tag-pinned
image (settings.BOXKITE_BASE_IMAGE_REFS), packages get layered on top in an
isolated RUN, and package specs are re-validated at the templating boundary
even though schemas.py already enforces exact-version pinning upstream.
"""

from __future__ import annotations

import pytest

from control_plane.image_builder import KanikoJobBuildRunner, UnknownBaseError, render_dockerfile


def test_render_dockerfile_uses_configured_base_image_ref():
    dockerfile = render_dockerfile(base="boxkite-default", python_packages=[], apt_packages=[])
    assert dockerfile.startswith("FROM ghcr.io/evalssment/boxkite-sandbox:latest\n")


def test_render_dockerfile_for_minimal_base_uses_its_own_image_ref():
    dockerfile = render_dockerfile(base="boxkite-minimal", python_packages=[], apt_packages=[])
    assert dockerfile.startswith("FROM ghcr.io/evalssment/boxkite-sandbox-minimal:latest\n")


def test_render_dockerfile_for_node_base_uses_its_own_image_ref():
    dockerfile = render_dockerfile(
        base="boxkite-node", python_packages=[], apt_packages=[], npm_packages=["typescript==5.6.0"]
    )
    assert dockerfile.startswith("FROM ghcr.io/evalssment/boxkite-sandbox-node:latest\n")
    assert "npm install -g typescript==5.6.0" in dockerfile


def test_render_dockerfile_for_go_base_uses_its_own_image_ref():
    dockerfile = render_dockerfile(base="boxkite-go", python_packages=[], apt_packages=[])
    assert dockerfile.startswith("FROM ghcr.io/evalssment/boxkite-sandbox-go:latest\n")


def test_render_dockerfile_for_nextjs_base_uses_its_own_image_ref():
    dockerfile = render_dockerfile(
        base="boxkite-nextjs", python_packages=[], apt_packages=[], npm_packages=["typescript==5.6.0"]
    )
    assert dockerfile.startswith("FROM ghcr.io/evalssment/boxkite-sandbox-nextjs:latest\n")
    assert "npm install -g typescript==5.6.0" in dockerfile


def test_render_dockerfile_for_rust_base_uses_its_own_image_ref():
    dockerfile = render_dockerfile(base="boxkite-rust", python_packages=[], apt_packages=[])
    assert dockerfile.startswith("FROM ghcr.io/evalssment/boxkite-sandbox-rust:latest\n")


def test_render_dockerfile_installs_and_removes_pip_in_one_layer():
    dockerfile = render_dockerfile(base="boxkite-default", python_packages=["polars==1.9.0"], apt_packages=[])
    assert "apk add --no-cache py3.11-pip" in dockerfile
    assert "python -m pip install --break-system-packages --no-cache-dir polars==1.9.0" in dockerfile
    assert "apk del py3.11-pip" in dockerfile
    # pip install/remove must be in the SAME RUN instruction, not separate
    # layers -- otherwise the "no package manager in the final image"
    # invariant would only hold at the Dockerfile-instruction level, not the
    # actual image-layer level (a prior layer could still contain pip).
    run_lines = [line for line in dockerfile.splitlines() if line.strip().startswith("RUN")]
    assert len(run_lines) == 1


def test_render_dockerfile_installs_apt_packages():
    dockerfile = render_dockerfile(base="boxkite-default", python_packages=[], apt_packages=["ripgrep==14.1.0-1"])
    # apt_packages is validated with the pip-style "name==version" pattern,
    # but Alpine's `apk` pins versions with a single "=" -- the templated
    # RUN line must use apk's real syntax, not the validation regex's.
    assert "apk add --no-cache ripgrep=14.1.0-1" in dockerfile
    assert "ripgrep==14.1.0-1" not in dockerfile


def test_render_dockerfile_no_packages_has_no_run_instruction():
    dockerfile = render_dockerfile(base="boxkite-default", python_packages=[], apt_packages=[])
    assert "RUN" not in dockerfile


def test_render_dockerfile_reverts_to_sandbox_user():
    dockerfile = render_dockerfile(base="boxkite-default", python_packages=["polars==1.9.0"], apt_packages=[])
    lines = dockerfile.strip().splitlines()
    assert lines[-1] == "USER sandbox"


def test_render_dockerfile_rejects_unpinned_package_even_though_schema_should_have_caught_it():
    with pytest.raises(ValueError, match="not exact-version pinned"):
        render_dockerfile(base="boxkite-default", python_packages=["polars"], apt_packages=[])


def test_render_dockerfile_unknown_base_raises():
    with pytest.raises(UnknownBaseError):
        render_dockerfile(base="not-a-real-base", python_packages=[], apt_packages=[])


def test_render_dockerfile_installs_and_removes_npm_in_one_layer():
    dockerfile = render_dockerfile(
        base="boxkite-minimal", python_packages=[], apt_packages=[], npm_packages=["typescript==5.6.0"]
    )
    assert "apk add --no-cache npm" in dockerfile
    assert "npm install -g typescript==5.6.0" in dockerfile
    assert "apk del npm node-gyp" in dockerfile
    run_lines = [line for line in dockerfile.splitlines() if line.strip().startswith("RUN")]
    assert len(run_lines) == 1


def test_render_dockerfile_installs_scoped_npm_package():
    dockerfile = render_dockerfile(
        base="boxkite-minimal",
        python_packages=[],
        apt_packages=[],
        npm_packages=["@anthropic-ai/claude-code==2.0.1"],
    )
    assert "npm install -g @anthropic-ai/claude-code==2.0.1" in dockerfile


def test_render_dockerfile_rejects_unpinned_npm_package():
    with pytest.raises(ValueError, match="not exact-version pinned"):
        render_dockerfile(base="boxkite-minimal", python_packages=[], apt_packages=[], npm_packages=["typescript"])


def test_render_dockerfile_no_npm_packages_omits_npm_steps():
    dockerfile = render_dockerfile(base="boxkite-minimal", python_packages=[], apt_packages=[])
    assert "npm" not in dockerfile


def test_build_job_spec_embeds_generated_dockerfile_for_requested_base():
    runner = KanikoJobBuildRunner()
    spec = runner.build_job_spec(
        image_id="img_abcdef123456",
        account_id="acct_1",
        base="boxkite-minimal",
        python_packages=["duckdb==1.1.3"],
        apt_packages=[],
    )
    dockerfile = spec["_boxkite_generated_dockerfile"]
    assert dockerfile.startswith("FROM ghcr.io/evalssment/boxkite-sandbox-minimal:latest\n")
    assert "duckdb==1.1.3" in dockerfile
