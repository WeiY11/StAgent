"""Blob garbage collection for the unified file catalog.

Reaps on-disk bytes for:

- soft-deleted rows past a grace period (default 7 days)
- rows whose ``expires_at`` has passed

Honors ``is_pinned`` and never deletes a ``storage_path`` still referenced by
any live (non-deleted) row — content dedup may share one blob across many
catalog entries.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlmodel import select

if TYPE_CHECKING:
    from uuid import UUID

    from StAgent.db.service import DatabaseService

logger = logging.getLogger(__name__)

DEFAULT_GRACE_HOURS = 168  # 7 days


async def run_file_gc(
    db: DatabaseService,
    *,
    grace_hours: int = DEFAULT_GRACE_HOURS,
    dry_run: bool = False,
) -> dict[str, int]:
    """Reap orphaned blobs and return summary counters."""
    from StAgent.db.models.file import File

    now = datetime.now(UTC).replace(tzinfo=None)
    grace_cutoff = now - timedelta(hours=grace_hours)
    removed_blobs = 0
    skipped_shared = 0
    skipped_pinned = 0
    candidates: list[tuple[UUID, str]] = []

    async with db.session() as session:
        stmt = select(File).where(File.is_deleted == True)  # noqa: E712
        result = await session.exec(stmt)
        for row in result.all():
            if row.is_pinned:
                skipped_pinned += 1
                continue
            if row.deleted_at and row.deleted_at > grace_cutoff:
                continue
            if row.storage_path:
                candidates.append((row.id, row.storage_path))

        expired_stmt = select(File).where(
            File.is_deleted == False,  # noqa: E712
            File.expires_at.is_not(None),  # type: ignore[attr-defined]
            File.expires_at < now,
            File.is_pinned == False,  # noqa: E712
        )
        expired_result = await session.exec(expired_stmt)
        for row in expired_result.all():
            if row.storage_path:
                candidates.append((row.id, row.storage_path))
            row.is_deleted = True
            row.deleted_at = now
            session.add(row)

    seen_paths: set[str] = set()
    for _fid, storage_path in candidates:
        if storage_path in seen_paths:
            continue
        seen_paths.add(storage_path)
        if not await _storage_path_is_exclusive(db, storage_path):
            skipped_shared += 1
            continue
        if dry_run:
            if storage_path.startswith("s3://") or os.path.isfile(storage_path):
                removed_blobs += 1
            continue
        if await _remove_orphaned_blob(db, _fid, storage_path):
            removed_blobs += 1
            logger.info("gc_removed_blob path=%s", storage_path)

    return {
        "removed_blobs": removed_blobs,
        "skipped_shared": skipped_shared,
        "skipped_pinned": skipped_pinned,
        "candidates": len(candidates),
    }


async def _storage_path_is_exclusive(db: DatabaseService, storage_path: str) -> bool:
    """True when no live row still references *storage_path*."""
    from StAgent.db.models.file import File

    async with db.session() as session:
        result = await session.exec(
            select(sa.func.count())
            .select_from(File)
            .where(File.storage_path == storage_path)
            .where(File.is_deleted == False)  # noqa: E712
        )
        count = result.one()
        return int(count or 0) == 0


async def _remove_orphaned_blob(
    db: DatabaseService,
    file_id: UUID,
    storage_path: str,
) -> bool:
    """Delete one exclusive local or S3-compatible blob.

    S3 paths stay opaque to the filesystem. The configured FileService owns
    URI-to-key conversion, so garbage collection works after a process restart
    and never applies ``os.unlink`` to an object-store URI.
    """
    if not storage_path.startswith("s3://"):
        if not os.path.isfile(storage_path):
            return False
        try:
            os.unlink(storage_path)
            return True
        except OSError as exc:
            logger.warning("gc_unlink_failed path=%s err=%s", storage_path, exc)
            return False

    try:
        from StAgent.services.service_manager import get_service_manager

        manager = get_service_manager()
        service = manager.file_service
        if service is None or getattr(manager, "db", None) is not db:
            logger.warning("gc_s3_service_unavailable path=%s", storage_path)
            return False
        ref = service.ref_from_persisted_record(
            file_id=file_id,
            filename="",
            storage_path=storage_path,
        )
        return await service.delete(ref)
    except Exception as exc:  # noqa: BLE001 - GC must finish remaining files
        logger.warning("gc_s3_delete_failed path=%s err=%s", storage_path, exc)
        return False
