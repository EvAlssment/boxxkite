"""Tests for the scratch_memory tool (GitHub issue #74)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from boxxkite.tools import create_sandbox_tool_specs
from boxxkite.tools.scratch_memory_tool import create_scratch_memory_tool_spec


def _manager() -> MagicMock:
    manager = MagicMock()
    manager.scratch_set = AsyncMock(return_value={"key": "todo", "bytes_stored": 12, "keys": 1})
    manager.scratch_get = AsyncMock(return_value={"key": "todo", "value": "read it", "found": True})
    manager.scratch_delete = AsyncMock(return_value={"key": "todo", "deleted": True, "keys": 0})
    manager.scratch_list = AsyncMock(
        return_value={
            "entries": [{"key": "todo", "bytes": 7}],
            "keys": 1,
            "total_bytes": 7,
            "max_keys": 256,
            "max_total_bytes": 1048576,
        }
    )
    return manager


def test_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError):
        create_scratch_memory_tool_spec()


async def test_set_forwards_key_and_value_to_the_manager():
    manager = _manager()
    spec = create_scratch_memory_tool_spec(sandbox_manager=manager, session_id="sess-1")

    result = await spec.handler(operation="set", key="todo", value="read manager.py")

    manager.scratch_set.assert_awaited_once_with("sess-1", "todo", "read manager.py")
    assert "Stored 'todo'" in result


async def test_get_returns_the_stored_value_verbatim():
    manager = _manager()
    spec = create_scratch_memory_tool_spec(sandbox_manager=manager, session_id="sess-1")

    assert await spec.handler(operation="get", key="todo") == "read it"


async def test_get_of_a_missing_key_reads_as_plain_text_not_an_error():
    manager = _manager()
    manager.scratch_get = AsyncMock(return_value={"key": "nope", "value": None, "found": False})
    spec = create_scratch_memory_tool_spec(sandbox_manager=manager, session_id="sess-1")

    result = await spec.handler(operation="get", key="nope")

    assert "No value stored" in result
    assert "Error" not in result


async def test_list_summarizes_keys_and_sizes():
    manager = _manager()
    spec = create_scratch_memory_tool_spec(sandbox_manager=manager, session_id="sess-1")

    result = await spec.handler(operation="list")

    assert "todo (7 bytes)" in result
    assert "1 key(s)" in result


async def test_list_on_an_empty_store_says_so():
    manager = _manager()
    manager.scratch_list = AsyncMock(
        return_value={"entries": [], "keys": 0, "total_bytes": 0, "max_keys": 256, "max_total_bytes": 1048576}
    )
    spec = create_scratch_memory_tool_spec(sandbox_manager=manager, session_id="sess-1")

    assert await spec.handler(operation="list") == "Scratch memory is empty."


async def test_unknown_operation_is_rejected_without_calling_the_manager():
    manager = _manager()
    spec = create_scratch_memory_tool_spec(sandbox_manager=manager, session_id="sess-1")

    result = await spec.handler(operation="truncate", key="todo")

    assert "unknown operation" in result
    manager.scratch_set.assert_not_awaited()
    manager.scratch_get.assert_not_awaited()
    manager.scratch_delete.assert_not_awaited()
    manager.scratch_list.assert_not_awaited()


async def test_missing_arguments_are_corrected_before_a_round_trip():
    manager = _manager()
    spec = create_scratch_memory_tool_spec(sandbox_manager=manager, session_id="sess-1")

    assert "'key' is required" in await spec.handler(operation="get")
    assert "'value' is required" in await spec.handler(operation="set", key="todo")
    manager.scratch_get.assert_not_awaited()
    manager.scratch_set.assert_not_awaited()


async def test_a_sidecar_failure_is_returned_as_text_not_raised():
    manager = _manager()
    manager.scratch_set = AsyncMock(side_effect=RuntimeError("sidecar unreachable"))
    spec = create_scratch_memory_tool_spec(sandbox_manager=manager, session_id="sess-1")

    result = await spec.handler(operation="set", key="k", value="v")

    assert "Error running scratch_memory set" in result
    assert "sidecar unreachable" in result


def test_tool_is_off_by_default_and_opt_in_via_the_factory():
    manager = MagicMock()

    default_names = {t.name for t in create_sandbox_tool_specs(sandbox_manager=manager, session_id="s")}
    assert "scratch_memory" not in default_names

    enabled = create_sandbox_tool_specs(
        sandbox_manager=manager, session_id="s", enable_scratch_memory=True
    )
    assert "scratch_memory" in {t.name for t in enabled}
    assert len(enabled) == len(default_names) + 1


# ── audit_sink wiring (review follow-up) ─────────────────────────────────
# The tool's own description tells the model to keep notes here instead of in
# a scratch file, so mutations have to reach AuditSink the way file_create's
# do -- otherwise the tool quietly moves agent writes off the audit trail.


def _sink() -> MagicMock:
    sink = MagicMock()
    sink.record_scratch_write = AsyncMock(return_value=None)
    return sink


async def test_set_is_mirrored_to_the_audit_sink():
    manager, sink = _manager(), _sink()
    spec = create_scratch_memory_tool_spec(
        sandbox_manager=manager, session_id="sess-1", audit_sink=sink, agent_name="agent-a"
    )

    await spec.handler(operation="set", key="todo", value="read manager.py")

    sink.record_scratch_write.assert_awaited_once()
    kwargs = sink.record_scratch_write.await_args.kwargs
    assert kwargs["operation"] == "set"
    assert kwargs["key"] == "todo"
    assert kwargs["size_bytes"] == 12
    assert kwargs["session_id"] == "sess-1"
    assert kwargs["agent_name"] == "agent-a"


async def test_delete_is_mirrored_to_the_audit_sink():
    manager, sink = _manager(), _sink()
    spec = create_scratch_memory_tool_spec(
        sandbox_manager=manager, session_id="sess-1", audit_sink=sink
    )

    await spec.handler(operation="delete", key="todo")

    kwargs = sink.record_scratch_write.await_args.kwargs
    assert kwargs["operation"] == "delete"
    assert kwargs["size_bytes"] == 0


async def test_a_delete_that_removed_nothing_is_not_recorded():
    manager, sink = _manager(), _sink()
    manager.scratch_delete = AsyncMock(return_value={"key": "nope", "deleted": False, "keys": 0})
    spec = create_scratch_memory_tool_spec(
        sandbox_manager=manager, session_id="sess-1", audit_sink=sink
    )

    await spec.handler(operation="delete", key="nope")

    sink.record_scratch_write.assert_not_awaited()


async def test_reads_are_not_recorded():
    manager, sink = _manager(), _sink()
    spec = create_scratch_memory_tool_spec(
        sandbox_manager=manager, session_id="sess-1", audit_sink=sink
    )

    await spec.handler(operation="get", key="todo")
    await spec.handler(operation="list")

    sink.record_scratch_write.assert_not_awaited()


async def test_a_sink_without_the_hook_is_a_no_op_not_a_crash():
    # safe_call treats a missing method as a no-op, so a partial sink written
    # before this hook existed must keep working.
    class PartialSink:
        async def record_file_write(self, **_kwargs):
            return None

    manager = _manager()
    spec = create_scratch_memory_tool_spec(
        sandbox_manager=manager, session_id="sess-1", audit_sink=PartialSink()
    )

    result = await spec.handler(operation="set", key="todo", value="x")

    assert "Stored 'todo'" in result
