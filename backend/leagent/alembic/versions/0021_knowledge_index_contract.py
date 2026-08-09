"""Add durable, versioned knowledge-index state and jobs.

Revision ID: 0021_knowledge_index_contract
Revises: 0020_chunk_text_hash
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0021_knowledge_index_contract"
down_revision: str | None = "0020_chunk_text_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("knowledge_index_states"):
        op.create_table(
            "knowledge_index_states",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("file_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=True),
            sa.Column("index_version", sa.String(length=64), nullable=False, server_default="rag-v1"),
            sa.Column("content_hash", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="uploaded"),
            sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("embedded_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["file_id"], ["files.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ux_knowledge_index_states_file_id",
            "knowledge_index_states",
            ["file_id"],
            unique=True,
        )
        op.create_index(
            "ix_knowledge_index_states_user_id",
            "knowledge_index_states",
            ["user_id"],
        )
        op.create_index(
            "ix_knowledge_index_states_index_version",
            "knowledge_index_states",
            ["index_version"],
        )
        op.create_index(
            "ix_knowledge_index_states_status",
            "knowledge_index_states",
            ["status"],
        )

    if not _has_table("knowledge_index_jobs"):
        op.create_table(
            "knowledge_index_jobs",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("file_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=True),
            sa.Column("index_version", sa.String(length=64), nullable=False, server_default="rag-v1"),
            sa.Column("content_hash", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
            sa.Column("available_at", sa.DateTime(), nullable=False),
            sa.Column("locked_at", sa.DateTime(), nullable=True),
            sa.Column("locked_by", sa.String(length=128), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["file_id"], ["files.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_knowledge_index_jobs_file_id",
            "knowledge_index_jobs",
            ["file_id"],
        )
        op.create_index(
            "ix_knowledge_index_jobs_status_available",
            "knowledge_index_jobs",
            ["status", "available_at"],
        )
        op.create_index(
            "ix_knowledge_index_jobs_user_id",
            "knowledge_index_jobs",
            ["user_id"],
        )


def downgrade() -> None:
    if _has_table("knowledge_index_jobs"):
        op.drop_index("ix_knowledge_index_jobs_user_id", table_name="knowledge_index_jobs")
        op.drop_index("ix_knowledge_index_jobs_status_available", table_name="knowledge_index_jobs")
        op.drop_index("ix_knowledge_index_jobs_file_id", table_name="knowledge_index_jobs")
        op.drop_table("knowledge_index_jobs")
    if _has_table("knowledge_index_states"):
        op.drop_index("ix_knowledge_index_states_status", table_name="knowledge_index_states")
        op.drop_index("ix_knowledge_index_states_index_version", table_name="knowledge_index_states")
        op.drop_index("ix_knowledge_index_states_user_id", table_name="knowledge_index_states")
        op.drop_index("ux_knowledge_index_states_file_id", table_name="knowledge_index_states")
        op.drop_table("knowledge_index_states")
