"""Tests for src/boxkite/capability_policy.py -- the phase-1, non-wired
`SessionCapabilityPolicy` stub (docs/UNIFIED-CAPABILITY-POLICY-SCOPING.md,
GitHub issue #155).

This covers only the construction logic: given the three existing
sources' real shapes (the `allowed_commands` shape `command_whitelist.py`
accepts, and the `{"name", "allowed_hosts"}` grant shape
`usage_policy.py`'s `_resolve_secret_grants`/`_resolve_mcp_connection_grants`
already produce), does `build_session_capability_policy` assemble the
expected object -- including the None-means-unrestricted and
per-grant-kind distinctions the scoping doc's §3/§4 call out as
load-bearing, not incidental.

Also covers `assert_policy_invariants` and the construction path's
concurrency/isolation guarantees. Nothing here calls into `manager.py` or
any real request-handling code directly -- `usage_policy.py`'s
`create_session` does call this module's construction functions (see its
own docstring), but that's exercised by control-plane's own test suite,
not here; this file stays a pure unit-test of `capability_policy.py`
itself.
"""

from __future__ import annotations

import copy

import pytest

from boxkite.capability_policy import (
    CommandRule,
    ExecPolicy,
    NetworkGrant,
    SessionCapabilityPolicy,
    assert_policy_invariants,
    build_session_capability_policy,
    command_rules_from_allowed_commands,
    network_grant_from_mcp_connection,
    network_grant_from_secret,
)

pytestmark = pytest.mark.pr


class TestCommandRulesFromAllowedCommands:
    def test_none_means_unrestricted(self):
        policy = command_rules_from_allowed_commands(None)

        assert policy == ExecPolicy(rules=None)

    def test_empty_list_means_unrestricted(self):
        policy = command_rules_from_allowed_commands([])

        assert policy == ExecPolicy(rules=None)

    def test_bare_string_entries_become_unconstrained_command_rules(self):
        policy = command_rules_from_allowed_commands(["git", "ls"])

        assert policy.rules == (
            CommandRule(command="git"),
            CommandRule(command="ls"),
        )

    def test_dict_entries_carry_args_allow_and_args_deny(self):
        policy = command_rules_from_allowed_commands(
            [
                {
                    "command": "git",
                    "args_allow": ["^status$", "^log.*"],
                    "args_deny": ["^push.*"],
                }
            ]
        )

        assert policy.rules == (
            CommandRule(
                command="git",
                args_allow=("^status$", "^log.*"),
                args_deny=("^push.*",),
            ),
        )

    def test_dict_entry_without_args_allow_or_args_deny_defaults_to_empty(self):
        policy = command_rules_from_allowed_commands([{"command": "ls"}])

        assert policy.rules == (CommandRule(command="ls", args_allow=(), args_deny=()),)

    def test_mixed_string_and_dict_entries(self):
        policy = command_rules_from_allowed_commands(
            ["ls", {"command": "git", "args_deny": ["^push.*"]}]
        )

        assert policy.rules == (
            CommandRule(command="ls"),
            CommandRule(command="git", args_deny=("^push.*",)),
        )

    def test_unsupported_entry_type_raises(self):
        with pytest.raises(TypeError):
            command_rules_from_allowed_commands([123])


class TestNetworkGrantConstructors:
    def test_network_grant_from_secret_has_secret_kind_and_no_token(self):
        grant = network_grant_from_secret("stripe-key", ["api.stripe.com"])

        assert grant == NetworkGrant(
            name="stripe-key",
            allowed_hosts=("api.stripe.com",),
            kind="secret",
            capability_token=None,
        )

    def test_network_grant_from_mcp_connection_has_mcp_kind_single_host(self):
        grant = network_grant_from_mcp_connection("linear", "mcp.linear.app")

        assert grant == NetworkGrant(
            name="linear",
            allowed_hosts=("mcp.linear.app",),
            kind="mcp_connection",
            capability_token=None,
        )


class TestBuildSessionCapabilityPolicy:
    def test_empty_inputs_produce_unrestricted_exec_and_no_network_grants(self):
        policy = build_session_capability_policy(
            account_id="acct_1", session_id="sess_1"
        )

        assert policy.account_id == "acct_1"
        assert policy.session_id == "sess_1"
        assert policy.exec == ExecPolicy(rules=None)
        assert policy.network_grants == ()
        assert policy.all_allowed_hosts() == ()

    def test_assembles_command_allowlist_from_existing_shape(self):
        policy = build_session_capability_policy(
            account_id="acct_1",
            session_id="sess_1",
            allowed_commands=["git", {"command": "ls", "args_deny": ["-r"]}],
        )

        assert policy.exec.rules == (
            CommandRule(command="git"),
            CommandRule(command="ls", args_deny=("-r",)),
        )

    def test_assembles_secret_grants_using_shared_capability_token(self):
        policy = build_session_capability_policy(
            account_id="acct_1",
            session_id="sess_1",
            secret_grants=[
                {"name": "stripe-key", "allowed_hosts": ["api.stripe.com"]},
                {"name": "sendgrid-key", "allowed_hosts": ["api.sendgrid.com"]},
            ],
            secret_capability_token="tok_abc123",
        )

        assert policy.network_grants == (
            NetworkGrant(
                name="stripe-key",
                allowed_hosts=("api.stripe.com",),
                kind="secret",
                capability_token="tok_abc123",
            ),
            NetworkGrant(
                name="sendgrid-key",
                allowed_hosts=("api.sendgrid.com",),
                kind="secret",
                capability_token="tok_abc123",
            ),
        )

    def test_assembles_mcp_connection_grants_with_no_capability_token(self):
        policy = build_session_capability_policy(
            account_id="acct_1",
            session_id="sess_1",
            mcp_connection_grants=[
                {"name": "linear", "allowed_hosts": ["mcp.linear.app"]},
            ],
        )

        assert policy.network_grants == (
            NetworkGrant(
                name="linear",
                allowed_hosts=("mcp.linear.app",),
                kind="mcp_connection",
                capability_token=None,
            ),
        )

    def test_secret_and_mcp_grants_are_both_present_and_distinguishable_by_kind(self):
        policy = build_session_capability_policy(
            account_id="acct_1",
            session_id="sess_1",
            secret_grants=[{"name": "stripe-key", "allowed_hosts": ["api.stripe.com"]}],
            mcp_connection_grants=[{"name": "linear", "allowed_hosts": ["mcp.linear.app"]}],
            secret_capability_token="tok_abc123",
        )

        secret_grant = policy.network_grant_by_name("stripe-key")
        mcp_grant = policy.network_grant_by_name("linear")

        assert secret_grant.kind == "secret"
        assert secret_grant.capability_token == "tok_abc123"
        assert mcp_grant.kind == "mcp_connection"
        assert mcp_grant.capability_token is None

    def test_network_grant_by_name_returns_none_for_unknown_name(self):
        policy = build_session_capability_policy(account_id="acct_1", session_id="sess_1")

        assert policy.network_grant_by_name("does-not-exist") is None

    def test_all_allowed_hosts_unions_and_deduplicates_across_grants(self):
        policy = build_session_capability_policy(
            account_id="acct_1",
            session_id="sess_1",
            secret_grants=[
                {"name": "a", "allowed_hosts": ["shared.example.com", "a-only.example.com"]},
            ],
            mcp_connection_grants=[
                {"name": "b", "allowed_hosts": ["shared.example.com"]},
            ],
        )

        assert policy.all_allowed_hosts() == (
            "shared.example.com",
            "a-only.example.com",
        )


class TestSessionCapabilityPolicyIsImmutable:
    def test_dataclasses_are_frozen(self):
        policy = SessionCapabilityPolicy(account_id="acct_1", session_id="sess_1")

        with pytest.raises(Exception):
            policy.account_id = "acct_2"  # type: ignore[misc]

    def test_network_grants_is_a_tuple_not_a_mutable_list(self):
        policy = build_session_capability_policy(
            account_id="acct_1",
            session_id="sess_1",
            secret_grants=[{"name": "a", "allowed_hosts": ["x.example.com"]}],
        )

        assert isinstance(policy.network_grants, tuple)
        assert isinstance(policy.exec, ExecPolicy)


class TestBuildSessionCapabilityPolicyFromLiveShapes:
    def test_build_session_capability_policy_from_live_account_and_grants(self):
        """Realistic shapes: `account.custom_allowed_commands`'s
        list-of-dicts JSON column, and the exact `{"name", "allowed_hosts"}`
        dicts `_resolve_secret_grants`/`_resolve_mcp_connection_grants`
        actually return."""
        custom_allowed_commands = [
            "ls",
            {"command": "git", "args_allow": ["^status$"], "args_deny": ["^push.*"]},
        ]
        secret_grants = [
            {"name": "stripe-key", "allowed_hosts": ["api.stripe.com"]},
        ]
        mcp_connection_grants = [
            {"name": "linear", "allowed_hosts": ["mcp.linear.app"]},
        ]

        policy = build_session_capability_policy(
            account_id="acct_live",
            session_id="sess_live",
            allowed_commands=custom_allowed_commands,
            secret_grants=secret_grants,
            mcp_connection_grants=mcp_connection_grants,
            secret_capability_token="tok_live",
        )

        assert policy.exec.rules == (
            CommandRule(command="ls"),
            CommandRule(command="git", args_allow=("^status$",), args_deny=("^push.*",)),
        )
        assert policy.network_grants == (
            NetworkGrant(
                name="stripe-key",
                allowed_hosts=("api.stripe.com",),
                kind="secret",
                capability_token="tok_live",
            ),
            NetworkGrant(
                name="linear",
                allowed_hosts=("mcp.linear.app",),
                kind="mcp_connection",
                capability_token=None,
            ),
        )
        assert policy.network_grant_by_name("stripe-key").kind == "secret"
        assert policy.network_grant_by_name("linear").kind == "mcp_connection"
        assert policy.all_allowed_hosts() == ("api.stripe.com", "mcp.linear.app")

    def test_concurrent_create_session_calls_never_share_grants(self):
        """Two independent `build_session_capability_policy` calls, made
        back-to-back in one process (as two concurrent `create_session`
        calls would), must never end up sharing mutable state -- frozen
        dataclasses over tuples should already guarantee this; this test
        pins that guarantee explicitly rather than leaving it implicit."""
        secret_grants_a = [{"name": "a", "allowed_hosts": ["a.example.com"]}]
        secret_grants_b = [{"name": "b", "allowed_hosts": ["b.example.com"]}]

        policy_a = build_session_capability_policy(
            account_id="acct_a", session_id="sess_a", secret_grants=secret_grants_a
        )
        policy_b = build_session_capability_policy(
            account_id="acct_b", session_id="sess_b", secret_grants=secret_grants_b
        )

        assert policy_a.network_grants is not policy_b.network_grants
        assert policy_a.network_grants[0] is not policy_b.network_grants[0]

        original_a = copy.deepcopy(policy_a)
        secret_grants_a.append({"name": "injected", "allowed_hosts": ["evil.example.com"]})
        secret_grants_a[0]["allowed_hosts"].append("also-injected.example.com")

        assert policy_a == original_a
        assert policy_a.network_grant_by_name("injected") is None

    def test_all_allowed_hosts_is_never_a_substitute_for_per_grant_check(self):
        """A narrow secret grant and a wider, overlapping-but-not-identical
        mcp_connection grant: `all_allowed_hosts()` unions both, but a
        per-request decision must only ever consult the ONE grant actually
        being used -- pinning why the union method must never be treated
        as a stand-in for `network_grant_by_name(...).allowed_hosts`."""
        policy = build_session_capability_policy(
            account_id="acct_1",
            session_id="sess_1",
            secret_grants=[
                {"name": "the-secret", "allowed_hosts": ["api.narrow.example.com"]}
            ],
            mcp_connection_grants=[
                {
                    "name": "the-mcp-connection",
                    "allowed_hosts": ["api.narrow.example.com", "wide.example.com"],
                }
            ],
        )

        secret_hosts = policy.network_grant_by_name("the-secret").allowed_hosts
        union_hosts = policy.all_allowed_hosts()

        assert secret_hosts == ("api.narrow.example.com",)
        assert union_hosts == ("api.narrow.example.com", "wide.example.com")
        assert secret_hosts != union_hosts
        assert "wide.example.com" not in secret_hosts


class TestAssertPolicyInvariants:
    def test_valid_policy_does_not_raise(self):
        policy = build_session_capability_policy(
            account_id="acct_1",
            session_id="sess_1",
            secret_grants=[{"name": "a", "allowed_hosts": ["a.example.com"]}],
            mcp_connection_grants=[{"name": "b", "allowed_hosts": ["b.example.com"]}],
            secret_capability_token="tok",
        )

        assert_policy_invariants(policy)

    def test_assert_policy_invariants_rejects_mcp_grant_with_capability_token(self):
        broken = SessionCapabilityPolicy(
            account_id="acct_1",
            session_id="sess_1",
            network_grants=(
                NetworkGrant(
                    name="linear",
                    allowed_hosts=("mcp.linear.app",),
                    kind="mcp_connection",
                    capability_token="should-not-exist",
                ),
            ),
        )

        with pytest.raises(ValueError, match="capability_token"):
            assert_policy_invariants(broken)

    def test_assert_policy_invariants_rejects_name_kind_conflict(self):
        broken = SessionCapabilityPolicy(
            account_id="acct_1",
            session_id="sess_1",
            network_grants=(
                NetworkGrant(
                    name="shared-name",
                    allowed_hosts=("api.example.com",),
                    kind="secret",
                    capability_token="tok",
                ),
                NetworkGrant(
                    name="shared-name",
                    allowed_hosts=("mcp.example.com",),
                    kind="mcp_connection",
                ),
            ),
        )

        with pytest.raises(ValueError, match="two different"):
            assert_policy_invariants(broken)
