"""File content model — extracted text stored separately from the File catalog.

Large document extractions (up to 500 KB per file) bloat the ``files`` table
row and slow down every query that touches it (list, paginate, catalog).
This table stores the heavy text payload in a dedicated row, joined only when
the text is actually needed (``knowledge_search`` read, re-index, export).

The ``text_hash`` field (SHA-256 first 16 hex chars) enables incremental
indexing: when a file is re-uploaded with identical content, the chunking
step can be skipped entirely.
"""

from __future__ import annotations

from uuid import UUID

from sqlmodel import Column, Field, SQLModel, Text

from StAgent.db.models.base import BaseModel


class FileContent(BaseModel, table=True):
    """Heavy text payload associated with a File row."""

    __tablename__ = "file_contents"

    file_id: UUID = Field(foreign_key="files.id", index=True, unique=True)
    user_id: UUID | None = Field(default=None, index=True)
    extracted_text: str | None = Field(default=None, sa_column=Column(Text))
    text_char_count: int = Field(default=0)
    text_hash: str | None = Field(default=None, max_length=64)


class FileContentRead(SQLModel):
    """Read schema for file content (used by knowledge read actions)."""

    id: UUID
    file_id: UUID
    user_id: UUID | None
    extracted_text: str | None
    text_char_count: int
    text_hash: str | None
