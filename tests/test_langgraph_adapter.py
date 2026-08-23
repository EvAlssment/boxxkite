import pytest

from boxxkite.tools import create_sandbox_tool_specs


class _FakeSandboxManager:
    pass


def test_to_langgraph_tools_produces_toolnode_compatible_tools():
    pytest.importorskip("langgraph.prebuilt")
    from boxxkite.tools.adapters import to_langgraph_tools

    specs = create_sandbox_tool_specs(sandbox_manager=_FakeSandboxManager(), session_id="s1")
    tools = to_langgraph_tools(specs)

    assert {tool.name for tool in tools} == {spec.name for spec in specs}


def test_create_langgraph_tool_node_wraps_every_spec():
    pytest.importorskip("langgraph.prebuilt")
    from boxxkite.tools.adapters import create_langgraph_tool_node

    specs = create_sandbox_tool_specs(sandbox_manager=_FakeSandboxManager(), session_id="s1")
    node = create_langgraph_tool_node(specs)

    assert set(node.tools_by_name) == {spec.name for spec in specs}
