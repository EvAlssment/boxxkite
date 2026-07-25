"""Output-file collection, skills, flush, snapshot, and health for SandboxManager."""

from ._manager_config import *  # noqa: F401,F403

class FilesSkillsMixin:
    async def present_files(
        self,
        session_id: str,
        filepaths: list[str],
        include_operations: bool = False,
    ) -> list[dict] | dict:
        """
        Ensure files are synced to S3 and get file info.

        Args:
            session_id: Session ID
            filepaths: List of file paths to present

        Returns:
            By default, list of dicts with file_path, s3_key, size, content_type.
            When include_operations=True, returns the raw sidecar response
            (includes files + copy_operations).
        """
        async def _request() -> list[dict] | dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/present-files", json={
                "filepaths": filepaths,
            })
            response.raise_for_status()
            result = response.json()
            if include_operations:
                return result
            return result.get("files", [])

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="present_files",
            request_fn=_request,
        )

    async def collect_output_files(
        self,
        session_id: str,
        sync_to_storage: bool = True,
    ) -> list[dict]:
        """
        Collect metadata for files under /mnt/user-data/outputs.

        This is used by chat streaming to keep the right-side Files tab aligned
        with sandbox deliverables. When sync_to_storage=True, each output file is
        synced via present_files so storage_key metadata is available.
        """
        started_at = time.perf_counter()
        glob_started_at = time.perf_counter()
        matches = await self.glob(
            session_id=session_id,
            pattern="**/*",
            path="/mnt/user-data/outputs",
        )
        logger.info(
            "[Timing] SandboxManager.collect_output_files.glob: %dms (session_id=%s matches=%d)",
            round((time.perf_counter() - glob_started_at) * 1000),
            session_id,
            len(matches),
        )

        if not matches:
            logger.info(
                "[Timing] SandboxManager.collect_output_files.total: %dms (session_id=%s matches=0)",
                round((time.perf_counter() - started_at) * 1000),
                session_id,
            )
            return []

        files_by_path: dict[str, dict] = {}
        for entry in matches:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path", "") or "")
            if not path:
                continue
            files_by_path[path] = entry

        if not files_by_path:
            return []

        presented_by_path: dict[str, dict] = {}
        if sync_to_storage:
            try:
                present_started_at = time.perf_counter()
                presented = await self.present_files(
                    session_id=session_id,
                    filepaths=sorted(files_by_path.keys()),
                )
                logger.info(
                    "[Timing] SandboxManager.collect_output_files.present_files: %dms (session_id=%s count=%d)",
                    round((time.perf_counter() - present_started_at) * 1000),
                    session_id,
                    len(presented),
                )
                for item in presented:
                    if not isinstance(item, dict):
                        continue
                    file_path = str(item.get("file_path", "") or "")
                    if file_path:
                        presented_by_path[file_path] = item
            except Exception as e:
                logger.warning(
                    f"[SandboxManager] Failed syncing outputs for session {session_id}: {e}"
                )

        results: list[dict] = []
        for path in sorted(files_by_path.keys()):
            listed = files_by_path[path]
            presented = presented_by_path.get(path, {})
            results.append(
                {
                    "path": path,
                    "size": int(presented.get("size", listed.get("size", 0)) or 0),
                    "modified_at": listed.get("modified_at"),
                    "content_type": presented.get("content_type"),
                    "storage_key": presented.get("storage_key")
                    or presented.get("s3_key"),
                }
            )

        logger.info(
            "[Timing] SandboxManager.collect_output_files.total: %dms (session_id=%s count=%d)",
            round((time.perf_counter() - started_at) * 1000),
            session_id,
            len(results),
        )
        return results

    def _compute_skills_rev(self, skills: list[dict]) -> str:
        """Compute deterministic hash used by sidecar ensure-skills endpoint."""
        canonical = json.dumps(skills, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def ensure_skills(
        self,
        session_id: str,
        skills: list[dict],
    ) -> dict:
        """Ensure normalized skills payload is materialized at /mnt/skills."""
        self._cache_session_skills(session_id, skills)
        skills_rev = self._compute_skills_rev(skills)

        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post(
                "/ensure-skills",
                json={
                    "skills": skills,
                    "skills_rev": skills_rev,
                },
            )
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="ensure_skills",
            request_fn=_request,
        )

    async def inject_skills(
        self,
        session_id: str,
        skills: list[dict],
    ) -> dict:
        """Compatibility wrapper for older callers."""
        return await self.ensure_skills(session_id=session_id, skills=skills)

    async def flush(self, session_id: str) -> None:
        """Flush any pending outputs to S3."""
        async def _request() -> None:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/flush")
            response.raise_for_status()

        await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="flush",
            request_fn=_request,
        )

    async def snapshot(self, session_id: str) -> dict:
        """Confirm-flush this session's workspace/outputs and return the
        as-of manifest a filesystem snapshot needs (docs/SNAPSHOT-DESIGN.md).

        Returns the sidecar's own `/flush/confirmed` response verbatim:
        `{"storage_prefix": ..., "storage_keys": [...]}` — `storage_keys`
        are the confirmed, durably-uploaded objects under `storage_prefix`,
        not the *pending* set `/flush` returns. SandboxManager does NOT
        perform the actual snapshot-storage copy itself: it holds no
        storage credentials of its own today (only the sidecar does, for
        per-session sync) — the caller (the control plane, which holds its
        own least-privilege `snapshots/*`-scoped credential per the design
        doc's security section) uses this manifest to perform its own
        storage-side copy.
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/flush/confirmed")
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="snapshot",
            request_fn=_request,
        )

    # =========================================================================
    # Health & Status
    # =========================================================================

    async def health_check(self, session_id: str) -> dict:
        """Check health of a session's sidecar."""
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.get("/health")
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="health_check",
            request_fn=_request,
        )

    async def list_sessions(self) -> list[dict]:
        """List all active sessions."""
        if self._use_docker_compose:
            return [
                {
                    "session_id": sid,
                    "organization_id": str(meta.get("organization_id", "")),
                    "work_item_id": str(meta.get("work_item_id", "")),
                    "pod_name": "compose-sandbox",
                }
                for sid, meta in self._compose_sessions.items()
            ]

        await self._init_k8s()
        if not self._k8s_core_api:
            return []

        try:
            pods = await self._k8s_core_api.list_namespaced_pod(
                namespace=SANDBOX_NAMESPACE,
                label_selector="app=sandbox,sandbox.boxkite.dev/status=claimed",
            )
        except Exception as e:
            logger.warning(f"[SandboxManager] Failed to list sessions: {e}")
            return []

        sessions = []
        for pod in pods.items:
            labels = pod.metadata.labels or {}
            annotations = pod.metadata.annotations or {}
            session_id, organization_id, work_item_id = self._identity_from_metadata(labels, annotations)
            if not session_id:
                continue
            sessions.append({
                "session_id": session_id,
                "organization_id": organization_id,
                "work_item_id": work_item_id,
                "pod_name": pod.metadata.name,
            })
        return sessions
