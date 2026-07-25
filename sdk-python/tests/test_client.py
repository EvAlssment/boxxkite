"""Sync BoxkiteClient tests. httpx.MockTransport stands in for the real
control-plane -- no network, no real deployment needed."""

from __future__ import annotations

import httpx
import pytest

from boxkite_client import BoxkiteApiError, BoxkiteClient


def _client_with(handler) -> BoxkiteClient:
    return BoxkiteClient(
        base_url="https://cp.example.com",
        api_key="bxk_live_test",
        transport=httpx.MockTransport(handler),
    )


def test_rejects_plain_http_to_a_remote_host():
    """api_key is a full-privilege, long-lived credential sent as
    `Authorization: Bearer` on every request -- an http:// URL to anything
    other than localhost would put it on the wire in cleartext."""
    with pytest.raises(ValueError, match="cleartext"):
        BoxkiteClient(base_url="http://cp.example.com", api_key="bxk_live_test")


def test_allows_http_localhost_for_local_dev():
    client = BoxkiteClient(base_url="http://localhost:8090", api_key="bxk_live_test")
    assert client is not None


def test_account_returns_parsed_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/account"
        assert request.headers["Authorization"] == "Bearer bxk_live_test"
        return httpx.Response(200, json={"id": "acct-1", "email": "a@example.com"})

    client = _client_with(handler)
    assert client.account() == {"id": "acct-1", "email": "a@example.com"}


def test_request_password_reset_posts_email():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/auth/password-reset/request"
        assert json.loads(request.content) == {"email": "user@example.com"}
        return httpx.Response(
            200,
            json={"message": "If an account with that email exists, a password reset link has been sent."},
        )

    client = _client_with(handler)
    result = client.request_password_reset("user@example.com")
    assert result["message"].startswith("If an account with that email exists")


def test_confirm_password_reset_posts_token_and_new_password():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/auth/password-reset/confirm"
        assert json.loads(request.content) == {"token": "reset-tok", "new_password": "new-hunter2"}
        return httpx.Response(200, json={"message": "Password has been reset. Please log in with your new password."})

    client = _client_with(handler)
    result = client.confirm_password_reset("reset-tok", "new-hunter2")
    assert result["message"].startswith("Password has been reset")


def test_confirm_password_reset_raises_on_invalid_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "invalid_or_expired_token", "message": "This password reset link is invalid or has expired."}},
        )

    client = _client_with(handler)
    with pytest.raises(BoxkiteApiError) as exc_info:
        client.confirm_password_reset("bad-tok", "new-hunter2")
    assert exc_info.value.code == "invalid_or_expired_token"


def test_verify_email_posts_token():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/auth/verify-email"
        assert json.loads(request.content) == {"token": "verify-tok"}
        return httpx.Response(200, json={"message": "Email verified."})

    client = _client_with(handler)
    assert client.verify_email("verify-tok") == {"message": "Email verified."}


def test_resend_verification_overrides_authorization_with_access_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/auth/resend-verification"
        # The dashboard JWT must replace, not merely accompany, the
        # client's api_key on this one call -- this control-plane rejects
        # an api_key on a route that requires a user session token.
        assert request.headers["Authorization"] == "Bearer dashboard-jwt-123"
        return httpx.Response(200, json={"message": "Verification email sent."})

    client = _client_with(handler)
    result = client.resend_verification("dashboard-jwt-123")
    assert result == {"message": "Verification email sent."}


def test_refresh_token_posts_refresh_token_and_returns_new_pair():
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

    client = _client_with(handler)
    result = client.refresh_token("old-refresh")
    assert result["access_token"] == "new-jwt"
    assert result["refresh_token"] == "new-refresh"


def test_refresh_token_raises_on_reused_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "refresh_token_reused", "message": "This refresh token has already been used."}},
        )

    client = _client_with(handler)
    with pytest.raises(BoxkiteApiError) as exc_info:
        client.refresh_token("already-used")
    assert exc_info.value.code == "refresh_token_reused"
    assert exc_info.value.status_code == 401


def test_logout_posts_refresh_token_and_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/auth/logout"
        assert json.loads(request.content) == {"refresh_token": "old-refresh"}
        return httpx.Response(204)

    client = _client_with(handler)
    assert client.logout("old-refresh") is None


def test_create_sandbox_sends_label():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/sandboxes"
        import json

        assert json.loads(request.content) == {"label": "demo"}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    client = _client_with(handler)
    result = client.create_sandbox(label="demo")
    assert result["id"] == "sess-1"


def test_exec_posts_command():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/exec"
        return httpx.Response(200, json={"exit_code": 0, "stdout": "hi\n", "stderr": ""})

    client = _client_with(handler)
    result = client.exec("sess-1", "echo hi")
    assert result["exit_code"] == 0


def test_create_sandbox_sends_secret_names():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"secret_names": ["prod-stripe"]}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    client = _client_with(handler)
    result = client.create_sandbox(secret_names=["prod-stripe"])
    assert result["id"] == "sess-1"


def test_http_request_posts_method_url_headers_body():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/sandboxes/sess-1/http-request"
        payload = json.loads(request.content)
        assert payload["method"] == "POST"
        assert payload["url"] == "https://api.example.com/v1/charges"
        assert payload["headers"] == {"Authorization": "Bearer {{secret:prod-stripe}}"}
        assert payload["body"] == "amount=2000"
        return httpx.Response(
            200,
            json={"status_code": 200, "headers": {"content-type": "text/plain"}, "body": "ok", "truncated": False},
        )

    client = _client_with(handler)
    result = client.http_request(
        "sess-1",
        "POST",
        "https://api.example.com/v1/charges",
        headers={"Authorization": "Bearer {{secret:prod-stripe}}"},
        body="amount=2000",
    )
    assert result["status_code"] == 200
    assert result["body"] == "ok"


def test_lsp_start_posts_language():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/sandboxes/sess-1/lsp/start"
        assert json.loads(request.content) == {"language": "python"}
        return httpx.Response(200, json={"lsp_id": "lsp-1"})

    client = _client_with(handler)
    result = client.lsp_start("sess-1", "python")
    assert result["lsp_id"] == "lsp-1"


def test_lsp_open_posts_path_and_content():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/open"
        assert json.loads(request.content) == {"path": "main.py", "content": "x = 1\n"}
        return httpx.Response(200, json={"status": "ok"})

    client = _client_with(handler)
    result = client.lsp_open("sess-1", "lsp-1", "main.py", "x = 1\n")
    assert result["status"] == "ok"


def test_lsp_completion_posts_path_line_character():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/completion"
        assert json.loads(request.content) == {"path": "main.py", "line": 3, "character": 5}
        return httpx.Response(200, json={"items": [{"label": "print"}]})

    client = _client_with(handler)
    result = client.lsp_completion("sess-1", "lsp-1", "main.py", 3, 5)
    assert result["items"] == [{"label": "print"}]


def test_lsp_stop_posts_to_lsp_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/lsp/lsp-1/stop"
        return httpx.Response(200, json={"status": "ok"})

    client = _client_with(handler)
    result = client.lsp_stop("sess-1", "lsp-1")
    assert result["status"] == "ok"


def test_sandbox_session_exposes_lsp_methods():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
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
        if request.url.path == "/v1/sandboxes/sess-1":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.url.path}")

    client = _client_with(handler)
    with client.sandbox() as sb:
        started = sb.lsp_start("python")
        assert sb.lsp_open(started["lsp_id"], "main.py", "x = 1")["status"] == "ok"
        assert sb.lsp_completion(started["lsp_id"], "main.py", 0, 0)["items"] == []
        assert sb.lsp_stop(started["lsp_id"])["status"] == "ok"
    assert calls == ["start", "open", "completion", "stop"]


def test_sandbox_session_exposes_http_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.url.path == "/v1/sandboxes/sess-1/http-request":
            calls.append(request)
            return httpx.Response(
                200,
                json={"status_code": 200, "headers": {}, "body": "ok", "truncated": False},
            )
        if request.url.path == "/v1/sandboxes/sess-1":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.url.path}")

    client = _client_with(handler)
    with client.sandbox(secret_names=["prod-stripe"]) as sb:
        result = sb.http_request("GET", "https://api.example.com/")
        assert result["status_code"] == 200
    assert len(calls) == 1


def test_sandbox_session_threads_mcp_connection_names():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.url.path == "/v1/sandboxes":
            assert json.loads(request.content) == {"mcp_connection_names": ["team-slack"]}
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.url.path == "/v1/sandboxes/sess-1":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.url.path}")

    client = _client_with(handler)
    with client.sandbox(mcp_connection_names=["team-slack"]) as sb:
        assert sb.id == "sess-1"


def test_destroy_sandbox_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(204)

    client = _client_with(handler)
    assert client.destroy_sandbox("sess-1") is None


def test_create_sandbox_sends_image_id():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"image_id": "img-1"}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    client = _client_with(handler)
    result = client.create_sandbox(image_id="img-1")
    assert result["id"] == "sess-1"


def test_create_sandbox_sends_mcp_connection_names():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"mcp_connection_names": ["team-slack"]}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    client = _client_with(handler)
    result = client.create_sandbox(mcp_connection_names=["team-slack"])
    assert result["id"] == "sess-1"


def test_create_image_sends_pinned_packages():
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

    client = _client_with(handler)
    result = client.create_image(
        label="demo",
        base="boxkite-minimal",
        python_packages=["requests==2.32.3"],
        apt_packages=["curl==8.5.0-2ubuntu10.1"],
    )
    assert result["id"] == "img-1"
    assert result["status"] == "queued"


def test_create_image_defaults_base_when_omitted():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"base": "boxkite-default"}
        return httpx.Response(202, json={"id": "img-2", "label": None, "status": "queued", "created_at": "now"})

    client = _client_with(handler)
    result = client.create_image()
    assert result["id"] == "img-2"


def test_get_image_returns_parsed_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/images/img-1"
        return httpx.Response(200, json={"id": "img-1", "status": "completed", "digest": "sha256:abc"})

    client = _client_with(handler)
    result = client.get_image("img-1")
    assert result["status"] == "completed"


def test_list_images_returns_empty_list_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/images"
        return httpx.Response(200, json=[])

    client = _client_with(handler)
    assert client.list_images() == []


def test_list_images_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "img-1"}, {"id": "img-2"}])

    client = _client_with(handler)
    result = client.list_images()
    assert [img["id"] for img in result] == ["img-1", "img-2"]


def test_delete_image_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/images/img-1"
        return httpx.Response(204)

    client = _client_with(handler)
    assert client.delete_image("img-1") is None


def test_create_image_sends_npm_packages():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {
            "base": "boxkite-node",
            "npm_packages": ["typescript==5.6.0"],
        }
        return httpx.Response(202, json={"id": "img-3", "label": None, "status": "queued", "created_at": "now"})

    client = _client_with(handler)
    result = client.create_image(base="boxkite-node", npm_packages=["typescript==5.6.0"])
    assert result["id"] == "img-3"


def test_create_image_omits_npm_packages_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert "npm_packages" not in body
        return httpx.Response(202, json={"id": "img-4", "label": None, "status": "queued", "created_at": "now"})

    client = _client_with(handler)
    result = client.create_image()
    assert result["id"] == "img-4"


def test_create_sandbox_sends_volume_mounts():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"volume_mounts": {"vol-1": "/data"}}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    client = _client_with(handler)
    result = client.create_sandbox(volume_mounts={"vol-1": "/data"})
    assert result["id"] == "sess-1"


def test_create_sandbox_omits_volume_mounts_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert "volume_mounts" not in body
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    client = _client_with(handler)
    result = client.create_sandbox()
    assert result["id"] == "sess-1"


def test_create_sandbox_sends_gpu_count():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"gpu_count": 2}
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    client = _client_with(handler)
    result = client.create_sandbox(gpu_count=2)
    assert result["id"] == "sess-1"


def test_create_sandbox_omits_gpu_count_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert "gpu_count" not in body
        return httpx.Response(201, json={"id": "sess-1", "status": "active"})

    client = _client_with(handler)
    result = client.create_sandbox()
    assert result["id"] == "sess-1"


def test_create_volume_sends_label_and_size():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/volumes"
        assert json.loads(request.content) == {"size_gb": 10.0, "label": "demo"}
        return httpx.Response(
            202,
            json={"id": "vol-1", "label": "demo", "size_gb": 10.0, "status": "queued", "created_at": "now"},
        )

    client = _client_with(handler)
    result = client.create_volume(label="demo", size_gb=10.0)
    assert result["id"] == "vol-1"
    assert result["status"] == "queued"


def test_create_volume_omits_label_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"size_gb": 5.0}
        return httpx.Response(
            202,
            json={"id": "vol-2", "label": None, "size_gb": 5.0, "status": "queued", "created_at": "now"},
        )

    client = _client_with(handler)
    result = client.create_volume(size_gb=5.0)
    assert result["id"] == "vol-2"


def test_get_volume_returns_parsed_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/volumes/vol-1"
        return httpx.Response(200, json={"id": "vol-1", "status": "ready", "size_gb": 10.0})

    client = _client_with(handler)
    result = client.get_volume("vol-1")
    assert result["status"] == "ready"


def test_list_volumes_returns_empty_list_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/volumes"
        return httpx.Response(200, json=[])

    client = _client_with(handler)
    assert client.list_volumes() == []


def test_list_volumes_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "vol-1"}, {"id": "vol-2"}])

    client = _client_with(handler)
    result = client.list_volumes()
    assert [vol["id"] for vol in result] == ["vol-1", "vol-2"]


def test_delete_volume_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/volumes/vol-1"
        return httpx.Response(204)

    client = _client_with(handler)
    assert client.delete_volume("vol-1") is None


def test_create_webhook_sends_url_and_event_types():
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

    client = _client_with(handler)
    result = client.create_webhook(url="https://example.com/hook", event_types=["sandbox.created"])
    assert result["id"] == "wh-1"
    assert result["secret"] == "whsec_abc123"


def test_create_webhook_accepts_audit_log_entry_event_type():
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

    client = _client_with(handler)
    result = client.create_webhook(url="https://example.com/hook", event_types=["audit_log.entry"])
    assert result["id"] == "wh-3"
    assert result["event_types"] == ["audit_log.entry"]


def test_create_webhook_sends_description_when_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert body["description"] == "Slack notifier"
        return httpx.Response(
            201,
            json={
                "id": "wh-2",
                "url": "https://example.com/hook",
                "event_types": ["sandbox.destroyed"],
                "description": "Slack notifier",
                "is_active": True,
                "created_at": "now",
                "secret": "whsec_def456",
            },
        )

    client = _client_with(handler)
    result = client.create_webhook(
        url="https://example.com/hook", event_types=["sandbox.destroyed"], description="Slack notifier"
    )
    assert result["description"] == "Slack notifier"


def test_create_webhook_omits_description_when_not_given():
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

    client = _client_with(handler)
    client.create_webhook(url="https://example.com/hook", event_types=["sandbox.created"])


def test_list_webhooks_returns_empty_list_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/webhooks"
        return httpx.Response(200, json=[])

    client = _client_with(handler)
    assert client.list_webhooks() == []


def test_list_webhooks_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "wh-1"}, {"id": "wh-2"}])

    client = _client_with(handler)
    result = client.list_webhooks()
    assert [wh["id"] for wh in result] == ["wh-1", "wh-2"]


def test_delete_webhook_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/webhooks/wh-1"
        return httpx.Response(204)

    client = _client_with(handler)
    assert client.delete_webhook("wh-1") is None


def test_list_webhook_deliveries_sends_limit_and_offset():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/webhooks/wh-1/deliveries"
        assert dict(request.url.params) == {"limit": "10", "offset": "5"}
        return httpx.Response(200, json=[{"id": "del-1", "status": "delivered"}])

    client = _client_with(handler)
    result = client.list_webhook_deliveries("wh-1", limit=10, offset=5)
    assert result[0]["status"] == "delivered"


def test_list_webhook_deliveries_omits_params_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {}
        return httpx.Response(200, json=[])

    client = _client_with(handler)
    assert client.list_webhook_deliveries("wh-1") == []


def test_create_mcp_connection_sends_label_and_catalog_id():
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

    client = _client_with(handler)
    result = client.create_mcp_connection(label="team-slack", catalog_id="slack")
    assert result["id"] == "mcpconn-1"
    assert result["host"] == "mcp.slack.com"


def test_list_mcp_connections_returns_empty_list_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/mcp-connections"
        return httpx.Response(200, json=[])

    client = _client_with(handler)
    assert client.list_mcp_connections() == []


def test_list_mcp_connections_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "mcpconn-1"}, {"id": "mcpconn-2"}])

    client = _client_with(handler)
    result = client.list_mcp_connections()
    assert [c["id"] for c in result] == ["mcpconn-1", "mcpconn-2"]


def test_delete_mcp_connection_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/mcp-connections/mcpconn-1"
        return httpx.Response(204)

    client = _client_with(handler)
    assert client.delete_mcp_connection("mcpconn-1") is None


def test_create_secret_sends_name_value_and_allowed_hosts():
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

    client = _client_with(handler)
    result = client.create_secret(
        name="stripe-key", value="sk_test_abc123", allowed_hosts=["api.stripe.com"]
    )
    assert result["id"] == "secret-1"
    assert "value" not in result


def test_create_secret_sends_trust_tier_when_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {
            "name": "wallet-key",
            "value": "0xabc",
            "allowed_hosts": ["rpc.example.com"],
            "trust_tier": "testnet",
        }
        return httpx.Response(
            201,
            json={
                "id": "secret-2",
                "name": "wallet-key",
                "allowed_hosts": ["rpc.example.com"],
                "trust_tier": "testnet",
                "created_at": "now",
                "last_used_at": None,
            },
        )

    client = _client_with(handler)
    result = client.create_secret(
        name="wallet-key",
        value="0xabc",
        allowed_hosts=["rpc.example.com"],
        trust_tier="testnet",
    )
    assert result["trust_tier"] == "testnet"


def test_list_secrets_returns_empty_list_when_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/secrets"
        return httpx.Response(200, json=[])

    client = _client_with(handler)
    assert client.list_secrets() == []


def test_list_secrets_returns_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "secret-1"}, {"id": "secret-2"}])

    client = _client_with(handler)
    result = client.list_secrets()
    assert [s["id"] for s in result] == ["secret-1", "secret-2"]


def test_delete_secret_returns_none_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/secrets/secret-1"
        return httpx.Response(204)

    client = _client_with(handler)
    assert client.delete_secret("secret-1") is None


def test_api_error_parses_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Sandbox session not found"}})

    client = _client_with(handler)
    with pytest.raises(BoxkiteApiError) as exc_info:
        client.get_sandbox("missing")

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "not_found"
    assert "Sandbox session not found" in str(exc_info.value)


def test_connection_error_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client_with(handler)
    from boxkite_client import BoxkiteConnectionError

    with pytest.raises(BoxkiteConnectionError):
        client.account()


# ── SandboxSession context manager: auto create/destroy ──────────────────
def test_sandbox_context_manager_creates_and_destroys():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/v1/sandboxes/sess-1/exec":
            return httpx.Response(200, json={"exit_code": 0, "stdout": "ok\n", "stderr": ""})
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    client = _client_with(handler)
    with client.sandbox(label="ctx-demo") as sb:
        assert sb.id == "sess-1"
        result = sb.exec("echo hi")
        assert result["exit_code"] == 0

    assert ("POST", "/v1/sandboxes") in calls
    assert ("DELETE", "/v1/sandboxes/sess-1") in calls


def test_ls_posts_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/ls"
        import json

        assert json.loads(request.content) == {"path": "/src"}
        return httpx.Response(200, json={"entries": [{"name": "main.py", "type": "file"}]})

    client = _client_with(handler)
    result = client.ls("sess-1", path="/src")
    assert result["entries"][0]["name"] == "main.py"


def test_ls_defaults_to_root():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"path": "/"}
        return httpx.Response(200, json={"entries": []})

    client = _client_with(handler)
    client.ls("sess-1")


def test_glob_posts_pattern_and_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/glob"
        import json

        assert json.loads(request.content) == {"pattern": "**/*.py", "path": "/"}
        return httpx.Response(200, json={"matches": [{"path": "/main.py"}]})

    client = _client_with(handler)
    result = client.glob("sess-1", "**/*.py")
    assert result["matches"][0]["path"] == "/main.py"


def test_grep_posts_pattern_glob_and_max_matches():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/files/grep"
        import json

        assert json.loads(request.content) == {
            "pattern": "TODO",
            "path": "/",
            "max_matches": 500,
            "glob": "*.py",
        }
        return httpx.Response(200, json={"matches": [], "error": None, "truncated": False})

    client = _client_with(handler)
    result = client.grep("sess-1", "TODO", glob="*.py")
    assert result["truncated"] is False


def test_grep_omits_glob_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {"pattern": "TODO", "path": "/", "max_matches": 500}
        return httpx.Response(200, json={"matches": [], "error": None, "truncated": False})

    client = _client_with(handler)
    client.grep("sess-1", "TODO")


def test_get_log_returns_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/log"
        assert dict(request.url.params) == {"limit": "50", "offset": "10"}
        return httpx.Response(200, json={"entries": [{"operation": "exec", "detail": {"command": "ls"}}]})

    client = _client_with(handler)
    result = client.get_log("sess-1", limit=50, offset=10)
    assert result["entries"][0]["operation"] == "exec"


def test_get_log_omits_params_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {}
        return httpx.Response(200, json={"entries": []})

    client = _client_with(handler)
    client.get_log("sess-1")


def test_watch_yields_parsed_sse_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/watch"
        body = (
            b'data: {"operation": "exec", "detail": {"command": "echo hi"}}\n\n'
            b'data: {"operation": "ls", "detail": {"path": "/"}}\n\n'
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = _client_with(handler)
    entries = list(client.watch("sess-1"))
    assert entries == [
        {"operation": "exec", "detail": {"command": "echo hi"}},
        {"operation": "ls", "detail": {"path": "/"}},
    ]


def test_watch_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no such session"}})

    client = _client_with(handler)
    with pytest.raises(BoxkiteApiError) as exc_info:
        list(client.watch("sess-1"))
    assert exc_info.value.status_code == 404


def test_stream_process_output_yields_output_then_exit():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/processes/proc-1/stream"
        assert request.url.params.get("since_offset") == "0"
        body = (
            b'event: output\ndata: {"type": "output", "stdout_chunk": "hello", "next_offset": 5}\n\n'
            b'event: exit\ndata: {"type": "exit", "status": "exited", "exit_code": 0}\n\n'
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = _client_with(handler)
    events = list(client.stream_process_output("sess-1", "proc-1"))
    assert events[0] == {"type": "output", "stdout_chunk": "hello", "next_offset": 5}
    assert events[-1] == {"type": "exit", "status": "exited", "exit_code": 0}


def test_stream_process_output_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "Process not found"}})

    client = _client_with(handler)
    with pytest.raises(BoxkiteApiError) as exc_info:
        list(client.stream_process_output("sess-1", "proc-1"))
    assert exc_info.value.status_code == 404


def test_start_process_posts_command_and_max_runtime():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/processes"
        import json

        assert json.loads(request.content) == {
            "command": "npm run dev",
            "max_runtime_seconds": 3600,
            "description": "dev server",
        }
        return httpx.Response(201, json={"process_id": "proc_1", "status": "running", "started_at": "now"})

    client = _client_with(handler)
    result = client.start_process("sess-1", "npm run dev", description="dev server")
    assert result["process_id"] == "proc_1"


def test_list_processes_gets_processes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/sandboxes/sess-1/processes"
        return httpx.Response(200, json={"processes": [{"process_id": "proc_1"}]})

    client = _client_with(handler)
    result = client.list_processes("sess-1")
    assert result["processes"][0]["process_id"] == "proc_1"


def test_get_process_output_passes_since_offset():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/processes/proc_1/output"
        assert dict(request.url.params) == {"since_offset": "10"}
        return httpx.Response(
            200,
            json={"status": "running", "stdout_chunk": "x", "next_offset": 11, "truncated": False, "exit_code": None},
        )

    client = _client_with(handler)
    result = client.get_process_output("sess-1", "proc_1", since_offset=10)
    assert result["stdout_chunk"] == "x"


def test_send_process_input_posts_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sandboxes/sess-1/processes/proc_1/input"
        import json

        assert json.loads(request.content) == {"data": "y\n"}
        return httpx.Response(200, json={"bytes_written": 2})

    client = _client_with(handler)
    result = client.send_process_input("sess-1", "proc_1", "y\n")
    assert result["bytes_written"] == 2


def test_stop_process_posts_to_stop_route():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/sandboxes/sess-1/processes/proc_1/stop"
        return httpx.Response(200, json={"status": "stopped", "exit_code": 143})

    client = _client_with(handler)
    result = client.stop_process("sess-1", "proc_1")
    assert result["status"] == "stopped"


def test_sandbox_session_wraps_ls_glob_grep():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/v1/sandboxes/sess-1/files/ls":
            return httpx.Response(200, json={"entries": []})
        if request.url.path == "/v1/sandboxes/sess-1/files/glob":
            return httpx.Response(200, json={"matches": []})
        if request.url.path == "/v1/sandboxes/sess-1/files/grep":
            return httpx.Response(200, json={"matches": [], "error": None, "truncated": False})
        if request.url.path == "/v1/sandboxes/sess-1/log":
            return httpx.Response(200, json={"entries": []})
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    client = _client_with(handler)
    with client.sandbox() as sb:
        assert sb.ls() == {"entries": []}
        assert sb.glob("**/*.py") == {"matches": []}
        assert sb.grep("TODO") == {"matches": [], "error": None, "truncated": False}
        assert sb.get_log() == {"entries": []}


def test_sandbox_session_wraps_process_methods():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/v1/sandboxes/sess-1/processes":
            return httpx.Response(201, json={"process_id": "proc_1", "status": "running", "started_at": "now"})
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

    client = _client_with(handler)
    with client.sandbox() as sb:
        started = sb.start_process("sleep 5")
        assert started["process_id"] == "proc_1"
        assert sb.list_processes()["processes"][0]["process_id"] == "proc_1"
        assert sb.get_process_output("proc_1")["stdout_chunk"] == "hi"
        assert sb.send_process_input("proc_1", "x") == {"bytes_written": 1}
        assert sb.stop_process("proc_1") == {"status": "stopped", "exit_code": 0}


def test_sandbox_context_manager_destroys_even_on_exception():
    destroyed = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            destroyed.append(request.url.path)
            return httpx.Response(204)
        raise AssertionError("unexpected call")

    client = _client_with(handler)
    with pytest.raises(ValueError):
        with client.sandbox() as sb:
            raise ValueError("boom")

    assert destroyed == ["/v1/sandboxes/sess-1"]


def test_takeover_connects_to_wss_url_with_authorization_header():
    """No real socket needed here -- `ws_connect` is injected the same way
    `transport` is for HTTP, so this just verifies the URL/header plumbing."""
    captured: dict = {}

    def fake_connect(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "fake-connection"

    client = BoxkiteClient(
        base_url="https://cp.example.com", api_key="bxk_live_test", ws_connect=fake_connect
    )
    result = client.takeover("sess-1")

    assert result == "fake-connection"
    assert captured["url"] == "wss://cp.example.com/v1/sandboxes/sess-1/takeover"
    assert captured["kwargs"] == {"additional_headers": {"Authorization": "Bearer bxk_live_test"}}


def test_takeover_sends_and_receives_raw_bytes():
    """End-to-end against a real local WebSocket server -- exercises the
    actual `websockets.sync.client.connect` default, not the injected fake
    above, to prove the raw-byte duplex contract actually works."""
    import threading

    from websockets.sync.server import serve

    received: list[bytes] = []

    def handler(connection):
        message = connection.recv()
        received.append(message)
        connection.send(b"echo:" + message)

    with serve(handler, "localhost", 0) as server:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            port = server.socket.getsockname()[1]
            client = BoxkiteClient(base_url=f"http://localhost:{port}", api_key="bxk_live_test")
            with client.takeover("sess-1") as ws:
                ws.send(b"hello pty")
                reply = ws.recv()
        finally:
            server.shutdown()
            server_thread.join(timeout=5)

    assert received == [b"hello pty"]
    assert reply == b"echo:hello pty"


def test_desktop_takeover_connects_to_wss_url_with_authorization_header():
    """No real socket needed here -- `ws_connect` is injected the same way
    `transport` is for HTTP, so this just verifies the URL/header plumbing."""
    captured: dict = {}

    def fake_connect(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "fake-connection"

    client = BoxkiteClient(
        base_url="https://cp.example.com", api_key="bxk_live_test", ws_connect=fake_connect
    )
    result = client.desktop_takeover("sess-1")

    assert result == "fake-connection"
    assert captured["url"] == "wss://cp.example.com/v1/sandboxes/sess-1/desktop"
    assert captured["kwargs"] == {"additional_headers": {"Authorization": "Bearer bxk_live_test"}}


def test_desktop_takeover_sends_and_receives_raw_bytes():
    """End-to-end against a real local WebSocket server -- exercises the
    actual `websockets.sync.client.connect` default, not the injected fake
    above, to prove the raw-byte duplex contract actually works."""
    import threading

    from websockets.sync.server import serve

    received: list[bytes] = []

    def handler(connection):
        message = connection.recv()
        received.append(message)
        connection.send(b"echo:" + message)

    with serve(handler, "localhost", 0) as server:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            port = server.socket.getsockname()[1]
            client = BoxkiteClient(base_url=f"http://localhost:{port}", api_key="bxk_live_test")
            with client.desktop_takeover("sess-1") as ws:
                ws.send(b"hello desktop")
                reply = ws.recv()
        finally:
            server.shutdown()
            server_thread.join(timeout=5)

    assert received == [b"hello desktop"]
    assert reply == b"echo:hello desktop"


def test_sandbox_session_desktop_takeover_delegates_to_client():
    def fake_connect(url: str, **kwargs):
        return url

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError("unexpected call")

    client = BoxkiteClient(
        base_url="https://cp.example.com",
        api_key="bxk_live_test",
        transport=httpx.MockTransport(handler),
        ws_connect=fake_connect,
    )
    with client.sandbox() as sb:
        assert sb.desktop_takeover() == "wss://cp.example.com/v1/sandboxes/sess-1/desktop"


def test_create_preview_url_posts_ttl_seconds():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/sandboxes/sess-1/preview/3000"
        assert json.loads(request.content) == {"ttl_seconds": 1800}
        return httpx.Response(
            200,
            json={"url": "/v1/sandboxes/sess-1/preview/3000/?token=abc", "expires_at": "now", "token_id": "tok-1"},
        )

    client = _client_with(handler)
    result = client.create_preview_url("sess-1", 3000, ttl_seconds=1800)
    assert result["token_id"] == "tok-1"


def test_create_preview_url_omits_ttl_seconds_when_not_given():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content) == {}
        return httpx.Response(
            200,
            json={"url": "/v1/sandboxes/sess-1/preview/3000/?token=abc", "expires_at": "now", "token_id": "tok-2"},
        )

    client = _client_with(handler)
    result = client.create_preview_url("sess-1", 3000)
    assert result["token_id"] == "tok-2"


def test_revoke_preview_url_posts_token_id():
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/v1/sandboxes/sess-1/preview/3000/revoke"
        assert json.loads(request.content) == {"token_id": "tok-1"}
        return httpx.Response(200, json={"revoked": True, "token_id": "tok-1"})

    client = _client_with(handler)
    result = client.revoke_preview_url("sess-1", 3000, "tok-1")
    assert result == {"revoked": True, "token_id": "tok-1"}


def test_sandbox_session_wraps_preview_url_methods():
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

    client = _client_with(handler)
    with client.sandbox() as sb:
        minted = sb.create_preview_url(3000)
        assert minted["token_id"] == "tok-1"
        revoked = sb.revoke_preview_url(3000, "tok-1")
        assert revoked == {"revoked": True, "token_id": "tok-1"}


def test_sandbox_session_takeover_delegates_to_client():
    def fake_connect(url: str, **kwargs):
        return url

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(201, json={"id": "sess-1", "status": "active"})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError("unexpected call")

    client = BoxkiteClient(
        base_url="https://cp.example.com",
        api_key="bxk_live_test",
        transport=httpx.MockTransport(handler),
        ws_connect=fake_connect,
    )
    with client.sandbox() as sb:
        assert sb.takeover() == "wss://cp.example.com/v1/sandboxes/sess-1/takeover"
