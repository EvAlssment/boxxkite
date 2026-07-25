"""Tests for SandboxManager.browser_navigate/browser_exec/browser_screenshot/
browser_close.

Mirrors tests/test_manager_node_interpreter.py's pattern: resolve the
session, get the pod's HTTP client, POST to the sidecar, return the parsed
JSON body.
"""

import pytest

from boxkite.manager import SandboxManager

pytestmark = pytest.mark.pr


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    def __init__(self, payload: dict):
        self._payload = payload
        self.post_calls: list[tuple[str, dict]] = []

    async def post(self, path: str, json: dict | None = None) -> _FakeResponse:
        self.post_calls.append((path, json or {}))
        return _FakeResponse(self._payload)


def _manager_with_stubbed_sidecar(payload: dict) -> tuple[SandboxManager, _FakeHttpClient]:
    manager = SandboxManager()
    fake_client = _FakeHttpClient(payload)

    async def fake_resolve_session(session_id: str):
        return ("sandbox-pod", "10.8.0.80")

    manager._resolve_session = fake_resolve_session
    manager._get_http_client = lambda *args, **kwargs: fake_client
    return manager, fake_client


async def test_browser_navigate_posts_url_and_options_to_sidecar():
    manager, fake_client = _manager_with_stubbed_sidecar(
        {"title": "Example", "url": "https://example.com", "status": 200, "error": None}
    )

    result = await manager.browser_navigate(
        session_id="session-1",
        url="https://example.com",
        wait_until="domcontentloaded",
        timeout_seconds=15,
    )

    assert result == {
        "title": "Example",
        "url": "https://example.com",
        "status": 200,
        "error": None,
    }
    assert fake_client.post_calls == [
        (
            "/browser/navigate",
            {"url": "https://example.com", "wait_until": "domcontentloaded", "timeout_seconds": 15},
        )
    ]


async def test_browser_navigate_defaults_wait_until_and_timeout():
    manager, fake_client = _manager_with_stubbed_sidecar(
        {"title": None, "url": None, "status": None, "error": None}
    )

    await manager.browser_navigate(session_id="session-1", url="https://example.com")

    assert fake_client.post_calls == [
        (
            "/browser/navigate",
            {"url": "https://example.com", "wait_until": "load", "timeout_seconds": 30},
        )
    ]


async def test_browser_exec_posts_script_and_timeout_to_sidecar():
    manager, fake_client = _manager_with_stubbed_sidecar({"result": 42, "error": None})

    result = await manager.browser_exec(session_id="session-1", script="21 * 2", timeout_seconds=5)

    assert result == {"result": 42, "error": None}
    assert fake_client.post_calls == [
        ("/browser/exec", {"script": "21 * 2", "timeout_seconds": 5})
    ]


async def test_browser_exec_defaults_timeout_to_10_seconds():
    manager, fake_client = _manager_with_stubbed_sidecar({"result": None, "error": None})

    await manager.browser_exec(session_id="session-1", script="1")

    assert fake_client.post_calls == [("/browser/exec", {"script": "1", "timeout_seconds": 10})]


async def test_browser_screenshot_posts_full_page_flag_to_sidecar():
    manager, fake_client = _manager_with_stubbed_sidecar(
        {"image_base64": "aGVsbG8=", "error": None}
    )

    result = await manager.browser_screenshot(session_id="session-1", full_page=True)

    assert result == {"image_base64": "aGVsbG8=", "error": None}
    assert fake_client.post_calls == [("/browser/screenshot", {"full_page": True})]


async def test_browser_screenshot_defaults_full_page_to_false():
    manager, fake_client = _manager_with_stubbed_sidecar({"image_base64": None, "error": None})

    await manager.browser_screenshot(session_id="session-1")

    assert fake_client.post_calls == [("/browser/screenshot", {"full_page": False})]


async def test_browser_close_posts_to_sidecar_with_no_body():
    manager, fake_client = _manager_with_stubbed_sidecar({"status": "closed"})

    result = await manager.browser_close(session_id="session-1")

    assert result == {"status": "closed"}
    assert fake_client.post_calls == [("/browser/close", {})]
