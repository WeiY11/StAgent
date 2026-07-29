"""Create file_contents table to offload extracted_text from files.

Revision ID: 0019_file_contents
Revises: 0018_fts_cjk_trigram
Create Date: 2026-07-29

Large document extractions (up to 500 KB per file) bloat the ``files`` table
row and slow down every query that touches it.  This migration creates a
dedicated ``file_contents`` table and copies existing ``extracted_text`` data
from ``files`` into it.  The ``files.extracted_text`` column is preserved for
backward compatibility and will be dropped in a future migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0019_file_contents"
down_revision: str | None = "0018_fts_cjk_trigram"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "file_contents"


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
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("text_char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(f"ix_{_TABLE}_file_id", _TABLE, ["file_id"], unique=True)
    op.create_index(f"ix_{_TABLE}_user_id", _TABLE, ["user_id"])

    # Migrate existing extracted_text from files table.
    bind = op.get_bind()
    if not _has_table("files") or not _has_column("files", "extracted_text"):
        return

    # Use raw SQL for cross-dialect data migration.
    if bind.dialect.name == "sqlite":
        op.execute(
            f"""
            INSERT INTO {_TABLE} (id, created_at, updated_at, file_id, user_id,
                                   extracted_text, text_char_count, text_hash)
            SELECT lower(hex(randomblob(16))),
                   datetime('now'), datetime('now'),
                   f.id, f.user_id,
                   f.extracted_text,
                   length(COALESCE(f.extracted_text, '')),
                   NULL
            FROM files f
            WHERE f.extracted_text IS NOT NULL
              AND f.extracted_text != ''
            """
        )
    else:
        op.execute(
            f"""
            INSERT INTO {_TABLE} (id, created_at, updated_at, file_id, user_id,
                                   extracted_text, text_char_count, text_hash)
            SELECT gen_random_uuid(),
                   now(), now(),
                   f.id, f.user_id,
                   f.extracted_text,
                   length(COALESCE(f.extracted_text, '')),
                   NULL
            FROM files f
            WHERE f.extracted_text IS NOT NULL
              AND f.extracted_text != ''
            """
        )


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    op.drop_index(f"ix_{_TABLE}_user_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_file_id", table_name=_TABLE)
    op.drop_table(_TABLE)
