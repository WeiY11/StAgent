"""Add mission tables for goal-scoped autonomous execution.

Revision ID: 0023_agent_missions
Revises: 0022_knowledge_acl
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0023_agent_missions"
down_revision: str | None = "0022_knowledge_acl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    # The app bootstraps identity tables before running migrations; on a
    # brand-new empty database ``create_all`` covers these tables, so this
    # revision is a no-op for that case (mirrors 0022_knowledge_acl).
    if not _has_table("users"):
        return

    if not _has_table("agent_missions"):
        op.create_table(
            "agent_missions",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=True),
            sa.Column("session_id", sa.Uuid(), nullable=True),
            sa.Column("goal", sa.String(length=4000), nullable=False),
            sa.Column("agent_id", sa.String(length=100), nullable=True),
            sa.Column(
                "status",
                sa.String(length=64),
                nullable=False,
                server_default="planning",
            ),
            sa.Column(
                "autonomy",
                sa.String(length=64),
                nullable=False,
                server_default="full",
            ),
            sa.Column(
                "verify_level",
                sa.String(length=64),
                nullable=False,
                server_default="heuristic",
            ),
            sa.Column("expected_output", sa.Text(), nullable=True),
            sa.Column("plan_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("budget_json", sa.Text(), nullable=True),
            sa.Column("usage_json", sa.Text(), nullable=True),
            sa.Column("pause_reason", sa.String(length=200), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_agent_missions_user_id", "agent_missions", ["user_id"])
        op.create_index("ix_agent_missions_session_id", "agent_missions", ["session_id"])
        op.create_index("ix_agent_missions_status", "agent_missions", ["status"])

    if not _has_table("agent_mission_steps"):
        op.create_table(
            "agent_mission_steps",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("mission_id", sa.Uuid(), nullable=False),
            sa.Column("step_no", sa.Integer(), nullable=False),
            sa.Column("plan_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("description", sa.String(length=4000), nullable=False),
            sa.Column(
                "kind",
                sa.String(length=64),
                nullable=False,
                server_default="generic",
            ),
            sa.Column("depends_on", sa.Text(), nullable=True),
            sa.Column("wave", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "status",
                sa.String(length=64),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("verify_result", sa.Text(), nullable=True),
            sa.Column("segment_summary", sa.Text(), nullable=True),
            sa.Column("checkpoint_id", sa.String(length=200), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["mission_id"], ["agent_missions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "mission_id",
                "plan_id",
                "step_no",
                name="ux_agent_mission_steps_plan_step",
            ),
        )
        op.create_index("ix_agent_mission_steps_mission_id", "agent_mission_steps", ["mission_id"])
        op.create_index("ix_agent_mission_steps_status", "agent_mission_steps", ["status"])


def downgrade() -> None:
    if _has_table("agent_mission_steps"):
        op.drop_index("ix_agent_mission_steps_status", table_name="agent_mission_steps")
        op.drop_index("ix_agent_mission_steps_mission_id", table_name="agent_mission_steps")
        op.drop_table("agent_mission_steps")
    if _has_table("agent_missions"):
        op.drop_index("ix_agent_missions_status", table_name="agent_missions")
        op.drop_index("ix_agent_missions_session_id", table_name="agent_missions")
        op.drop_index("ix_agent_missions_user_id", table_name="agent_missions")
        op.drop_table("agent_missions")
