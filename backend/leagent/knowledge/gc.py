"""Knowledge base garbage collection.

Cleans up orphaned and stale data in the knowledge storage layer:

1. **Orphaned chunks**: ``document_chunks`` rows whose parent ``files.id``
   has been soft-deleted (``is_deleted = 1``) or no longer exists.
2. **Orphaned file_contents**: ``file_contents`` rows whose parent file
   has been deleted.
3. **Failed processing cleanup**: files stuck in ``PROCESSING`` status
   for longer than a configurable timeout.
4. **Expired files**: files past their ``expires_at`` timestamp.

The GC is designed to run as a periodic background task (cron or manual
trigger) and is idempotent — safe to run multiple times.

Usage::

    from leagent.knowledge.gc import KnowledgeGarbageCollector

    gc = KnowledgeGarbageCollector(db)
    report = await gc.collect()
    print(report)  # {"deleted_chunks": 42, "deleted_contents": 3, ...}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

if TYPE_CHECKING:
    from uuid import UUID

    from leagent.db.service import DatabaseService

logger = logging.getLogger(__name__)

#: Default timeout for stuck PROCESSING files (2 hours).
_STUCK_PROCESSING_HOURS = 2


@dataclass
class GCReport:
    """Summary of a garbage collection run."""

    deleted_chunks: int = 0
    deleted_contents: int = 0
    cleaned_stuck_processing: int = 0
    deleted_expired_files: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deleted_chunks": self.deleted_chunks,
            "deleted_contents": self.deleted_contents,
            "cleaned_stuck_processing": self.cleaned_stuck_processing,
            "deleted_expired_files": self.deleted_expired_files,
            "errors": self.errors,
        }


class KnowledgeGarbageCollector:
    """Clean up orphaned and stale knowledge base data.

    Each ``collect_*`` method is independent and idempotent.
    The top-level :meth:`collect` runs all cleanup passes in order.
    """

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def collect(self) -> GCReport:
        """Run all garbage collection passes and return a summary report."""
        report = GCReport()

        # Pass 1: Orphaned chunks (parent file deleted).
        try:
            report.deleted_chunks = await self._collect_orphaned_chunks()
        except Exception as exc:
            msg = f"collect_orphaned_chunks: {exc}"
            logger.warning(msg)
            report.errors.append(msg)

        # Pass 2: Orphaned file_contents (parent file deleted).
        try:
            report.deleted_contents = await self._collect_orphaned_contents()
        except Exception as exc:
            msg = f"collect_orphaned_contents: {exc}"
            logger.warning(msg)
            report.errors.append(msg)

        # Pass 3: Stuck PROCESSING files.
        try:
            report.cleaned_stuck_processing = await self._collect_stuck_processing()
        except Exception as exc:
            msg = f"collect_stuck_processing: {exc}"
            logger.warning(msg)
            report.errors.append(msg)

        # Pass 4: Expired files.
        try:
            report.deleted_expired_files = await self._collect_expired_files()
        except Exception as exc:
            msg = f"collect_expired_files: {exc}"
            logger.warning(msg)
            report.errors.append(msg)

        logger.info(
            "knowledge_gc_complete chunks=%s contents=%s stuck=%s expired=%s errors=%s",
            report.deleted_chunks,
            report.deleted_contents,
            report.cleaned_stuck_processing,
            report.deleted_expired_files,
            len(report.errors),
        )
        return report

    # ── Individual passes ────────────────────────────────────────────

    async def _collect_orphaned_chunks(self) -> int:
        """Delete chunks whose parent file is soft-deleted or missing."""
        from uuid import UUID

        file_ids: list[UUID] = []
        async with self._db.session() as session:
            rows = await session.execute(
                sa.text(
                    "SELECT DISTINCT dc.file_id FROM document_chunks dc "
                    "JOIN files f ON f.id = dc.file_id WHERE f.is_deleted = 1"
                )
            )
            for row in rows.fetchall():
                try:
                    file_ids.append(UUID(str(row[0])))
                except (IndexError, TypeError, ValueError):
                    continue
            # Subquery: file IDs that are soft-deleted.
            result = await session.execute(
                sa.text("DELETE FROM document_chunks WHERE file_id IN (  SELECT id FROM files WHERE is_deleted = 1)")
            )
            count = result.rowcount or 0
            if count:
                logger.info("knowledge_gc_orphaned_chunks count=%s", count)
        if count and file_ids:
            await self._delete_vector_scopes(list(dict.fromkeys(file_ids)))
        return count

    async def _delete_vector_scopes(self, file_ids: list[UUID]) -> None:
        """Best-effort cleanup of vector replicas after SQL rows are removed."""
        try:
            from leagent.knowledge.vector_index import get_knowledge_vector_index

            index = get_knowledge_vector_index()
            if index is None or not index.available:
                return
            for file_id in file_ids:
                await index.delete_for_file(file_id)
        except Exception:  # noqa: BLE001 - SQL cleanup remains authoritative
            logger.debug("knowledge_gc_vector_cleanup_failed", exc_info=True)

    async def _collect_orphaned_contents(self) -> int:
        """Delete file_contents whose parent file is soft-deleted or missing."""
        async with self._db.session() as session:
            result = await session.execute(
                sa.text("DELETE FROM file_contents WHERE file_id IN (  SELECT id FROM files WHERE is_deleted = 1)")
            )
            count = result.rowcount or 0
            if count:
                logger.info("knowledge_gc_orphaned_contents count=%s", count)
            return count

    async def _collect_stuck_processing(self) -> int:
        """Reset files stuck in PROCESSING for too long to FAILED status."""
        cutoff = datetime.utcnow() - timedelta(hours=_STUCK_PROCESSING_HOURS)
        async with self._db.session() as session:
            result = await session.execute(
                sa.text(
                    "UPDATE files SET status = 'failed', is_indexed = 0, "
                    "updated_at = :now "
                    "WHERE status = 'processing' AND updated_at < :cutoff"
                ),
                {"now": datetime.utcnow(), "cutoff": cutoff},
            )
            count = result.rowcount or 0
            if count:
                logger.info("knowledge_gc_stuck_processing count=%s", count)
            return count

    async def _collect_expired_files(self) -> int:
        """Soft-delete files past their expires_at timestamp."""
        now = datetime.utcnow()
        async with self._db.session() as session:
            # Only soft-delete (set is_deleted = 1) — actual blob cleanup
            # is handled by the file storage layer's own GC.
            result = await session.execute(
                sa.text(
                    "UPDATE files SET is_deleted = 1, updated_at = :now "
                    "WHERE expires_at IS NOT NULL "
                    "AND expires_at < :now "
                    "AND is_deleted = 0"
                ),
                {"now": now},
            )
            count = result.rowcount or 0
            if count:
                logger.info("knowledge_gc_expired_files count=%s", count)
            return count


__all__ = [
    "KnowledgeGarbageCollector",
    "GCReport",
]
