"""Basic organizations / teams: create, membership, roles."""

from __future__ import annotations

import httpx

from conftest import signup_and_get_api_key


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def test_create_organization_makes_creator_an_owner(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "org-owner@example.com")
    resp = await client.post("/v1/organizations", json={"name": "Acme"}, headers=_auth(key))
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Acme"
    assert body["role"] == "owner"

    listed = await client.get("/v1/organizations", headers=_auth(key))
    assert listed.status_code == 200
    assert [o["id"] for o in listed.json()] == [body["id"]]


async def test_list_only_shows_orgs_you_belong_to(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "org-a@example.com")
    key_b = await signup_and_get_api_key(client, "org-b@example.com")
    await client.post("/v1/organizations", json={"name": "A-org"}, headers=_auth(key_a))

    assert (await client.get("/v1/organizations", headers=_auth(key_b))).json() == []


async def test_non_member_cannot_see_org(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "org-owner2@example.com")
    key_b = await signup_and_get_api_key(client, "outsider@example.com")
    org = (await client.post("/v1/organizations", json={"name": "Private"}, headers=_auth(key_a))).json()

    resp = await client.get(f"/v1/organizations/{org['id']}", headers=_auth(key_b))
    assert resp.status_code == 404


async def test_owner_can_add_existing_account_and_member_sees_org(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "owner3@example.com")
    await signup_and_get_api_key(client, "teammate@example.com")  # target must already exist
    org = (await client.post("/v1/organizations", json={"name": "Team"}, headers=_auth(owner))).json()

    add = await client.post(
        f"/v1/organizations/{org['id']}/members",
        json={"email": "teammate@example.com", "role": "member"},
        headers=_auth(owner),
    )
    assert add.status_code == 201
    assert add.json()["role"] == "member"

    members = await client.get(f"/v1/organizations/{org['id']}/members", headers=_auth(owner))
    emails = {m["email"] for m in members.json()}
    assert emails == {"owner3@example.com", "teammate@example.com"}


async def test_adding_unknown_email_is_404(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "owner4@example.com")
    org = (await client.post("/v1/organizations", json={"name": "T4"}, headers=_auth(owner))).json()
    resp = await client.post(
        f"/v1/organizations/{org['id']}/members",
        json={"email": "nobody@example.com"},
        headers=_auth(owner),
    )
    assert resp.status_code == 404


async def test_plain_member_cannot_add_members(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "owner5@example.com")
    member_key = await signup_and_get_api_key(client, "member5@example.com")
    org = (await client.post("/v1/organizations", json={"name": "T5"}, headers=_auth(owner))).json()
    await client.post(
        f"/v1/organizations/{org['id']}/members",
        json={"email": "member5@example.com", "role": "member"},
        headers=_auth(owner),
    )
    resp = await client.post(
        f"/v1/organizations/{org['id']}/members",
        json={"email": "owner5@example.com"},
        headers=_auth(member_key),
    )
    assert resp.status_code == 403


async def test_duplicate_member_is_409(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "owner6@example.com")
    await signup_and_get_api_key(client, "dup6@example.com")
    org = (await client.post("/v1/organizations", json={"name": "T6"}, headers=_auth(owner))).json()
    payload = {"email": "dup6@example.com", "role": "member"}
    first = await client.post(f"/v1/organizations/{org['id']}/members", json=payload, headers=_auth(owner))
    assert first.status_code == 201
    second = await client.post(f"/v1/organizations/{org['id']}/members", json=payload, headers=_auth(owner))
    assert second.status_code == 409


async def test_cannot_remove_last_owner(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "solo-owner@example.com")
    org = (await client.post("/v1/organizations", json={"name": "Solo"}, headers=_auth(owner))).json()
    account = (await client.get("/v1/account", headers=_auth(owner))).json()
    resp = await client.delete(
        f"/v1/organizations/{org['id']}/members/{account['id']}", headers=_auth(owner)
    )
    assert resp.status_code == 409


async def test_owner_can_remove_a_member(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "owner7@example.com")
    await signup_and_get_api_key(client, "removeme@example.com")
    org = (await client.post("/v1/organizations", json={"name": "T7"}, headers=_auth(owner))).json()
    added = (
        await client.post(
            f"/v1/organizations/{org['id']}/members",
            json={"email": "removeme@example.com", "role": "member"},
            headers=_auth(owner),
        )
    ).json()
    resp = await client.delete(
        f"/v1/organizations/{org['id']}/members/{added['account_id']}", headers=_auth(owner)
    )
    assert resp.status_code == 204
    members = await client.get(f"/v1/organizations/{org['id']}/members", headers=_auth(owner))
    assert {m["email"] for m in members.json()} == {"owner7@example.com"}
