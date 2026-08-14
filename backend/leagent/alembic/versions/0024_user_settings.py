"""user_settings key/value store

Revision ID: 0024_user_settings
Revises: 0023_agent_missions
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0024_user_settings"
down_revision: str | None = "0023_agent_missions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("user_settings"):
        return
    op.create_table(
        "user_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "key", name="uq_user_settings_user_key"),
    )
    op.create_index(op.f("ix_user_settings_user_id"), "user_settings", ["user_id"])
    op.create_index(op.f("ix_user_settings_key"), "user_settings", ["key"])


def downgrade() -> None:
    if not _has_table("user_settings"):
        return
    op.drop_index(op.f("ix_user_settings_key"), table_name="user_settings")
    op.drop_index(op.f("ix_user_settings_user_id"), table_name="user_settings")
    op.drop_table("user_settings")
