"""API key `last_used_at` tracking — set on first authenticated use, then
kept up to date, so an account can tell whether a key is actually in use
before revoking it.
"""

from __future__ import annotations

import httpx

from conftest import create_api_key, signup


async def test_last_used_at_is_null_before_first_use(client: httpx.AsyncClient):
    access_token = (await signup(client, "key-unused@example.com"))["access_token"]
    created = await create_api_key(client, access_token, name="never used")

    resp = await client.get("/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"})
    [key] = resp.json()
    assert key["id"] == created["id"]
    assert key["last_used_at"] is None


async def test_last_used_at_set_after_authenticated_request(client: httpx.AsyncClient):
    access_token = (await signup(client, "key-used@example.com"))["access_token"]
    created = await create_api_key(client, access_token, name="will be used")
    raw_key = created["key"]

    await client.get("/v1/sandboxes", headers={"Authorization": f"Bearer {raw_key}"})

    resp = await client.get("/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"})
    [key] = resp.json()
    assert key["last_used_at"] is not None
