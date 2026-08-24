"""Managed, one-passage-per-chunk imports for public retrieval evaluation.

This module is deliberately narrower than the normal asynchronous file
processing path. Public document-level qrels require every corpus passage to
have exactly one local chunk, so evaluation callers must use a fresh isolated
database and explicitly opt into this importer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from StAgent.db.models.base import utc_now
from StAgent.db.models.file import File, FileStatus, FileType, InboxState, LibraryScope
from StAgent.db.models.file_content import FileContent
from StAgent.db.models.knowledge_acl import KnowledgeAccessScope
from StAgent.db.models.knowledge_index import KnowledgeIndexStatus
from StAgent.db.repositories.knowledge_index import INDEX_VERSION
from StAgent.file.primitives import FileScope
from StAgent.library.chunking import Chunk

if TYPE_CHECKING:
    from uuid import UUID

    from StAgent.db.service import DatabaseService
    from StAgent.file.service import FileService
    from StAgent.knowledge.evaluation_dataset import PublicRetrievalDataset


_ORIGIN_TYPE = "retrieval_eval"
_MAX_ORIGIN_REF_LENGTH = 100


@dataclass(frozen=True)
class ImportedEvaluationPassage:
    """Auditable local projection of exactly one upstream corpus passage."""

    source_id: str
    file_id: UUID
    chunk_id: UUID
    text_sha256: str
    text_char_count: int
    sequence: int
    start_offset: int
    end_offset: int

    def to_dict(self) -> dict[str, int | str]:
        """Return a JSON-serializable mapping record for the evaluation run."""

        return {
            "source_id": self.source_id,
            "file_id": str(self.file_id),
            "chunk_id": str(self.chunk_id),
            "text_sha256": self.text_sha256,
            "text_char_count": self.text_char_count,
            "sequence": self.sequence,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
        }


def _validate_dataset_for_import(dataset: PublicRetrievalDataset) -> None:
    """Reject identifiers that cannot be recorded faithfully in ``File.origin_ref``."""

    for passage in dataset.corpus:
        if len(passage.source_id) > _MAX_ORIGIN_REF_LENGTH:
            raise ValueError(f"corpus source id exceeds File.origin_ref capacity: {passage.source_id[:40]!r}")


def _evaluation_filename(source_id: str, position: int) -> str:
    """Create a stable, filesystem-safe name without treating source IDs as paths."""

    source_digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]
    return f"retrieval-eval-{position:05d}-{source_digest}.txt"


async def import_public_retrieval_dataset(
    db: DatabaseService,
    file_service: FileService,
    dataset: PublicRetrievalDataset,
    *,
    user_id: UUID,
    knowledge_space_id: UUID,
) -> list[ImportedEvaluationPassage]:
    """Persist a public corpus through managed blobs and canonical chunk storage.

    The caller owns the database and blob-root isolation. This function never
    invokes the regular auto-chunker; that preserves the required one source
    passage to one local chunk mapping.
    """

    _validate_dataset_for_import(dataset)
    mappings: list[ImportedEvaluationPassage] = []
    for position, passage in enumerate(dataset.corpus):
        raw = passage.text.encode("utf-8")
        text_sha256 = hashlib.sha256(raw).hexdigest()
        text_hash = text_sha256[:16]
        ref = await file_service.register(
            raw,
            filename=_evaluation_filename(passage.source_id, position),
            content_type="text/plain",
            user_id=user_id,
            scope=FileScope.KNOWLEDGE,
            category=_ORIGIN_TYPE,
            library_scope=LibraryScope.KNOWLEDGE.value,
            origin_type=_ORIGIN_TYPE,
            origin_ref=passage.source_id,
            metadata={
                "dataset_id": dataset.dataset_id,
                "dataset_revision": dataset.revision,
                "source_id": passage.source_id,
            },
            persist_db_row=False,
            dedup=False,
        )
        storage_path = ref.metadata.get("storage_path")
        if not isinstance(storage_path, str) or not storage_path:
            raise RuntimeError("managed evaluation blob did not return a durable storage path")

        async with db.session() as session:
            session.add(
                File(
                    id=ref.id,
                    user_id=user_id,
                    workspace_id=user_id,
                    knowledge_space_id=knowledge_space_id,
                    access_scope=KnowledgeAccessScope.PRIVATE,
                    name=ref.filename,
                    original_name=ref.filename,
                    file_type=FileType.DOCUMENT,
                    mime_type=ref.content_type,
                    size=ref.size,
                    checksum=ref.checksum,
                    status=FileStatus.PROCESSED,
                    storage_path=storage_path,
                    extracted_text=passage.text,
                    file_metadata=json.dumps(
                        {
                            "dataset_id": dataset.dataset_id,
                            "dataset_revision": dataset.revision,
                            "source_id": passage.source_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    is_indexed=True,
                    library_scope=LibraryScope.KNOWLEDGE,
                    inbox_state=InboxState.NEW,
                    origin_type=_ORIGIN_TYPE,
                    origin_ref=passage.source_id,
                )
            )
            await session.flush()
            session.add(
                FileContent(
                    file_id=ref.id,
                    user_id=user_id,
                    extracted_text=passage.text,
                    text_char_count=len(passage.text),
                    text_hash=text_hash,
                )
            )

        await db.repositories.document_chunks.replace_for_file(
            ref.id,
            user_id,
            [Chunk.with_hash(0, 0, len(passage.text), passage.text)],
        )
        chunk_ids = await db.repositories.document_chunks.list_ids_for_file(ref.id)
        if len(chunk_ids) != 1:
            raise RuntimeError(
                f"evaluation importer requires one chunk per passage, got {len(chunk_ids)} for {passage.source_id!r}"
            )
        chunk_id = chunk_ids[0]
        await db.repositories.knowledge_index.upsert_state(
            ref.id,
            user_id,
            status=KnowledgeIndexStatus.READY,
            index_version=INDEX_VERSION,
            content_hash=text_hash,
            chunk_count=1,
            embedded_count=0,
            completed_at=utc_now(),
        )
        mappings.append(
            ImportedEvaluationPassage(
                source_id=passage.source_id,
                file_id=ref.id,
                chunk_id=chunk_id,
                text_sha256=text_sha256,
                text_char_count=len(passage.text),
                sequence=0,
                start_offset=0,
                end_offset=len(passage.text),
            )
        )
    return mappings


__all__ = ["ImportedEvaluationPassage", "import_public_retrieval_dataset"]
