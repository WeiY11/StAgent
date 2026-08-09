"""Add organization, knowledge-space, and document ACL tables.

Revision ID: 0022_knowledge_acl
Revises: 0021_knowledge_index_contract
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0022_knowledge_acl"
down_revision: str | None = "0021_knowledge_index_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return _has_table(table) and any(
        item["name"] == column for item in inspect(op.get_bind()).get_columns(table)
    )


def upgrade() -> None:
    # Standalone Alembic can be invoked before SQLModel bootstrap on a brand
    # new database.  The application creates ``files``/identity tables before
    # running migrations; leave this revision as a no-op for that empty case.
    if not _has_table("files"):
        return
    if not _has_column("files", "knowledge_space_id"):
        op.add_column("files", sa.Column("knowledge_space_id", sa.Uuid(), nullable=True))
        op.create_index("ix_files_knowledge_space_id", "files", ["knowledge_space_id"])
    if not _has_column("files", "access_scope"):
        op.add_column(
            "files",
            sa.Column("access_scope", sa.String(length=20), nullable=False, server_default="private"),
        )
        op.create_index("ix_files_access_scope", "files", ["access_scope"])

    if not _has_table("workspace_members"):
        op.create_table(
            "workspace_members",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("role", sa.String(length=20), nullable=False, server_default="member"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_workspace_members_workspace_id", "workspace_members", ["workspace_id"])
        op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])
        op.create_index(
            "ux_workspace_members_workspace_user",
            "workspace_members",
            ["workspace_id", "user_id"],
            unique=True,
        )

    if not _has_table("knowledge_spaces"):
        op.create_table(
            "knowledge_spaces",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("slug", sa.String(length=160), nullable=False),
            sa.Column("description", sa.String(length=2000), nullable=True),
            sa.Column("default_access_scope", sa.String(length=20), nullable=False, server_default="space"),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_knowledge_spaces_workspace_id", "knowledge_spaces", ["workspace_id"])
        op.create_index("ix_knowledge_spaces_slug", "knowledge_spaces", ["slug"])
        op.create_index("ix_knowledge_spaces_default_access_scope", "knowledge_spaces", ["default_access_scope"])
        op.create_index("ix_knowledge_spaces_is_default", "knowledge_spaces", ["is_default"])
        op.create_index("ix_knowledge_spaces_is_deleted", "knowledge_spaces", ["is_deleted"])
        op.create_index(
            "ux_knowledge_spaces_workspace_slug",
            "knowledge_spaces",
            ["workspace_id", "slug"],
            unique=True,
        )

    if not _has_table("knowledge_space_members"):
        op.create_table(
            "knowledge_space_members",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("space_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("role", sa.String(length=20), nullable=False, server_default="viewer"),
            sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_knowledge_space_members_space_id", "knowledge_space_members", ["space_id"])
        op.create_index("ix_knowledge_space_members_user_id", "knowledge_space_members", ["user_id"])
        op.create_index(
            "ux_knowledge_space_members_space_user",
            "knowledge_space_members",
            ["space_id", "user_id"],
            unique=True,
        )

    if not _has_table("knowledge_document_grants"):
        op.create_table(
            "knowledge_document_grants",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("file_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("permission", sa.String(length=20), nullable=False, server_default="read"),
            sa.ForeignKeyConstraint(["file_id"], ["files.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_knowledge_document_grants_file_id", "knowledge_document_grants", ["file_id"])
        op.create_index("ix_knowledge_document_grants_user_id", "knowledge_document_grants", ["user_id"])
        op.create_index("ix_knowledge_document_grants_permission", "knowledge_document_grants", ["permission"])
        op.create_index(
            "ux_knowledge_document_grants_file_user",
            "knowledge_document_grants",
            ["file_id", "user_id"],
            unique=True,
        )


def downgrade() -> None:
    if _has_table("knowledge_document_grants"):
        op.drop_index("ux_knowledge_document_grants_file_user", table_name="knowledge_document_grants")
        op.drop_index("ix_knowledge_document_grants_permission", table_name="knowledge_document_grants")
        op.drop_index("ix_knowledge_document_grants_user_id", table_name="knowledge_document_grants")
        op.drop_index("ix_knowledge_document_grants_file_id", table_name="knowledge_document_grants")
        op.drop_table("knowledge_document_grants")
    if _has_table("knowledge_space_members"):
        op.drop_index("ux_knowledge_space_members_space_user", table_name="knowledge_space_members")
        op.drop_index("ix_knowledge_space_members_user_id", table_name="knowledge_space_members")
        op.drop_index("ix_knowledge_space_members_space_id", table_name="knowledge_space_members")
        op.drop_table("knowledge_space_members")
    if _has_table("knowledge_spaces"):
        op.drop_index("ux_knowledge_spaces_workspace_slug", table_name="knowledge_spaces")
        op.drop_index("ix_knowledge_spaces_is_deleted", table_name="knowledge_spaces")
        op.drop_index("ix_knowledge_spaces_is_default", table_name="knowledge_spaces")
        op.drop_index("ix_knowledge_spaces_default_access_scope", table_name="knowledge_spaces")
        op.drop_index("ix_knowledge_spaces_slug", table_name="knowledge_spaces")
        op.drop_index("ix_knowledge_spaces_workspace_id", table_name="knowledge_spaces")
        op.drop_table("knowledge_spaces")
    if _has_table("workspace_members"):
        op.drop_index("ux_workspace_members_workspace_user", table_name="workspace_members")
        op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
        op.drop_index("ix_workspace_members_workspace_id", table_name="workspace_members")
        op.drop_table("workspace_members")
    if _has_column("files", "access_scope"):
        op.drop_index("ix_files_access_scope", table_name="files")
        op.drop_column("files", "access_scope")
    if _has_column("files", "knowledge_space_id"):
        op.drop_index("ix_files_knowledge_space_id", table_name="files")
        op.drop_column("files", "knowledge_space_id")
