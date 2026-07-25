"""Payload-fuzzing tests for the SCIM 2.0 webhook (routers/scim.py).

Issue #153 ("SCIM/SSO never exercised against a real IdP") flags this as
the one automatable piece of that issue's outstanding-verification list --
docs/ENTERPRISE-SSO-DESIGN.md's own SCIM section explicitly calls out
"fuzzing the webhook payload shape" as unfinished. test_scim_provisioning.py
only ever posts the exact happy-path Directory User shape WorkOS's docs
describe; this file is the adversarial complement: malformed/wrong-typed/
oversized/injection-shaped bodies, all past a VALID signature (fuzzing the
payload shape, not the signature verifier -- that's covered separately in
test_scim_provisioning.py's signature tests).

The contract under test, for every case below: the handler must fail
closed. Concretely, for a signed request:
- a 400/413/422 with no server-side exception, OR
- a 200 that safely no-ops (an explicitly unhandled event/field, matching
  the same tolerant-by-design behavior the happy-path tests already cover
  for e.g. dsync.group.created) -- never a 500, and never a state change
  the payload didn't legitimately earn (no account created/deactivated
  from a field that failed validation).

This test file does NOT and cannot exercise: a live WorkOS Directory Sync
connection, or load-testing the signature verifier under concurrent
delivery -- both remain manual, tracked in issue #153 and this repo's PR
description, not something a fuzz suite can substitute for.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from sqlalchemy import select

from control_plane import db as db_module
from control_plane.config import settings
from control_plane.models_orm import Account
from control_plane.repository import AccountRepository
from test_scim_provisioning import SCIM_SECRET, _enable_scim, _post_scim_webhook, _signature_header


def _signed(body: bytes) -> str:
    return _signature_header(SCIM_SECRET, body)


async def _post_signed(client: httpx.AsyncClient, body: bytes) -> httpx.Response:
    return await _post_scim_webhook(client, body, _signed(body))


async def _account_count(directory_user_id: str) -> int:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(
            select(Account).where(Account.scim_directory_user_id == directory_user_id)
        )
        return len(result.scalars().all())


# ── Category A: malformed top-level JSON / non-object payload ──────────────
@pytest.mark.parametrize(
    "body",
    [
        b"[1, 2, 3]",
        b'"just a string"',
        b"42",
        b"null",
        b"true",
        b"",
        b"not json at all {{{",
        b'{"event": "dsync.user.created", "data": {',  # truncated
        b"\x00\x01\x02binary garbage, not json",
    ],
    ids=["array", "string", "number", "null", "bool", "empty", "garbage", "truncated", "binary"],
)
async def test_non_object_or_unparseable_body_fails_closed(
    client: httpx.AsyncClient, monkeypatch, body: bytes
):
    _enable_scim(monkeypatch)
    resp = await _post_signed(client, body)
    assert resp.status_code in (400, 422), resp.text
    assert resp.json()["error"]["code"] in ("invalid_payload",)


async def test_deeply_nested_json_does_not_crash(client: httpx.AsyncClient, monkeypatch):
    """A JSON array nested tens of thousands of levels deep blows Python's
    json decoder's own recursion limit (RecursionError), well before
    SCIM_WEBHOOK_MAX_BODY_BYTES kicks in on size alone -- this must come
    back as a clean 400, not an unhandled exception."""
    _enable_scim(monkeypatch)
    body = (b"[" * 50_000) + (b"]" * 50_000)
    resp = await _post_signed(client, body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_payload"


# ── Category B: `data` field has the wrong shape ────────────────────────────
@pytest.mark.parametrize(
    "data_value",
    [["not", "a", "dict"], "a string", 42, None, True, {"nested": {"deeply": {"unexpected": [1, 2, 3]}}}],
    ids=["list", "string", "number", "null", "bool", "deeply_nested_but_no_id"],
)
async def test_data_field_wrong_shape_fails_closed_not_crash(
    client: httpx.AsyncClient, monkeypatch, data_value
):
    _enable_scim(monkeypatch)
    payload = {"id": "event_1", "event": "dsync.user.created", "data": data_value}
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    # Either normalized to "missing id" (400) or, for the dict case with no
    # usable id, also 400 -- never a 500.
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_payload"


async def test_missing_data_field_entirely(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    payload = {"id": "event_1", "event": "dsync.user.created"}
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    assert resp.status_code == 400, resp.text


async def test_missing_event_field_is_ignored_not_crashed(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    payload = {"id": "event_1", "data": {"id": "du_x", "emails": [{"primary": True, "value": "x@example.com"}]}}
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    assert resp.status_code == 200, resp.text  # falls into the "unhandled event" branch


async def test_non_string_event_field_is_ignored_not_crashed(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    payload = {
        "id": "event_1",
        "event": ["dsync.user.created", "extra"],
        "data": {"id": "du_x", "emails": [{"primary": True, "value": "x@example.com"}]},
    }
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    assert resp.status_code == 200, resp.text
    assert await _account_count("du_x") == 0  # never matched a handled event type


# ── Category C: `id` field edge cases ───────────────────────────────────────
@pytest.mark.parametrize(
    "id_value",
    [None, "", 12345, ["du_1"], {"id": "du_1"}, True, "x" * 500],
    ids=["missing", "empty_string", "number", "list", "dict", "bool", "too_long"],
)
async def test_malformed_directory_user_id_rejected_not_crashed(
    client: httpx.AsyncClient, monkeypatch, id_value
):
    _enable_scim(monkeypatch)
    data: dict = {"emails": [{"primary": True, "value": "someone@example.com"}]}
    if id_value is not None:
        data["id"] = id_value
    payload = {"id": "event_1", "event": "dsync.user.created", "data": data}
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_payload"


# ── Category D: `emails` field edge cases ───────────────────────────────────
@pytest.mark.parametrize(
    "emails_value",
    [
        None,
        "not-a-list@example.com",
        42,
        {"value": "x@example.com"},
        [],
        ["just", "strings", "not", "objects"],
        [None, 42, True],
        [{"primary": True}],  # no `value` key
        [{"primary": True, "value": None}],
        [{"primary": True, "value": 12345}],
        [{"primary": True, "value": ["nested", "list"]}],
        [{"primary": True, "value": ""}],
        [{"primary": True, "value": "not-an-email-at-all"}],
        [{"primary": True, "value": "attacker\x00@example.com"}],
        [{"primary": True, "value": "'; DROP TABLE accounts; --@example.com"}],
        [{"primary": True, "value": "x" * 300 + "@example.com"}],
    ],
    ids=[
        "null",
        "string_not_list",
        "number",
        "dict_not_list",
        "empty_list",
        "list_of_strings",
        "list_of_scalars",
        "no_value_key",
        "null_value",
        "numeric_value",
        "nested_list_value",
        "empty_value",
        "not_email_shaped",
        "null_byte",
        "sql_injection_shaped",
        "oversized",
    ],
)
async def test_malformed_emails_rejected_not_crashed(client: httpx.AsyncClient, monkeypatch, emails_value):
    _enable_scim(monkeypatch)
    directory_user_id = "du_email_fuzz"
    data: dict = {"id": directory_user_id}
    if emails_value is not None:
        data["emails"] = emails_value
    payload = {"id": "event_1", "event": "dsync.user.created", "data": data}
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_payload"
    assert await _account_count(directory_user_id) == 0


async def test_unicode_email_and_names_that_are_actually_valid_are_accepted(
    client: httpx.AsyncClient, monkeypatch
):
    """Unicode isn't inherently malformed -- a genuinely valid internationalized
    address/name should still provision normally, and injection-shaped
    `first_name`/`username` values (fields the handler never reads) must
    not affect behavior at all."""
    _enable_scim(monkeypatch)
    directory_user_id = "du_unicode_ok"
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {
            "id": directory_user_id,
            "emails": [{"primary": True, "value": "josé@example.com"}],
            "first_name": "<script>alert(1)</script>",
            "last_name": "'; DROP TABLE accounts; --",
            "username": "unicode-username-name",
            "organization_id": "org_1",
            "state": "active",
        },
    }
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    assert resp.status_code == 200, resp.text
    assert await _account_count(directory_user_id) == 1


# ── Category E: `organization_id` edge cases ────────────────────────────────
@pytest.mark.parametrize(
    "org_value",
    [12345, ["org_1"], {"id": "org_1"}, True, "o" * 500],
    ids=["number", "list", "dict", "bool", "too_long"],
)
async def test_malformed_organization_id_is_dropped_not_crashed(
    client: httpx.AsyncClient, monkeypatch, org_value
):
    _enable_scim(monkeypatch)
    directory_user_id = "du_org_fuzz"
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {
            "id": directory_user_id,
            "emails": [{"primary": True, "value": "orgfuzz@example.com"}],
            "organization_id": org_value,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    # A bad organization_id is not fatal to provisioning -- it's just
    # dropped (None), never written verbatim, never crashes the request.
    assert resp.status_code == 200, resp.text

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_scim_directory_user_id(directory_user_id)
        assert account is not None
        assert account.sso_organization_id is None


# ── Category F: `state` edge cases ──────────────────────────────────────────
@pytest.mark.parametrize(
    "state_value",
    [12345, ["active"], {"state": "active"}, True],
    ids=["number", "list", "dict", "bool"],
)
async def test_malformed_state_is_ignored_not_crashed(client: httpx.AsyncClient, monkeypatch, state_value):
    """`state in _DEACTIVATED_STATES` is a frozenset membership test -- an
    unhashable `state` (a list/dict) used to raise TypeError before the
    isinstance guard was added. A non-string state must be treated as "no
    transition", not crash and not deactivate/reactivate anything."""
    _enable_scim(monkeypatch)
    directory_user_id = "du_state_fuzz"
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {
            "id": directory_user_id,
            "emails": [{"primary": True, "value": "statefuzz@example.com"}],
            "state": state_value,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    assert resp.status_code == 200, resp.text

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_scim_directory_user_id(directory_user_id)
        assert account is not None
        assert account.scim_deactivated_at is None


async def test_unrecognized_state_string_is_a_safe_noop(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    directory_user_id = "du_weird_state"
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {
            "id": directory_user_id,
            "emails": [{"primary": True, "value": "weirdstate@example.com"}],
            "state": "banned",  # not a real WorkOS state value
        },
    }
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    assert resp.status_code == 200, resp.text

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_scim_directory_user_id(directory_user_id)
        assert account is not None
        assert account.scim_deactivated_at is None


# ── Category G: oversized payloads ──────────────────────────────────────────
async def test_oversized_body_rejected_with_413_before_processing(
    client: httpx.AsyncClient, monkeypatch
):
    _enable_scim(monkeypatch)
    directory_user_id = "du_oversized"
    filler = "a" * (settings.SCIM_WEBHOOK_MAX_BODY_BYTES + 1024)
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {
            "id": directory_user_id,
            "emails": [{"primary": True, "value": "oversized@example.com"}],
            "custom_attributes": {"filler": filler},
        },
    }
    body = json.dumps(payload).encode("utf-8")
    assert len(body) > settings.SCIM_WEBHOOK_MAX_BODY_BYTES
    resp = await _post_signed(client, body)
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert await _account_count(directory_user_id) == 0


async def test_body_at_the_limit_is_still_processed_normally(client: httpx.AsyncClient, monkeypatch):
    """Sanity check for the boundary itself -- a real (if unusually large)
    delivery just under the cap must not be collateral damage from the
    oversized-payload guard."""
    _enable_scim(monkeypatch)
    directory_user_id = "du_at_limit"
    base_payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {"id": directory_user_id, "emails": [{"primary": True, "value": "atlimit@example.com"}]},
    }
    base_len = len(json.dumps(base_payload).encode("utf-8"))
    padding_len = max(0, settings.SCIM_WEBHOOK_MAX_BODY_BYTES - base_len - 40)
    base_payload["data"]["custom_attributes"] = {"padding": "b" * padding_len}
    body = json.dumps(base_payload).encode("utf-8")
    assert len(body) <= settings.SCIM_WEBHOOK_MAX_BODY_BYTES
    resp = await _post_signed(client, body)
    assert resp.status_code == 200, resp.text
    assert await _account_count(directory_user_id) == 1


# ── Category H: conflicting/duplicate operations, no batching support ──────
async def test_data_as_array_of_events_mimicking_a_batch_is_safely_rejected(
    client: httpx.AsyncClient, monkeypatch
):
    """This webhook's contract (docs/ENTERPRISE-SSO-DESIGN.md's SCIM
    section) is one event per delivery -- WorkOS does not batch multiple
    dsync events into a single `data` array. A caller (buggy proxy,
    malicious replay, or a future WorkOS behavior change) sending `data` as
    an array of multiple Directory User objects must be rejected cleanly,
    not partially processed (e.g. only acting on the first entry, or
    crashing while iterating)."""
    _enable_scim(monkeypatch)
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": [
            {"id": "du_batch_1", "emails": [{"primary": True, "value": "batch1@example.com"}]},
            {"id": "du_batch_2", "emails": [{"primary": True, "value": "batch2@example.com"}]},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    assert resp.status_code == 400, resp.text
    assert await _account_count("du_batch_1") == 0
    assert await _account_count("du_batch_2") == 0


async def test_rapid_conflicting_state_updates_last_write_wins_no_corruption(
    client: httpx.AsyncClient, monkeypatch
):
    """Two back-to-back deliveries for the same directory_user_id disagree
    on state (active vs. suspended) and email -- exercises the upsert path
    under conflicting input without any batching; the end state must
    reflect the LAST delivered event cleanly, with exactly one account
    row, not a corrupted mix of both."""
    _enable_scim(monkeypatch)
    directory_user_id = "du_conflict"

    def _event(*, email: str, state: str, org: str) -> bytes:
        payload = {
            "id": "event_x",
            "event": "dsync.user.updated",
            "data": {
                "id": directory_user_id,
                "emails": [{"primary": True, "value": email}],
                "state": state,
                "organization_id": org,
            },
        }
        return json.dumps(payload).encode("utf-8")

    created = _event(email="conflict@example.com", state="active", org="org_a")
    await _post_signed(client, created)

    conflicting_a = _event(email="conflict-updated@example.com", state="suspended", org="org_b")
    conflicting_b = _event(email="conflict-final@example.com", state="active", org="org_c")

    resp_a = await _post_signed(client, conflicting_a)
    resp_b = await _post_signed(client, conflicting_b)
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text

    assert await _account_count(directory_user_id) == 1
    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_scim_directory_user_id(directory_user_id)
        assert account is not None
        assert account.email == "conflict-final@example.com"
        assert account.sso_organization_id == "org_c"
        assert account.scim_deactivated_at is None  # last event reactivated it


async def test_malformed_event_leaves_no_partial_account_state(client: httpx.AsyncClient, monkeypatch):
    """A well-formed `id` with a broken `emails` field must not create a
    half-provisioned account -- the whole event is rejected atomically."""
    _enable_scim(monkeypatch)
    directory_user_id = "du_partial_check"
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {"id": directory_user_id, "emails": "not-a-list"},
    }
    body = json.dumps(payload).encode("utf-8")
    resp = await _post_signed(client, body)
    assert resp.status_code == 400, resp.text
    assert await _account_count(directory_user_id) == 0


async def test_high_volume_redelivery_stays_idempotent(client: httpx.AsyncClient, monkeypatch):
    """Many redeliveries of the exact same well-formed event in a row --
    still exactly one account, never a crash, never a duplicate. Bumps the
    rate-limit bucket since this alone would otherwise trip the default
    per-minute cap within a single test."""
    _enable_scim(monkeypatch)
    monkeypatch.setattr(settings, "BOXKITE_SCIM_WEBHOOK_RATE_LIMIT_PER_MINUTE", 1000)
    directory_user_id = "du_redelivery_storm"
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {"id": directory_user_id, "emails": [{"primary": True, "value": "storm@example.com"}]},
    }
    body = json.dumps(payload).encode("utf-8")
    for _ in range(25):
        resp = await _post_scim_webhook(
            client, body, _signature_header(SCIM_SECRET, body, timestamp_ms=int(time.time() * 1000))
        )
        assert resp.status_code == 200, resp.text
    assert await _account_count(directory_user_id) == 1
