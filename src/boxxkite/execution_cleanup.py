"""
Shared sandbox cleanup helpers for execution lifecycles.
"""

from __future__ import annotations

import logging
from uuid import UUID

from . import get_sandbox_manager

logger = logging.getLogger(__name__)


async def reap_execution_sandbox(execution_id: UUID) -> None:
    """
    Best-effort sandbox cleanup for terminal execution transitions.
    """
    session_id = f"execution:{execution_id}"
    try:
        sandbox_manager = get_sandbox_manager()
        await sandbox_manager.destroy_session(session_id)
        logger.info(f"Reaped sandbox session {session_id}")
    except Exception as e:
        logger.warning(f"Failed to reap sandbox session {session_id}: {e}")
