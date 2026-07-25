"""Declarative-builder API -- docs/DECLARATIVE-BUILDER-DESIGN.md.

Mirrors test_snapshots.py's structure: happy-path build/get/list/delete
first, then validation (pinned-version-only packages), then the
cross-tenant 404-not-403 guarantees and the "never silently fall back to
the default image" guarantee on sandbox create, which are the highest-
severity risks called out in the design doc's security section.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from conftest import FakeSandboxManager, signup_and_get_api_key
from control_plane.config import settings


@pytest.fixture(autouse=True)
def _enable_image_builder(monkeypatch):
    # Off by default (opt-in feature) -- every test in this file explicitly
    # turns it on, so a test that forgets this fixture would get a 404 and
    # fail loudly rather than silently exercising a disabled feature.
    monkeypatch.setattr(settings, "BOXKITE_IMAGE_BUILDER_ENABLED", True)


async def _build_image(
    client: httpx.AsyncClient,
    key: str,
    *,
    label: str = "data-eng",
    base: str = "boxkite-default",
    python_packages: list[str] | None = None,
    apt_packages: list[str] | None = None,
    npm_packages: list[str] | None = None,
) -> dict:
    resp = await client.post(
        "/v1/images",
        json={
            "label": label,
            "base": base,
            "python_packages": ["polars==1.9.0"] if python_packages is None else python_packages,
            "apt_packages": apt_packages or [],
            "npm_packages": npm_packages or [],
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _wait_for_status(client: httpx.AsyncClient, key: str, image_id: str, *, timeout: float = 2.0) -> dict:
    """The build dispatch runs as a detached asyncio task (see
    routers/images.py) -- poll GET /v1/images/{id} the same way a real
    caller would, rather than reaching into internals."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        resp = await client.get(f"/v1/images/{image_id}", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in {"completed", "failed", "rejected"}:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"image {image_id} never reached a terminal status in time")


async def test_build_disabled_by_default(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_IMAGE_BUILDER_ENABLED", False)
    key = await signup_and_get_api_key(client, "images-disabled@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "boxkite-default", "python_packages": ["polars==1.9.0"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


async def test_build_request_is_queued_then_completes(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-create@example.com")
    accepted = await _build_image(client, key)
    assert accepted["status"] == "queued"
    assert accepted["label"] == "data-eng"

    final = await _wait_for_status(client, key, accepted["id"])
    assert final["status"] == "completed"
    assert final["digest"] is not None and final["digest"].startswith("sha256:")
    # Immutable-digest requirement (design doc section 5): the registry_ref
    # must embed the digest, never be a bare tag.
    assert final["registry_ref"].endswith(f"@{final['digest']}")
    assert final["scan_result"]["policy"]


async def test_npm_package_build_is_queued_then_completes(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-npm@example.com")
    accepted = await _build_image(
        client,
        key,
        base="boxkite-minimal",
        python_packages=[],
        npm_packages=["@anthropic-ai/claude-code==2.0.1"],
    )
    assert accepted["status"] == "queued"

    final = await _wait_for_status(client, key, accepted["id"])
    assert final["status"] == "completed"
    assert final["npm_packages"] == ["@anthropic-ai/claude-code==2.0.1"]


async def test_node_base_build_is_queued_then_completes(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-node-base@example.com")
    accepted = await _build_image(
        client, key, base="boxkite-node", python_packages=[], npm_packages=["typescript==5.6.0"]
    )
    assert accepted["status"] == "queued"

    final = await _wait_for_status(client, key, accepted["id"])
    assert final["status"] == "completed"
    assert final["base"] == "boxkite-node"


async def test_node_base_rejects_python_packages_with_422(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-node-python@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "boxkite-node", "python_packages": ["polars==1.9.0"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_nextjs_base_build_is_queued_then_completes(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-nextjs-base@example.com")
    accepted = await _build_image(
        client, key, base="boxkite-nextjs", python_packages=[], npm_packages=["typescript==5.6.0"]
    )
    assert accepted["status"] == "queued"

    final = await _wait_for_status(client, key, accepted["id"])
    assert final["status"] == "completed"
    assert final["base"] == "boxkite-nextjs"
    assert final["npm_packages"] == ["typescript==5.6.0"]


async def test_nextjs_base_rejects_python_packages_with_422(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-nextjs-python@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "boxkite-nextjs", "python_packages": ["polars==1.9.0"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_go_base_build_is_queued_then_completes(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-go-base@example.com")
    accepted = await _build_image(
        client, key, base="boxkite-go", python_packages=[], apt_packages=["ripgrep==14.1.0-1"]
    )
    assert accepted["status"] == "queued"

    final = await _wait_for_status(client, key, accepted["id"])
    assert final["status"] == "completed"
    assert final["base"] == "boxkite-go"


async def test_go_base_rejects_python_packages_with_422(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-go-python@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "boxkite-go", "python_packages": ["polars==1.9.0"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_go_base_rejects_npm_packages_with_422(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-go-npm@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "boxkite-go", "python_packages": [], "npm_packages": ["typescript==5.6.0"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_rust_base_build_is_queued_then_completes(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-rust-base@example.com")
    accepted = await _build_image(
        client, key, base="boxkite-rust", python_packages=[], apt_packages=["ripgrep==14.1.0-1"]
    )
    assert accepted["status"] == "queued"

    final = await _wait_for_status(client, key, accepted["id"])
    assert final["status"] == "completed"
    assert final["base"] == "boxkite-rust"


async def test_rust_base_rejects_python_packages_with_422(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-rust-python@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "boxkite-rust", "python_packages": ["polars==1.9.0"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_rust_base_rejects_npm_packages_with_422(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-rust-npm@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "boxkite-rust", "python_packages": [], "npm_packages": ["typescript==5.6.0"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_unpinned_npm_package_is_rejected_with_400(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-npm-unpinned@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "boxkite-minimal", "python_packages": [], "npm_packages": ["typescript"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_unpinned_python_package_is_rejected_with_400(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-unpinned@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "boxkite-default", "python_packages": ["polars"], "apt_packages": []},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_range_pinned_package_is_rejected_with_400(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-ranged@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "boxkite-default", "python_packages": ["polars>=1.9.0"], "apt_packages": []},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_non_default_base_is_rejected(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-badbase@example.com")
    resp = await client.post(
        "/v1/images",
        json={"base": "arbitrary-registry/whatever", "python_packages": ["polars==1.9.0"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_boxkite_minimal_base_is_accepted_and_builds(client: httpx.AsyncClient):
    """boxkite-minimal is a second pre-approved base (still an enum value,
    never a free-form image reference) for callers who want a lean base
    with none of boxkite-default's preinstalled data-science/document/
    browser stack."""
    key = await signup_and_get_api_key(client, "images-minimal@example.com")
    accepted = await _build_image(client, key, base="boxkite-minimal", python_packages=["duckdb==1.1.3"])
    final = await _wait_for_status(client, key, accepted["id"])
    assert final["status"] == "completed"
    assert final["digest"] is not None


async def test_build_that_fails_scan_gate_is_rejected_not_completed(client: httpx.AsyncClient):
    """FakeImageBuildRunner deterministically fails the scan gate for any
    package containing "malware" -- exercises the "a build that fails its
    scan must be `rejected`, never silently promoted to `completed`" path
    (design doc section 3) without needing a real scanner."""
    key = await signup_and_get_api_key(client, "images-rejected@example.com")
    accepted = await _build_image(client, key, python_packages=["totally-not-malware==1.0.0"])
    final = await _wait_for_status(client, key, accepted["id"])
    assert final["status"] == "rejected"
    assert final["digest"] is None
    assert final["failure_reason"]


async def test_build_cache_hit_reuses_digest_within_window(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "images-cache@example.com")
    first = await _build_image(client, key, label="first")
    first_final = await _wait_for_status(client, key, first["id"])

    second = await _build_image(client, key, label="second")
    # A cache hit resolves synchronously in the request itself (see
    # routers/images.py) -- no need to poll.
    assert second["status"] == "completed"
    second_get = await client.get(f"/v1/images/{second['id']}", headers={"Authorization": f"Bearer {key}"})
    assert second_get.json()["digest"] == first_final["digest"]
    assert second["id"] != first["id"]


async def test_image_build_limit_returns_429(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MAX_IMAGES_PER_ACCOUNT", 1)
    key = await signup_and_get_api_key(client, "images-limit@example.com")
    await _build_image(client, key, label="one", python_packages=["polars==1.9.0"])
    resp = await client.post(
        "/v1/images",
        json={"label": "two", "base": "boxkite-default", "python_packages": ["duckdb==1.1.3"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "image_build_limit_reached"


async def test_account_cannot_get_another_accounts_image(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "images-cross-a@example.com")
    key_b = await signup_and_get_api_key(client, "images-cross-b@example.com")
    accepted = await _build_image(client, key_a)

    resp = await client.get(f"/v1/images/{accepted['id']}", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 404


async def test_account_cannot_delete_another_accounts_image(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "images-del-a@example.com")
    key_b = await signup_and_get_api_key(client, "images-del-b@example.com")
    accepted = await _build_image(client, key_a)

    resp = await client.delete(f"/v1/images/{accepted['id']}", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 404


async def test_list_images_only_returns_own_account(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "images-list-a@example.com")
    key_b = await signup_and_get_api_key(client, "images-list-b@example.com")
    await _build_image(client, key_a)

    resp = await client.get("/v1/images", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_sandbox_with_completed_image_id_passes_registry_ref_to_manager(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "images-sandbox-ok@example.com")
    accepted = await _build_image(client, key)
    final = await _wait_for_status(client, key, accepted["id"])

    resp = await client.post(
        "/v1/sandboxes",
        json={"image_id": accepted["id"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    assert fake_manager.created[session_id]["image_ref"] == final["registry_ref"]


async def test_create_sandbox_with_queued_image_id_404s_never_falls_back_to_default(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    """The design doc is explicit: creating a sandbox against a
    still-building image must fail closed, never silently substitute the
    default image -- a silent fallback would be a security footgun (a
    caller believing they're on their reviewed package set while actually
    running the shared default)."""
    key = await signup_and_get_api_key(client, "images-sandbox-notready@example.com")

    # Freeze the fake runner mid-build by monkeypatching dispatch to a
    # never-completing coroutine, so the image stays "queued".
    import control_plane.routers.images as images_router

    async def _never_finishes(**_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(images_router, "dispatch_build", _never_finishes)

    accepted = await _build_image(client, key)
    assert accepted["status"] == "queued"

    resp = await client.post(
        "/v1/sandboxes",
        json={"image_id": accepted["id"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert not fake_manager.created


async def test_create_sandbox_with_foreign_image_id_404s(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "images-sandbox-foreign-a@example.com")
    key_b = await signup_and_get_api_key(client, "images-sandbox-foreign-b@example.com")
    accepted = await _build_image(client, key_a)
    await _wait_for_status(client, key_a, accepted["id"])

    resp = await client.post(
        "/v1/sandboxes",
        json={"image_id": accepted["id"]},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404


async def test_create_sandbox_without_image_id_behaves_exactly_as_before(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "images-sandbox-default@example.com")
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    assert fake_manager.created[session_id]["image_ref"] is None
