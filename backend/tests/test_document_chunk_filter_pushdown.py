"""Regression coverage for chunk-search filter pushdown on SQLite."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from StAgent.config.settings import Settings
from StAgent.db.models.file import File, FileStatus, FileType, LibraryScope
from StAgent.db.models.folder import Folder
from StAgent.db.models.identity_stub import UserStub, WorkspaceStub
from StAgent.db.models.knowledge_acl import KnowledgeSpace
from StAgent.db.service import DatabaseService
from StAgent.library.chunking import Chunk

if TYPE_CHECKING:
    from pathlib import Path


async def _add_indexed_file(
    db: DatabaseService,
    *,
    file_id: UUID,
    user_id: UUID,
    space_id: UUID,
    folder_id: UUID,
    file_type: FileType,
    text: str,
) -> None:
    """Persist one visible knowledge file and its sole indexed chunk."""

    async with db.session() as session:
        session.add(
            File(
                id=file_id,
                user_id=user_id,
                workspace_id=user_id,
                knowledge_space_id=space_id,
                folder_id=folder_id,
                name=f"{file_id}.txt",
                original_name=f"{file_id}.txt",
                file_type=file_type,
                size=len(text.encode("utf-8")),
                status=FileStatus.PROCESSED,
                storage_path=f"/tmp/{file_id}.txt",
                extracted_text=text,
                is_indexed=True,
                library_scope=LibraryScope.KNOWLEDGE,
            )
        )
    await db.repositories.document_chunks.replace_for_file(
        file_id,
        user_id,
        [Chunk.with_hash(0, 0, len(text), text)],
    )


@pytest.mark.asyncio
async def test_sqlite_chunk_search_pushes_all_file_filters_before_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Space, folder and type filters must run before every SQLite ``LIMIT``.

    Removing any predicate below is a production bug: a more relevant file in
    another visible scope can consume the one-row candidate budget and leave
    no target document for the pipeline's defensive second-pass filtering.
    """

    db_path = tmp_path / "chunk-filter-pushdown.sqlite3"
    monkeypatch.setenv("DB_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    db = DatabaseService(Settings())
    await db.create_tables()
    try:
        user_id = uuid4()
        target_space_id, other_space_id = uuid4(), uuid4()
        target_folder_id, other_folder_id = uuid4(), uuid4()
        target_file_id = uuid4()

        async with db.session() as session:
            session.add(UserStub(id=user_id))
            session.add(WorkspaceStub(id=user_id))
        async with db.session() as session:
            session.add_all(
                [
                    KnowledgeSpace(
                        id=target_space_id,
                        workspace_id=user_id,
                        name="Target space",
                        slug="target-space",
                    ),
                    KnowledgeSpace(
                        id=other_space_id,
                        workspace_id=user_id,
                        name="Other space",
                        slug="other-space",
                    ),
                    Folder(
                        id=target_folder_id,
                        user_id=user_id,
                        workspace_id=user_id,
                        name="Target folder",
                    ),
                    Folder(
                        id=other_folder_id,
                        user_id=user_id,
                        workspace_id=user_id,
                        name="Other folder",
                    ),
                ]
            )

        # These three records are all ACL-visible to the owner, intentionally
        # precede the target, and match every query below more strongly or at
        # least equally. They exercise each independent filter predicate.
        distractor_text = "中国首都在哪里 ftsneedle ftsneedle 火星轨道导航 lexicalneedle"
        await _add_indexed_file(
            db,
            file_id=uuid4(),
            user_id=user_id,
            space_id=other_space_id,
            folder_id=target_folder_id,
            file_type=FileType.DOCUMENT,
            text=distractor_text,
        )
        await _add_indexed_file(
            db,
            file_id=uuid4(),
            user_id=user_id,
            space_id=target_space_id,
            folder_id=other_folder_id,
            file_type=FileType.DOCUMENT,
            text=distractor_text,
        )
        await _add_indexed_file(
            db,
            file_id=uuid4(),
            user_id=user_id,
            space_id=target_space_id,
            folder_id=target_folder_id,
            file_type=FileType.IMAGE,
            text=distractor_text,
        )
        target_text = "北京是中国的首都。ftsneedle 火星轨道导航 lexicalneedle"
        await _add_indexed_file(
            db,
            file_id=target_file_id,
            user_id=user_id,
            space_id=target_space_id,
            folder_id=target_folder_id,
            file_type=FileType.DOCUMENT,
            text=target_text,
        )

        repository = db.repositories.document_chunks
        filters = {
            "space_id": target_space_id,
            "folder_id": target_folder_id,
            "file_type_filter": [FileType.DOCUMENT.value],
        }
        async with db.session() as session:
            path_hits = {
                "cjk": await repository._search_cjk_terms(
                    session,
                    user_id,
                    "中国首都在哪里",
                    1,
                    LibraryScope.KNOWLEDGE.value,
                    **filters,
                ),
                "fts": await repository._search_fts(
                    session,
                    user_id,
                    "ftsneedle",
                    1,
                    LibraryScope.KNOWLEDGE.value,
                    **filters,
                ),
                "trigram": await repository._search_trigram(
                    session,
                    user_id,
                    "火星轨道导航",
                    1,
                    LibraryScope.KNOWLEDGE.value,
                    **filters,
                ),
                "lexical": await repository._search_lexical(
                    session,
                    user_id,
                    "lexicalneedle",
                    1,
                    LibraryScope.KNOWLEDGE.value,
                    **filters,
                ),
            }

        for path, hits in path_hits.items():
            assert hits is not None, f"{path} index unexpectedly unavailable"
            assert [hit.file_id for hit in hits] == [target_file_id], path

        public_hits = await repository.search(
            user_id,
            "中国首都在哪里",
            limit=1,
            library_scope=LibraryScope.KNOWLEDGE.value,
            **filters,
        )
        assert [hit.file_id for hit in public_hits] == [target_file_id]
    finally:
        await db.dispose()
