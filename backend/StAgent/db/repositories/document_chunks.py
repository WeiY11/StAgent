"""Document-chunk persistence + chunk-level knowledge retrieval.

Writes go through :meth:`DbDocumentChunkRepository.replace_for_file` (idempotent
re-index of a single file). Reads use BM25 over the SQLite FTS5 index when
available and fall back to a portable ``ILIKE`` scan on PostgreSQL / when the
FTS table is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import col, delete, select

from StAgent.db.models.base import utc_now
from StAgent.db.models.document_chunk import DocumentChunk
from StAgent.db.sqlite_compat import (
    session_dialect_name,
    sqlite_parent_id_text,
    uuid_column_equals,
)
from StAgent.library.chunking import Chunk  # noqa: TC001 - protocol annotations are runtime-facing
from StAgent.library.fts import FTS_TABLE, TRIGRAM_TABLE, escape_fts_query, escape_trigram_query
from StAgent.memory.lexical_backend import session_dialect

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from StAgent.db.service import DatabaseService


@dataclass
class ChunkHit:
    """A single chunk-level search result grounded in a source file."""

    chunk_id: UUID
    file_id: UUID
    file_name: str
    seq: int
    start_offset: int
    end_offset: int
    text: str
    snippet: str
    score: float


class DocumentChunkRepository(Protocol):
    async def replace_for_file(
        self, file_id: UUID, user_id: UUID | None, chunks: list[Chunk]
    ) -> int: ...

    async def delete_for_file(self, file_id: UUID) -> int: ...

    async def list_ids_for_file(self, file_id: UUID) -> list[UUID]: ...

    async def search(
        self,
        user_id: UUID | None,
        query: str,
        *,
        limit: int = 20,
        library_scope: str | None = None,
    ) -> list[ChunkHit]: ...


class DbDocumentChunkRepository:
    """``DatabaseService``-backed :class:`DocumentChunkRepository`."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def replace_for_file(
        self, file_id: UUID, user_id: UUID | None, chunks: list[Chunk]
    ) -> int:
        """Persist *chunks* for *file_id* using diff-based incremental indexing.

        When every incoming chunk carries a ``text_hash`` (SHA-256 prefix), the
        method queries existing chunks and only deletes/inserts the rows whose
        content actually changed.  Legacy rows without a hash trigger a
        full-replace fallback to maintain correctness.
        """
        async with self._db.session() as session:
            if session_dialect_name(session) == "sqlite":
                return await self._replace_sqlite(
                    session, file_id, user_id, chunks
                )
            return await self._replace_generic(
                session, file_id, user_id, chunks
            )

    # ── SQLite fast-path (raw SQL + hex UUID comparison) ──────────

    async def _replace_sqlite(
        self,
        session: AsyncSession,
        file_id: UUID,
        user_id: UUID | None,
        chunks: list[Chunk],
    ) -> int:
        file_db_id = await sqlite_parent_id_text(session, "files", file_id)
        file_hex = file_id.hex.lower()

        # New hash set — if any chunk is unhashed, fall back to full replace.
        new_hashes = {c.text_hash for c in chunks if c.text_hash}
        if len(new_hashes) != len(chunks):
            # Legacy path: full delete + insert.
            return await self._full_replace_sqlite(
                session, file_hex, file_db_id, user_id, chunks
            )

        # Fetch existing chunk ids grouped by text_hash.
        rows = (
            await session.execute(
                sa.text(
                    "SELECT id, text_hash FROM document_chunks "
                    "WHERE lower(replace(CAST(file_id AS TEXT), '-', '')) = :file_hex"
                ),
                {"file_hex": file_hex},
            )
        ).fetchall()

        old_map: dict[str, list[str]] = {}  # hash → [chunk_id, …]
        for row in rows:
            old_map.setdefault(row.text_hash or "", []).append(row.id)

        old_hashes = set(old_map.keys())

        # Three-way diff.
        to_delete_ids: list[str] = []
        for h in old_hashes - new_hashes:
            to_delete_ids.extend(old_map[h])
        to_insert = [c for c in chunks if c.text_hash not in old_hashes]

        deleted = len(to_delete_ids)
        inserted = len(to_insert)

        # Apply deletes.
        if to_delete_ids:
            # Batch delete by primary key — no UUID comparison needed.
            id_list = ", ".join(f"'{cid}'" for cid in to_delete_ids)
            await session.execute(
                sa.text(f"DELETE FROM document_chunks WHERE id IN ({id_list})")
            )

        # Apply inserts.
        if to_insert:
            now = utc_now()
            for chunk in to_insert:
                await session.execute(
                    sa.text(
                        "INSERT INTO document_chunks "
                        "(created_at, updated_at, id, file_id, user_id, seq, "
                        "start_offset, end_offset, text, text_hash) "
                        "VALUES (:created_at, :updated_at, :id, :file_id, "
                        ":user_id, :seq, :start_offset, :end_offset, :text, "
                        ":text_hash)"
                    ),
                    {
                        "created_at": now,
                        "updated_at": now,
                        "id": str(uuid4()),
                        "file_id": file_db_id,
                        "user_id": str(user_id) if user_id else None,
                        "seq": chunk.seq,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                        "text": chunk.text,
                        "text_hash": chunk.text_hash,
                    },
                )

        if deleted or inserted:
            from StAgent.db.models.base import utc_now as _now

            await session.execute(
                sa.text(
                    "UPDATE files SET updated_at = :now "
                    "WHERE lower(replace(CAST(id AS TEXT), '-', '')) = :file_hex"
                ),
                {"now": _now(), "file_hex": file_hex},
            )

        return len(chunks)

    async def _full_replace_sqlite(
        self,
        session: AsyncSession,
        file_hex: str,
        file_db_id: str | None,
        user_id: UUID | None,
        chunks: list[Chunk],
    ) -> int:
        """Legacy full delete + insert (used when chunks lack text_hash)."""
        await session.execute(
            sa.text(
                "DELETE FROM document_chunks "
                "WHERE lower(replace(CAST(file_id AS TEXT), '-', '')) = :file_hex"
            ),
            {"file_hex": file_hex},
        )
        if chunks:
            now = utc_now()
            for chunk in chunks:
                await session.execute(
                    sa.text(
                        "INSERT INTO document_chunks "
                        "(created_at, updated_at, id, file_id, user_id, seq, "
                        "start_offset, end_offset, text, text_hash) "
                        "VALUES (:created_at, :updated_at, :id, :file_id, "
                        ":user_id, :seq, :start_offset, :end_offset, :text, "
                        ":text_hash)"
                    ),
                    {
                        "created_at": now,
                        "updated_at": now,
                        "id": str(uuid4()),
                        "file_id": file_db_id,
                        "user_id": str(user_id) if user_id else None,
                        "seq": chunk.seq,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                        "text": chunk.text,
                        "text_hash": chunk.text_hash,
                    },
                )
        return len(chunks)

    # ── Generic (PostgreSQL) path ────────────────────────────────

    async def _replace_generic(
        self,
        session: AsyncSession,
        file_id: UUID,
        user_id: UUID | None,
        chunks: list[Chunk],
    ) -> int:
        new_hashes = {c.text_hash for c in chunks if c.text_hash}
        if len(new_hashes) != len(chunks):
            # Full replace fallback.
            await session.exec(
                delete(DocumentChunk).where(
                    uuid_column_equals(session, DocumentChunk.file_id, file_id)
                )
            )
            if chunks:
                session.add_all([
                    DocumentChunk(
                        file_id=file_id,
                        user_id=user_id,
                        seq=c.seq,
                        start_offset=c.start_offset,
                        end_offset=c.end_offset,
                        text=c.text,
                        text_hash=c.text_hash,
                    )
                    for c in chunks
                ])
            return len(chunks)

        # Diff-based update.
        existing = (
            await session.exec(
                select(DocumentChunk.id, DocumentChunk.text_hash).where(
                    uuid_column_equals(session, DocumentChunk.file_id, file_id)
                )
            )
        ).all()
        old_map: dict[str, list[Any]] = {}
        for chunk_id, th in existing:
            old_map.setdefault(th or "", []).append(chunk_id)
        old_hashes = set(old_map.keys())

        to_delete_ids = [cid for h in (old_hashes - new_hashes) for cid in old_map[h]]
        to_insert = [c for c in chunks if c.text_hash not in old_hashes]

        if to_delete_ids:
            await session.exec(
                delete(DocumentChunk).where(col(DocumentChunk.id).in_(to_delete_ids))
            )
        if to_insert:
            session.add_all([
                DocumentChunk(
                    file_id=file_id,
                    user_id=user_id,
                    seq=c.seq,
                    start_offset=c.start_offset,
                    end_offset=c.end_offset,
                    text=c.text,
                    text_hash=c.text_hash,
                )
                for c in to_insert
            ])
        return len(chunks)

    async def delete_for_file(self, file_id: UUID) -> int:
        async with self._db.session() as session:
            if session_dialect_name(session) == "sqlite":
                file_hex = file_id.hex.lower()
                count = (
                    await session.execute(
                        sa.text(
                            "SELECT COUNT(*) FROM document_chunks "
                            "WHERE lower(replace(CAST(file_id AS TEXT), '-', '')) = :file_hex"
                        ),
                        {"file_hex": file_hex},
                    )
                ).scalar_one()
                if count:
                    await session.execute(
                        sa.text(
                            "DELETE FROM document_chunks "
                            "WHERE lower(replace(CAST(file_id AS TEXT), '-', '')) = :file_hex"
                        ),
                        {"file_hex": file_hex},
                    )
                # Drop the associated extracted-text payload as well so the
                # file_contents table does not accumulate orphans.
                await session.execute(
                    sa.text(
                        "DELETE FROM file_contents "
                        "WHERE lower(replace(CAST(file_id AS TEXT), '-', '')) = :file_hex"
                    ),
                    {"file_hex": file_hex},
                )
                return int(count)

            result = await session.exec(
                select(DocumentChunk.id).where(
                    uuid_column_equals(session, DocumentChunk.file_id, file_id)
                )
            )
            ids = list(result.all())
            if ids:
                await session.exec(
                    delete(DocumentChunk).where(
                        uuid_column_equals(session, DocumentChunk.file_id, file_id)
                    )
                )
            from StAgent.db.models.file_content import FileContent

            await session.exec(
                delete(FileContent).where(
                    uuid_column_equals(session, FileContent.file_id, file_id)
                )
            )
            return len(ids)

    async def list_ids_for_file(self, file_id: UUID) -> list[UUID]:
        """Return persisted chunk ids in sequence order for vector upserts."""
        async with self._db.session() as session:
            result = await session.exec(
                select(DocumentChunk.id)
                .where(uuid_column_equals(session, DocumentChunk.file_id, file_id))
                .order_by(DocumentChunk.seq)
            )
            return list(result.all())

    async def search(
        self,
        user_id: UUID | None,
        query: str,
        *,
        limit: int = 20,
        library_scope: str | None = None,
    ) -> list[ChunkHit]:
        query = (query or "").strip()
        if not query:
            return []
        async with self._db.session() as session:
            dialect = session_dialect(session)
            if dialect == "sqlite" and await self._fts_available(session):
                hits = await self._search_fts(
                    session, user_id, query, limit, library_scope
                )
                if hits:
                    return hits
                # Trigram fallback for CJK substring matching when BM25
                # returns no results (e.g. multi-character Chinese phrases
                # that the unicode61 tokenizer splits into single chars).
                if await self._trigram_available(session):
                    trigram_hits = await self._search_trigram(
                        session, user_id, query, limit, library_scope
                    )
                    if trigram_hits:
                        return trigram_hits
            return await self._search_lexical(
                session, user_id, query, limit, library_scope
            )

    # ── internals ────────────────────────────────────────────────

    async def _fts_available(self, session: AsyncSession) -> bool:
        try:
            result = await session.execute(
                sa.text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name=:name"
                ),
                {"name": FTS_TABLE},
            )
            return result.first() is not None
        except Exception:
            return False

    async def _search_fts(
        self,
        session: AsyncSession,
        user_id: UUID | None,
        query: str,
        limit: int,
        library_scope: str | None,
    ) -> list[ChunkHit] | None:
        from StAgent.knowledge.acl import (
            SQLITE_KNOWLEDGE_ACCESS_SQL,
            sqlite_access_params,
        )

        match_expr = escape_fts_query(query)
        if not match_expr:
            return []
        # SQLite may persist UUIDs as dashed or hex-only text; compare both.
        # SQLModel/SQLite may store Enum by name ("KNOWLEDGE") or value ("knowledge").
        scope_value, scope_name = _scope_variants(library_scope)
        sql = sa.text(
            f"""
            SELECT dc.id AS chunk_id, dc.file_id AS file_id, dc.seq AS seq,
                   dc.start_offset AS start_offset, dc.end_offset AS end_offset,
                   dc.text AS text,
                   f.name AS file_name,
                   bm25({FTS_TABLE}) AS score,
                   snippet({FTS_TABLE}, 0, '[', ']', ' … ', 16) AS snippet
            FROM {FTS_TABLE}
            JOIN document_chunks dc ON dc.rowid = {FTS_TABLE}.rowid
            JOIN files f ON f.id = dc.file_id
            WHERE {FTS_TABLE} MATCH :match
              AND f.is_deleted = 0
              AND {SQLITE_KNOWLEDGE_ACCESS_SQL}
              AND (
                    :scope_value IS NULL
                    OR f.library_scope = :scope_value
                    OR f.library_scope = :scope_name
                  )
            ORDER BY score
            LIMIT :limit
            """
        )
        try:
            result = await session.execute(
                sql,
                {
                    "match": match_expr,
                    "scope_value": scope_value,
                    "scope_name": scope_name,
                    "limit": limit,
                    **sqlite_access_params(user_id),
                },
            )
        except Exception:
            return None
        return [self._row_to_hit(row, score=-float(row.score)) for row in result]

    async def _trigram_available(self, session: AsyncSession) -> bool:
        """True when the supplementary trigram FTS5 index exists."""
        try:
            result = await session.execute(
                sa.text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name=:name"
                ),
                {"name": TRIGRAM_TABLE},
            )
            return result.first() is not None
        except Exception:
            return False

    async def _search_trigram(
        self,
        session: AsyncSession,
        user_id: UUID | None,
        query: str,
        limit: int,
        library_scope: str | None,
    ) -> list[ChunkHit]:
        """Trigram-based fallback for CJK substring matching.

        The trigram tokenizer does not support BM25 ranking, so results are
        returned with a flat score of 1.0 (ordered by chunk sequence). This is
        acceptable as a *rescue* path when the main BM25 index misses CJK
        multi-character queries.
        """
        from StAgent.knowledge.acl import (
            SQLITE_KNOWLEDGE_ACCESS_SQL,
            sqlite_access_params,
        )

        match_expr = escape_trigram_query(query)
        if not match_expr:
            return []
        scope_value, scope_name = _scope_variants(library_scope)
        sql = sa.text(
            f"""
            SELECT dc.id AS chunk_id, dc.file_id AS file_id, dc.seq AS seq,
                   dc.start_offset AS start_offset, dc.end_offset AS end_offset,
                   dc.text AS text,
                   f.name AS file_name,
                   1.0 AS score,
                   dc.text AS snippet
            FROM {TRIGRAM_TABLE}
            JOIN document_chunks dc ON dc.rowid = {TRIGRAM_TABLE}.rowid
            JOIN files f ON f.id = dc.file_id
            WHERE {TRIGRAM_TABLE} MATCH :match
              AND f.is_deleted = 0
              AND {SQLITE_KNOWLEDGE_ACCESS_SQL}
              AND (
                    :scope_value IS NULL
                    OR f.library_scope = :scope_value
                    OR f.library_scope = :scope_name
                  )
            LIMIT :limit
            """
        )
        try:
            result = await session.execute(
                sql,
                {
                    "match": match_expr,
                    "scope_value": scope_value,
                    "scope_name": scope_name,
                    "limit": limit,
                    **sqlite_access_params(user_id),
                },
            )
        except Exception:
            return []
        hits = [
            self._row_to_hit(row, score=float(row.score))
            for row in result
        ]
        # Trigram has no BM25 ranking — build a snippet from the query.
        for hit in hits:
            hit.snippet = _lexical_snippet(hit.text, query)
        return hits

    async def _search_lexical(
        self,
        session: AsyncSession,
        user_id: UUID | None,
        query: str,
        limit: int,
        library_scope: str | None,
    ) -> list[ChunkHit]:
        from StAgent.db.models.file import File

        stmt = (
            select(DocumentChunk, File.name)
            .join(File, col(File.id) == col(DocumentChunk.file_id))
            .where(File.is_deleted == False)  # noqa: E712
            .where(DocumentChunk.text.ilike(f"%{query}%"))  # type: ignore[attr-defined]
        )
        if user_id is not None:
            from StAgent.knowledge.acl import knowledge_file_access_clause

            stmt = stmt.where(knowledge_file_access_clause(session, user_id))
        if library_scope is not None:
            from StAgent.db.models.file import LibraryScope as _LibraryScope

            try:
                scope_enum = _LibraryScope(str(library_scope).lower())
            except ValueError:
                scope_enum = _LibraryScope[str(library_scope).upper()]
            stmt = stmt.where(File.library_scope == scope_enum)
        stmt = stmt.limit(limit)
        result = await session.exec(stmt)
        hits: list[ChunkHit] = []
        for chunk, file_name in result.all():
            hits.append(
                ChunkHit(
                    chunk_id=chunk.id,
                    file_id=chunk.file_id,
                    file_name=file_name or "",
                    seq=chunk.seq,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    text=chunk.text,
                    snippet=_lexical_snippet(chunk.text, query),
                    score=1.0,
                )
            )
        return hits

    @staticmethod
    def _row_to_hit(row: Any, *, score: float) -> ChunkHit:
        return ChunkHit(
            chunk_id=_as_uuid(row.chunk_id),
            file_id=_as_uuid(row.file_id),
            file_name=row.file_name or "",
            seq=int(row.seq),
            start_offset=int(row.start_offset),
            end_offset=int(row.end_offset),
            text=row.text or "",
            snippet=row.snippet or "",
            score=score,
        )


def _as_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _scope_variants(library_scope: str | None) -> tuple[str | None, str | None]:
    """Return ``(value, NAME)`` forms for SQLite enum storage drift."""
    if library_scope is None:
        return None, None
    raw = str(library_scope).strip()
    if not raw:
        return None, None
    return raw.lower(), raw.upper()


def _lexical_snippet(text: str, query: str, *, radius: int = 120) -> str:
    lower = text.lower()
    idx = lower.find(query.lower())
    if idx < 0:
        return text[: radius * 2].strip()
    start = max(0, idx - radius)
    end = min(len(text), idx + len(query) + radius)
    prefix = "… " if start > 0 else ""
    suffix = " …" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"
