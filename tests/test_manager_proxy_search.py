import pytest

from boxxkite._manager_proxy import SidecarProxyMixin


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _HttpClient:
    def __init__(self):
        self.calls = []

    async def post(self, path, json):
        self.calls.append((path, json))
        return _Response({"matches": [], "truncated": False})


class _Proxy(SidecarProxyMixin):
    def __init__(self):
        self.client = _HttpClient()

    async def _resolve_session(self, session_id):
        assert session_id == "session-1"
        return "pod-1", "10.0.0.2"

    def _get_http_client(self, pod_name, pod_ip):
        assert (pod_name, pod_ip) == ("pod-1", "10.0.0.2")
        return self.client

    async def _call_sidecar_with_recovery(self, *, request_fn, **_kwargs):
        return await request_fn()


@pytest.mark.asyncio
async def test_semantic_search_proxy_uses_authenticated_sidecar_client():
    proxy = _Proxy()

    result = await proxy.semantic_search(
        session_id="session-1",
        query="retry logic",
        path="/workspace/src",
        max_results=7,
    )

    assert result == {"matches": [], "truncated": False}
    assert proxy.client.calls == [
        (
            "/semantic-search",
            {
                "query": "retry logic",
                "path": "/workspace/src",
                "max_results": 7,
            },
        )
    ]
