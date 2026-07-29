"""Add text_hash to document_chunks for incremental indexing.

Revision ID: 0020_chunk_text_hash
Revises: 0019_file_contents
Create Date: 2026-07-29

Adds a ``text_hash`` column (SHA-256 first 16 hex chars) to each chunk,
enabling diff-based re-indexing: only changed chunks are deleted/inserted
when a file is re-processed, rather than the previous full-replace strategy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0020_chunk_text_hash"
down_revision: str | None = "0019_file_contents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "document_chunks"
_COLUMN = "text_hash"


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(
        c["name"] == column_name
        for c in inspect(op.get_bind()).get_columns(table_name)
    )


def upgrade() -> None:
    if not _has_table(_TABLE):
        return
    if _has_column(_TABLE, _COLUMN):
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    if not _has_column(_TABLE, _COLUMN):
        return
    op.drop_column(_TABLE, _COLUMN)
