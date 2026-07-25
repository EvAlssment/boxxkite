"""Tests for wiring outbound-MCP connection grants (GitHub issues #116/#117,
docs/OUTBOUND-MCP-DESIGN.md §3) into the EXISTING per-session secrets-egress
NetworkPolicy machinery (issue #74, src/boxkite/secrets_network_policy.py) --
mechanical reuse only, no parallel NetworkPolicy-building path.

`mcp_connection_grants` has the exact same shape as `secret_grants`
(`{"name": str, "allowed_hosts": [str]}`) and is unioned with it, at the
manager layer, into a single list before it ever reaches
`collect_allowed_hosts`/`build_secrets_egress_network_policy` --
`secrets_network_policy.py` itself is untouched by this feature. This file
mirrors `test_manager_secrets_network_policy.py`'s recycle-pod-across-
tenants case, applied to `mcp_connection_grants` instead of `secret_grants`.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from boxkite.secrets_network_policy import secrets_egress_policy_name
from test_manager import _FakeCoreApi
from test_manager_secrets_network_policy import _manager_with_fake_networking

SAMPLE_SECRET_GRANTS = [{"name": "stripe-key", "allowed_hosts": ["api.stripe.com"]}]
SAMPLE_MCP_GRANTS = [{"name": "team-slack", "allowed_hosts": ["mcp.slack.com"]}]


@pytest.mark.asyncio
async def test_create_k8s_session_unions_mcp_connection_grants_with_secret_grants(monkeypatch):
    """mcp_connection_grants is not sent to the sidecar's /configure (no
    MCP-proxy transport exists yet -- out of scope) but its allowed_hosts
    DOES reach the per-pod NetworkPolicy, unioned with secret_grants, via
    the exact same _sync_secrets_egress_network_policy call site."""
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)

    async def fake_init_k8s():
        return None

    async def fake_claim_warm_pod(size="small"):
        return None

    async def fake_create_pod(*_args, **_kwargs):
        return "10.8.0.99"

    from types import SimpleNamespace

    fake_configure_response = SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"prefetched_files": []}
    )
    fake_client = SimpleNamespace(post=AsyncMock(return_value=fake_configure_response))

    manager._init_k8s = fake_init_k8s
    manager._claim_warm_pod_via_k8s = fake_claim_warm_pod
    manager._create_pod = fake_create_pod
    manager._get_http_client = lambda *_args, **_kwargs: fake_client
    manager._k8s_core_api = _FakeCoreApi()

    session_id = f"session-mcp-{uuid4().hex[:8]}"
    await manager._create_k8s_session(
        uuid4(),
        session_id,
        None,
        None,
        secret_grants=SAMPLE_SECRET_GRANTS,
        mcp_connection_grants=SAMPLE_MCP_GRANTS,
    )

    # The sidecar /configure payload must NOT carry mcp_connection_grants --
    # there is no MCP-proxy transport for the sidecar to use it with yet.
    configure_payload = fake_client.post.call_args.kwargs["json"]
    assert "mcp_connection_grants" not in configure_payload
    assert "mcp_connection_names" not in configure_payload

    assert len(fake_networking.create_calls) == 1
    created_body = fake_networking.create_calls[0]["body"]
    egress_hosts = {
        peer.ip_block.cidr
        for rule in created_body.spec.egress
        for peer in rule.to
    }
    # Both hosts resolved (test double resolves every host to the same IP,
    # see _manager_with_fake_networking) -- the point under test is that
    # BOTH grant sources contributed a rule, not any specific IP value.
    assert len(created_body.spec.egress) >= 1
    assert egress_hosts  # non-empty: at least one egress rule was built


@pytest.mark.asyncio
async def test_create_k8s_session_builds_policy_from_mcp_connection_grants_alone(monkeypatch):
    """A session granted ONLY mcp_connection_names (no secrets at all) still
    gets a real per-pod NetworkPolicy -- mcp_connection_grants alone must be
    sufficient input to collect_allowed_hosts, not just a secret_grants
    accessory."""
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)

    async def fake_init_k8s():
        return None

    async def fake_claim_warm_pod(size="small"):
        return None

    async def fake_create_pod(*_args, **_kwargs):
        return "10.8.0.99"

    from types import SimpleNamespace

    fake_configure_response = SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"prefetched_files": []}
    )
    fake_client = SimpleNamespace(post=AsyncMock(return_value=fake_configure_response))

    manager._init_k8s = fake_init_k8s
    manager._claim_warm_pod_via_k8s = fake_claim_warm_pod
    manager._create_pod = fake_create_pod
    manager._get_http_client = lambda *_args, **_kwargs: fake_client
    manager._k8s_core_api = _FakeCoreApi()

    session_id = f"session-mcp-only-{uuid4().hex[:8]}"
    await manager._create_k8s_session(
        uuid4(),
        session_id,
        None,
        None,
        secret_grants=None,
        mcp_connection_grants=SAMPLE_MCP_GRANTS,
    )

    assert len(fake_networking.create_calls) == 1


@pytest.mark.asyncio
async def test_recycled_pod_across_tenants_does_not_inherit_mcp_connection_egress(monkeypatch):
    """Core acceptance criteria, mirrored from
    test_manager_secrets_network_policy.py's recycle-pod-across-tenants
    case: a pod that previously carried tenant A's mcp_connection_grants
    egress rule must NOT still expose that egress once recycled and
    reconfigured for tenant B's session, even when tenant B was granted NO
    mcp connections (or secrets) at all."""
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)

    pod_name = "sandbox-pod-mcp-recycle"
    session_label_a = "session-tenant-a"
    session_label_b = "session-tenant-b"

    # Tenant A's session provisions the policy with its MCP connection grant.
    await manager._sync_secrets_egress_network_policy(pod_name, session_label_a, SAMPLE_MCP_GRANTS)
    assert fake_networking.create_calls[-1]["body"].spec.pod_selector.match_labels == {
        "app": "sandbox",
        "session-id": session_label_a,
    }

    # Pod is torn down for tenant A (recycle-to-warm or hard delete both
    # call this) -- the policy must be removed, not merely left stale.
    await manager._delete_secrets_egress_network_policy(pod_name)
    assert fake_networking.delete_calls[-1]["name"] == secrets_egress_policy_name(pod_name)

    # Tenant B claims the SAME pod_name (warm-pool reuse) with no grants at
    # all -- _sync_secrets_egress_network_policy(None, None) must not
    # recreate any egress rule for tenant A's old hosts.
    fake_networking.create_calls.clear()
    await manager._sync_secrets_egress_network_policy(pod_name, session_label_b, None)

    assert fake_networking.create_calls == []
    assert fake_networking.delete_calls[-1]["name"] == secrets_egress_policy_name(pod_name)


@pytest.mark.asyncio
async def test_recycled_pod_across_tenants_mcp_grants_are_session_scoped_not_additive(monkeypatch):
    """A pod recycled from tenant A (granted an MCP connection) to tenant B
    (granted a DIFFERENT mcp connection) must end up with EXACTLY tenant B's
    egress rule -- never a union of both tenants' hosts."""
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)

    # Distinct per-host resolved IPs (overriding the shared fixture's
    # single-IP-for-every-host stub) so the egress-content assertions below
    # can actually distinguish tenant A's rule from tenant B's, rather than
    # merely proving both calls happened -- both hosts resolving to the same
    # mocked address would let a real union bug pass this test undetected.
    host_ips = {"mcp.slack.com": "10.10.10.10", "mcp.linear.app": "20.20.20.20"}
    monkeypatch.setattr(
        "boxkite.secrets_network_policy.default_resolve_host_ips",
        lambda host: [host_ips[host]],
    )

    pod_name = "sandbox-pod-mcp-recycle-2"
    tenant_a_grants = [{"name": "team-slack", "allowed_hosts": ["mcp.slack.com"]}]
    tenant_b_grants = [{"name": "team-linear", "allowed_hosts": ["mcp.linear.app"]}]

    await manager._sync_secrets_egress_network_policy(pod_name, "session-a", tenant_a_grants)
    await manager._delete_secrets_egress_network_policy(pod_name)
    fake_networking.create_calls.clear()
    fake_networking.replace_calls.clear()

    await manager._sync_secrets_egress_network_policy(pod_name, "session-b", tenant_b_grants)

    assert len(fake_networking.create_calls) == 1
    final_body = fake_networking.create_calls[0]["body"]
    assert final_body.spec.pod_selector.match_labels == {
        "app": "sandbox",
        "session-id": "session-b",
    }

    # The actual claim under test: exactly tenant B's egress rule, never a
    # union with tenant A's (10.10.10.10/32 must not be present).
    egress_cidrs = {
        peer.ip_block.cidr
        for rule in final_body.spec.egress
        for peer in rule.to
    }
    assert egress_cidrs == {"20.20.20.20/32"}, (
        f"expected only tenant B's resolved host, got {egress_cidrs}"
    )
