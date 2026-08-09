"""Progressive knowledge-base tool: catalog → search → read.

Does not inject KB content into the system prompt. The agent enables this
path by calling the tool when the turn looks knowledge-related (tool pool
scoring + a one-line capabilities ad). Explicit ``@knowledge:`` attachments
remain a shortcut for whole-file mounts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlmodel import col, select

from leagent.db.models.file import File, LibraryScope
from leagent.db.sqlite_compat import uuid_column_equals
from leagent.library.summary import summarize_extracted_text
from leagent.tools.base import BaseTool, ToolCapability, ToolCategory, ToolContext
from leagent.tools.context import resolve_effective_user_id

logger = structlog.get_logger(__name__)

_DEFAULT_SEARCH_LIMIT = 8
_MAX_SEARCH_LIMIT = 20
_DEFAULT_READ_CHARS = 8000
_MAX_READ_CHARS = 32_000
_MAX_CATALOG = 100


def _pet_project_library_file_ids_subquery(session: Any, user_id: UUID):
    from leagent.db.models import PetProject, PetProjectFile

    return (
        select(PetProjectFile.file_id)
        .join(PetProject, PetProjectFile.pet_project_id == PetProject.id)
        .where(
            uuid_column_equals(session, PetProject.user_id, user_id),
            PetProject.is_deleted == False,  # noqa: E712
        )
    )


class KnowledgeSearchTool(BaseTool):
    """User knowledge base: catalog summaries, BM25 search, then read text windows."""

    name = "knowledge_search"
    description = (
        "User knowledge base (catalog → search → read). "
        "Call action=catalog first for document summaries, then action=search with a query, "
        "then action=read with file_id (and optional offsets) for extracted text. "
        "Do not invent document contents. If the user already @knowledge-attached a file, "
        "prefer existing document readers on that path instead."
    )
    category = ToolCategory.DOC
    version = "1.0.0"
    timeout_sec = 30
    is_read_only = True
    is_concurrency_safe = True
    capabilities = (ToolCapability.FILE_READ,)
    search_hint = (
        "knowledge kb 知识库 文档库 资料 查阅 document library catalog search "
        "根据文档 参考文档 查阅资料"
    )
    max_result_size_chars = 50_000

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["action"],
            "additionalProperties": False,
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["catalog", "search", "read"],
                    "description": (
                        "catalog: list KB docs with summaries; "
                        "search: BM25 over indexed chunks (requires query); "
                        "read: extracted text window (requires file_id)."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "Search query (required for action=search).",
                    "minLength": 1,
                    "maxLength": 1000,
                },
                "file_id": {
                    "type": "string",
                    "description": "Knowledge file UUID (required for action=read).",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max search hits (1–{_MAX_SEARCH_LIMIT}, default {_DEFAULT_SEARCH_LIMIT}).",
                    "minimum": 1,
                    "maximum": _MAX_SEARCH_LIMIT,
                },
                "start_offset": {
                    "type": "integer",
                    "description": "Inclusive start offset into extracted_text (action=read).",
                    "minimum": 0,
                },
                "end_offset": {
                    "type": "integer",
                    "description": "Exclusive end offset into extracted_text (action=read).",
                    "minimum": 0,
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        f"Max characters to return from start_offset when end_offset "
                        f"is omitted (default {_DEFAULT_READ_CHARS}, max {_MAX_READ_CHARS})."
                    ),
                    "minimum": 1,
                    "maximum": _MAX_READ_CHARS,
                },
            },
        }

    def get_activity_description(self, params: dict[str, Any] | None = None) -> str | None:
        action = (params or {}).get("action") or "catalog"
        if action == "search":
            q = (params or {}).get("query") or ""
            return f"Searching knowledge base{f' for {q!r}' if q else ''}"
        if action == "read":
            return "Reading knowledge document"
        return "Listing knowledge base"

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        action = str(params.get("action") or "").strip()
        if action not in ("catalog", "search", "read"):
            return {"error": "action must be catalog, search, or read"}

        from leagent.db import get_database_service

        user_id = resolve_effective_user_id(context.user_id, session_id=context.session_id)
        if user_id is None:
            return {"error": "user_id required to access knowledge base", "action": action}

        db = get_database_service()

        if action == "catalog":
            return await self._catalog(db, user_id)
        if action == "search":
            query = str(params.get("query") or "").strip()
            if not query:
                return {"error": "query is required for action=search", "action": action}
            limit = int(params.get("limit") or _DEFAULT_SEARCH_LIMIT)
            limit = max(1, min(limit, _MAX_SEARCH_LIMIT))
            return await self._search(db, user_id, query, limit=limit)
        return await self._read(db, user_id, params)

    async def _batch_ensure_summaries(
        self, db: Any, rows: list[File]
    ) -> dict[UUID, str | None]:
        """Generate and persist missing summaries in a single batch.

        Returns a mapping of ``file_id → summary`` for all rows. Files that
        already have a summary or lack extracted text are returned as-is
        without extra queries.
        """
        result: dict[UUID, str | None] = {}
        to_update: list[tuple[UUID, str]] = []

        for row in rows:
            if row.summary:
                result[row.id] = row.summary
                continue
            if not row.extracted_text:
                result[row.id] = None
                continue
            summary = summarize_extracted_text(row.extracted_text)
            result[row.id] = summary
            if summary is not None:
                to_update.append((row.id, summary))
                row.summary = summary

        if not to_update:
            return result

        # Persist all new summaries in a single batch update.
        try:
            now = datetime.utcnow()
            async with db.session() as session:
                for file_id, summary in to_update:
                    await session.execute(
                        sa.text(
                            "UPDATE files SET summary = :summary, "
                            "updated_at = :now "
                            "WHERE lower(replace(CAST(id AS TEXT), '-', '')) "
                            "= :file_hex"
                        ),
                        {
                            "summary": summary,
                            "now": now,
                            "file_hex": str(file_id).replace("-", "").lower(),
                        },
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "knowledge_batch_summary_write_failed",
                error=str(exc),
                count=len(to_update),
            )

        return result

    async def _catalog(self, db: Any, user_id: UUID) -> dict[str, Any]:
        # TODO: migrate to FileRepository — catalog query requires subquery against PetProjectFile
        # and filtering by LibraryScope.KNOWLEDGE, which is not yet encapsulated in the repository.
        async with db.session() as session:
            from leagent.knowledge.acl import knowledge_file_access_clause

            pet_lib = _pet_project_library_file_ids_subquery(session, user_id)
            stmt = (
                select(File)
                .where(
                    knowledge_file_access_clause(session, user_id),
                    File.is_deleted == False,  # noqa: E712
                    File.library_scope == LibraryScope.KNOWLEDGE,
                    ~File.id.in_(pet_lib),
                )
                .order_by(col(File.created_at).desc())
                .limit(_MAX_CATALOG)
            )
            rows = list((await session.exec(stmt)).all())

        # Batch-ensure summaries for all rows in one pass.
        summary_map = await self._batch_ensure_summaries(db, rows)

        documents: list[dict[str, Any]] = []
        for row in rows:
            documents.append(
                {
                    "file_id": str(row.id),
                    "name": row.original_name or row.name,
                    "file_type": row.file_type.value if hasattr(row.file_type, "value") else str(row.file_type),
                    "is_indexed": bool(row.is_indexed),
                    "summary": summary_map.get(row.id),
                }
            )

        if not documents:
            return {
                "action": "catalog",
                "total": 0,
                "documents": [],
                "hint": "Knowledge base is empty. Ask the user to upload documents or promote chat files.",
            }
        return {
            "action": "catalog",
            "total": len(documents),
            "documents": documents,
            "hint": "Next: action=search with a focused query, then action=read for needed file_id windows.",
        }

    async def _search(
        self,
        db: Any,
        user_id: UUID,
        query: str,
        *,
        limit: int,
    ) -> dict[str, Any]:
        from leagent.library.retrieval import (
            KnowledgeRetrievalPipeline,
            SearchOptions,
            get_knowledge_retrieval_pipeline,
        )

        pipeline = get_knowledge_retrieval_pipeline()
        if pipeline is None:
            pipeline = KnowledgeRetrievalPipeline(db)

        options = SearchOptions(
            query=query,
            user_id=user_id,
            limit=limit,
            library_scope=LibraryScope.KNOWLEDGE.value,
        )
        retrieval_results = await pipeline.search(options)
        from leagent.knowledge.evidence import build_evidence

        evidence = build_evidence(retrieval_results, strict=True)

        results: list[dict[str, Any]] = [
            {
                "citation_id": f"K{index}",
                "file_id": str(r.file_id),
                "name": r.file_name,
                "score": r.score,
                "snippet": r.snippet,
                "chunk_id": str(r.chunk_id),
                "start_offset": r.start_offset,
                "end_offset": r.end_offset,
            }
            for index, r in enumerate(retrieval_results, start=1)
        ]

        return {
            "action": "search",
            "query": query,
            "total": len(results),
            "results": results,
            "evidence_status": evidence.status.value,
            "citations": [
                {
                    "citation_id": citation.citation_id,
                    "file_id": str(citation.file_id),
                    "chunk_id": str(citation.chunk_id),
                    "start_offset": citation.start_offset,
                    "end_offset": citation.end_offset,
                }
                for citation in evidence.citations
            ],
            "hint": (
                "Use action=read with file_id and start_offset/end_offset from a hit "
                "to load the matching text window."
                if results
                else "No chunk matches. Try action=catalog or a broader query."
            ),
        }

    async def _read(self, db: Any, user_id: UUID, params: dict[str, Any]) -> dict[str, Any]:
        raw_id = str(params.get("file_id") or "").strip()
        if not raw_id:
            return {"error": "file_id is required for action=read", "action": "read"}
        try:
            file_id = UUID(raw_id)
        except (TypeError, ValueError):
            return {"error": f"invalid file_id: {raw_id}", "action": "read"}

        row = await db.repositories.files.get(file_id)
        can_read = False
        if row is not None:
            from sqlmodel import select

            from leagent.db.models.file import File
            from leagent.db.sqlite_compat import uuid_column_equals
            from leagent.knowledge.acl import knowledge_file_access_clause

            async with db.session() as session:
                can_read = (
                    await session.exec(
                        select(File.id)
                        .where(uuid_column_equals(session, File.id, file_id))
                        .where(knowledge_file_access_clause(session, user_id))
                    )
                ).first() is not None

        if (
            row is None
            or row.is_deleted
            or not can_read
            or row.library_scope != LibraryScope.KNOWLEDGE
        ):
            return {
                "error": "file not found in your knowledge base",
                "action": "read",
                "file_id": raw_id,
            }

        # Prefer FileContent table; fall back to legacy File.extracted_text.
        text = await self._load_extracted_text(db, file_id) or row.extracted_text
        if not text or not str(text).strip():
            return {
                "error": "no extracted text available; file may still be processing or unsupported",
                "action": "read",
                "file_id": str(row.id),
                "name": row.original_name or row.name,
                "is_indexed": bool(row.is_indexed),
            }

        start = int(params.get("start_offset") or 0)
        start = max(0, min(start, len(text)))
        end_raw = params.get("end_offset")
        if end_raw is not None:
            end = int(end_raw)
            end = max(start, min(end, len(text)))
        else:
            max_chars = int(params.get("max_chars") or _DEFAULT_READ_CHARS)
            max_chars = max(1, min(max_chars, _MAX_READ_CHARS))
            end = min(len(text), start + max_chars)

        summary_map = await self._batch_ensure_summaries(db, [row])
        summary = summary_map.get(row.id)
        window = text[start:end]
        truncated = end < len(text)

        return {
            "action": "read",
            "file_id": str(row.id),
            "name": row.original_name or row.name,
            "summary": summary,
            "start_offset": start,
            "end_offset": end,
            "total_chars": len(text),
            "truncated": truncated,
            "text": window,
            "hint": (
                "Increase start_offset or call again with a later window if truncated."
                if truncated
                else None
            ),
        }

    @staticmethod
    async def _load_extracted_text(db: Any, file_id: UUID) -> str | None:
        """Load extracted text from FileContent table (preferred) or return None."""
        from sqlmodel import select

        from leagent.db.models.file_content import FileContent
        from leagent.db.sqlite_compat import uuid_column_equals

        try:
            async with db.session() as session:
                result = await session.exec(
                    select(FileContent.extracted_text).where(
                        uuid_column_equals(session, FileContent.file_id, file_id)
                    )
                )
                return result.first()
        except Exception:
            return None
