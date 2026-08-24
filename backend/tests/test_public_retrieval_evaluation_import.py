"""Integration coverage for the isolated public-retrieval importer."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select

from StAgent.config.settings import DatabaseSettings, FilesSettings, Settings
from StAgent.db.models.document_chunk import DocumentChunk
from StAgent.db.models.file import File, FileStatus, LibraryScope
from StAgent.db.models.file_content import FileContent
from StAgent.db.models.identity_stub import UserStub, WorkspaceStub
from StAgent.db.models.knowledge_acl import KnowledgeSpace
from StAgent.db.service import DatabaseService
from StAgent.file.service import FileService
from StAgent.file.storage import LocalStorageBackend
from StAgent.knowledge.acl import ensure_personal_knowledge_space
from StAgent.knowledge.evaluation_dataset import build_evaluation_cases, parse_public_retrieval_dataset
from StAgent.knowledge.evaluation_import import import_public_retrieval_dataset
from StAgent.services.auth.service import LOCAL_USER_ID


@pytest.mark.asyncio
async def test_public_retrieval_import_uses_managed_files_and_one_chunk_per_passage(
    tmp_path: Path,
) -> None:
    """A document-level qrel is imported through the real file/chunk contract."""

    dataset = parse_public_retrieval_dataset(
        dataset_id="example/public-retrieval",
        revision="revision-1",
        corpus_rows=[
            {"_id": "passage-a", "text": "火星轨道导航系统用于深空探测。"},
            {"_id": "passage-b", "text": "海洋气候观测浮标记录海温。"},
        ],
        query_rows=[{"_id": "query-a", "text": "火星轨道导航系统是什么？"}],
        qrel_rows=[{"query-id": "query-a", "corpus-id": "passage-a"}],
    )
    settings = Settings(
        database=DatabaseSettings(sqlite_path=str(tmp_path / "isolated-eval.sqlite3")),
        files=FilesSettings(upload_dir=str(tmp_path / "unused-uploads")),
    )
    db = DatabaseService(settings)
    file_service = FileService(
        default_backend=LocalStorageBackend(tmp_path / "blobs"),
        database=db,
    )
    await db.create_tables()
    try:
        space_id = await ensure_personal_knowledge_space(db, LOCAL_USER_ID)
        async with db.session() as session:
            assert await session.get(UserStub, LOCAL_USER_ID) is not None
            assert await session.get(WorkspaceStub, LOCAL_USER_ID) is not None
            assert await session.get(KnowledgeSpace, space_id) is not None
        mappings = await import_public_retrieval_dataset(
            db,
            file_service,
            dataset,
            user_id=LOCAL_USER_ID,
            knowledge_space_id=space_id,
        )

        assert [mapping.source_id for mapping in mappings] == ["passage-a", "passage-b"]
        assert len({mapping.file_id for mapping in mappings}) == 2
        assert len({mapping.chunk_id for mapping in mappings}) == 2
        assert all(mapping.sequence == 0 for mapping in mappings)
        assert all(mapping.start_offset == 0 for mapping in mappings)

        async with db.session() as session:
            files = list((await session.exec(select(File).order_by(File.origin_ref))).all())
            contents = list((await session.exec(select(FileContent))).all())
            chunks = list((await session.exec(select(DocumentChunk))).all())

        assert len(files) == len(dataset.corpus)
        assert len(contents) == len(dataset.corpus)
        assert len(chunks) == len(dataset.corpus)
        assert {file.origin_ref for file in files} == {"passage-a", "passage-b"}
        assert all(file.origin_type == "retrieval_eval" for file in files)
        assert all(file.library_scope == LibraryScope.KNOWLEDGE for file in files)
        assert all(file.status == FileStatus.PROCESSED and file.is_indexed for file in files)
        assert all(Path(file.storage_path).is_file() for file in files)
        assert {content.extracted_text for content in contents} == {record.text for record in dataset.corpus}
        assert {chunk.text for chunk in chunks} == {record.text for record in dataset.corpus}
        assert all(chunk.seq == 0 and chunk.start_offset == 0 for chunk in chunks)

        cases = build_evaluation_cases(
            dataset,
            source_to_chunk_ids={mapping.source_id: str(mapping.chunk_id) for mapping in mappings},
        )
        assert len(cases) == 1
        assert cases[0].relevant_chunk_ids == frozenset(
            {str(next(mapping.chunk_id for mapping in mappings if mapping.source_id == "passage-a"))}
        )
    finally:
        await db.dispose()
