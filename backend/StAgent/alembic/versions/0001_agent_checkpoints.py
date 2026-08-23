"""create agent_checkpoints table

Revision ID: 0001_agent_checkpoints
Revises:
Create Date: 2026-06-09

Durable agent-run checkpoints (Codex RolloutRecorder / Claude SessionStore
analogue) backing :class:`StAgent.sdk.kernel.checkpoint.SQLCheckpointStore`.
For the zero-config SQLite default this table is also created by
``DatabaseService.create_tables`` (``SQLModel.metadata.create_all``); this
revision exists for PostgreSQL / explicit-migration deployments.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy import inspect

revision: str = "0001_agent_checkpoints"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(i["name"] == index_name for i in inspect(op.get_bind()).get_indexes(table_name))


def upgrade() -> None:
    # Idempotent against SQLModel.metadata.create_all (the schema owner): on a
    # create_all-initialised DB (SQLite default and the app's PostgreSQL path)
    # this table already exists, so guard each op like migrations 0003+.
    if not _has_table("agent_checkpoints"):
        op.create_table(
            "agent_checkpoints",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("checkpoint_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("agent_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("turn", sa.Integer(), nullable=False),
            sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index("agent_checkpoints", op.f("ix_agent_checkpoints_checkpoint_id")):
        op.create_index(
            op.f("ix_agent_checkpoints_checkpoint_id"),
            "agent_checkpoints",
            ["checkpoint_id"],
            unique=True,
        )
    if not _has_index("agent_checkpoints", op.f("ix_agent_checkpoints_session_id")):
        op.create_index(
            op.f("ix_agent_checkpoints_session_id"),
            "agent_checkpoints",
            ["session_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_agent_checkpoints_session_id"),
        table_name="agent_checkpoints",
    )
    op.drop_index(
        op.f("ix_agent_checkpoints_checkpoint_id"),
        table_name="agent_checkpoints",
    )
    op.drop_table("agent_checkpoints")
