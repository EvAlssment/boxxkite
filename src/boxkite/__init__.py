"""
boxkite — a self-hostable, Kubernetes-native sandbox for agent code execution.

This package provides production-grade sandboxing for agent code execution:

**Architecture (SandboxManager + sidecar):**
- SandboxManager: K8s pod lifecycle & HTTP routing to the sidecar
- WarmPoolManager: Pre-warmed pod pool for fast session startup
- LazySandboxRuntime: defers session creation until a tool actually needs it
- Tool surface: bash_tool, file_create, view, str_replace, present_files
  (LangChain tools, in `boxkite.tools`)

All code execution happens via SandboxManager, which routes HTTP calls to
the sidecar container running in a K8s pod (or Docker Compose for local dev).

**File Storage:**
Files are stored in the sidecar's own S3/Azure storage at:
  work-items/{org_id}/{work_item_id}/
    uploads/       - User-uploaded files (read-only to agents)
    workspace/     - Agent workspace files (read-write)
    outputs/       - Agent deliverables

**Optional integration points:**
- `boxkite.audit.AuditSink` — mirror file writes into your own system of record
- `boxkite.session_store.SessionMetadataStore` — reconstruct session ownership
  when a pod is lost before K8s labels/annotations can be read

Neither is required. See docs/EXTENDING.md for details.
"""

from .manager import get_sandbox_manager, close_sandbox_manager, SandboxManager
from .warm_pool import get_warm_pool, close_warm_pool, WarmPoolManager

__all__ = [
    "get_sandbox_manager",
    "close_sandbox_manager",
    "SandboxManager",
    "get_warm_pool",
    "close_warm_pool",
    "WarmPoolManager",
]

__version__ = "0.2.1"
