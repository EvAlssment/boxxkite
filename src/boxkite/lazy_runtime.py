"""Lazy sandbox runtime for first-use sandbox initialization."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


async def _ensure_session_skills_with_retry(
    manager: Any,
    *,
    session_id: str,
    session_skills: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ensure session skills, retrying once before surfacing failure."""
    try:
        return await manager.ensure_skills(session_id, session_skills)
    except Exception as first_error:
        logger.warning(
            "[LazySandboxRuntime] Failed to ensure skills for %s; retrying once: %s",
            session_id,
            first_error,
            exc_info=True,
        )
        return await manager.ensure_skills(session_id, session_skills)


@dataclass
class PreparedSandboxRuntime:
    """Resolved sandbox runtime state shared by tool calls and middleware."""

    source: str
    sandbox_manager: Any
    sandbox_session: dict
    session_id: str
    skills_prompt: str
    sandbox_session_ms: float
    sandbox_session_elapsed_ms: float
    ensure_skills_ms: float
    ensure_skills_elapsed_ms: Optional[float]
    ensured_skills_count: int
    skills_changed: Optional[bool]


async def prepare_sandbox_runtime(
    *,
    session_id: str,
    organization_id: Optional[UUID],
    work_item_id: Optional[UUID],
    upload_file_ids: Optional[List[str]],
    session_skills: Optional[List[Dict[str, Any]]] = None,
    sandbox_manager: Optional[Any] = None,
    source: str = "first_use",
) -> PreparedSandboxRuntime:
    """Create or reuse the sandbox session and ensure any required skills."""

    from . import get_sandbox_manager

    manager = sandbox_manager or get_sandbox_manager()
    overall_started = time.monotonic()

    session_wait_start = time.monotonic()
    try:
        sandbox_session = await manager.create_session(
            organization_id=organization_id,
            session_id=session_id,
            work_item_id=work_item_id,
            upload_file_ids=upload_file_ids,
        )
    except Exception as first_error:
        logger.warning(
            "[LazySandboxRuntime] Sandbox session creation failed for %s; retrying once: %s",
            session_id,
            first_error,
        )
        sandbox_session = await manager.create_session(
            organization_id=organization_id,
            session_id=session_id,
            work_item_id=work_item_id,
            upload_file_ids=upload_file_ids,
        )
    session_wait_ms = (time.monotonic() - session_wait_start) * 1000
    sandbox_session_elapsed_ms = (time.monotonic() - overall_started) * 1000

    ensure_skills_ms = 0.0
    ensure_skills_elapsed_ms: Optional[float] = None
    ensured_skills_count = 0
    skills_changed: Optional[bool] = None

    if session_skills:
        ensure_start = time.monotonic()
        ensured_skills_count = len(session_skills)
        ensure_result = await _ensure_session_skills_with_retry(
            manager,
            session_id=session_id,
            session_skills=session_skills,
        )
        skills_changed = ensure_result.get("changed")
        ensure_skills_ms = (time.monotonic() - ensure_start) * 1000
        ensure_skills_elapsed_ms = (time.monotonic() - overall_started) * 1000

    return PreparedSandboxRuntime(
        source=source,
        sandbox_manager=manager,
        sandbox_session=sandbox_session,
        session_id=session_id,
        skills_prompt="",
        sandbox_session_ms=session_wait_ms,
        sandbox_session_elapsed_ms=sandbox_session_elapsed_ms,
        ensure_skills_ms=ensure_skills_ms,
        ensure_skills_elapsed_ms=ensure_skills_elapsed_ms,
        ensured_skills_count=ensured_skills_count,
        skills_changed=skills_changed,
    )


class LazySandboxRuntime:
    """Initialize sandbox state only when a sandbox-backed consumer needs it."""

    def __init__(
        self,
        *,
        session_id: str,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        upload_file_ids: Optional[List[str]],
        session_skills: Optional[List[Dict[str, Any]]] = None,
        sandbox_manager: Optional[Any] = None,
    ) -> None:
        self.session_id = session_id
        self._organization_id = organization_id
        self._work_item_id = work_item_id
        self._upload_file_ids = upload_file_ids
        self._session_skills = list(session_skills or [])
        self._sandbox_manager = sandbox_manager
        self._prepared: Optional[PreparedSandboxRuntime] = None
        self._init_lock = asyncio.Lock()

    def get_if_ready(self) -> Optional[PreparedSandboxRuntime]:
        """Return the prepared runtime only when already available."""
        return self._prepared

    async def ensure_ready(self) -> PreparedSandboxRuntime:
        """Return a ready sandbox runtime, initializing it on first use."""
        if self._prepared is not None:
            return self._prepared

        async with self._init_lock:
            if self._prepared is not None:
                return self._prepared

            prepared = await prepare_sandbox_runtime(
                session_id=self.session_id,
                organization_id=self._organization_id,
                work_item_id=self._work_item_id,
                upload_file_ids=self._upload_file_ids,
                session_skills=self._session_skills,
                sandbox_manager=self._sandbox_manager,
                source="first_use",
            )

            self._prepared = prepared
            logger.debug(
                "[LazySandboxRuntime] Sandbox runtime ready for session %s via %s",
                self.session_id,
                prepared.source,
            )
            return prepared


async def resolve_sandbox_operation_context(
    *,
    lazy_runtime: Optional[LazySandboxRuntime] = None,
    sandbox_manager: Optional[Any] = None,
    session_id: Optional[str] = None,
) -> tuple[Any, Optional[str]]:
    """Resolve the sandbox manager/session pair used by a tool or middleware."""
    if lazy_runtime is not None:
        prepared = await lazy_runtime.ensure_ready()
        return prepared.sandbox_manager, prepared.session_id

    if sandbox_manager is None:
        raise ValueError("sandbox_manager must be provided")

    return sandbox_manager, session_id
