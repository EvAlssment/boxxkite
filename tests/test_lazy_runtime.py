import asyncio
from uuid import uuid4

import pytest

from boxkite.lazy_runtime import LazySandboxRuntime


pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    def __init__(self):
        self.create_session_calls = []
        self.ensure_skills_calls = []
        self._create_attempt = 0
        self._ensure_skills_attempt = 0
        self.create_started = asyncio.Event()
        self.allow_create_finish = asyncio.Event()
        self.fail_first_create = False
        self.fail_first_ensure_skills = False
        self.ensure_skills_error = None

    async def create_session(self, **kwargs):
        self._create_attempt += 1
        self.create_session_calls.append(kwargs)
        self.create_started.set()
        if self.fail_first_create and self._create_attempt == 1:
            raise RuntimeError("first-use create failed")
        if not self.allow_create_finish.is_set():
            await self.allow_create_finish.wait()
        return {"pod_name": f"sandbox-pod-{self._create_attempt}"}

    async def ensure_skills(self, session_id, skills):
        self._ensure_skills_attempt += 1
        self.ensure_skills_calls.append((session_id, skills))
        if self.fail_first_ensure_skills and self._ensure_skills_attempt == 1:
            raise RuntimeError("first ensure_skills failed")
        if self.ensure_skills_error is not None:
            raise self.ensure_skills_error
        return {"changed": bool(skills)}


@pytest.mark.asyncio
async def test_lazy_sandbox_runtime_initializes_on_first_use():
    manager = _FakeSandboxManager()
    manager.allow_create_finish.set()
    runtime = LazySandboxRuntime(
        session_id="session-1",
        organization_id=uuid4(),
        work_item_id=None,
        upload_file_ids=None,
        session_skills=[{"instance_slug": "document/pdf"}],
        sandbox_manager=manager,
    )

    prepared = await asyncio.wait_for(runtime.ensure_ready(), timeout=0.2)

    assert prepared.source == "first_use"
    assert len(manager.create_session_calls) == 1
    assert manager.ensure_skills_calls == [
        ("session-1", [{"instance_slug": "document/pdf"}]),
    ]
    assert runtime.get_if_ready() is prepared


@pytest.mark.asyncio
async def test_lazy_sandbox_runtime_collapses_concurrent_first_use_initialization():
    manager = _FakeSandboxManager()
    runtime = LazySandboxRuntime(
        session_id="session-2",
        organization_id=uuid4(),
        work_item_id=None,
        upload_file_ids=None,
        session_skills=[],
        sandbox_manager=manager,
    )

    first_task = asyncio.create_task(runtime.ensure_ready())
    second_task = asyncio.create_task(runtime.ensure_ready())

    await asyncio.wait_for(manager.create_started.wait(), timeout=0.2)
    manager.allow_create_finish.set()
    first_ready, second_ready = await asyncio.gather(first_task, second_task)

    assert len(manager.create_session_calls) == 1
    assert first_ready is second_ready
    assert first_ready.source == "first_use"


@pytest.mark.asyncio
async def test_lazy_sandbox_runtime_retries_after_first_use_failure():
    manager = _FakeSandboxManager()
    manager.fail_first_create = True
    manager.allow_create_finish.set()
    runtime = LazySandboxRuntime(
        session_id="session-3",
        organization_id=uuid4(),
        work_item_id=None,
        upload_file_ids=None,
        session_skills=[],
        sandbox_manager=manager,
    )

    prepared = await asyncio.wait_for(runtime.ensure_ready(), timeout=0.2)

    assert len(manager.create_session_calls) == 2
    assert prepared.source == "first_use"
    assert runtime.get_if_ready() is prepared


@pytest.mark.asyncio
async def test_lazy_sandbox_runtime_retries_ensure_skills_once():
    manager = _FakeSandboxManager()
    manager.allow_create_finish.set()
    manager.fail_first_ensure_skills = True
    runtime = LazySandboxRuntime(
        session_id="session-4",
        organization_id=uuid4(),
        work_item_id=None,
        upload_file_ids=None,
        session_skills=[{"instance_slug": "document/pdf"}],
        sandbox_manager=manager,
    )

    prepared = await asyncio.wait_for(runtime.ensure_ready(), timeout=0.2)

    assert len(manager.create_session_calls) == 1
    assert len(manager.ensure_skills_calls) == 2
    assert prepared.source == "first_use"
    assert prepared.skills_changed is True
    assert runtime.get_if_ready() is prepared


@pytest.mark.asyncio
async def test_lazy_sandbox_runtime_does_not_cache_partial_init_when_ensure_skills_fails():
    manager = _FakeSandboxManager()
    manager.allow_create_finish.set()
    manager.ensure_skills_error = RuntimeError("skills sync failed")
    runtime = LazySandboxRuntime(
        session_id="session-5",
        organization_id=uuid4(),
        work_item_id=None,
        upload_file_ids=None,
        session_skills=[{"instance_slug": "document/pdf"}],
        sandbox_manager=manager,
    )

    with pytest.raises(RuntimeError, match="skills sync failed"):
        await asyncio.wait_for(runtime.ensure_ready(), timeout=0.2)

    assert len(manager.create_session_calls) == 1
    assert len(manager.ensure_skills_calls) == 2
    assert runtime.get_if_ready() is None
