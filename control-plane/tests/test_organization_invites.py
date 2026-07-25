"""Organization invite flow (issue #225): invite by email, accept, revoke."""

from __future__ import annotations

import httpx

from conftest import signup_and_get_api_key


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _make_org(client, key, name="Org") -> str:
    return (await client.post("/v1/organizations", json={"name": name}, headers=_auth(key))).json()["id"]


async def test_owner_invites_and_invitee_accepts(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "inv-owner@example.com")
    invitee = await signup_and_get_api_key(client, "invitee@example.com")
    org_id = await _make_org(client, owner)

    inv = await client.post(
        f"/v1/organizations/{org_id}/invites",
        json={"email": "invitee@example.com", "role": "member"},
        headers=_auth(owner),
    )
    assert inv.status_code == 201
    token = inv.json()["token"]
    assert token  # shown once

    # It shows up in the pending list...
    listed = await client.get(f"/v1/organizations/{org_id}/invites", headers=_auth(owner))
    assert [i["email"] for i in listed.json()] == ["invitee@example.com"]
    assert "token" not in listed.json()[0]  # never re-exposed

    # ...and the invitee can accept it.
    accept = await client.post(
        "/v1/organizations/accept-invite", json={"token": token}, headers=_auth(invitee)
    )
    assert accept.status_code == 200
    assert accept.json()["id"] == org_id
    assert accept.json()["role"] == "member"

    # The invitee is now a member (can see the org) and the invite is consumed.
    assert (await client.get(f"/v1/organizations/{org_id}", headers=_auth(invitee))).status_code == 200
    assert (await client.get(f"/v1/organizations/{org_id}/invites", headers=_auth(owner))).json() == []


async def test_accept_with_mismatched_email_is_403(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "inv-owner2@example.com")
    other = await signup_and_get_api_key(client, "other@example.com")
    org_id = await _make_org(client, owner)
    token = (
        await client.post(
            f"/v1/organizations/{org_id}/invites",
            json={"email": "someone-else@example.com"},
            headers=_auth(owner),
        )
    ).json()["token"]

    resp = await client.post("/v1/organizations/accept-invite", json={"token": token}, headers=_auth(other))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "email_mismatch"


async def test_invalid_token_is_404(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "inv-bad@example.com")
    resp = await client.post(
        "/v1/organizations/accept-invite", json={"token": "not-a-real-token"}, headers=_auth(key)
    )
    assert resp.status_code == 404


async def test_double_accept_is_409(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "inv-owner3@example.com")
    invitee = await signup_and_get_api_key(client, "invitee3@example.com")
    org_id = await _make_org(client, owner)
    token = (
        await client.post(
            f"/v1/organizations/{org_id}/invites",
            json={"email": "invitee3@example.com"},
            headers=_auth(owner),
        )
    ).json()["token"]

    assert (await client.post("/v1/organizations/accept-invite", json={"token": token}, headers=_auth(invitee))).status_code == 200
    second = await client.post("/v1/organizations/accept-invite", json={"token": token}, headers=_auth(invitee))
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invite_used"


async def test_plain_member_cannot_invite(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "inv-owner4@example.com")
    member_key = await signup_and_get_api_key(client, "member4@example.com")
    org_id = await _make_org(client, owner)
    await client.post(
        f"/v1/organizations/{org_id}/members",
        json={"email": "member4@example.com", "role": "member"},
        headers=_auth(owner),
    )
    resp = await client.post(
        f"/v1/organizations/{org_id}/invites",
        json={"email": "x@example.com"},
        headers=_auth(member_key),
    )
    assert resp.status_code == 403


async def test_inviting_existing_member_is_409(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "inv-owner5@example.com")
    org_id = await _make_org(client, owner)
    resp = await client.post(
        f"/v1/organizations/{org_id}/invites",
        json={"email": "inv-owner5@example.com"},  # the owner themselves
        headers=_auth(owner),
    )
    assert resp.status_code == 409


async def test_revoked_invite_cannot_be_accepted(client: httpx.AsyncClient):
    owner = await signup_and_get_api_key(client, "inv-owner6@example.com")
    invitee = await signup_and_get_api_key(client, "invitee6@example.com")
    org_id = await _make_org(client, owner)
    created = (
        await client.post(
            f"/v1/organizations/{org_id}/invites",
            json={"email": "invitee6@example.com"},
            headers=_auth(owner),
        )
    ).json()

    revoke = await client.delete(f"/v1/organizations/{org_id}/invites/{created['id']}", headers=_auth(owner))
    assert revoke.status_code == 204
    accept = await client.post(
        "/v1/organizations/accept-invite", json={"token": created["token"]}, headers=_auth(invitee)
    )
    assert accept.status_code == 404
