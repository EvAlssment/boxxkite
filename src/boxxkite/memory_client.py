"""Small async client for Boxkite's own account-scoped MemoryBase API."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx


class MemoryClientError(RuntimeError):
    pass


class AsyncMemoryClient:
    def __init__(self, *, base_url: str, api_key: str, timeout: float = 10.0):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("memory base_url must be an absolute http(s) URL")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("memory base_url must use https outside localhost")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
        ) as client:
            try:
                response = await client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise MemoryClientError(f"MemoryBase connection failed: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message")
            except (ValueError, TypeError):
                detail = None
            raise MemoryClientError(detail or f"MemoryBase request failed with HTTP {response.status_code}")
        return response.json() if response.content else None

    async def remember(self, **memory: Any) -> dict:
        return await self._request("POST", "/v1/memory", json=memory)

    async def ingest(self, **document: Any) -> dict:
        return await self._request("POST", "/v1/memory/ingest", json=document)

    async def recall(self, *, query: str, scope: str | None = None, limit: int = 10) -> dict:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if scope:
            params["scope"] = scope
        return await self._request("GET", "/v1/memory/search", params=params)

    async def profile(self, *, scope: str | None = None, limit: int = 20) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if scope:
            params["scope"] = scope
        return await self._request("GET", "/v1/memory/profile", params=params)

    async def export(self, *, scope: str | None = None) -> dict:
        params = {"scope": scope} if scope else None
        return await self._request("GET", "/v1/memory/export", params=params)

    async def metrics(self, *, scope: str | None = None) -> dict:
        params = {"scope": scope} if scope else None
        return await self._request("GET", "/v1/memory/metrics", params=params)

    async def import_memories(self, *, memories: list[dict], relations: list[dict] | None = None) -> dict:
        return await self._request(
            "POST",
            "/v1/memory/import",
            json={"memories": memories, "relations": relations or []},
        )

    async def forget(self, memory_id: str) -> None:
        await self._request("DELETE", f"/v1/memory/{memory_id}")
