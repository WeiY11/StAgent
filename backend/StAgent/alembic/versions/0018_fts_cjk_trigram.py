"""Add trigram FTS5 index for better CJK knowledge search.

Revision ID: 0018_fts_cjk_trigram
Revises: 0017_complaints
Create Date: 2026-07-29

Creates a supplementary ``document_chunks_trigram`` virtual table using the
FTS5 ``trigram`` tokenizer. The default ``unicode61`` tokenizer does not
perform reliable word segmentation for continuous CJK prose, making a whole
multi-character question over-constrained. The trigram tokenizer indexes
3-character overlapping n-grams without a language-specific dictionary.

The trigram table is *not* a replacement for the BM25 table — it is used as
a fallback when the main BM25 path returns zero hits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0018_fts_cjk_trigram"
down_revision: str | None = "0017_complaints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGRAM_TABLE = "document_chunks_trigram"
_SOURCE_TABLE = "document_chunks"


def _is_sqlite() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "sqlite"


def _has_table(table_name: str) -> bool:
    from sqlalchemy import inspect

    return table_name in inspect(op.get_bind()).get_table_names()


def _has_fts_table(table_name: str) -> bool:
    """Check if an FTS5 virtual table exists (SQLite only)."""
    if not _is_sqlite():
        return False
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name=:name"
        ),
        {"name": table_name},
    )
    return result.first() is not None


def upgrade() -> None:
    if not _is_sqlite():
        return
    if not _has_table(_SOURCE_TABLE):
        return
    if _has_fts_table(_TRIGRAM_TABLE):
        return

    for ddl in _trigram_ddl():
        op.execute(ddl)

    # Rebuild the trigram index from existing document_chunks data.
    op.execute(
        f"INSERT INTO {_TRIGRAM_TABLE}({_TRIGRAM_TABLE}) VALUES('rebuild')"
    )


def downgrade() -> None:
    if not _is_sqlite():
        return
    if not _has_fts_table(_TRIGRAM_TABLE):
        return
    op.execute(f"DROP TABLE IF EXISTS {_TRIGRAM_TABLE}")


def _trigram_ddl() -> tuple[str, ...]:
    return (
        f"""
        CREATE VIRTUAL TABLE {_TRIGRAM_TABLE} USING fts5(
            text,
            content='{_SOURCE_TABLE}',
            content_rowid='rowid',
            tokenize='trigram'
        )
        """,
    )
