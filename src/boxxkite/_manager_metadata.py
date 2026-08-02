"""Identity, session-metadata reconstruction, and K8s-client init helpers for SandboxManager."""

from ._manager_config import *  # noqa: F401,F403

class MetadataMixin:
    @staticmethod
    def _normalize_upload_file_ids(upload_file_ids: Optional[list[str]]) -> list[str]:
        normalized: list[str] = []
        if not isinstance(upload_file_ids, (list, tuple, set)):
            return normalized
        for file_id in upload_file_ids:
            file_id_str = str(file_id).strip()
            if file_id_str and file_id_str not in normalized:
                normalized.append(file_id_str)
        return normalized

    @staticmethod
    def _build_storage_prefix(
        organization_id: Optional[UUID],
        session_id: str,
        work_item_id: Optional[UUID],
    ) -> str:
        if organization_id and work_item_id:
            return f"work-items/{organization_id}/{work_item_id}"
        org_str = str(organization_id) if organization_id else "default"
        return f"sessions/{org_str}/{session_id}"

    @staticmethod
    def _parse_uuid(value: Any) -> Optional[UUID]:
        if value is None or value == "":
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _identity_labels_and_annotations(
        session_id: str,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Build label-safe identity labels and canonical identity annotations."""
        labels: dict[str, str] = {
            "session-id": _to_k8s_label_value(session_id, prefix="session"),
        }
        annotations: dict[str, str] = {
            SESSION_ID_ANNOTATION: str(session_id or ""),
            ORGANIZATION_ID_ANNOTATION: str(organization_id) if organization_id else "",
            WORK_ITEM_ID_ANNOTATION: str(work_item_id) if work_item_id else "",
        }
        if organization_id:
            labels["organization-id"] = _to_k8s_label_value(str(organization_id), prefix="org")
        if work_item_id:
            labels["work-item-id"] = _to_k8s_label_value(str(work_item_id), prefix="workitem")
        return labels, annotations

    @staticmethod
    def _identity_from_metadata(
        labels: dict[str, str],
        annotations: dict[str, str],
    ) -> tuple[str, str, str]:
        """Extract canonical (session_id, organization_id, work_item_id) from pod metadata."""
        session_id = str(annotations.get(SESSION_ID_ANNOTATION) or labels.get("session-id") or "").strip()
        organization_id = str(
            annotations.get(ORGANIZATION_ID_ANNOTATION) or labels.get("organization-id") or ""
        ).strip()
        work_item_id = str(annotations.get(WORK_ITEM_ID_ANNOTATION) or labels.get("work-item-id") or "").strip()
        return session_id, organization_id, work_item_id

    @staticmethod
    def _metadata_matches_session_id(
        labels: dict[str, str],
        annotations: dict[str, str],
        session_id: str,
    ) -> bool:
        """
        Match pod identity against a canonical session id.

        Prefer annotation-based identity; use raw legacy label fallback only if
        annotation is missing.
        """
        expected = str(session_id or "").strip()
        annotated = str(annotations.get(SESSION_ID_ANNOTATION) or "").strip()
        if annotated:
            return annotated == expected
        return str(labels.get("session-id") or "").strip() == expected

    @staticmethod
    def _extract_work_item_id_from_execution_args(arguments: Any) -> Optional[UUID]:
        if not isinstance(arguments, dict):
            return None

        direct_value = arguments.get("work_item_id")
        direct_uuid = MetadataMixin._parse_uuid(direct_value)
        if direct_uuid:
            return direct_uuid

        for container_key in ("work_item_event", "file_event"):
            nested = arguments.get(container_key)
            if not isinstance(nested, dict):
                continue
            nested_uuid = MetadataMixin._parse_uuid(nested.get("work_item_id"))
            if nested_uuid:
                return nested_uuid

        return None

    async def _reconstruct_session_metadata_from_db(self, session_id: str) -> Optional[dict]:
        """
        Reconstruct recovery metadata via the optional SessionMetadataStore hook,
        for use when pod metadata is unavailable (e.g. the pod was already deleted).

        This is a thin adapter: the actual reconstruction logic lives wherever
        the caller's SessionMetadataStore is implemented (see session_store.py).
        With no store configured (the default), this always returns None, which
        the caller treats identically to "recovery metadata not found".
        """
        session_id_str = str(session_id or "").strip()
        if not session_id_str:
            return None

        try:
            reconstructed = await self._session_metadata_store.reconstruct(session_id_str)
        except Exception as e:
            logger.warning(
                f"[SandboxManager] SessionMetadataStore.reconstruct failed for "
                f"{session_id_str}: {e}"
            )
            return None

        if reconstructed is None:
            return None

        return {
            "organization_id": reconstructed.organization_id,
            "work_item_id": reconstructed.work_item_id,
            "upload_file_ids": self._normalize_upload_file_ids(reconstructed.upload_file_ids),
            "storage_prefix": reconstructed.storage_prefix
            or self._build_storage_prefix(
                reconstructed.organization_id,
                session_id_str,
                reconstructed.work_item_id,
            ),
        }

    async def _record_session_metadata(
        self,
        session_id: str,
        *,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        storage_prefix: str,
        upload_file_ids: Optional[list[str]] = None,
    ) -> None:
        """
        Best-effort mirror of a newly-established session into the optional
        SessionMetadataStore, so stores that implement `record()` (e.g.
        SQLiteSessionMetadataStore) stay current with no extra caller code.

        `record` is not part of the SessionMetadataStore Protocol (only
        `reconstruct` is required), so this feature-detects via `hasattr` the
        same way AuditSink call sites do — a store that only implements
        `reconstruct()` is unaffected, and a failure here never blocks session
        creation.
        """
        record = getattr(self._session_metadata_store, "record", None)
        if record is None:
            return
        try:
            await record(
                session_id,
                organization_id=organization_id,
                work_item_id=work_item_id,
                storage_prefix=storage_prefix,
                upload_file_ids=upload_file_ids,
            )
        except Exception as e:
            logger.warning(
                f"[SandboxManager] SessionMetadataStore.record failed for "
                f"{session_id}: {e}"
            )

    async def _forget_session_metadata(self, session_id: str) -> None:
        """
        Best-effort cleanup of a torn-down session from the optional
        SessionMetadataStore, mirroring `_record_session_metadata` above.
        Feature-detected the same way; a store with no `forget()` is
        unaffected, and a failure here never blocks session teardown.
        """
        forget = getattr(self._session_metadata_store, "forget", None)
        if forget is None:
            return
        try:
            await forget(session_id)
        except Exception as e:
            logger.warning(
                f"[SandboxManager] SessionMetadataStore.forget failed for "
                f"{session_id}: {e}"
            )

    async def _init_k8s(self):
        """Initialize K8s client (lazy)."""
        if self._k8s_initialized:
            return
        async with self._k8s_init_lock:
            if self._k8s_initialized:
                return

            try:
                config_source = await load_kubernetes_config()
                logger.info(f"[SandboxManager] Using {config_source} K8s config")
            except ConfigException as e:
                logger.warning(f"[SandboxManager] K8s config failed: {e}")
                return

            self._k8s_api_client = build_kubernetes_api_client()
            self._k8s_core_api = client.CoreV1Api(self._k8s_api_client)
            self._k8s_networking_api = client.NetworkingV1Api(self._k8s_api_client)
            self._k8s_initialized = True

            if self._use_k8s_proxy:
                logger.info(
                    f"[SandboxManager] K8s API proxy mode enabled, "
                    f"routing sidecar HTTP via {self._k8s_proxy_url}"
                )

    # =========================================================================
    # Session Resolution (K8s as source of truth)
    # =========================================================================

