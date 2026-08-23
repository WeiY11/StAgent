"""Runtime state and blue/green rebuilds for local semantic knowledge search.

The SQLite vector sidecar is a disposable projection.  This module never
changes the SQL chunk store, so every failure continues to leave the lexical
BM25/FTS5 path available.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from StAgent.knowledge.vector_backend import (
    KnowledgeVectorIndexBackend,
    canonical_index_version,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from StAgent.db.models.task import TaskContext
    from StAgent.db.service import DatabaseService

logger = logging.getLogger(__name__)
_REBUILD_BATCH_SIZE = 192
_CAPACITY_WARNING_CHUNKS = 100_000
_SAFE_ERROR = re.compile(r"[^A-Za-z0-9_.:-]+")
_SECRET_ERROR = re.compile(r"(?:token|secret|password|api[_-]?key|authorization)\s*[:=]", re.IGNORECASE)


def redact_semantic_error(error: object | None) -> str | None:
    """Keep file paths, URLs, tokens, and exception detail out of UI status."""
    if not error:
        return None
    text = str(error).replace("\\", "/")
    if "/" in text or "http" in text.lower() or _SECRET_ERROR.search(text):
        return "semantic_backend_unavailable"
    return _SAFE_ERROR.sub("_", text)[:160] or "semantic_backend_unavailable"


@dataclass
class SemanticRuntimeState:
    """Secret-free state exposed by the service manager and API layer."""

    mode: str = "off"
    active_version: str | None = None
    candidate_version: str | None = None
    model_id: str | None = None
    model_fingerprint: str | None = None
    task_id: str | None = None
    task_progress: int | None = None
    task_progress_message: str | None = None
    document_count: int = 0
    chunk_count: int = 0
    capacity_warning: bool = False
    error: str | None = None
    environment_managed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.mode,
            "active_version": self.active_version,
            "candidate_version": self.candidate_version,
            "model_id": self.model_id,
            "task_id": self.task_id,
            "task_progress": self.task_progress,
            "task_progress_message": self.task_progress_message,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "capacity_warning": self.capacity_warning,
            "error": redact_semantic_error(self.error),
            "environment_managed": self.environment_managed,
        }


class CandidateDualWriteIndex:
    """Expose an active index while mirroring writes to a candidate version.

    A model switch cannot safely write newly encoded vectors to an old active
    version.  In that situation ``serve_active`` is false and the wrapper
    writes only the candidate while callers fall through to lexical search.
    """

    # The active sidecar still requires the retrieval pipeline to calculate
    # the ACL-visible file set before querying.  Keep this marker when the
    # wrapper temporarily replaces the active backend during a rebuild.
    requires_allowed_file_ids = True

    def __init__(
        self,
        backend: KnowledgeVectorIndexBackend,
        *,
        active_version: str,
        candidate_version: str,
        serve_active: bool,
        active_backend: KnowledgeVectorIndexBackend | None = None,
    ) -> None:
        self._backend = backend
        self._active_backend = active_backend or backend
        self._active_version = active_version
        self._candidate_version = candidate_version
        self._serve_active = serve_active

    @property
    def available(self) -> bool:
        return self._serve_active and self._active_backend.available

    @property
    def index_version(self) -> str:
        return self._active_version

    async def upsert_chunks(self, *args: Any, **kwargs: Any) -> int:
        kwargs.pop("index_version", None)
        candidate = await self._backend.upsert_chunks(*args, index_version=self._candidate_version, **kwargs)
        if self._serve_active:
            await self._active_backend.upsert_chunks(*args, index_version=self._active_version, **kwargs)
        return int(candidate)

    async def delete_for_file(self, file_id: UUID, **kwargs: Any) -> int:
        kwargs.pop("index_version", None)
        candidate = await self._backend.delete_for_file(file_id, index_version=self._candidate_version, **kwargs)
        if self._serve_active:
            await self._active_backend.delete_for_file(file_id, index_version=self._active_version, **kwargs)
        return int(candidate)

    async def embed_query(self, query: str) -> list[float]:
        return await self._active_backend.embed_query(query)

    async def search_vector(self, query_vector: list[float], **kwargs: Any) -> list[tuple[str, float]]:
        if not self._serve_active:
            return []
        kwargs["index_version"] = self._active_version
        return await self._active_backend.search_vector(query_vector, **kwargs)

    async def begin_version(self, index_version: str) -> bool:
        return bool(await self._backend.begin_version(index_version))

    async def activate_version(self, index_version: str) -> bool:
        return bool(await self._backend.activate_version(index_version))

    async def discard_version(self, index_version: str) -> bool:
        return bool(await self._backend.discard_version(index_version))


class KnowledgeSemanticRebuilder:
    """Populate a candidate version from durable SQL chunks and activate it."""

    def __init__(
        self,
        db: DatabaseService,
        backend: KnowledgeVectorIndexBackend,
        state: SemanticRuntimeState,
        *,
        on_index_changed: Callable[[KnowledgeVectorIndexBackend | CandidateDualWriteIndex | None], None],
        active_backend: KnowledgeVectorIndexBackend | None = None,
        serve_active: bool = True,
    ) -> None:
        self._db = db
        self._backend = backend
        self._state = state
        self._on_index_changed = on_index_changed
        self._active_backend = active_backend or backend
        self._serve_active = serve_active
        self._lock = asyncio.Lock()

    @property
    def state(self) -> SemanticRuntimeState:
        return self._state

    def new_candidate_version(self) -> str:
        model_marker = (self._state.model_id or "model").replace("local-", "")[:16]
        fingerprint = (self._state.model_fingerprint or "")[:12]
        suffix = f"-{fingerprint}" if fingerprint else ""
        return f"semantic-{model_marker}{suffix}-{uuid.uuid4().hex[:12]}"

    async def rebuild(
        self,
        *,
        task_ctx: TaskContext | None = None,
        session: AsyncSession | None = None,
        candidate_version: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Build all SQL chunks into a persisted candidate then atomically swap."""
        async with self._lock:
            if not self._backend.available:
                self._state.mode = "degraded"
                self._state.error = "semantic_backend_unavailable"
                raise RuntimeError("semantic_backend_unavailable")

            active = self._backend.index_version
            requested_candidate = canonical_index_version(candidate_version)
            candidate = requested_candidate or self.new_candidate_version()
            if candidate == active:
                candidate = self.new_candidate_version()
            self._state.mode = "building"
            self._state.active_version = active
            self._state.candidate_version = candidate
            self._state.task_id = task_id or self._state.task_id
            self._state.task_progress = 0
            self._state.task_progress_message = None
            self._state.error = None
            # A persisted candidate can contain rows from a previous attempt
            # that no longer exist in the authoritative chunk table (for
            # example, a document deleted while the process was down).  Start
            # retries from a clean candidate; the durable task is still
            # recoverable, while stale vectors can never be activated.
            if requested_candidate is not None and not await self._backend.discard_version(candidate):
                raise RuntimeError("semantic_candidate_reset_failed")
            started = await self._backend.begin_version(candidate)
            if not started:
                raise RuntimeError("semantic_candidate_begin_failed")

            # Never replace a possibly incompatible active projection during a
            # model switch.  The wrapper keeps old-model vectors read-only.
            serve_active = self._serve_active
            wrapper = CandidateDualWriteIndex(
                self._backend,
                active_version=active,
                candidate_version=candidate,
                serve_active=serve_active,
                active_backend=self._active_backend,
            )
            self._on_index_changed(wrapper)
            try:
                total, documents = await self._count_corpus()
                snapshot_generation = await self._corpus_generation()
                self._state.chunk_count = total
                self._state.document_count = documents
                self._state.capacity_warning = total >= _CAPACITY_WARNING_CHUNKS
                await self._progress(session, task_id, 1, f"rebuilding 0/{total} chunks")

                written = 0
                offset = 0
                while True:
                    if task_ctx is not None and task_ctx.is_aborted:
                        raise RuntimeError("semantic_rebuild_cancelled")
                    rows = await self._load_batch(offset)
                    if not rows:
                        break
                    grouped: dict[tuple[UUID, UUID | None], list[Any]] = {}
                    for row in rows:
                        grouped.setdefault((row.file_id, row.user_id), []).append(row)
                    for (file_id, user_id), group in grouped.items():
                        from StAgent.library.chunking import Chunk

                        chunks = [
                            Chunk(
                                seq=int(row.seq),
                                start_offset=int(row.start_offset),
                                end_offset=int(row.end_offset),
                                text=str(row.text),
                                text_hash=row.text_hash,
                            )
                            for row in group
                        ]
                        count = await self._backend.upsert_chunks(
                            file_id,
                            user_id,
                            chunks,
                            chunk_ids=[row.id for row in group],
                            index_version=candidate,
                        )
                        if count != len(chunks):
                            raise RuntimeError("semantic_candidate_write_incomplete")
                        written += count
                    offset += len(rows)
                    progress = min(99, max(1, int(written * 100 / max(total, 1))))
                    await self._progress(session, task_id, progress, f"rebuilding {written}/{total} chunks")

                if written != total:
                    raise RuntimeError("semantic_candidate_validation_failed")
                current_generation = await self._corpus_generation()
                if (
                    snapshot_generation is not None
                    and current_generation is not None
                    and current_generation != snapshot_generation
                ):
                    # File processing dual-writes changes while a candidate
                    # is building.  Refuse to activate when the authoritative
                    # chunk generation changed during the snapshot; a retry
                    # then starts from a clean candidate instead of exposing
                    # an offset-pagination race or stale rows.
                    raise RuntimeError("semantic_corpus_changed_during_rebuild")
                if not await self._backend.activate_version(candidate):
                    raise RuntimeError("semantic_candidate_activation_failed")
                self._state.active_version = candidate
                self._state.candidate_version = None
                self._state.mode = "ready"
                self._state.error = None
                self._on_index_changed(self._backend)
                await self._progress(session, task_id, 100, f"rebuilt {written} chunks")
                return {"active_version": candidate, "chunk_count": written, "document_count": documents}
            except Exception as exc:
                self._state.mode = "degraded" if serve_active else "lexical_only"
                self._state.error = redact_semantic_error(exc)
                # The active version remains untouched.  Discarding a failed
                # candidate also ensures a retry does not query partial rows.
                await self._backend.discard_version(candidate)
                self._state.candidate_version = None
                self._on_index_changed(self._active_backend if serve_active else None)
                raise

    async def _count_corpus(self) -> tuple[int, int]:
        from sqlalchemy import func
        from sqlmodel import select

        from StAgent.db.models.document_chunk import DocumentChunk

        async with self._db.session() as session:
            chunks = int(
                (
                    await session.exec(
                        select(func.count(DocumentChunk.id))  # type: ignore[arg-type]
                    )
                ).one()
                or 0
            )
            documents = int((await session.exec(select(func.count(func.distinct(DocumentChunk.file_id))))).one() or 0)
        return chunks, documents

    async def _corpus_generation(self) -> str | None:
        """Hash the authoritative chunk key/content generation for rebuild validation.

        The vector sidecar is a projection, so its candidate must never be
        activated from an offset scan that raced a chunk replacement or
        deletion.  A compact digest of stable chunk identity, content hash,
        and update timestamp provides a backend-neutral generation marker.
        Test doubles without a database session intentionally return ``None``.
        """
        session_factory = getattr(self._db, "session", None)
        if session_factory is None:
            return None
        from sqlmodel import select

        from StAgent.db.models.document_chunk import DocumentChunk

        try:
            async with session_factory() as session:
                rows = await session.exec(
                    select(
                        DocumentChunk.id,
                        DocumentChunk.text_hash,
                        DocumentChunk.updated_at,
                    ).order_by(DocumentChunk.id)  # type: ignore[arg-type]
                )
                digest = hashlib.sha256()
                for chunk_id, text_hash, updated_at in rows:
                    digest.update(f"{chunk_id}\x1f{text_hash or ''}\x1f{updated_at!s}\n".encode())
                return digest.hexdigest()
        except Exception:  # noqa: BLE001 - generation is a safety check
            logger.debug("semantic_corpus_generation_skipped", exc_info=True)
            return None

    async def _load_batch(self, offset: int) -> list[Any]:
        from sqlmodel import select

        from StAgent.db.models.document_chunk import DocumentChunk

        async with self._db.session() as session:
            statement = (
                select(DocumentChunk)
                .order_by(
                    DocumentChunk.file_id,  # type: ignore[arg-type]
                    DocumentChunk.seq,  # type: ignore[arg-type]
                    DocumentChunk.id,  # type: ignore[arg-type]
                )
                .offset(offset)
                .limit(_REBUILD_BATCH_SIZE)
            )
            return list((await session.exec(statement)).all())

    async def _progress(
        self,
        session: AsyncSession | None,
        task_id: str | None,
        progress: int,
        message: str,
    ) -> None:
        if not task_id:
            return
        self._state.task_progress = max(0, min(progress, 100))
        self._state.task_progress_message = message[:500]
        from sqlalchemy import update

        from StAgent.db.models.task import Task
        from StAgent.db.sqlite_compat import uuid_column_equals

        # The TaskManager keeps the handler's session open until completion.
        # Use a short independent transaction so task-progress polling sees
        # every committed batch instead of one final update at 100%.
        async with self._db.session() as progress_session:
            await progress_session.execute(
                update(Task)
                .where(uuid_column_equals(progress_session, Task.id, UUID(task_id)))
                .values(progress=max(0, min(progress, 100)), progress_message=message[:500])
            )


__all__ = [
    "CandidateDualWriteIndex",
    "KnowledgeSemanticRebuilder",
    "SemanticRuntimeState",
    "redact_semantic_error",
]
