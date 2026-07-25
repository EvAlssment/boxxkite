from __future__ import annotations

import re
from pathlib import Path

import pytest

from boxkite_handoff.core import Credential, LocatedSession, SessionFile
from boxkite_handoff.orchestrator import create_handoff_sandbox


class FakeWebsocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)


class FakeBoxkiteClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.files_created: list[tuple[str, str, str]] = []
        self.takeover_calls: list[str] = []
        self._ws = FakeWebsocket()

    def create_sandbox(self, *, label=None, lifetime_minutes=None, **_kwargs):
        self.created.append({"label": label, "lifetime_minutes": lifetime_minutes})
        return {"session_id": "sandbox-123"}

    def file_create(self, session_id: str, path: str, content: str, **_kwargs) -> dict:
        self.files_created.append((session_id, path, content))
        return {"path": path, "size": len(content), "created": True}

    def takeover(self, session_id: str):
        self.takeover_calls.append(session_id)
        return self._ws


@pytest.fixture
def session_file(tmp_path: Path) -> Path:
    p = tmp_path / "session.jsonl"
    p.write_text('{"role": "user", "content": "hello"}\n', encoding="utf-8")
    return p


@pytest.fixture
def located_session(session_file: Path) -> LocatedSession:
    return LocatedSession(
        tool="claude-code",
        session_id="abc123",
        files=(SessionFile(local_path=session_file, sandbox_path="/workspace/.claude/projects/x/abc123.jsonl"),),
        credential=Credential(env_var="CLAUDE_CODE_OAUTH_TOKEN", value="sk-test-token"),
        resume_command="claude --resume abc123",
        workdir="/workspace/project",
    )


def _credential_file_pushes(client: FakeBoxkiteClient, credential_value: str) -> list[tuple[str, str, str]]:
    return [entry for entry in client.files_created if entry[2] == credential_value]


def test_create_handoff_sandbox_provisions_a_fresh_sandbox(located_session: LocatedSession) -> None:
    client = FakeBoxkiteClient()

    result = create_handoff_sandbox(client, located_session)

    assert result.sandbox_id == "sandbox-123"
    assert len(client.created) == 1


def test_create_handoff_sandbox_pushes_every_session_file(located_session: LocatedSession) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)

    assert (
        "sandbox-123",
        "/workspace/.claude/projects/x/abc123.jsonl",
        '{"role": "user", "content": "hello"}\n',
    ) in client.files_created


def test_create_handoff_sandbox_opens_takeover_for_the_new_sandbox(located_session: LocatedSession) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)

    assert client.takeover_calls == ["sandbox-123"]


def test_create_handoff_sandbox_types_unset_histfile_before_anything_else(located_session: LocatedSession) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)

    assert client._ws.sent[0] == b"unset HISTFILE\n"


def test_create_handoff_sandbox_never_types_the_raw_credential_value(located_session: LocatedSession) -> None:
    """Regression test for a real security-review finding: takeover()
    connects through the control-plane, which durably logs (unredacted)
    every byte typed on that channel to exec_log_entries and any audit-log
    webhook subscriber. The raw credential value must never appear in any
    typed line -- only a reference to a file pushed via file_create, whose
    own audit entry records path only, never content."""
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)

    for line in client._ws.sent:
        assert b"sk-test-token" not in line


def test_create_handoff_sandbox_pushes_the_credential_value_via_file_create_only(
    located_session: LocatedSession,
) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)

    pushes = _credential_file_pushes(client, "sk-test-token")
    assert len(pushes) == 1
    _, credential_path, _ = pushes[0]
    assert credential_path.startswith("/tmp/")


def test_create_handoff_sandbox_types_a_cat_and_rm_referencing_the_pushed_credential_path(
    located_session: LocatedSession,
) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)

    _, credential_path, _ = _credential_file_pushes(client, "sk-test-token")[0]
    quoted_path = f"'{credential_path}'"
    expected = (
        f'export CLAUDE_CODE_OAUTH_TOKEN="$(cat {quoted_path})"; rm -f {quoted_path}\n'
    ).encode("utf-8")
    assert expected in client._ws.sent


def test_create_handoff_sandbox_cds_into_the_session_workdir(located_session: LocatedSession) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)

    assert b"cd '/workspace/project'\n" in client._ws.sent


def test_create_handoff_sandbox_sends_resume_command_last(located_session: LocatedSession) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)

    assert client._ws.sent[-1] == b"claude --resume abc123\n"


def test_create_handoff_sandbox_pushes_credential_values_containing_single_quotes_verbatim(
    session_file: Path,
) -> None:
    """The credential value itself goes through file_create (which writes
    content as-is, no shell quoting needed) rather than being typed -- so a
    value containing a single quote must reach the sandbox unmodified, not
    escaped the way it would need to be if it were typed."""
    client = FakeBoxkiteClient()
    session = LocatedSession(
        tool="claude-code",
        session_id="abc123",
        files=(),
        credential=Credential(env_var="TOKEN", value="it's-a-secret"),
        resume_command="claude --resume abc123",
        workdir="/workspace",
    )

    create_handoff_sandbox(client, session)

    pushes = _credential_file_pushes(client, "it's-a-secret")
    assert len(pushes) == 1
    for line in client._ws.sent:
        assert b"it's-a-secret" not in line


def test_create_handoff_sandbox_uses_a_default_label_when_none_given(located_session: LocatedSession) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)

    assert client.created[0]["label"] == "handoff-claude-code-abc123"


def test_create_handoff_sandbox_honors_custom_lifetime_minutes(located_session: LocatedSession) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session, lifetime_minutes=30)

    assert client.created[0]["lifetime_minutes"] == 30


def test_create_handoff_sandbox_uses_a_fresh_credential_path_each_call(located_session: LocatedSession) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)
    create_handoff_sandbox(client, located_session)

    paths = [entry[1] for entry in _credential_file_pushes(client, "sk-test-token")]
    assert len(paths) == 2
    assert paths[0] != paths[1]
    assert all(re.fullmatch(r"/tmp/\.boxkite-handoff-credential-[0-9a-f]{32}", p) for p in paths)


def test_create_handoff_sandbox_calls_session_cleanup_after_pushing_files(
    session_file: Path,
) -> None:
    client = FakeBoxkiteClient()
    cleanup_calls: list[str] = []
    session = LocatedSession(
        tool="opencode",
        session_id="ses1",
        files=(SessionFile(local_path=session_file, sandbox_path="/workspace/x.json"),),
        credential=Credential(env_var="TOKEN", value="tok"),
        resume_command="opencode --session ses1",
        workdir="/workspace",
        cleanup=lambda: cleanup_calls.append("cleaned"),
    )

    create_handoff_sandbox(client, session)

    assert cleanup_calls == ["cleaned"]


def test_create_handoff_sandbox_calls_cleanup_even_if_file_push_fails(
    session_file: Path,
) -> None:
    class FailingClient(FakeBoxkiteClient):
        def file_create(self, session_id: str, path: str, content: str, **_kwargs) -> dict:
            raise RuntimeError("push failed")

    client = FailingClient()
    cleanup_calls: list[str] = []
    session = LocatedSession(
        tool="opencode",
        session_id="ses1",
        files=(SessionFile(local_path=session_file, sandbox_path="/workspace/x.json"),),
        credential=Credential(env_var="TOKEN", value="tok"),
        resume_command="opencode --session ses1",
        workdir="/workspace",
        cleanup=lambda: cleanup_calls.append("cleaned"),
    )

    with pytest.raises(RuntimeError, match="push failed"):
        create_handoff_sandbox(client, session)

    assert cleanup_calls == ["cleaned"]


def test_create_handoff_sandbox_tolerates_no_cleanup_set(located_session: LocatedSession) -> None:
    client = FakeBoxkiteClient()

    create_handoff_sandbox(client, located_session)  # must not raise
