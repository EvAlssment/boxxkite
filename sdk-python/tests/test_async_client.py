"""AsyncBoxkiteClient tests -- mirrors test_client.py's coverage for the
async variant. Wrapped in asyncio.run() from plain sync test functions so
no pytest-asyncio/anyio plugin dependency is needed."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from boxkite_client import AsyncBoxkiteClient, BoxkiteApiError


def _client_with(handler) -> AsyncBoxkiteClient:
    return AsyncBoxkiteClient(
        base_url="https://cp.example.com",
        api_key="bxk_live_test",
        transport=httpx.MockTransport(handler),
    )


def test_rejects_plain_http_to_a_remote_host():
    with pytest.raises(ValueError, match="cleartext"):
        AsyncBoxkiteClient(base_url="http://cp.example.com", api_key="bxk_live_test")


def test_allows_http_localhost_for_local_dev():
    client = AsyncBoxkiteClient(base_url="http://localhost:8090", api_key="bxk_live_test")
    assert client is not None


def test_async_exec():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/exec"
        return httpx.Response(200, json={"exit_code": 0, "stdout": "hi\n", "stderr": ""})

    async def run():
        client = _client_with(handler)
        result = await client.exec("sess-1", "echo hi")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["exit_code"] == 0


def test_async_request_password_reset_posts_email():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/auth/password-reset/request"
        assert json.loads(request.content) == {"email": "user@example.com"}
        return httpx.Response(
            200,
            json={"message": "If an account with that email exists, a password reset link has been sent."},
        )

    async def run():
        client = _client_with(handler)
        result = await client.request_password_reset("user@example.com")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["message"].startswith("If an account with that email exists")


def test_async_confirm_password_reset_posts_token_and_new_password():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/auth/password-reset/confirm"
        assert json.loads(request.content) == {"token": "reset-tok", "new_password": "new-hunter2"}
        return httpx.Response(200, json={"message": "Password has been reset. Please log in with your new password."})

    async def run():
        client = _client_with(handler)
        result = await client.confirm_password_reset("reset-tok", "new-hunter2")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["message"].startswith("Password has been reset")


def test_async_confirm_password_reset_raises_on_invalid_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "invalid_or_expired_token", "message": "This password reset link is invalid or has expired."}},
        )

    async def run():
        client = _client_with(handler)
        try:
            await client.confirm_password_reset("bad-tok", "new-hunter2")
        finally:
            await client.aclose()

    with pytest.raises(BoxkiteApiError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code == "invalid_or_expired_token"


def test_async_verify_email_posts_token():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/auth/verify-email"
        assert json.loads(request.content) == {"token": "verify-tok"}
        return httpx.Response(200, json={"message": "Email verified."})

    async def run():
        client = _client_with(handler)
        result = await client.verify_email("verify-tok")
        await client.aclose()
        return result

    assert asyncio.run(run()) == {"message": "Email verified."}


def test_async_resend_verification_overrides_authorization_with_access_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/auth/resend-verification"
        assert request.headers["Authorization"] == "Bearer dashboard-jwt-123"
        return httpx.Response(200, json={"message": "Verification email sent."})

    async def run():
        client = _client_with(handler)
        result = await client.resend_verification("dashboard-jwt-123")
        await client.aclose()
        return result

    assert asyncio.run(run()) == {"message": "Verification email sent."}


def test_async_refresh_token_posts_refresh_token_and_returns_new_pair():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/auth/refresh"
        assert json.loads(request.content) == {"refresh_token": "old-refresh"}
        return httpx.Response(
            200,
            json={
                "access_token": "new-jwt",
                "token_type": "bearer",
                "expires_in": 3600,
                "refresh_token": "new-refresh",
                "account": {"id": "acct-1", "email": "a@example.com", "created_at": "2026-01-01T00:00:00Z"},
            },
        )

    async def run():
        client = _client_with(handler)
        result = await client.refresh_token("old-refresh")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["access_token"] == "new-jwt"
    assert result["refresh_token"] == "new-refresh"


def test_async_logout_posts_refresh_token_and_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/auth/logout"
        assert json.loads(request.content) == {"refresh_token": "old-refresh"}
        return httpx.Response(204)

    async def run():
        client = _client_with(handler)
        result = await client.logout("old-refresh")
        await client.aclose()
        return result

    assert asyncio.run(run()) is None


def test_async_http_request():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/http-request"
        import json

        payload = json.loads(request.content)
        assert payload["headers"] == {"Authorization": "Bearer {{secret:prod-stripe}}"}
        return httpx.Response(
            200,
            json={"status_code": 200, "headers": {}, "body": "ok", "truncated": False},
        )

    async def run():
        client = _client_with(handler)
        result = await client.http_request(
            "sess-1",
            "POST",
            "https://api.example.com/",
            headers={"Authorization": "Bearer {{secret:prod-stripe}}"},
        )
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["status_code"] == 200


def test_async_lsp_start():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/sandboxes/sess-1/lsp/start"
        assert json.loads(request.content) == {"language": "python"}
        return httpx.Response(200, json={"lsp_id": "lsp-1"})

    async def run():
        client = _client_with(handler)
        result = await client.lsp_start("sess-1", "python")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["lsp_id"] == "lsp-1"


def test_async_lsp_open():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/open"
        assert json.loads(request.content) == {"path": "main.py", "content": "x = 1\n"}
        return httpx.Response(200, json={"status": "ok"})

    async def run():
        client = _client_with(handler)
        result = await client.lsp_open("sess-1", "lsp-1", "main.py", "x = 1\n")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["status"] == "ok"


def test_async_lsp_completion():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/completion"
        assert json.loads(request.content) == {"path": "main.py", "line": 3, "character": 5}
        return httpx.Response(200, json={"items": []})

    async def run():
        client = _client_with(handler)
        result = await client.lsp_completion("sess-1", "lsp-1", "main.py", 3, 5)
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["items"] == []


def test_async_lsp_stop():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/stop"
        return httpx.Response(200, json={"status": "ok"})

    async def run():
        client = _client_with(handler)
        result = await client.lsp_stop("sess-1", "lsp-1")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["status"] == "ok"


def test_async_sandbox_session_wraps_lsp_methods():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/v1/sandboxes/sess-1/lsp/start":
            calls.append("start")
            return httpx.Response(200, json={"lsp_id": "lsp-1"})
        if request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/open":
            calls.append("open")
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/completion":
            calls.append("completion")
            return httpx.Response(200, json={"items": []})
        if request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/stop":
            calls.append("stop")
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    async def run():
        client = _client_with(handler)
        async with client.sandbox() as sb:
            started = await sb.lsp_start("python")
            await sb.lsp_open(started["lsp_id"], "main.py", "x = 1")
            await sb.lsp_completion(started["lsp_id"], "main.py", 0, 0)
            await sb.lsp_stop(started["lsp_id"])
        await client.aclose()

    asyncio.run(run())
    assert calls == ["start", "open", "completion", "stop"]


def test_async_create_sandbox_sends_secret_names():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"secret_names": ["s1"]}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    async def run():
        client = _client_with(handler)
        result = await client.create_sandbox(secret_names=["s1"])
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "sess-1"


def test_async_create_sandbox_sends_image_id():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"image_id": "img-1"}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    async def run():
        client = _client_with(handler)
        result = await client.create_sandbox(image_id="img-1")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "sess-1"


def test_async_create_sandbox_sends_mcp_connection_names():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"mcp_connection_names": ["team-slack"]}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    async def run():
        client = _client_with(handler)
        result = await client.create_sandbox(mcp_connection_names=["team-slack"])
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "sess-1"


def test_async_create_image_sends_pinned_packages():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/images"
        assert json.loads(request.content) == {
            "label": "demo",
            "base": "boxkite-minimal",
            "python_packages": ["requests==2.32.3"],
            "apt_packages": ["curl==8.5.0-2ubuntu10.1"],
        }
        return httpx.Response(202, json={"id": "img-1", "label": "demo", "status": "queued", "created_at": "now"})

    async def run():
        client = _client_with(handler)
        result = await client.create_image(
            label="demo",
            base="boxkite-minimal",
            python_packages=["requests==2.32.3"],
            apt_packages=["curl==8.5.0-2ubuntu10.1"],
        )
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "img-1"
    assert result["status"] == "queued"


def test_async_create_image_defaults_base_when_omitted():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"base": "boxkite-default"}
        return httpx.Response(202, json={"id": "img-2", "label": None, "status": "queued", "created_at": "now"})

    async def run():
        client = _client_with(handler)
        result = await client.create_image()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "img-2"


def test_async_get_image_returns_parsed_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/images/img-1"
        return httpx.Response(200, json={"id": "img-1", "status": "completed", "digest": "sha256:abc"})

    async def run():
        client = _client_with(handler)
        result = await client.get_image("img-1")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["status"] == "completed"


def test_async_list_images_returns_empty_list_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/images"
        return httpx.Response(200, json=[])

    async def run():
        client = _client_with(handler)
        result = await client.list_images()
        await client.aclose()
        return result

    assert asyncio.run(run()) == []


def test_async_list_images_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "img-1"}, {"id": "img-2"}])

    async def run():
        client = _client_with(handler)
        result = await client.list_images()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert [img["id"] for img in result] == ["img-1", "img-2"]


def test_async_delete_image_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/images/img-1"
        return httpx.Response(204)

    async def run():
        client = _client_with(handler)
        result = await client.delete_image("img-1")
        await client.aclose()
        return result

    assert asyncio.run(run()) is None


def test_async_create_image_sends_npm_packages():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {
            "base": "boxkite-node",
            "npm_packages": ["typescript==5.6.0"],
        }
        return httpx.Response(202, json={"id": "img-3", "label": None, "status": "queued", "created_at": "now"})

    async def run():
        client = _client_with(handler)
        result = await client.create_image(base="boxkite-node", npm_packages=["typescript==5.6.0"])
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "img-3"


def test_async_create_image_omits_npm_packages_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert "npm_packages" not in body
        return httpx.Response(202, json={"id": "img-4", "label": None, "status": "queued", "created_at": "now"})

    async def run():
        client = _client_with(handler)
        result = await client.create_image()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "img-4"


def test_async_create_sandbox_sends_volume_mounts():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"volume_mounts": {"vol-1": "/data"}}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    async def run():
        client = _client_with(handler)
        result = await client.create_sandbox(volume_mounts={"vol-1": "/data"})
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "sess-1"


def test_async_create_sandbox_omits_volume_mounts_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert "volume_mounts" not in body
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    async def run():
        client = _client_with(handler)
        result = await client.create_sandbox()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "sess-1"


def test_async_create_sandbox_sends_gpu_count():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"gpu_count": 2}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    async def run():
        client = _client_with(handler)
        result = await client.create_sandbox(gpu_count=2)
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "sess-1"


def test_async_create_sandbox_omits_gpu_count_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert "gpu_count" not in body
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    async def run():
        client = _client_with(handler)
        result = await client.create_sandbox()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "sess-1"


def test_async_create_volume_sends_label_and_size():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/volumes"
        assert json.loads(request.content) == {"size_gb": 10.0, "label": "demo"}
        return httpx.Response(
            202,
            json={"id": "vol-1", "label": "demo", "size_gb": 10.0, "status": "queued", "created_at": "now"},
        )

    async def run():
        client = _client_with(handler)
        result = await client.create_volume(label="demo", size_gb=10.0)
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "vol-1"
    assert result["status"] == "queued"


def test_async_create_volume_omits_label_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"size_gb": 5.0}
        return httpx.Response(
            202,
            json={"id": "vol-2", "label": None, "size_gb": 5.0, "status": "queued", "created_at": "now"},
        )

    async def run():
        client = _client_with(handler)
        result = await client.create_volume(size_gb=5.0)
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "vol-2"


def test_async_get_volume_returns_parsed_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/volumes/vol-1"
        return httpx.Response(200, json={"id": "vol-1", "status": "ready", "size_gb": 10.0})

    async def run():
        client = _client_with(handler)
        result = await client.get_volume("vol-1")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["status"] == "ready"


def test_async_list_volumes_returns_empty_list_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/volumes"
        return httpx.Response(200, json=[])

    async def run():
        client = _client_with(handler)
        result = await client.list_volumes()
        await client.aclose()
        return result

    assert asyncio.run(run()) == []


def test_async_list_volumes_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "vol-1"}, {"id": "vol-2"}])

    async def run():
        client = _client_with(handler)
        result = await client.list_volumes()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert [vol["id"] for vol in result] == ["vol-1", "vol-2"]


def test_async_delete_volume_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/volumes/vol-1"
        return httpx.Response(204)

    async def run():
        client = _client_with(handler)
        result = await client.delete_volume("vol-1")
        await client.aclose()
        return result

    assert asyncio.run(run()) is None


def test_async_create_webhook_sends_url_and_event_types():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/webhooks"
        assert json.loads(request.content) == {
            "url": "https://example.com/hook",
            "event_types": ["sandbox.created"],
        }
        return httpx.Response(
            201,
            json={
                "id": "wh-1",
                "url": "https://example.com/hook",
                "event_types": ["sandbox.created"],
                "description": None,
                "is_active": True,
                "created_at": "now",
                "secret": "whsec_abc123",
            },
        )

    async def run():
        client = _client_with(handler)
        result = await client.create_webhook(url="https://example.com/hook", event_types=["sandbox.created"])
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "wh-1"
    assert result["secret"] == "whsec_abc123"


def test_async_create_webhook_accepts_audit_log_entry_event_type():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/webhooks"
        assert json.loads(request.content) == {
            "url": "https://example.com/hook",
            "event_types": ["audit_log.entry"],
        }
        return httpx.Response(
            201,
            json={
                "id": "wh-3",
                "url": "https://example.com/hook",
                "event_types": ["audit_log.entry"],
                "description": None,
                "is_active": True,
                "created_at": "now",
                "secret": "whsec_ghi789",
            },
        )

    async def run():
        client = _client_with(handler)
        result = await client.create_webhook(url="https://example.com/hook", event_types=["audit_log.entry"])
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "wh-3"
    assert result["event_types"] == ["audit_log.entry"]


def test_async_create_webhook_omits_description_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert "description" not in body
        return httpx.Response(
            201,
            json={
                "id": "wh-3",
                "url": "https://example.com/hook",
                "event_types": ["sandbox.created"],
                "description": None,
                "is_active": True,
                "created_at": "now",
                "secret": "whsec_ghi789",
            },
        )

    async def run():
        client = _client_with(handler)
        await client.create_webhook(url="https://example.com/hook", event_types=["sandbox.created"])
        await client.aclose()

    asyncio.run(run())


def test_async_list_webhooks_returns_empty_list_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/webhooks"
        return httpx.Response(200, json=[])

    async def run():
        client = _client_with(handler)
        result = await client.list_webhooks()
        await client.aclose()
        return result

    assert asyncio.run(run()) == []


def test_async_list_webhooks_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "wh-1"}, {"id": "wh-2"}])

    async def run():
        client = _client_with(handler)
        result = await client.list_webhooks()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert [wh["id"] for wh in result] == ["wh-1", "wh-2"]


def test_async_delete_webhook_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/webhooks/wh-1"
        return httpx.Response(204)

    async def run():
        client = _client_with(handler)
        result = await client.delete_webhook("wh-1")
        await client.aclose()
        return result

    assert asyncio.run(run()) is None


def test_async_list_webhook_deliveries_sends_limit_and_offset():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/webhooks/wh-1/deliveries"
        assert dict(request.url.params) == {"limit": "10", "offset": "5"}
        return httpx.Response(200, json=[{"id": "del-1", "status": "delivered"}])

    async def run():
        client = _client_with(handler)
        result = await client.list_webhook_deliveries("wh-1", limit=10, offset=5)
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result[0]["status"] == "delivered"


def test_async_list_webhook_deliveries_omits_params_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {}
        return httpx.Response(200, json=[])

    async def run():
        client = _client_with(handler)
        result = await client.list_webhook_deliveries("wh-1")
        await client.aclose()
        return result

    assert asyncio.run(run()) == []


def test_async_create_mcp_connection_sends_label_and_catalog_id():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/mcp-connections"
        assert json.loads(request.content) == {"label": "team-slack", "catalog_id": "slack"}
        return httpx.Response(
            201,
            json={
                "id": "mcpconn-1",
                "label": "team-slack",
                "catalog_id": "slack",
                "host": "mcp.slack.com",
                "created_at": "now",
                "last_used_at": None,
            },
        )

    async def run():
        client = _client_with(handler)
        result = await client.create_mcp_connection(label="team-slack", catalog_id="slack")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "mcpconn-1"
    assert result["host"] == "mcp.slack.com"


def test_async_list_mcp_connections_returns_empty_list_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/mcp-connections"
        return httpx.Response(200, json=[])

    async def run():
        client = _client_with(handler)
        result = await client.list_mcp_connections()
        await client.aclose()
        return result

    assert asyncio.run(run()) == []


def test_async_list_mcp_connections_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "mcpconn-1"}, {"id": "mcpconn-2"}])

    async def run():
        client = _client_with(handler)
        result = await client.list_mcp_connections()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert [c["id"] for c in result] == ["mcpconn-1", "mcpconn-2"]


def test_async_delete_mcp_connection_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/mcp-connections/mcpconn-1"
        return httpx.Response(204)

    async def run():
        client = _client_with(handler)
        result = await client.delete_mcp_connection("mcpconn-1")
        await client.aclose()
        return result

    assert asyncio.run(run()) is None


def test_async_create_secret_sends_name_value_and_allowed_hosts():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/secrets"
        assert json.loads(request.content) == {
            "name": "stripe-key",
            "value": "sk_test_abc123",
            "allowed_hosts": ["api.stripe.com"],
        }
        return httpx.Response(
            201,
            json={
                "id": "secret-1",
                "name": "stripe-key",
                "allowed_hosts": ["api.stripe.com"],
                "trust_tier": None,
                "created_at": "now",
                "last_used_at": None,
            },
        )

    async def run():
        client = _client_with(handler)
        result = await client.create_secret(
            name="stripe-key", value="sk_test_abc123", allowed_hosts=["api.stripe.com"]
        )
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["id"] == "secret-1"
    assert "value" not in result


def test_async_list_secrets_returns_empty_list_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/secrets"
        return httpx.Response(200, json=[])

    async def run():
        client = _client_with(handler)
        result = await client.list_secrets()
        await client.aclose()
        return result

    assert asyncio.run(run()) == []


def test_async_list_secrets_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "secret-1"}, {"id": "secret-2"}])

    async def run():
        client = _client_with(handler)
        result = await client.list_secrets()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert [s["id"] for s in result] == ["secret-1", "secret-2"]


def test_async_delete_secret_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/secrets/secret-1"
        return httpx.Response(204)

    async def run():
        client = _client_with(handler)
        result = await client.delete_secret("secret-1")
        await client.aclose()
        return result

    assert asyncio.run(run()) is None


def test_async_api_error_parses_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow down"}})

    async def run():
        client = _client_with(handler)
        try:
            with pytest.raises(BoxkiteApiError) as exc_info:
                await client.account()
            return exc_info.value.status_code
        finally:
            await client.aclose()

    status_code = asyncio.run(run())
    assert status_code == 429


def test_async_ls_glob_grep():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sandboxes/sess-1/files/ls":
            return httpx.Response(200, json={"entries": [{"name": "main.py", "type": "file"}]})
        if request.url.path == "/v1/sandboxes/sess-1/files/glob":
            return httpx.Response(200, json={"matches": [{"path": "/main.py"}]})
        if request.url.path == "/v1/sandboxes/sess-1/files/grep":
            return httpx.Response(200, json={"matches": [], "error": None, "truncated": False})
        raise AssertionError(f"unexpected call: {request.url.path}")

    async def run():
        client = _client_with(handler)
        ls_result = await client.ls("sess-1", path="/src")
        glob_result = await client.glob("sess-1", "**/*.py")
        grep_result = await client.grep("sess-1", "TODO", glob="*.py")
        await client.aclose()
        return ls_result, glob_result, grep_result

    ls_result, glob_result, grep_result = asyncio.run(run())
    assert ls_result["entries"][0]["name"] == "main.py"
    assert glob_result["matches"][0]["path"] == "/main.py"
    assert grep_result["truncated"] is False


def test_async_get_log_returns_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/log"
        assert dict(request.url.params) == {"limit": "50"}
        return httpx.Response(200, json={"entries": [{"operation": "exec"}]})

    async def run():
        client = _client_with(handler)
        result = await client.get_log("sess-1", limit=50)
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["entries"][0]["operation"] == "exec"


def test_async_watch_yields_parsed_sse_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/watch"
        body = b'data: {"operation": "exec", "detail": {"command": "echo hi"}}\n\n'
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def run():
        client = _client_with(handler)
        entries = [entry async for entry in client.watch("sess-1")]
        await client.aclose()
        return entries

    entries = asyncio.run(run())
    assert entries == [{"operation": "exec", "detail": {"command": "echo hi"}}]


def test_async_stream_process_output_yields_output_then_exit():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/processes/proc-1/stream"
        body = (
            b'event: output\ndata: {"type": "output", "stdout_chunk": "hi", "next_offset": 2}\n\n'
            b'event: exit\ndata: {"type": "exit", "status": "exited", "exit_code": 0}\n\n'
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def run():
        client = _client_with(handler)
        events = [e async for e in client.stream_process_output("sess-1", "proc-1")]
        await client.aclose()
        return events

    events = asyncio.run(run())
    assert events[0] == {"type": "output", "stdout_chunk": "hi", "next_offset": 2}
    assert events[-1] == {"type": "exit", "status": "exited", "exit_code": 0}


def test_async_watch_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no such session"}})

    async def run():
        client = _client_with(handler)
        try:
            with pytest.raises(BoxkiteApiError) as exc_info:
                async for _entry in client.watch("sess-1"):
                    pass
            return exc_info.value.status_code
        finally:
            await client.aclose()

    status_code = asyncio.run(run())
    assert status_code == 404


def test_async_start_process_posts_command():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/processes"
        return httpx.Response(201, json={"process_id": "proc_1", "status": "running", "started_at": "now"})

    async def run():
        client = _client_with(handler)
        result = await client.start_process("sess-1", "sleep 5")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["process_id"] == "proc_1"


def test_async_list_get_output_send_input_stop_process():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sandboxes/sess-1/processes":
            return httpx.Response(200, json={"processes": [{"process_id": "proc_1"}]})
        if request.url.path == "/v1/sandboxes/sess-1/processes/proc_1/output":
            return httpx.Response(
                200,
                json={
                    "status": "running",
                    "stdout_chunk": "hi",
                    "next_offset": 2,
                    "truncated": False,
                    "exit_code": None,
                },
            )
        if request.url.path == "/v1/sandboxes/sess-1/processes/proc_1/input":
            return httpx.Response(200, json={"bytes_written": 1})
        if request.url.path == "/v1/sandboxes/sess-1/processes/proc_1/stop":
            return httpx.Response(200, json={"status": "stopped", "exit_code": 0})
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    async def run():
        client = _client_with(handler)
        listing = await client.list_processes("sess-1")
        output = await client.get_process_output("sess-1", "proc_1")
        write = await client.send_process_input("sess-1", "proc_1", "x")
        stop = await client.stop_process("sess-1", "proc_1")
        await client.aclose()
        return listing, output, write, stop

    listing, output, write, stop = asyncio.run(run())
    assert listing["processes"][0]["process_id"] == "proc_1"
    assert output["stdout_chunk"] == "hi"
    assert write == {"bytes_written": 1}
    assert stop == {"status": "stopped", "exit_code": 0}


def test_async_sandbox_session_wraps_process_methods():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/v1/sandboxes/sess-1/processes":
            return httpx.Response(201, json={"process_id": "proc_1", "status": "running", "started_at": "now"})
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    async def run():
        client = _client_with(handler)
        async with client.sandbox() as sb:
            result = await sb.start_process("sleep 5")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["process_id"] == "proc_1"


def test_async_sandbox_session_wraps_ls_glob_grep():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/v1/sandboxes/sess-1/files/ls":
            return httpx.Response(200, json={"entries": []})
        raise AssertionError(f"unexpected call: {request.url.path}")

    async def run():
        client = _client_with(handler)
        async with client.sandbox() as sb:
            result = await sb.ls()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result == {"entries": []}


def test_async_create_preview_url_posts_ttl_seconds():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/sandboxes/sess-1/preview/3000"
        assert json.loads(request.content) == {"ttl_seconds": 1800}
        return httpx.Response(
            200,
            json={"url": "/v1/sandboxes/sess-1/preview/3000/?token=abc", "expires_at": "now", "token_id": "tok-1"},
        )

    async def run():
        client = _client_with(handler)
        result = await client.create_preview_url("sess-1", 3000, ttl_seconds=1800)
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result["token_id"] == "tok-1"


def test_async_revoke_preview_url_posts_token_id():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/sandboxes/sess-1/preview/3000/revoke"
        assert json.loads(request.content) == {"token_id": "tok-1"}
        return httpx.Response(200, json={"revoked": True, "token_id": "tok-1"})

    async def run():
        client = _client_with(handler)
        result = await client.revoke_preview_url("sess-1", 3000, "tok-1")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result == {"revoked": True, "token_id": "tok-1"}


def test_async_sandbox_session_wraps_preview_url_methods():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/v1/sandboxes/sess-1/preview/3000":
            return httpx.Response(
                200, json={"url": "/preview/3000/?token=abc", "expires_at": "now", "token_id": "tok-1"}
            )
        if request.url.path == "/v1/sandboxes/sess-1/preview/3000/revoke":
            return httpx.Response(200, json={"revoked": True, "token_id": "tok-1"})
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    async def run():
        client = _client_with(handler)
        async with client.sandbox() as sb:
            minted = await sb.create_preview_url(3000)
            revoked = await sb.revoke_preview_url(3000, "tok-1")
        await client.aclose()
        return minted, revoked

    minted, revoked = asyncio.run(run())
    assert minted["token_id"] == "tok-1"
    assert revoked == {"revoked": True, "token_id": "tok-1"}


def test_async_takeover_connects_to_wss_url_with_authorization_header():
    """No real socket needed here -- `ws_connect` is injected the same way
    `transport` is for HTTP, so this just verifies the URL/header plumbing."""
    captured: dict = {}

    async def fake_connect(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "fake-connection"

    async def run():
        client = AsyncBoxkiteClient(
            base_url="https://cp.example.com", api_key="bxk_live_test", ws_connect=fake_connect
        )
        result = await client.takeover("sess-1")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result == "fake-connection"
    assert captured["url"] == "wss://cp.example.com/v1/sandboxes/sess-1/takeover"
    assert captured["kwargs"] == {"additional_headers": {"Authorization": "Bearer bxk_live_test"}}


def test_async_takeover_sends_and_receives_raw_bytes():
    """End-to-end against a real local WebSocket server -- exercises the
    actual `websockets.connect` default, not the injected fake above, to
    prove the raw-byte duplex contract actually works."""
    from websockets.asyncio.server import serve

    received: list[bytes] = []

    async def handler(connection):
        message = await connection.recv()
        received.append(message)
        await connection.send(b"echo:" + message)

    async def run():
        async with serve(handler, "localhost", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = AsyncBoxkiteClient(base_url=f"http://localhost:{port}", api_key="bxk_live_test")
            ws = await client.takeover("sess-1")
            try:
                await ws.send(b"hello pty")
                reply = await ws.recv()
            finally:
                await ws.close()
            await client.aclose()
            return reply

    reply = asyncio.run(run())
    assert received == [b"hello pty"]
    assert reply == b"echo:hello pty"


def test_async_sandbox_session_takeover_delegates_to_client():
    async def fake_connect(url: str, **kwargs):
        return url

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError("unexpected call")

    async def run():
        client = AsyncBoxkiteClient(
            base_url="https://cp.example.com",
            api_key="bxk_live_test",
            transport=httpx.MockTransport(handler),
            ws_connect=fake_connect,
        )
        async with client.sandbox() as sb:
            result = await sb.takeover()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result == "wss://cp.example.com/v1/sandboxes/sess-1/takeover"


def test_async_desktop_takeover_connects_to_wss_url_with_authorization_header():
    """No real socket needed here -- `ws_connect` is injected the same way
    `transport` is for HTTP, so this just verifies the URL/header plumbing."""
    captured: dict = {}

    async def fake_connect(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "fake-connection"

    async def run():
        client = AsyncBoxkiteClient(
            base_url="https://cp.example.com", api_key="bxk_live_test", ws_connect=fake_connect
        )
        result = await client.desktop_takeover("sess-1")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result == "fake-connection"
    assert captured["url"] == "wss://cp.example.com/v1/sandboxes/sess-1/desktop"
    assert captured["kwargs"] == {"additional_headers": {"Authorization": "Bearer bxk_live_test"}}


def test_async_desktop_takeover_sends_and_receives_raw_bytes():
    """End-to-end against a real local WebSocket server -- exercises the
    actual `websockets.connect` default, not the injected fake above, to
    prove the raw-byte duplex contract actually works."""
    from websockets.asyncio.server import serve

    received: list[bytes] = []

    async def handler(connection):
        message = await connection.recv()
        received.append(message)
        await connection.send(b"echo:" + message)

    async def run():
        async with serve(handler, "localhost", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = AsyncBoxkiteClient(base_url=f"http://localhost:{port}", api_key="bxk_live_test")
            ws = await client.desktop_takeover("sess-1")
            try:
                await ws.send(b"hello desktop")
                reply = await ws.recv()
            finally:
                await ws.close()
            await client.aclose()
            return reply

    reply = asyncio.run(run())
    assert received == [b"hello desktop"]
    assert reply == b"echo:hello desktop"


def test_async_sandbox_session_desktop_takeover_delegates_to_client():
    async def fake_connect(url: str, **kwargs):
        return url

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError("unexpected call")

    async def run():
        client = AsyncBoxkiteClient(
            base_url="https://cp.example.com",
            api_key="bxk_live_test",
            transport=httpx.MockTransport(handler),
            ws_connect=fake_connect,
        )
        async with client.sandbox() as sb:
            result = await sb.desktop_takeover()
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert result == "wss://cp.example.com/v1/sandboxes/sess-1/desktop"


def test_async_sandbox_context_manager():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError("unexpected call")

    async def run():
        client = _client_with(handler)
        async with client.sandbox(label="async-demo") as sb:
            assert sb.id == "sess-1"
        await client.aclose()

    asyncio.run(run())
    assert ("DELETE", "/v1/sandboxes/sess-1") in calls


def test_async_sandbox_session_threads_mcp_connection_names():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.url.path == "/v1/sandboxes":
            assert json.loads(request.content) == {"mcp_connection_names": ["team-slack"]}
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.url.path == "/v1/sandboxes/sess-1":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.url.path}")

    async def run():
        client = _client_with(handler)
        async with client.sandbox(mcp_connection_names=["team-slack"]) as sb:
            assert sb.id == "sess-1"
        await client.aclose()

    asyncio.run(run())
