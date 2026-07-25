from __future__ import annotations

import pytest

from boxkite_handoff import cli
from boxkite_handoff.adapters import ADAPTERS
from boxkite_handoff.core import Credential, HandoffError, LocatedSession
from boxkite_client.exceptions import BoxkiteConnectionError


class FakeAdapter:
    name = "fake-tool"

    def __init__(self, *, raise_error: bool = False) -> None:
        self.raise_error = raise_error
        self.locate_calls: list[str | None] = []

    def locate_session(self, *, session_ref=None):
        self.locate_calls.append(session_ref)
        if self.raise_error:
            raise HandoffError("no local session found")
        return LocatedSession(
            tool=self.name,
            session_id="s1",
            files=(),
            credential=Credential(env_var="TOOL_TOKEN", value="tok"),
            resume_command="fake-tool --resume s1",
            workdir="/workspace",
        )


@pytest.fixture(autouse=True)
def registered_fake_adapter():
    ADAPTERS["fake-tool"] = FakeAdapter
    yield
    ADAPTERS.pop("fake-tool", None)


def test_main_requires_api_key(monkeypatch, capsys) -> None:
    monkeypatch.delenv("BOXKITE_API_KEY", raising=False)
    monkeypatch.delenv("BOXKITE_BASE_URL", raising=False)

    exit_code = cli.main(["fake-tool", "--base-url", "https://example.test"])

    assert exit_code == 2
    assert "BOXKITE_API_KEY" in capsys.readouterr().err


def test_main_requires_base_url(monkeypatch, capsys) -> None:
    monkeypatch.delenv("BOXKITE_BASE_URL", raising=False)

    exit_code = cli.main(["fake-tool", "--api-key", "key123"])

    assert exit_code == 2
    assert "BOXKITE_BASE_URL" in capsys.readouterr().err


def test_main_reports_handoff_error_from_adapter_without_crashing(capsys) -> None:
    ADAPTERS["fake-tool"] = lambda: FakeAdapter(raise_error=True)

    exit_code = cli.main(
        ["fake-tool", "--api-key", "key123", "--base-url", "https://example.test"]
    )

    assert exit_code == 1
    assert "no local session found" in capsys.readouterr().err


def test_main_reports_boxkite_client_errors_without_a_raw_traceback(monkeypatch, capsys) -> None:
    """create_handoff_sandbox talks to a real BoxkiteClient, which can raise
    its own exception hierarchy (bad api key, unreachable base_url, a 4xx/5xx
    from the control plane) -- these must be caught and reported the same
    clean way as an adapter's own HandoffError, not left to propagate as an
    unhandled traceback."""

    def _boom(*_args, **_kwargs):
        raise BoxkiteConnectionError("could not connect")

    monkeypatch.setattr(cli, "create_handoff_sandbox", _boom)

    exit_code = cli.main(
        ["fake-tool", "--api-key", "key123", "--base-url", "https://example.test"]
    )

    assert exit_code == 1
    assert "could not connect" in capsys.readouterr().err


def test_main_rejects_unknown_tool(capsys) -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["not-a-real-tool", "--api-key", "k", "--base-url", "https://example.test"])
