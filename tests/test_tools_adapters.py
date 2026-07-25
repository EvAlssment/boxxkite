"""
Tests for boxkite.tools' framework-agnostic core and its adapters.

boxkite.tools.create_sandbox_tool_specs() (and each create_*_tool_spec())
returns plain ToolSpec objects (name, description, JSON-schema parameters,
and a plain async handler) with no LangChain dependency anywhere in
bash_tool.py, file_tools.py, present_files.py, search_tools.py,
process_tools.py, git_tools.py, python_interpreter_tool.py, or
node_interpreter_tool.py.
boxkite.tools.adapters converts those specs into framework-specific shapes:
to_langchain_tools (LangChain BaseTool objects) and to_openai_functions
(an OpenAI-style function-calling schema, pure stdlib).

create_sandbox_tools()/create_bash_tool()/etc. remain as backward-compatible
wrappers that call to_langchain_tools internally — see test_bash_tool.py,
test_search_tools.py, and test_tool_factory.py for coverage of those
call sites, unchanged.
"""

import inspect

import pytest

import boxkite.tools.bash_tool as bash_tool_module
import boxkite.tools.file_tools as file_tools_module
import boxkite.tools.git_tools as git_tools_module
import boxkite.tools.node_interpreter_tool as node_interpreter_tool_module
import boxkite.tools.present_files as present_files_module
import boxkite.tools.process_tools as process_tools_module
import boxkite.tools.python_interpreter_tool as python_interpreter_tool_module
import boxkite.tools.search_tools as search_tools_module
from boxkite.tools import create_sandbox_tool_specs
from boxkite.tools.bash_tool import create_bash_tool_spec
from boxkite.tools.file_tools import create_view_tool_spec
from boxkite.tools.git_tools import create_git_status_tool_spec, create_git_tool_specs
from boxkite.tools.node_interpreter_tool import create_node_interpreter_tool_spec
from boxkite.tools.process_tools import create_start_process_tool_spec
from boxkite.tools.python_interpreter_tool import create_python_interpreter_tool_spec
from boxkite.tools.types import ToolImageResult, ToolSpec

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(self):
        self.execute_calls = []

    async def execute(self, session_id, command, timeout, secret_env=None):
        self.execute_calls.append(command)
        return {"exit_code": 0, "stdout": "hello", "stderr": ""}

    async def view(self, session_id, path, view_range=None, description=None):
        return {"content": "line one", "lines": 1, "is_directory": False}

    async def read_image(self, session_id, path, description=None):
        return {"base64_data": "AAAA", "mime_type": "image/png"}


@pytest.mark.parametrize(
    "module",
    [
        bash_tool_module,
        file_tools_module,
        present_files_module,
        search_tools_module,
        process_tools_module,
        git_tools_module,
        python_interpreter_tool_module,
        node_interpreter_tool_module,
    ],
)
def test_tool_modules_have_no_top_level_langchain_import(module):
    # Each tool module's actual logic must be importable and callable with
    # zero LangChain dependency. Only the legacy create_*_tool() wrappers
    # reach for langchain_core, lazily, inside their own function bodies
    # via boxkite.tools.adapters -- never at module scope.
    source = inspect.getsource(module)
    top_level_import_lines = [
        line
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    assert not any("langchain" in line or "langgraph" in line for line in top_level_import_lines)


def test_create_bash_tool_spec_returns_a_framework_agnostic_tool_spec():
    spec = create_bash_tool_spec(session_id="s1", sandbox_manager=_FakeSandboxManager())

    assert isinstance(spec, ToolSpec)
    assert spec.name == "bash_tool"
    assert spec.parameters["type"] == "object"
    assert "command" in spec.parameters["properties"]
    assert spec.parameters["required"] == ["command"]


@pytest.mark.asyncio
async def test_bash_tool_spec_handler_is_a_plain_callable_no_framework_needed():
    manager = _FakeSandboxManager()
    spec = create_bash_tool_spec(session_id="s1", sandbox_manager=manager)

    # No LangChain, no @tool decorator, no .ainvoke() -- just call it.
    result = await spec.handler(command="echo hi")

    assert result == "hello"
    assert manager.execute_calls == ["echo hi"]


def test_create_sandbox_tool_specs_returns_the_full_agnostic_tool_set():
    specs = create_sandbox_tool_specs(
        sandbox_manager=_FakeSandboxManager(),
        session_id="session-1",
    )

    assert {s.name for s in specs} == {
        "bash_tool",
        "python_interpreter",
        "file_create",
        "view",
        "str_replace",
        "present_files",
        "ls",
        "glob",
        "grep",
        "start_process",
        "get_process_output",
        "send_process_input",
        "stop_process",
        "list_processes",
        "watch_directory",
    }
    assert len(specs) == 15
    assert all(isinstance(s, ToolSpec) for s in specs)
    assert all(callable(s.handler) for s in specs)


def test_create_sandbox_tool_specs_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_sandbox_tool_specs()


@pytest.mark.asyncio
async def test_view_spec_handler_returns_tool_image_result_for_images():
    spec = create_view_tool_spec(sandbox_manager=_FakeSandboxManager(), session_id="s1")

    assert spec.returns_multimodal is True

    text_result = await spec.handler(path="notes.txt")
    assert isinstance(text_result, str)
    assert "line one" in text_result

    image_result = await spec.handler(path="photo.png")
    assert isinstance(image_result, ToolImageResult)
    assert image_result.base64_data == "AAAA"
    assert image_result.mime_type == "image/png"


def test_to_openai_functions_produces_the_expected_schema_shape():
    from boxkite.tools.adapters import to_openai_functions

    specs = create_sandbox_tool_specs(sandbox_manager=_FakeSandboxManager(), session_id="s1")
    schema = to_openai_functions(specs)

    assert len(schema) == len(specs)
    by_name = {entry["function"]["name"]: entry for entry in schema}
    bash_entry = by_name["bash_tool"]
    assert bash_entry["type"] == "function"
    assert bash_entry["function"]["parameters"]["type"] == "object"
    assert "command" in bash_entry["function"]["parameters"]["properties"]


def test_to_langchain_tools_produces_working_langchain_tools():
    from boxkite.tools.adapters import to_langchain_tools

    manager = _FakeSandboxManager()
    specs = create_sandbox_tool_specs(sandbox_manager=manager, session_id="s1")
    tools = to_langchain_tools(specs)

    assert {t.name for t in tools} == {s.name for s in specs}


@pytest.mark.asyncio
async def test_to_langchain_tools_bash_tool_round_trips_through_ainvoke():
    from boxkite.tools.adapters import to_langchain_tools

    manager = _FakeSandboxManager()
    spec = create_bash_tool_spec(session_id="s1", sandbox_manager=manager)
    tool = to_langchain_tools([spec])[0]

    result = await tool.ainvoke({"command": "echo hi"})

    assert result == "hello"


@pytest.mark.asyncio
async def test_to_langchain_tools_view_wraps_images_in_a_tool_message():
    from boxkite.tools.adapters import to_langchain_tools

    spec = create_view_tool_spec(sandbox_manager=_FakeSandboxManager(), session_id="s1")
    tool = to_langchain_tools([spec])[0]

    result = await tool.ainvoke(
        {"name": "view", "args": {"path": "photo.png"}, "id": "call-1", "type": "tool_call"}
    )

    assert result.tool_call_id == "call-1"
    assert result.additional_kwargs["read_file_media_type"] == "image/png"


def test_to_llamaindex_tools_produces_working_function_tools():
    from boxkite.tools.adapters import to_llamaindex_tools

    manager = _FakeSandboxManager()
    specs = create_sandbox_tool_specs(sandbox_manager=manager, session_id="s1")
    tools = to_llamaindex_tools(specs)

    assert {t.metadata.name for t in tools} == {s.name for s in specs}


@pytest.mark.asyncio
async def test_to_llamaindex_tools_bash_tool_round_trips_through_acall():
    from boxkite.tools.adapters import to_llamaindex_tools

    manager = _FakeSandboxManager()
    spec = create_bash_tool_spec(session_id="s1", sandbox_manager=manager)
    tool = to_llamaindex_tools([spec])[0]

    result = await tool.acall(command="echo hi")

    assert result.raw_output == "hello"
    assert manager.execute_calls == ["echo hi"]


def test_to_openai_agents_tools_produces_working_function_tools():
    from boxkite.tools.adapters import to_openai_agents_tools

    manager = _FakeSandboxManager()
    specs = create_sandbox_tool_specs(sandbox_manager=manager, session_id="s1")
    tools = to_openai_agents_tools(specs)

    assert {t.name for t in tools} == {s.name for s in specs}
    assert all(t.strict_json_schema is False for t in tools)


@pytest.mark.asyncio
async def test_to_openai_agents_tools_bash_tool_round_trips_through_on_invoke_tool():
    import json

    from boxkite.tools.adapters import to_openai_agents_tools

    manager = _FakeSandboxManager()
    spec = create_bash_tool_spec(session_id="s1", sandbox_manager=manager)
    tool = to_openai_agents_tools([spec])[0]

    result = await tool.on_invoke_tool(None, json.dumps({"command": "echo hi"}))

    assert result == "hello"
    assert manager.execute_calls == ["echo hi"]


@pytest.mark.asyncio
async def test_to_openai_agents_tools_view_surfaces_image_metadata_as_text():
    import json

    from boxkite.tools.adapters import to_openai_agents_tools

    spec = create_view_tool_spec(sandbox_manager=_FakeSandboxManager(), session_id="s1")
    tool = to_openai_agents_tools([spec])[0]

    result = await tool.on_invoke_tool(None, json.dumps({"path": "photo.png"}))

    assert "image/png" in result


@pytest.mark.asyncio
async def test_to_llamaindex_tools_view_surfaces_image_metadata_as_text():
    from boxkite.tools.adapters import to_llamaindex_tools

    spec = create_view_tool_spec(sandbox_manager=_FakeSandboxManager(), session_id="s1")
    tool = to_llamaindex_tools([spec])[0]

    result = await tool.acall(path="photo.png")

    assert "image/png" in result.raw_output


def test_to_llamaindex_tools_schema_marks_required_and_optional_params_correctly():
    from boxkite.tools.adapters import to_llamaindex_tools

    spec = create_bash_tool_spec(session_id="s1", sandbox_manager=_FakeSandboxManager())
    tool = to_llamaindex_tools([spec])[0]

    schema_fields = tool.metadata.fn_schema.model_fields
    assert schema_fields["command"].is_required()
    assert not schema_fields["timeout"].is_required()
    assert schema_fields["timeout"].default == 120


# ---------------------------------------------------------------------------
# process_tools.py / git_tools.py / python_interpreter_tool.py -- these were
# newly made framework-agnostic alongside bash_tool.py/file_tools.py above;
# same coverage shape (a plain ToolSpec, a plain callable handler, no
# LangChain needed to exercise the tool's logic).
# ---------------------------------------------------------------------------


class _FakeProcessSandboxManager:
    async def start_process(self, session_id, command, description=None, max_runtime_seconds=3600):
        return {"process_id": "proc_1", "status": "running"}


def test_create_start_process_tool_spec_returns_a_framework_agnostic_tool_spec():
    spec = create_start_process_tool_spec(sandbox_manager=_FakeProcessSandboxManager())

    assert isinstance(spec, ToolSpec)
    assert spec.name == "start_process"
    assert spec.parameters["type"] == "object"
    assert "command" in spec.parameters["properties"]
    assert spec.parameters["required"] == ["command"]


@pytest.mark.asyncio
async def test_start_process_spec_handler_is_a_plain_callable_no_framework_needed():
    manager = _FakeProcessSandboxManager()
    spec = create_start_process_tool_spec(sandbox_manager=manager, session_id="s1")

    result = await spec.handler(command="npm run dev")

    assert "proc_1" in result


class _FakeInterpreterSandboxManager:
    async def interpreter_exec(self, session_id, code, timeout):
        return {"stdout": "", "result": "42", "error": None, "truncated": False}


def test_create_python_interpreter_tool_spec_returns_a_framework_agnostic_tool_spec():
    spec = create_python_interpreter_tool_spec(sandbox_manager=_FakeInterpreterSandboxManager())

    assert isinstance(spec, ToolSpec)
    assert spec.name == "python_interpreter"
    assert "code" in spec.parameters["properties"]


@pytest.mark.asyncio
async def test_python_interpreter_spec_handler_is_a_plain_callable_no_framework_needed():
    manager = _FakeInterpreterSandboxManager()
    spec = create_python_interpreter_tool_spec(sandbox_manager=manager, session_id="s1")

    result = await spec.handler(code="40 + 2")

    assert result == "42"


class _FakeNodeInterpreterSandboxManager:
    async def node_interpreter_exec(self, session_id, code, timeout):
        return {"stdout": "", "result": "42", "error": None, "truncated": False}


def test_create_node_interpreter_tool_spec_returns_a_framework_agnostic_tool_spec():
    spec = create_node_interpreter_tool_spec(
        sandbox_manager=_FakeNodeInterpreterSandboxManager()
    )

    assert isinstance(spec, ToolSpec)
    assert spec.name == "node_interpreter"
    assert "code" in spec.parameters["properties"]


@pytest.mark.asyncio
async def test_node_interpreter_spec_handler_is_a_plain_callable_no_framework_needed():
    manager = _FakeNodeInterpreterSandboxManager()
    spec = create_node_interpreter_tool_spec(sandbox_manager=manager, session_id="s1")

    result = await spec.handler(code="40 + 2")

    assert result == "42"


class _FakeGitSandboxManager:
    async def execute(self, session_id, command, timeout, secret_env=None):
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    async def file_create(self, session_id, path, content, description=None):
        return {"path": path, "size": len(content)}


def test_create_git_status_tool_spec_returns_a_framework_agnostic_tool_spec():
    spec = create_git_status_tool_spec(sandbox_manager=_FakeGitSandboxManager())

    assert isinstance(spec, ToolSpec)
    assert spec.name == "git_status"


@pytest.mark.asyncio
async def test_git_status_spec_handler_is_a_plain_callable_no_framework_needed():
    manager = _FakeGitSandboxManager()
    spec = create_git_status_tool_spec(sandbox_manager=manager, session_id="s1")

    result = await spec.handler(path="/workspace/repo")

    assert "ok" in result


def test_create_git_tool_specs_returns_all_eight_operations_as_specs():
    specs = create_git_tool_specs(sandbox_manager=_FakeGitSandboxManager(), session_id="s1")

    names = {s.name for s in specs}
    assert names == {
        "git_clone",
        "git_status",
        "git_add",
        "git_commit",
        "git_push",
        "git_pull",
        "git_branch",
        "git_checkout",
    }
    assert len(specs) == 8
    assert all(isinstance(s, ToolSpec) for s in specs)
    assert all(callable(s.handler) for s in specs)


# ---------------------------------------------------------------------------
# Generic guard: every ToolSpec -- including every opt-in one, and any new
# tool added to create_sandbox_tool_specs in the future -- must flow through
# all four adapters the same way. This is deliberately generic (it asserts
# on the *full* spec list, not by tool name) so a future new tool is covered
# automatically without anyone remembering to add a bespoke test for it, the
# same way node_interpreter's addition prompted writing this in the first
# place: create_sandbox_tool_specs()/its adapters never special-case any one
# tool by name, so nothing here should either.
# ---------------------------------------------------------------------------


class _FakeFullSandboxManager:
    """A manager satisfying every opt-in tool's constructor requirements.

    ToolSpec *creation* never calls manager methods eagerly (see
    test_tool_factory.py's own _FakeSandboxManager, which is a bare stand-in
    for the same reason) -- this class exists only so
    create_sandbox_tool_specs(**all flags enabled) can be built without
    raising, to exercise every adapter against the resulting full spec list,
    node_interpreter included.
    """


def _create_every_tool_spec() -> list[ToolSpec]:
    return create_sandbox_tool_specs(
        sandbox_manager=_FakeFullSandboxManager(),
        session_id="session-1",
        enable_git_tools=True,
        enable_http_request_tool=True,
        enable_secret_env=True,
        enable_agent_pty=True,
        enable_node_interpreter=True,
    )


def test_every_tool_spec_including_node_interpreter_is_a_tool_spec():
    specs = _create_every_tool_spec()

    names = {s.name for s in specs}
    assert "node_interpreter" in names
    assert "python_interpreter" in names
    assert len(specs) == len(names)  # no accidental duplicate names
    assert all(isinstance(s, ToolSpec) for s in specs)
    assert all(callable(s.handler) for s in specs)


def test_to_openai_functions_covers_every_tool_spec_including_node_interpreter():
    from boxkite.tools.adapters import to_openai_functions

    specs = _create_every_tool_spec()
    schema = to_openai_functions(specs)

    assert {entry["function"]["name"] for entry in schema} == {s.name for s in specs}
    node_entry = next(e for e in schema if e["function"]["name"] == "node_interpreter")
    assert "code" in node_entry["function"]["parameters"]["properties"]


def test_to_langchain_tools_covers_every_tool_spec_including_node_interpreter():
    from boxkite.tools.adapters import to_langchain_tools

    specs = _create_every_tool_spec()
    tools = to_langchain_tools(specs)

    assert {t.name for t in tools} == {s.name for s in specs}


def test_to_llamaindex_tools_covers_every_tool_spec_including_node_interpreter():
    from boxkite.tools.adapters import to_llamaindex_tools

    specs = _create_every_tool_spec()
    tools = to_llamaindex_tools(specs)

    assert {t.metadata.name for t in tools} == {s.name for s in specs}


def test_to_openai_agents_tools_covers_every_tool_spec_including_node_interpreter():
    from boxkite.tools.adapters import to_openai_agents_tools

    specs = _create_every_tool_spec()
    tools = to_openai_agents_tools(specs)

    assert {t.name for t in tools} == {s.name for s in specs}
    assert all(t.strict_json_schema is False for t in tools)


# ---------------------------------------------------------------------------
# node_interpreter-specific round trips through each adapter -- mirrors the
# bash_tool/view round-trip coverage above, so node_interpreter gets the same
# depth of adapter coverage python_interpreter itself still lacks (tracked
# as a pre-existing gap, not introduced here -- see the generic tests above
# for why any new tool, including python_interpreter if it were added today,
# would now be caught).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_to_langchain_tools_node_interpreter_round_trips_through_ainvoke():
    from boxkite.tools.adapters import to_langchain_tools

    manager = _FakeNodeInterpreterSandboxManager()
    spec = create_node_interpreter_tool_spec(session_id="s1", sandbox_manager=manager)
    tool = to_langchain_tools([spec])[0]

    result = await tool.ainvoke({"code": "40 + 2"})

    assert result == "42"


@pytest.mark.asyncio
async def test_to_llamaindex_tools_node_interpreter_round_trips_through_acall():
    from boxkite.tools.adapters import to_llamaindex_tools

    manager = _FakeNodeInterpreterSandboxManager()
    spec = create_node_interpreter_tool_spec(session_id="s1", sandbox_manager=manager)
    tool = to_llamaindex_tools([spec])[0]

    result = await tool.acall(code="40 + 2")

    assert result.raw_output == "42"


@pytest.mark.asyncio
async def test_to_openai_agents_tools_node_interpreter_round_trips_through_on_invoke_tool():
    import json

    from boxkite.tools.adapters import to_openai_agents_tools

    manager = _FakeNodeInterpreterSandboxManager()
    spec = create_node_interpreter_tool_spec(session_id="s1", sandbox_manager=manager)
    tool = to_openai_agents_tools([spec])[0]

    result = await tool.on_invoke_tool(None, json.dumps({"code": "40 + 2"}))

    assert result == "42"
