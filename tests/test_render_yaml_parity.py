"""Tests for deploy/render.yaml (GitHub issue #218) staying in sync with
control-plane/.env.example and control-plane/Dockerfile -- same drift-guard
discipline as test_pod_template_parity.py, applied to the Render Blueprint
instead of the K8s pod template. Nothing enforced these staying in sync
before; a future edit to either file without the other should fail CI
instead of silently producing a Blueprint that deploys a control plane
which then fails to start (or starts insecurely) on Render.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML_PATH = REPO_ROOT / "deploy" / "render.yaml"
ENV_EXAMPLE_PATH = REPO_ROOT / "control-plane" / ".env.example"
DOCKERFILE_PATH = REPO_ROOT / "control-plane" / "Dockerfile"


def _render_blueprint() -> dict:
    return yaml.safe_load(RENDER_YAML_PATH.read_text())


def _control_plane_service() -> dict:
    doc = _render_blueprint()
    for service in doc["services"]:
        if service["name"] == "boxkite-control-plane":
            return service
    raise AssertionError("No 'boxkite-control-plane' service found in deploy/render.yaml")


def _env_vars_by_key(service: dict) -> dict[str, dict]:
    return {e["key"]: e for e in service["envVars"]}


def _changeme_env_var_names_from_example() -> set[str]:
    """Extract the env var name from every CHANGEME-marked line in
    control-plane/.env.example -- both live (`KEY=value  # CHANGEME`) and
    commented-out (`# KEY=value  # CHANGEME`) forms."""
    names = set()
    for line in ENV_EXAMPLE_PATH.read_text().splitlines():
        if "CHANGEME" not in line:
            continue
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=", line.strip())
        if match:
            names.add(match.group(1))
    return names


def test_every_changeme_env_var_is_addressed_in_render_yaml():
    """Every CHANGEME-marked field in control-plane/.env.example must appear
    as an envVar on the render.yaml control-plane service -- either given a
    real value, auto-generated (generateValue), sourced from the provisioned
    database (fromDatabase), or explicitly deferred to the operator
    (sync: false) -- so a Blueprint deploy can't silently omit a field this
    project's own local-dev config considers required."""
    service = _control_plane_service()
    render_keys = set(_env_vars_by_key(service).keys())
    changeme_keys = _changeme_env_var_names_from_example()
    missing = changeme_keys - render_keys
    assert not missing, (
        f"deploy/render.yaml is missing CHANGEME env var(s) from "
        f"control-plane/.env.example: {sorted(missing)} -- add them (see "
        "the other envVars entries in that file for the generateValue/"
        "fromDatabase/sync:false patterns to follow)."
    )


def test_jwt_secret_is_generated_not_a_literal_placeholder():
    env_vars = _env_vars_by_key(_control_plane_service())
    jwt_secret = env_vars["JWT_SECRET"]
    assert jwt_secret.get("generateValue") is True, (
        "JWT_SECRET must use generateValue: true in deploy/render.yaml -- "
        "a literal placeholder value would ship every Render deploy of "
        "this Blueprint with the same guessable JWT signing secret."
    )
    assert "value" not in jwt_secret


def test_database_url_is_sourced_from_the_provisioned_database():
    env_vars = _env_vars_by_key(_control_plane_service())
    database_url = env_vars["DATABASE_URL"]
    assert "fromDatabase" in database_url, (
        "DATABASE_URL must be sourced via fromDatabase, not a hardcoded "
        "literal, so it actually points at the database this same "
        "Blueprint provisions."
    )
    referenced_db_name = database_url["fromDatabase"]["name"]
    db_names = {db["name"] for db in _render_blueprint()["databases"]}
    assert referenced_db_name in db_names, (
        f"DATABASE_URL references database {referenced_db_name!r}, which "
        f"is not declared under deploy/render.yaml's top-level `databases:` "
        f"(declared: {sorted(db_names)})"
    )


def test_port_env_var_matches_the_dockerfile_s_actual_listening_port():
    """control-plane/Dockerfile's CMD hardcodes uvicorn's --port -- it does
    NOT read config.py's CONTROL_PLANE_PORT setting. render.yaml's PORT
    envVar must match whatever port the Dockerfile actually binds, or
    Render's health check / routing hits a port nothing is listening on."""
    dockerfile_text = DOCKERFILE_PATH.read_text()
    match = re.search(r"--port[\"',\s]+(\d+)", dockerfile_text)
    assert match, "Could not find a --port argument in control-plane/Dockerfile's CMD"
    dockerfile_port = match.group(1)

    env_vars = _env_vars_by_key(_control_plane_service())
    assert env_vars["PORT"]["value"] == dockerfile_port, (
        f"deploy/render.yaml's PORT ({env_vars['PORT']['value']!r}) has "
        f"drifted from control-plane/Dockerfile's actual --port "
        f"({dockerfile_port!r})"
    )


def test_runtime_mode_defaults_to_k8s_matching_the_documented_deploy_path():
    env_vars = _env_vars_by_key(_control_plane_service())
    assert env_vars["RUNTIME_MODE"]["value"] == "k8s"


def test_kubeconfig_and_image_refs_require_manual_operator_input():
    """These have no safe generic default (a real cluster's kubeconfig and
    image registry references) -- must be sync: false, prompting the
    operator, never a guessed/hardcoded value."""
    env_vars = _env_vars_by_key(_control_plane_service())
    for key in ("KUBECONFIG", "SANDBOX_IMAGE", "SIDECAR_IMAGE"):
        assert env_vars[key].get("sync") is False, (
            f"{key} must be sync: false in deploy/render.yaml -- there is "
            "no safe default value to ship"
        )
