"""create workflow_state_snapshots table

Revision ID: 0002_workflow_state_snapshots
Revises: 0001_agent_checkpoints
Create Date: 2026-06-12

Durable workflow run snapshots backing :class:`leagent.workflow.state_store.SQLWorkflowStateStore`.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy import inspect

revision: str = "0002_workflow_state_snapshots"
down_revision: str | None = "0001_agent_checkpoints"
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
    if not _has_table("workflow_state_snapshots"):
        op.create_table(
            "workflow_state_snapshots",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("state_id", sa.Uuid(), nullable=False),
            sa.Column("execution_id", sa.Uuid(), nullable=True),
            sa.Column("prompt_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index("workflow_state_snapshots", op.f("ix_workflow_state_snapshots_state_id")):
        op.create_index(
            op.f("ix_workflow_state_snapshots_state_id"),
            "workflow_state_snapshots",
            ["state_id"],
            unique=True,
        )
    if not _has_index("workflow_state_snapshots", op.f("ix_workflow_state_snapshots_execution_id")):
        op.create_index(
            op.f("ix_workflow_state_snapshots_execution_id"),
            "workflow_state_snapshots",
            ["execution_id"],
            unique=False,
        )
    if not _has_index("workflow_state_snapshots", op.f("ix_workflow_state_snapshots_prompt_id")):
        op.create_index(
            op.f("ix_workflow_state_snapshots_prompt_id"),
            "workflow_state_snapshots",
            ["prompt_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_workflow_state_snapshots_prompt_id"),
        table_name="workflow_state_snapshots",
    )
    op.drop_index(
        op.f("ix_workflow_state_snapshots_execution_id"),
        table_name="workflow_state_snapshots",
    )
    op.drop_index(
        op.f("ix_workflow_state_snapshots_state_id"),
        table_name="workflow_state_snapshots",
    )
    op.drop_table("workflow_state_snapshots")
