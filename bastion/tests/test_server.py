from __future__ import annotations

import asyncssh

from boxkite_bastion.config import BastionSettings
from boxkite_bastion.server import run_bastion


async def test_run_bastion_starts_listening_with_ephemeral_host_key():
    """No `host_key_path` set -- run_bastion must generate an ephemeral key
    and actually bind a listening socket rather than raising."""
    settings = BastionSettings(control_plane_ws_base_url="wss://control-plane.invalid", listen_host="127.0.0.1", listen_port=0)
    acceptor = await run_bastion(settings)
    try:
        assert acceptor.sockets[0].getsockname()[1] > 0
    finally:
        acceptor.close()
        await acceptor.wait_closed()


async def test_run_bastion_disables_agent_and_x11_forwarding_and_sets_login_timeout(monkeypatch):
    """Security review follow-up (issue #134): minimal runtime privilege
    means no SSH-agent/X11 forwarding, and a bounded login window backs
    the per-host connection limiter."""
    captured: dict = {}
    real_create_server = asyncssh.create_server

    async def _spy_create_server(*args, **kwargs):
        captured.update(kwargs)
        return await real_create_server(*args, **kwargs)

    monkeypatch.setattr(asyncssh, "create_server", _spy_create_server)

    settings = BastionSettings(
        control_plane_ws_base_url="wss://control-plane.invalid",
        listen_host="127.0.0.1",
        listen_port=0,
        login_timeout_seconds=12.5,
    )
    acceptor = await run_bastion(settings)
    try:
        assert captured["agent_forwarding"] is False
        assert captured["x11_forwarding"] is False
        assert captured["login_timeout"] == 12.5
    finally:
        acceptor.close()
        await acceptor.wait_closed()


async def test_run_bastion_uses_configured_host_key_path(tmp_path):
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519")
    key_path = tmp_path / "host_key"
    key_path.write_bytes(key.export_private_key())

    settings = BastionSettings(
        control_plane_ws_base_url="wss://control-plane.invalid",
        listen_host="127.0.0.1",
        listen_port=0,
        host_key_path=str(key_path),
    )
    acceptor = await run_bastion(settings)
    try:
        assert acceptor.sockets[0].getsockname()[1] > 0
    finally:
        acceptor.close()
        await acceptor.wait_closed()
