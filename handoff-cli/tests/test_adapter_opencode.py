from __future__ import annotations

import json
from pathlib import Path

import pytest

from boxkite_handoff.adapters.opencode import OpencodeAdapter
from boxkite_handoff.core import HandoffError


class FakeRunner:
    """Records every argv it's called with and returns a canned stdout string
    (or raises) based on the command's first two positional args."""

    def __init__(self, responses: dict[tuple[str, ...], str | Exception]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        key = tuple(argv[1:])
        for prefix, response in self.responses.items():
            if key[: len(prefix)] == prefix:
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"FakeRunner has no response configured for {argv}")


def session_list_json(sessions: list[dict]) -> str:
    return json.dumps(sessions)


def export_json(session_id: str, messages: list[dict]) -> str:
    return json.dumps({"info": {"id": session_id, "title": "test session"}, "messages": messages})


def assistant_message(provider_id: str, model_id: str = "some-model") -> dict:
    return {
        "info": {"role": "assistant", "providerID": provider_id, "modelID": model_id},
        "parts": [],
    }


def user_message() -> dict:
    return {"info": {"role": "user"}, "parts": []}


@pytest.fixture
def auth_json(tmp_path: Path) -> Path:
    data_dir = tmp_path / "opencode-data"
    data_dir.mkdir()
    auth_path = data_dir / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "anthropic": {"type": "api", "key": "sk-ant-portable-key"},
                "github-copilot": {"type": "oauth", "refresh": "r", "access": "a", "expires": 123},
            }
        ),
        encoding="utf-8",
    )
    return data_dir


def make_adapter(tmp_path: Path, data_dir: Path, runner: FakeRunner) -> OpencodeAdapter:
    return OpencodeAdapter(data_dir=data_dir, runner=runner, export_dir=tmp_path / "exports")


def test_locate_session_picks_most_recently_updated_session_when_ref_is_none(
    tmp_path: Path, auth_json: Path
) -> None:
    sessions = [
        {"id": "ses_old", "title": "old", "updated": 100, "created": 100},
        {"id": "ses_new", "title": "new", "updated": 999, "created": 900},
    ]
    runner = FakeRunner(
        {
            ("session", "list"): session_list_json(sessions),
            ("export", "ses_new"): export_json("ses_new", [assistant_message("anthropic")]),
        }
    )
    adapter = make_adapter(tmp_path, auth_json, runner)

    located = adapter.locate_session()

    assert located.session_id == "ses_new"


def test_locate_session_uses_session_ref_directly_without_listing(
    tmp_path: Path, auth_json: Path
) -> None:
    runner = FakeRunner(
        {("export", "ses_explicit"): export_json("ses_explicit", [assistant_message("anthropic")])}
    )
    adapter = make_adapter(tmp_path, auth_json, runner)

    located = adapter.locate_session(session_ref="ses_explicit")

    assert located.session_id == "ses_explicit"
    assert all(call[1] != "session" for call in runner.calls)


def test_locate_session_raises_when_no_local_sessions_exist(
    tmp_path: Path, auth_json: Path
) -> None:
    runner = FakeRunner({("session", "list"): session_list_json([])})
    adapter = make_adapter(tmp_path, auth_json, runner)

    with pytest.raises(HandoffError):
        adapter.locate_session()


def test_locate_session_rejects_a_malicious_session_id_from_session_list(
    tmp_path: Path, auth_json: Path
) -> None:
    """Regression test for a real command-injection finding: the most-
    recent session id comes from `opencode session list`'s JSON output --
    locally-stored, attacker-adjacent metadata on a compromised machine --
    and later flows unquoted into resume_command. Must be rejected here."""
    sessions = [{"id": "x'; touch pwned #", "title": "evil", "updated": 999, "created": 900}]
    runner = FakeRunner({("session", "list"): session_list_json(sessions)})
    adapter = make_adapter(tmp_path, auth_json, runner)

    with pytest.raises(HandoffError):
        adapter.locate_session()


def test_locate_session_rejects_a_malicious_explicit_session_ref(
    tmp_path: Path, auth_json: Path
) -> None:
    adapter = make_adapter(tmp_path, auth_json, FakeRunner({}))

    with pytest.raises(HandoffError):
        adapter.locate_session(session_ref="x'; touch pwned #")


def test_locate_session_provides_a_cleanup_that_removes_its_own_temp_export(
    auth_json: Path,
) -> None:
    """When no export_dir is given, the adapter makes its own temp
    directory for the exported session JSON -- `cleanup` must remove it so
    a conversation transcript doesn't linger on the local disk once it's
    been pushed to the sandbox."""
    runner = FakeRunner(
        {("export", "ses_1"): export_json("ses_1", [assistant_message("anthropic")])}
    )
    adapter = OpencodeAdapter(data_dir=auth_json, runner=runner)

    located = adapter.locate_session(session_ref="ses_1")

    export_path = located.files[0].local_path
    assert export_path.exists()
    assert located.cleanup is not None

    located.cleanup()

    assert not export_path.exists()
    assert not export_path.parent.exists()


def test_locate_session_does_not_clean_up_a_caller_provided_export_dir(
    tmp_path: Path, auth_json: Path
) -> None:
    runner = FakeRunner(
        {("export", "ses_1"): export_json("ses_1", [assistant_message("anthropic")])}
    )
    adapter = make_adapter(tmp_path, auth_json, runner)

    located = adapter.locate_session(session_ref="ses_1")

    assert located.cleanup is None
    assert located.files[0].local_path.exists()


def test_locate_session_pushes_the_exported_session_as_a_file(
    tmp_path: Path, auth_json: Path
) -> None:
    raw_export = export_json("ses_1", [assistant_message("anthropic")])
    runner = FakeRunner({("export", "ses_1"): raw_export})
    adapter = make_adapter(tmp_path, auth_json, runner)

    located = adapter.locate_session(session_ref="ses_1")

    assert len(located.files) == 1
    session_file = located.files[0]
    assert session_file.local_path.read_text(encoding="utf-8") == raw_export
    assert session_file.sandbox_path == "/workspace/.opencode-handoff/ses_1.json"


def test_locate_session_resume_command_imports_then_resumes_by_session_id(
    tmp_path: Path, auth_json: Path
) -> None:
    runner = FakeRunner(
        {("export", "ses_1"): export_json("ses_1", [assistant_message("anthropic")])}
    )
    adapter = make_adapter(tmp_path, auth_json, runner)

    located = adapter.locate_session(session_ref="ses_1")

    assert located.resume_command == (
        "opencode import /workspace/.opencode-handoff/ses_1.json && opencode --session ses_1"
    )
    assert located.workdir == "/workspace"


def test_locate_session_picks_the_provider_used_by_the_most_recent_assistant_message(
    tmp_path: Path, auth_json: Path
) -> None:
    messages = [
        user_message(),
        assistant_message("github-copilot"),
        user_message(),
        assistant_message("anthropic"),
    ]
    runner = FakeRunner({("export", "ses_1"): export_json("ses_1", messages)})
    # github-copilot is oauth-only in the fixture auth.json; if the adapter picked
    # the *first* assistant message's provider instead of the last, this would
    # raise HandoffError instead of succeeding.
    adapter = make_adapter(tmp_path, auth_json, runner)

    located = adapter.locate_session(session_ref="ses_1")

    assert located.credential.env_var == "OPENCODE_AUTH_CONTENT"
    assert json.loads(located.credential.value) == {
        "anthropic": {"type": "api", "key": "sk-ant-portable-key"}
    }


def test_locate_session_raises_when_no_message_carries_a_provider_id(
    tmp_path: Path, auth_json: Path
) -> None:
    runner = FakeRunner({("export", "ses_1"): export_json("ses_1", [user_message()])})
    adapter = make_adapter(tmp_path, auth_json, runner)

    with pytest.raises(HandoffError):
        adapter.locate_session(session_ref="ses_1")


def test_locate_session_raises_when_provider_has_no_configured_credential(
    tmp_path: Path, auth_json: Path
) -> None:
    runner = FakeRunner({("export", "ses_1"): export_json("ses_1", [assistant_message("openai")])})
    adapter = make_adapter(tmp_path, auth_json, runner)

    with pytest.raises(HandoffError, match="openai"):
        adapter.locate_session(session_ref="ses_1")


def test_locate_session_raises_when_provider_credential_is_oauth_only(
    tmp_path: Path, auth_json: Path
) -> None:
    runner = FakeRunner(
        {("export", "ses_1"): export_json("ses_1", [assistant_message("github-copilot")])}
    )
    adapter = make_adapter(tmp_path, auth_json, runner)

    with pytest.raises(HandoffError, match="OAuth"):
        adapter.locate_session(session_ref="ses_1")


def test_locate_session_raises_when_auth_json_is_missing(tmp_path: Path) -> None:
    empty_data_dir = tmp_path / "no-opencode-here"
    empty_data_dir.mkdir()
    runner = FakeRunner(
        {("export", "ses_1"): export_json("ses_1", [assistant_message("anthropic")])}
    )
    adapter = make_adapter(tmp_path, empty_data_dir, runner)

    with pytest.raises(HandoffError):
        adapter.locate_session(session_ref="ses_1")


def test_locate_session_raises_when_export_returns_invalid_json(
    tmp_path: Path, auth_json: Path
) -> None:
    runner = FakeRunner({("export", "ses_1"): "not json"})
    adapter = make_adapter(tmp_path, auth_json, runner)

    with pytest.raises(HandoffError):
        adapter.locate_session(session_ref="ses_1")


def test_locate_session_raises_when_export_command_itself_fails(
    tmp_path: Path, auth_json: Path
) -> None:
    runner = FakeRunner({("export", "ses_1"): HandoffError("opencode command failed")})
    adapter = make_adapter(tmp_path, auth_json, runner)

    with pytest.raises(HandoffError):
        adapter.locate_session(session_ref="ses_1")


def test_adapter_name_is_opencode(tmp_path: Path, auth_json: Path) -> None:
    adapter = make_adapter(tmp_path, auth_json, FakeRunner({}))
    assert adapter.name == "opencode"
