"""Keep the CJK trigram FTS index synchronized with document chunks.

Revision ID: 0025_fts_cjk_trigram_sync
Revises: 0024_user_settings
Create Date: 2026-08-24
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0025_fts_cjk_trigram_sync"
down_revision: str | None = "0024_user_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_TABLE = "document_chunks"
_TRIGRAM_TABLE = "document_chunks_trigram"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _has_table(table_name: str) -> bool:
    result = op.get_bind().execute(
        sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table_name},
    )
    return result.first() is not None


def upgrade() -> None:
    if not _is_sqlite() or not _has_table(_SOURCE_TABLE):
        return
    op.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {_TRIGRAM_TABLE} USING fts5(
            text,
            content='{_SOURCE_TABLE}',
            content_rowid='rowid',
            tokenize='trigram'
        )
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_TRIGRAM_TABLE}_ai
        AFTER INSERT ON {_SOURCE_TABLE} BEGIN
            INSERT INTO {_TRIGRAM_TABLE}(rowid, text) VALUES (new.rowid, new.text);
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_TRIGRAM_TABLE}_ad
        AFTER DELETE ON {_SOURCE_TABLE} BEGIN
            INSERT INTO {_TRIGRAM_TABLE}({_TRIGRAM_TABLE}, rowid, text)
            VALUES ('delete', old.rowid, old.text);
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_TRIGRAM_TABLE}_au
        AFTER UPDATE ON {_SOURCE_TABLE} BEGIN
            INSERT INTO {_TRIGRAM_TABLE}({_TRIGRAM_TABLE}, rowid, text)
            VALUES ('delete', old.rowid, old.text);
            INSERT INTO {_TRIGRAM_TABLE}(rowid, text) VALUES (new.rowid, new.text);
        END
        """
    )
    op.execute(f"INSERT INTO {_TRIGRAM_TABLE}({_TRIGRAM_TABLE}) VALUES('rebuild')")


def downgrade() -> None:
    if not _is_sqlite():
        return
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGRAM_TABLE}_au")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGRAM_TABLE}_ad")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGRAM_TABLE}_ai")
