"""Unified file processing service.

Auto-detects file type by MIME/extension and delegates to the appropriate
tool for text extraction. Updates File.status through the processing pipeline.

Handler resolution is delegated to :class:`HandlerRegistry`
(``leagent.services.file_processing.registry``), which supports extension
and MIME-type based resolution with priority ordering and runtime extensibility.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def _build_legacy_ext_map() -> dict[str, str]:
    """Build the legacy EXTENSION_HANDLER_MAP from the HandlerRegistry.

    Kept for backward compatibility with existing tests and callers that
    import ``EXTENSION_HANDLER_MAP`` from this module.
    """
    from leagent.services.file_processing.registry import get_handler_registry

    registry = get_handler_registry()
    ext_map: dict[str, str] = {}
    for h in registry.list_handlers():
        for ext in h.extensions:
            ext_map[ext] = h.name
    return ext_map


# Backward-compatible extension → handler map, derived from the
# HandlerRegistry. Tests and external callers may import this directly.
EXTENSION_HANDLER_MAP: dict[str, str] = _build_legacy_ext_map()


class FileProcessingService:
    """Service that auto-processes uploaded files to extract text and metadata."""

    def __init__(self) -> None:
        self._started = True

    @staticmethod
    def _active_index_version() -> str:
        """Return the version that identifies the active RAG projection."""
        from leagent.config.settings import get_settings

        return get_settings().knowledge.index_version

    async def _get_index_state(self, file_id: str) -> Any | None:
        """Read the current RAG projection state without making it required."""
        from sqlmodel import select

        from leagent.db.models.knowledge_index import KnowledgeIndexState
        from leagent.db.sqlite_compat import uuid_column_equals
        from leagent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return None
        try:
            logical_id = UUID(file_id)
            async with sm.db.session() as session:
                result = await session.exec(
                    select(KnowledgeIndexState).where(
                        uuid_column_equals(
                            session,
                            KnowledgeIndexState.file_id,
                            logical_id,
                        )
                    )
                )
                return result.first()
        except Exception:  # noqa: BLE001 - migration 0021 may not exist yet
            logger.debug(
                "knowledge_index_state_lookup_skipped file_id=%s",
                file_id,
                exc_info=True,
            )
            return None

    async def process_file(
        self,
        file_path: str,
        mime_type: str | None = None,
        original_name: str | None = None,
    ) -> dict[str, Any]:
        """Process a file and extract text content.

        Returns a dict with 'extracted_text', 'metadata', and 'handler' keys.
        """
        path = Path(file_path)
        if not path.exists():
            return {"error": f"File not found: {file_path}", "extracted_text": None}

        handler_name = self._resolve_handler(path, mime_type, original_name)
        if not handler_name:
            return {
                "extracted_text": None,
                "handler": None,
                "message": "No handler available for this file type",
            }

        try:
            result = await self._run_handler(handler_name, path)
            text = self._extract_text_from_result(handler_name, result)
            return {
                "extracted_text": text,
                "metadata": result,
                "handler": handler_name,
            }
        except Exception as exc:
            logger.warning("File processing failed: %s", exc, exc_info=True)
            return {
                "extracted_text": None,
                "handler": handler_name,
                "error": str(exc),
            }

    def _resolve_handler(
        self,
        path: Path,
        mime_type: str | None,
        original_name: str | None,
    ) -> str | None:
        from leagent.services.file_processing.registry import get_handler_registry

        registry = get_handler_registry()
        return registry.resolve_name(path=path, mime_type=mime_type, original_name=original_name)

    async def _run_handler(self, handler_name: str, path: Path) -> dict[str, Any]:
        from leagent.tools.base import ToolContext
        from leagent.tools.registry import get_registry

        registry = get_registry()
        tool = registry.get(handler_name)
        if tool is None:
            raise RuntimeError(f"Tool '{handler_name}' not found in registry")

        ctx = ToolContext(user_id=None, session_id=None, task_id=None)

        if handler_name == "archive_manager":
            params = {"operation": "info", "archive_path": str(path)}
        elif handler_name in (
            "csv_processor",
            "text_processor",
            "markdown_processor",
            "html_processor",
            "config_file",
        ):
            params = {"operation": "read", "file_path": str(path)}
        elif handler_name == "image_ocr":
            params = {"image_path": str(path)}
        elif handler_name in ("pdf_reader", "word_reader", "excel_reader"):
            params = {"file_path": str(path)}
        else:
            params = {"file_path": str(path)}

        result = await tool.run(params, ctx)
        return result.data if hasattr(result, "data") else result

    def _extract_text_from_result(self, handler_name: str, result: dict[str, Any]) -> str | None:
        if not isinstance(result, dict):
            return None

        for key in ("text", "extracted_text", "raw_text", "content"):
            if key in result and isinstance(result[key], str):
                return result[key][:500_000]

        if "data" in result and isinstance(result["data"], dict):
            return self._extract_text_from_result(handler_name, result["data"])

        return None

    async def process_and_update_db(
        self,
        file_id: str,
        file_path: str,
        mime_type: str | None = None,
        original_name: str | None = None,
    ) -> dict[str, Any]:
        """Process a file and update the database File record.

        Extracted text is written to the ``file_contents`` table (separated
        from the ``files`` catalog row to avoid bloat).  When the content
        hash is unchanged, chunk re-indexing is skipped entirely.
        """
        import hashlib

        from leagent.db.models.file import FileStatus
        from leagent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return await self.process_file(file_path, mime_type, original_name)

        # Capture the prior projection before moving it to ``extracting``.
        # The skip decision runs after text extraction, so reading the current
        # state there would only see this in-progress transition and would
        # either always rebuild or accidentally accept a replaced version.
        previous_index_state = await self._get_index_state(file_id)
        await self._update_file_record(
            file_id,
            status=FileStatus.PROCESSING,
            is_indexed=False,
        )
        await self._set_index_state(file_id, "extracting")
        staged_path: str | None = None
        try:
            local_path, staged_path = await self._materialize_processing_source(
                sm,
                file_id=file_id,
                storage_path=file_path,
                original_name=original_name,
            )
            result = await self.process_file(local_path, mime_type, original_name)
        except Exception:
            await self._update_file_record(
                file_id,
                status=FileStatus.FAILED,
                is_indexed=False,
            )
            await self._set_index_state(file_id, "failed", last_error="extraction_failed")
            raise
        finally:
            if staged_path is not None:
                try:
                    os.unlink(staged_path)
                except OSError:
                    logger.debug("remote_processing_stage_cleanup_failed", exc_info=True)
        if result.get("error"):
            await self._update_file_record(
                file_id,
                status=FileStatus.FAILED,
                is_indexed=False,
            )
            await self._set_index_state(file_id, "failed", last_error=str(result["error"]))
            raise RuntimeError(str(result["error"]))

        extracted = result.get("extracted_text")
        if not isinstance(extracted, str) or not extracted.strip():
            await self._update_file_record(
                file_id,
                status=FileStatus.PROCESSED,
                is_indexed=False,
                summary=None,
            )
            # Clear stale FileContent if any.
            await self._upsert_file_content(file_id, None, None)
            await self._set_index_state(
                file_id,
                "degraded",
                last_error="no_extracted_text",
            )
            return result

        content_hash = hashlib.sha256(extracted.encode("utf-8")).hexdigest()[:16]

        index_job_id = await self._enqueue_index_job(file_id, content_hash)

        # Check the previous index state before replacing ``FileContent``.
        # The content upsert below deliberately happens before the file row
        # update, but it must not erase the old hash that is used to decide
        # whether re-indexing can be skipped.  Reading after the upsert makes
        # every re-upload with existing chunks look unchanged.
        index_skipped = await self._content_unchanged(
            file_id,
            content_hash,
            index_state=previous_index_state,
        )

        # Write extracted_text to the dedicated FileContent table instead of
        # bloating the files row.  The legacy File.extracted_text column is
        # still set for backward compatibility (read paths that haven't been
        # migrated yet).
        await self._upsert_file_content(file_id, extracted, content_hash)

        await self._update_file_record(
            file_id,
            status=FileStatus.PROCESSING,
            extracted_text=extracted,
            is_indexed=False,
            summary=None,
        )

        # Skip chunk re-indexing when content is unchanged.
        if index_skipped:
            await self._update_file_record(
                file_id,
                status=FileStatus.PROCESSED,
                is_indexed=True,
            )
            await self._set_index_state(
                file_id,
                "ready",
                content_hash=content_hash,
                completed_at=datetime.utcnow(),
            )
            if index_job_id is not None:
                await self._mark_index_job_succeeded(index_job_id, chunk_count=1)
            result["_index_skipped"] = True
            return result

        try:
            await self._set_index_state(
                file_id,
                "chunked",
                content_hash=content_hash,
            )
            await self._index_chunks(file_id, result)
        except Exception:
            await self._update_file_record(
                file_id,
                status=FileStatus.FAILED,
                is_indexed=False,
            )
            await self._set_index_state(
                file_id,
                "failed",
                content_hash=content_hash,
                last_error="indexing_failed",
            )
            if index_job_id is not None:
                await self._mark_index_job_failed(index_job_id, "indexing_failed")
            raise

        if index_job_id is not None:
            await self._mark_index_job_succeeded(
                index_job_id,
                chunk_count=int(result.get("_index_chunk_count") or 0),
                embedded_count=int(result.get("_index_embedded_count") or 0),
            )

        return result

    @staticmethod
    async def _materialize_processing_source(
        service_manager: Any,
        *,
        file_id: str,
        storage_path: str,
        original_name: str | None,
    ) -> tuple[str, str | None]:
        """Return a local input path, staging S3 content only for extraction.

        Extractor tools use path-based libraries.  The temporary file is not a
        managed artifact and is removed immediately after extraction; the
        durable source remains the object-storage blob.
        """
        if not str(storage_path).startswith("s3://"):
            return storage_path, None

        file_service = getattr(service_manager, "file_service", None)
        if file_service is None:
            raise RuntimeError("S3 source cannot be processed without FileService")
        try:
            ref = await file_service.resolve(UUID(file_id))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("S3 source has an invalid file id") from exc
        if ref is None:
            raise RuntimeError("S3 source file is missing from the file catalog")
        recorded_path = str((ref.metadata or {}).get("storage_path") or "")
        if recorded_path and recorded_path != storage_path:
            raise RuntimeError("S3 source does not match the file catalog entry")

        raw, _content_type = await file_service.download(ref)
        suffix = Path(original_name or ref.filename).suffix
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=suffix,
            prefix="leagent-extract-",
            delete=False,
        ) as staged:
            staged.write(raw)
            return staged.name, staged.name

    async def _enqueue_index_job(self, file_id: str, content_hash: str) -> Any | None:
        """Create an idempotent durable index record for the current content."""
        from uuid import UUID

        from leagent.db.models.file import File
        from leagent.db.sqlite_compat import load_entity_by_id
        from leagent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return None
        try:
            async with sm.db.session() as session:
                file = await load_entity_by_id(
                    session,
                    File,
                    UUID(file_id),
                    parent_table="files",
                )
                user_id = file.user_id if file is not None else None
            return await sm.db.repositories.knowledge_index.enqueue(
                UUID(file_id),
                user_id,
                content_hash=content_hash,
                index_version=self._active_index_version(),
            )
        except Exception:  # noqa: BLE001 - pre-0021 installations remain usable
            logger.debug(
                "knowledge_index_job_enqueue_skipped file_id=%s",
                file_id,
                exc_info=True,
            )
            return None

    async def _mark_index_job_succeeded(
        self,
        job_id: Any,
        *,
        chunk_count: int,
        embedded_count: int = 0,
    ) -> None:
        from leagent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return
        try:
            await sm.db.repositories.knowledge_index.mark_succeeded(
                job_id,
                chunk_count=chunk_count,
                embedded_count=embedded_count,
            )
        except Exception:  # noqa: BLE001
            logger.debug("knowledge_index_job_success_update_skipped", exc_info=True)

    async def _mark_index_job_failed(self, job_id: Any, error: str) -> None:
        from leagent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return
        try:
            await sm.db.repositories.knowledge_index.mark_failed(job_id, error)
        except Exception:  # noqa: BLE001
            logger.debug("knowledge_index_job_failure_update_skipped", exc_info=True)

    async def _set_index_state(
        self,
        file_id: str,
        status: str,
        *,
        content_hash: str | None = None,
        chunk_count: int | None = None,
        embedded_count: int | None = None,
        last_error: str | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        """Best-effort persistence of the versioned indexing lifecycle.

        The helper is intentionally non-blocking for legacy installations that
        have not run migration 0021 yet.  File processing remains usable while
        the state table is introduced, but every current database created by
        the application has the durable contract.
        """
        from uuid import UUID

        from leagent.db.models.knowledge_index import KnowledgeIndexStatus
        from leagent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return
        try:
            repositories = getattr(sm.db, "repositories", None)
            repository = getattr(repositories, "knowledge_index", None)
            if repository is None:
                return
            user_id = None
            from leagent.db.models.file import File
            from leagent.db.sqlite_compat import load_entity_by_id

            async with sm.db.session() as session:
                row = await load_entity_by_id(
                    session,
                    File,
                    UUID(file_id),
                    parent_table="files",
                )
                user_id = row.user_id if row is not None else None
            await repository.upsert_state(
                UUID(file_id),
                user_id,
                status=KnowledgeIndexStatus(status),
                index_version=self._active_index_version(),
                content_hash=content_hash,
                chunk_count=chunk_count,
                embedded_count=embedded_count,
                last_error=last_error,
                completed_at=completed_at,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "knowledge_index_state_update_skipped file_id=%s",
                file_id,
                exc_info=True,
            )

    async def _update_file_record(self, file_id: str, **values: Any) -> None:
        """Update one file using the cross-dialect UUID comparison contract."""
        from uuid import UUID

        from sqlalchemy import update as sa_update

        from leagent.db.models.file import File
        from leagent.db.sqlite_compat import uuid_column_equals
        from leagent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return
        logical_id = UUID(file_id)
        values["updated_at"] = datetime.utcnow()
        async with sm.db.session() as session:
            await session.execute(
                sa_update(File).where(uuid_column_equals(session, File.id, logical_id)).values(**values)
            )

    async def _upsert_file_content(
        self,
        file_id: str,
        extracted_text: str | None,
        content_hash: str | None,
    ) -> None:
        """Insert or update the ``file_contents`` row for *file_id*."""
        from uuid import UUID, uuid4

        import sqlalchemy as sa
        from sqlmodel import select

        from leagent.db.models.file_content import FileContent
        from leagent.db.sqlite_compat import (
            load_entity_by_id,
            session_dialect_name,
            sqlite_parent_id_text,
            uuid_column_equals,
        )
        from leagent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return
        logical_id = UUID(file_id)
        async with sm.db.session() as session:
            from leagent.db.models.file import File

            file_row = await load_entity_by_id(session, File, logical_id, parent_table="files")
            user_id = file_row.user_id if file_row else None

            # Cross-dialect upsert: select then update-or-insert.
            existing = (
                await session.exec(
                    select(FileContent).where(uuid_column_equals(session, FileContent.file_id, logical_id))
                )
            ).first()

            now = datetime.utcnow()
            if existing is not None:
                # Do not mutate the ORM instance here.  Legacy SQLite rows
                # can store UUIDs with dashes while SQLAlchemy binds UUID
                # primary keys as hex strings; an ORM flush would then report
                # a false ``StaleDataError``.  The normalized FK predicate is
                # dialect-safe and updates the exact legacy row.
                await session.execute(
                    sa.update(FileContent)
                    .where(uuid_column_equals(session, FileContent.file_id, logical_id))
                    .values(
                        extracted_text=extracted_text,
                        text_char_count=len(extracted_text) if extracted_text else 0,
                        text_hash=content_hash,
                        updated_at=now,
                    )
                )
            elif session_dialect_name(session) == "sqlite":
                # Raw insert preserving the exact parent-id text so the FK
                # check passes for both dashed and hex stored file ids.
                parent_id_text = await sqlite_parent_id_text(session, "files", logical_id)
                await session.execute(
                    sa.text(
                        "INSERT INTO file_contents "
                        "(created_at, updated_at, id, file_id, user_id, "
                        "extracted_text, text_char_count, text_hash) "
                        "VALUES (:created_at, :updated_at, :id, :file_id, "
                        ":user_id, :extracted_text, :text_char_count, :text_hash)"
                    ),
                    {
                        "created_at": now,
                        "updated_at": now,
                        "id": str(uuid4()),
                        "file_id": parent_id_text,
                        "user_id": str(user_id) if user_id else None,
                        "extracted_text": extracted_text,
                        "text_char_count": len(extracted_text) if extracted_text else 0,
                        "text_hash": content_hash,
                    },
                )
            else:
                session.add(
                    FileContent(
                        file_id=logical_id,
                        user_id=user_id,
                        extracted_text=extracted_text,
                        text_char_count=len(extracted_text) if extracted_text else 0,
                        text_hash=content_hash,
                    )
                )

    async def _content_unchanged(
        self,
        file_id: str,
        new_hash: str,
        *,
        index_state: Any | None = None,
    ) -> bool:
        """True only when the active RAG projection already matches *new_hash*.

        A matching text hash alone is insufficient: the chunk projection must
        exist, and its recorded version, status, and content hash must match
        the active configuration.  ``index_state`` lets the caller preserve
        the ready state that existed before it marked the new attempt as
        ``extracting``.
        """
        from uuid import UUID

        from leagent.db.models.file_content import FileContent
        from leagent.db.models.knowledge_index import KnowledgeIndexState, KnowledgeIndexStatus
        from leagent.db.sqlite_compat import uuid_column_equals
        from leagent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return False
        logical_id = UUID(file_id)
        async with sm.db.session() as session:
            from sqlmodel import select

            result = await session.exec(
                select(FileContent.text_hash).where(uuid_column_equals(session, FileContent.file_id, logical_id))
            )
            existing_hash = result.first()
            if existing_hash is None:
                return False

            # Also check that chunks actually exist (may be empty on first
            # upload if indexing previously failed).
            from leagent.db.models.document_chunk import DocumentChunk

            chunk_count = (
                await session.exec(
                    select(DocumentChunk.id)
                    .where(uuid_column_equals(session, DocumentChunk.file_id, logical_id))
                    .limit(1)
                )
            ).first()
            if chunk_count is None:
                return False

            if index_state is None:
                state_result = await session.exec(
                    select(KnowledgeIndexState).where(
                        uuid_column_equals(
                            session,
                            KnowledgeIndexState.file_id,
                            logical_id,
                        )
                    )
                )
                index_state = state_result.first()

            if index_state is None:
                return False
            state_status = getattr(index_state.status, "value", index_state.status)
            if state_status != KnowledgeIndexStatus.READY.value:
                return False
            if index_state.index_version != self._active_index_version():
                return False
            if index_state.content_hash != new_hash:
                return False
            return existing_hash == new_hash

    async def _index_chunks(self, file_id: str, result: dict[str, Any]) -> None:
        """Chunk extracted text and persist to the knowledge storage layer.

        Uses :func:`chunk_auto` for Markdown-aware chunking when the source
        file is detected as Markdown.  Also writes chunk embeddings to the
        vector index when available.
        """
        extracted = result.get("extracted_text")
        if not extracted or not isinstance(extracted, str) or not extracted.strip():
            return
        from uuid import UUID

        from leagent.db.models.file import File, FileStatus
        from leagent.db.sqlite_compat import load_entity_by_id
        from leagent.library.chunking import chunk_auto
        from leagent.library.summary import summarize_extracted_text
        from leagent.services.service_manager import get_service_manager

        sm = get_service_manager()
        if sm.db is None:
            return

        logical_id = UUID(file_id)
        async with sm.db.session() as session:
            row = await load_entity_by_id(
                session,
                File,
                logical_id,
                parent_table="files",
            )
            if row is None:
                return
            user_id = row.user_id
            file_mime = row.mime_type
            file_name = row.original_name or row.name

        chunks = chunk_auto(extracted, mime_type=file_mime, filename=file_name)
        await sm.db.repositories.document_chunks.replace_for_file(logical_id, user_id, chunks)
        try:
            chunk_ids = await sm.db.repositories.document_chunks.list_ids_for_file(logical_id)
        except Exception:  # noqa: BLE001 - local chunk writes remain authoritative
            chunk_ids = []
            logger.debug(
                "chunk_id_projection_lookup_skipped file_id=%s",
                file_id,
                exc_info=True,
            )
        await self._set_index_state(
            file_id,
            "embedding",
            chunk_count=len(chunks),
        )

        # Write embeddings to the vector index (best-effort, graceful degradation).
        embedded_count = 0
        try:
            from leagent.knowledge.vector_index import get_knowledge_vector_index

            vi = get_knowledge_vector_index()
            if vi is not None and vi.available:
                await vi.delete_for_file(logical_id)
                embedded_count = await vi.upsert_chunks(
                    logical_id,
                    user_id,
                    chunks,
                    chunk_ids=chunk_ids,
                )
        except Exception:  # noqa: BLE001
            logger.debug("vector_index_upsert_skipped file_id=%s", file_id)

        # OpenSearch is an optional lexical accelerator.  Keep the local SQL
        # projection authoritative when the remote service is unavailable.
        opensearch_count = 0
        try:
            from leagent.config.settings import get_settings
            from leagent.knowledge.opensearch import OpenSearchKnowledgeIndex

            cfg = get_settings().knowledge
            if cfg.opensearch_url:
                remote = OpenSearchKnowledgeIndex(
                    cfg.opensearch_url,
                    index=cfg.opensearch_index,
                    api_key=cfg.opensearch_api_key,
                    timeout_seconds=cfg.opensearch_timeout_seconds,
                )
                try:
                    await remote.delete_for_file(
                        logical_id,
                        index_version=cfg.index_version,
                    )
                except Exception:  # noqa: BLE001 - stale remote rows are harmless
                    logger.debug(
                        "opensearch_stale_delete_skipped file_id=%s",
                        file_id,
                        exc_info=True,
                    )
                try:
                    opensearch_count = await remote.upsert_chunks(
                        logical_id,
                        file_name,
                        chunks,
                        chunk_ids=chunk_ids,
                        index_version=cfg.index_version,
                    )
                except Exception:  # noqa: BLE001 - local indexing remains usable
                    logger.debug(
                        "opensearch_index_upsert_skipped file_id=%s",
                        file_id,
                        exc_info=True,
                    )
        except Exception:  # noqa: BLE001 - local indexing must remain usable
            logger.debug(
                "opensearch_index_upsert_skipped file_id=%s",
                file_id,
                exc_info=True,
            )

        await self._set_index_state(
            file_id,
            "ready",
            chunk_count=len(chunks),
            embedded_count=embedded_count,
            completed_at=datetime.utcnow(),
        )
        result["_index_chunk_count"] = len(chunks)
        result["_index_embedded_count"] = embedded_count
        result["_index_opensearch_count"] = opensearch_count

        await self._update_file_record(
            file_id,
            status=FileStatus.PROCESSED,
            is_indexed=True,
            summary=summarize_extracted_text(extracted),
        )
