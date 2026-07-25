"""Hardening follow-ups from the #67 security review of the declarative
image builder (BOXKITE_IMAGE_BUILDER_ENABLED): a cluster-wide concurrent-
build cap (mirroring BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES) and resource
requests/limits + a wall-clock timeout on the Kaniko build Job -- both
called out as real gaps in the review, independent of whether the feature
itself defaults on or stays opt-in.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import signup_and_get_api_key
from control_plane import db as db_module
from control_plane.config import settings
from control_plane.image_builder import KanikoJobBuildRunner
from control_plane.repository import SandboxImageRepository


@pytest.fixture(autouse=True)
def _enable_image_builder(monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_IMAGE_BUILDER_ENABLED", True)


async def test_count_in_flight_total_counts_only_non_terminal_statuses(client: httpx.AsyncClient):
    async with db_module.get_session_factory()() as db:
        repo = SandboxImageRepository(db)
        common = dict(
            account_id="acct-1",
            label=None,
            base="boxkite-default",
            python_packages=[],
            apt_packages=[],
            cache_key="k",
        )
        await repo.create(image_id="img-queued", status="queued", **common)
        await repo.create(image_id="img-building", status="building", **common)
        await repo.create(image_id="img-scanning", status="scanning", **common)
        await repo.create(image_id="img-completed", status="completed", **common)
        await repo.create(image_id="img-failed", status="failed", **common)
        await repo.create(image_id="img-rejected", status="rejected", **common)

        count = await repo.count_in_flight_total()

    assert count == 3


async def test_in_flight_count_spans_all_accounts(client: httpx.AsyncClient):
    async with db_module.get_session_factory()() as db:
        repo = SandboxImageRepository(db)

        async def _make(image_id: str, account_id: str) -> None:
            await repo.create(
                image_id=image_id,
                account_id=account_id,
                label=None,
                base="boxkite-default",
                python_packages=[],
                apt_packages=[],
                cache_key="k",
                status="building",
            )

        await _make("img-a", "acct-a")
        await _make("img-b", "acct-b")

        count = await repo.count_in_flight_total()

    assert count == 2


async def test_build_request_blocked_when_global_capacity_reached(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_GLOBAL_MAX_CONCURRENT_IMAGE_BUILDS", 0)
    key = await signup_and_get_api_key(client, "images-global-cap@example.com")

    resp = await client.post(
        "/v1/images",
        json={
            "label": "x",
            "base": "boxkite-default",
            "python_packages": ["polars==1.9.0"],
            "apt_packages": [],
            "npm_packages": [],
        },
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "global_build_capacity_reached"


def test_build_job_spec_sets_resource_limits_and_timeout():
    runner = KanikoJobBuildRunner()

    spec = runner.build_job_spec(
        image_id="11111111",
        account_id="acct-1",
        base="boxkite-default",
        python_packages=["polars==1.9.0"],
        apt_packages=[],
    )

    container = spec["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"]["cpu"] == settings.BOXKITE_IMAGE_BUILD_CPU_REQUEST
    assert container["resources"]["requests"]["memory"] == settings.BOXKITE_IMAGE_BUILD_MEMORY_REQUEST
    assert container["resources"]["limits"]["cpu"] == settings.BOXKITE_IMAGE_BUILD_CPU_LIMIT
    assert container["resources"]["limits"]["memory"] == settings.BOXKITE_IMAGE_BUILD_MEMORY_LIMIT
    assert spec["spec"]["activeDeadlineSeconds"] == settings.BOXKITE_IMAGE_BUILD_TIMEOUT_SECONDS
