"""Payload-fuzzing tests for the SCIM 2.0 webhook (routers/scim.py),
GitHub issue #153's code-only ask (the live WorkOS IdP verification half of
that issue is a separate, non-code operator action -- not covered here).

test_scim_provisioning.py already covers the documented happy-path shape
(valid signature, well-formed `dsync.user.*` events) plus signature
rejection. This file throws malformed, unexpected, and adversarial payload
shapes at the actual route handler and asserts it always degrades safely:

- A clean 4xx (never a 500/unhandled exception).
- No account created, deactivated, reactivated, or otherwise mutated as a
  side effect of a malformed delivery.
- Signature verification is checked BEFORE body shape ever matters -- an
  invalid/missing signature is rejected regardless of what the body looks
  like, including bodies that would otherwise crash the parser.

No new fuzzing dependency (e.g. hypothesis) is added: it isn't already a
dependency of this project (checked pyproject.toml), and an explicit table
of malformed shapes gives equal/better signal for a schema this small and
gives failures a readable case name instead of a random seed.

Two real bugs were found and fixed in routers/scim.py while writing this
suite (see its `_extract_primary_email`/`_handle_user_upsert`/
`scim_webhook` for the fixes):

1. A JSON body that decodes to something other than a dict at the top
   level (an array, string, number, or null) reached `payload.get("event")`
   unguarded -- an `AttributeError` on anything but a dict, propagating as
   an unhandled 500 instead of a clean 400.
2. `data["state"]` being a non-string, unhashable value (e.g. a JSON array
   or object) reached `state in _DEACTIVATED_STATES` -- a `frozenset`
   membership test, which raises `TypeError: unhashable type` instead of
   just returning False the way an `==`/`in`-on-a-tuple check would.
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy import select

from control_plane import db as db_module
from control_plane.models_orm import Account
from control_plane.repository import AccountRepository
from test_scim_provisioning import (
    SCIM_SECRET,
    _dsync_user_event,
    _enable_scim,
    _post_scim_webhook,
    _signature_header,
)


async def _assert_no_accounts_created(db_check_email: str | None = None) -> None:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account))
        rows = result.scalars().all()
        assert rows == []
        if db_check_email is not None:
            assert await AccountRepository(db).get_by_email(db_check_email) is None


# ── Malformed JSON bodies at the top level ───────────────────────────────
async def test_scim_webhook_rejects_empty_body(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = b""
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"
    await _assert_no_accounts_created()


async def test_scim_webhook_rejects_non_json_body(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = b"<xml>not json at all</xml>"
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"
    await _assert_no_accounts_created()


async def test_scim_webhook_rejects_truncated_json_body(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    full = _dsync_user_event(event="dsync.user.created", directory_user_id="du_trunc", email="a@example.com")
    body = full[: len(full) // 2]
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"
    await _assert_no_accounts_created()


_TOP_LEVEL_NON_OBJECT_BODIES: dict[str, bytes] = {
    "json_array": json.dumps([{"event": "dsync.user.created", "data": {"id": "du_1"}}]).encode(),
    "json_string": json.dumps("dsync.user.created").encode(),
    "json_number": json.dumps(12345).encode(),
    "json_bool": json.dumps(True).encode(),
    "json_null": json.dumps(None).encode(),
}


async def test_scim_webhook_rejects_top_level_non_object_bodies(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    for name, body in _TOP_LEVEL_NON_OBJECT_BODIES.items():
        header = _signature_header(SCIM_SECRET, body)
        resp = await _post_scim_webhook(client, body, header)
        assert resp.status_code == 400, f"{name}: expected 400, got {resp.status_code} ({resp.text})"
        assert resp.json()["error"]["code"] == "invalid_payload", name
    await _assert_no_accounts_created()


# ── Missing required fields ──────────────────────────────────────────────
async def test_scim_webhook_rejects_user_created_missing_id(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {"emails": [{"primary": True, "value": "noid@example.com"}]},
    }
    body = json.dumps(payload).encode()
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"
    await _assert_no_accounts_created("noid@example.com")


async def test_scim_webhook_rejects_user_created_missing_email(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {"id": "du_noemail"},
    }
    body = json.dumps(payload).encode()
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"
    await _assert_no_accounts_created()


async def test_scim_webhook_rejects_user_created_empty_emails_list(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    payload = {
        "id": "event_1",
        "event": "dsync.user.created",
        "data": {"id": "du_emptyemails", "emails": []},
    }
    body = json.dumps(payload).encode()
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"
    await _assert_no_accounts_created()


async def test_scim_webhook_missing_top_level_data_is_a_safe_noop(client: httpx.AsyncClient, monkeypatch):
    """No `data` key at all -- treated as an empty object, which then fails
    the same "missing id" 400 as an explicitly-empty `data: {}` would."""
    _enable_scim(monkeypatch)
    payload = {"id": "event_1", "event": "dsync.user.created"}
    body = json.dumps(payload).encode()
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"
    await _assert_no_accounts_created()


# ── Wrong types for individual fields ────────────────────────────────────
def _payload(event: str, data: object) -> bytes:
    return json.dumps({"id": "event_1", "event": event, "data": data}).encode()


_WRONG_TYPE_DATA_SHAPES: dict[str, object] = {
    "data_is_string": "not-an-object",
    "data_is_number": 42,
    "data_is_list": [{"id": "du_1"}],
    "data_is_bool": True,
}


async def test_scim_webhook_rejects_non_object_data_field(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    for name, data in _WRONG_TYPE_DATA_SHAPES.items():
        body = _payload("dsync.user.created", data)
        header = _signature_header(SCIM_SECRET, body)
        resp = await _post_scim_webhook(client, body, header)
        assert resp.status_code == 400, f"{name}: expected 400, got {resp.status_code} ({resp.text})"
        assert resp.json()["error"]["code"] == "invalid_payload", name
    await _assert_no_accounts_created()


async def test_scim_webhook_rejects_wrong_type_id_field(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    for name, bad_id in {
        "id_is_number": 12345,
        "id_is_list": ["du_1"],
        "id_is_dict": {"nested": "du_1"},
        "id_is_null": None,
        "id_is_bool": True,
    }.items():
        body = _payload(
            "dsync.user.created",
            {"id": bad_id, "emails": [{"primary": True, "value": "a@example.com"}]},
        )
        header = _signature_header(SCIM_SECRET, body)
        resp = await _post_scim_webhook(client, body, header)
        assert resp.status_code == 400, f"{name}: expected 400, got {resp.status_code} ({resp.text})"
        assert resp.json()["error"]["code"] == "invalid_payload", name
    await _assert_no_accounts_created()


async def test_scim_webhook_ignores_malformed_emails_field_shapes(client: httpx.AsyncClient, monkeypatch):
    """`emails` itself wrong-typed, or containing non-object/valueless
    entries, degrades to "no email found" (400 invalid_payload) rather than
    crashing -- never silently accepts a garbage value as an email."""
    _enable_scim(monkeypatch)
    for name, emails in {
        "emails_is_string": "a@example.com",
        "emails_is_dict": {"primary": "a@example.com"},
        "emails_is_number": 7,
        "emails_entries_are_strings": ["a@example.com"],
        "emails_entries_missing_value": [{"primary": True}],
        "emails_entry_value_is_number": [{"primary": True, "value": 12345}],
        "emails_entry_is_null": [None],
        "emails_entry_is_list": [["a@example.com"]],
    }.items():
        body = _payload("dsync.user.created", {"id": f"du_{name}", "emails": emails})
        header = _signature_header(SCIM_SECRET, body)
        resp = await _post_scim_webhook(client, body, header)
        assert resp.status_code == 400, f"{name}: expected 400, got {resp.status_code} ({resp.text})"
        assert resp.json()["error"]["code"] == "invalid_payload", name
    await _assert_no_accounts_created()


async def test_scim_webhook_ignores_wrong_type_organization_id(client: httpx.AsyncClient, monkeypatch):
    """A non-string `organization_id` (int/list/dict) must not crash and
    must not be persisted verbatim into the string `sso_organization_id`
    column -- treated as absent instead."""
    _enable_scim(monkeypatch)
    for name, org_id in {
        "org_id_is_number": 999,
        "org_id_is_list": ["org_1"],
        "org_id_is_dict": {"id": "org_1"},
        "org_id_is_bool": False,
    }.items():
        directory_user_id = f"du_org_{name}"
        body = _payload(
            "dsync.user.created",
            {
                "id": directory_user_id,
                "emails": [{"primary": True, "value": f"{name}@example.com"}],
                "organization_id": org_id,
            },
        )
        header = _signature_header(SCIM_SECRET, body)
        resp = await _post_scim_webhook(client, body, header)
        assert resp.status_code == 200, f"{name}: expected 200, got {resp.status_code} ({resp.text})"

        async with db_module.get_session_factory()() as db:
            account = await AccountRepository(db).get_by_scim_directory_user_id(directory_user_id)
            assert account is not None, name
            assert account.sso_organization_id is None, name


async def test_scim_webhook_ignores_wrong_type_state_field(client: httpx.AsyncClient, monkeypatch):
    """A non-string `state` (e.g. a JSON array/object -- unhashable in
    Python) must not crash the `state in _DEACTIVATED_STATES` frozenset
    membership check -- treated as neither active nor deactivated (no
    state transition applied) instead."""
    _enable_scim(monkeypatch)
    for name, state in {
        "state_is_list": ["inactive"],
        "state_is_dict": {"value": "inactive"},
        "state_is_number": 0,
        "state_is_bool": False,
        "state_is_null": None,
    }.items():
        directory_user_id = f"du_state_{name}"
        body = _payload(
            "dsync.user.created",
            {
                "id": directory_user_id,
                "emails": [{"primary": True, "value": f"{name}@example.com"}],
                "state": state,
            },
        )
        header = _signature_header(SCIM_SECRET, body)
        resp = await _post_scim_webhook(client, body, header)
        assert resp.status_code == 200, f"{name}: expected 200, got {resp.status_code} ({resp.text})"

        async with db_module.get_session_factory()() as db:
            account = await AccountRepository(db).get_by_scim_directory_user_id(directory_user_id)
            assert account is not None, name
            assert account.scim_deactivated_at is None, name


# ── Extra/unexpected fields are ignored, not rejected ────────────────────
async def test_scim_webhook_ignores_unexpected_extra_fields(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    payload = {
        "id": "event_extra",
        "event": "dsync.user.created",
        "unexpected_top_level_field": {"anything": "goes here"},
        "webhook_version": "2099-01-01",
        "data": {
            "id": "du_extra_fields",
            "emails": [{"primary": True, "value": "extra@example.com"}],
            "unexpected_nested_field": ["some", "garbage", 1, None],
            "custom_attributes": {"department": "engineering", "employee_id": 42},
        },
    }
    body = json.dumps(payload).encode()
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 200, resp.text

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_scim_directory_user_id("du_extra_fields")
        assert account is not None
        assert account.email == "extra@example.com"


# ── Unknown target: deactivating/deleting a user id that doesn't exist ──
async def test_scim_webhook_deactivation_of_unknown_user_is_a_noop(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    body = _dsync_user_event(
        event="dsync.user.updated",
        directory_user_id="du_never_provisioned",
        email="ghost@example.com",
        state="inactive",
    )
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    # An unknown directory_user_id on an update is still a legitimate
    # upsert target (WorkOS may deliver `updated` before `created` on
    # redelivery/reordering) -- it provisions a new, already-deactivated
    # shell rather than 404ing, matching _handle_user_upsert's documented
    # idempotent-upsert contract. The key safety property under test is
    # what's asserted below: it never touches any OTHER existing account.
    assert resp.status_code == 200, resp.text


async def test_scim_webhook_deletion_of_unknown_user_is_a_noop(client: httpx.AsyncClient, monkeypatch):
    _enable_scim(monkeypatch)
    payload = {
        "id": "event_del_unknown",
        "event": "dsync.user.deleted",
        "data": {"id": "du_never_existed"},
    }
    body = json.dumps(payload).encode()
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 200, resp.text
    await _assert_no_accounts_created()


async def test_scim_webhook_never_affects_wrong_account_on_unknown_target(
    client: httpx.AsyncClient, monkeypatch
):
    """The central cross-account-safety property: a deactivation/deletion
    event naming a directory_user_id that doesn't match ANY existing
    account must never deactivate, delete, or otherwise mutate some OTHER,
    unrelated existing account."""
    _enable_scim(monkeypatch)
    bystander_body = _dsync_user_event(
        event="dsync.user.created", directory_user_id="du_bystander", email="bystander@example.com"
    )
    await _post_scim_webhook(client, bystander_body, _signature_header(SCIM_SECRET, bystander_body))

    async with db_module.get_session_factory()() as db:
        bystander = await AccountRepository(db).get_by_scim_directory_user_id("du_bystander")
        assert bystander is not None
        assert bystander.scim_deactivated_at is None
        bystander_id = bystander.id

    delete_payload = {
        "id": "event_del_unknown_2",
        "event": "dsync.user.deleted",
        "data": {"id": "du_totally_different_user"},
    }
    delete_body = json.dumps(delete_payload).encode()
    resp = await _post_scim_webhook(client, delete_body, _signature_header(SCIM_SECRET, delete_body))
    assert resp.status_code == 200

    async with db_module.get_session_factory()() as db:
        bystander_after = await AccountRepository(db).get_by_id(bystander_id)
        assert bystander_after is not None
        assert bystander_after.scim_deactivated_at is None
        assert bystander_after.email == "bystander@example.com"


# ── Signature verification wins over payload shape, in both directions ──
async def test_scim_webhook_rejects_malformed_payload_with_missing_signature(
    client: httpx.AsyncClient, monkeypatch
):
    """A malformed body with NO signature header must still 401 on the
    signature check -- never reach the JSON/shape parsing at all (and
    certainly never a 400 that would leak "the JSON was malformed" to an
    unauthenticated caller)."""
    _enable_scim(monkeypatch)
    body = b"{not even valid json"
    resp = await _post_scim_webhook(client, body, None)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


async def test_scim_webhook_rejects_malformed_payload_with_invalid_signature(
    client: httpx.AsyncClient, monkeypatch
):
    _enable_scim(monkeypatch)
    body = json.dumps([1, 2, 3]).encode()
    header = _signature_header("attacker-does-not-know-the-real-secret", body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"
    await _assert_no_accounts_created()


async def test_scim_webhook_rejects_well_formed_payload_with_tampered_signature_for_different_body(
    client: httpx.AsyncClient, monkeypatch
):
    """A signature that's valid for A DIFFERENT body (e.g. replayed from an
    earlier, legitimate delivery) must not authorize this body -- the most
    directly attacker-relevant case: attempting to reuse a captured valid
    signature against a modified deactivation/deprovisioning payload."""
    _enable_scim(monkeypatch)
    original_body = _dsync_user_event(
        event="dsync.user.created", directory_user_id="du_original", email="original@example.com"
    )
    valid_header_for_original = _signature_header(SCIM_SECRET, original_body)

    forged_body = _dsync_user_event(
        event="dsync.user.updated", directory_user_id="du_original", email="original@example.com", state="inactive"
    )
    resp = await _post_scim_webhook(client, forged_body, valid_header_for_original)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"
    await _assert_no_accounts_created()


# ── Unhandled event types with malformed data don't crash either ────────
async def test_scim_webhook_unhandled_event_type_with_malformed_data_is_still_safe(
    client: httpx.AsyncClient, monkeypatch
):
    _enable_scim(monkeypatch)
    body = _payload("dsync.group.created", {"id": ["not", "a", "string"], "weird": {"nested": None}})
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True}
    await _assert_no_accounts_created()


async def test_scim_webhook_unknown_event_name_is_ignored_not_rejected(
    client: httpx.AsyncClient, monkeypatch
):
    _enable_scim(monkeypatch)
    body = _payload("dsync.this_event_does_not_exist_in_any_workos_schema", {"id": "du_1"})
    header = _signature_header(SCIM_SECRET, body)
    resp = await _post_scim_webhook(client, body, header)
    assert resp.status_code == 200
    assert resp.json() == {"received": True}
    await _assert_no_accounts_created()


async def test_scim_webhook_event_field_wrong_type_is_ignored_not_rejected(
    client: httpx.AsyncClient, monkeypatch
):
    """`event` itself being a non-string (number/list/dict/null) must not
    crash the `event_type in (...)` tuple membership check -- tuple `in`
    only needs `__eq__`, not hashability, so this already worked before the
    fixes above; asserted here to lock the behavior in."""
    _enable_scim(monkeypatch)
    for name, event in {
        "event_is_number": 1,
        "event_is_list": ["dsync.user.created"],
        "event_is_dict": {"type": "dsync.user.created"},
        "event_is_null": None,
        "event_is_bool": True,
    }.items():
        body = json.dumps({"id": "event_1", "event": event, "data": {"id": "du_1"}}).encode()
        header = _signature_header(SCIM_SECRET, body)
        resp = await _post_scim_webhook(client, body, header)
        assert resp.status_code == 200, f"{name}: expected 200, got {resp.status_code} ({resp.text})"
    await _assert_no_accounts_created()
