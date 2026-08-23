"""Persist string enum values instead of enum names.

Revision ID: 0016_enum_values
Revises: 0015_files_summary
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Connection, inspect
from sqlalchemy.dialects import postgresql

revision: str = "0016_enum_values"
down_revision: str | None = "0015_files_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("coding_projects", "runtime_kind", "codingprojectruntimekind"),
    ("coding_projects", "status", "codingprojectstatus"),
    ("files", "file_type", "filetype"),
    ("files", "status", "filestatus"),
    ("files", "library_scope", "libraryscope"),
    ("files", "inbox_state", "inboxstate"),
    ("flows", "status", "flowstatus"),
    ("flows", "flow_type", "flowtype"),
    ("messages", "role", "messagerole"),
    ("messages", "status", "messagestatus"),
    ("tasks", "task_type", "tasktype"),
    ("tasks", "status", "taskstatus"),
    ("tasks", "priority", "taskpriority"),
)
_POSTGRES_ENUM_TYPES = tuple(sorted({enum_type for _, _, enum_type in _ENUM_COLUMNS}))


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _has_table(bind: Connection, table_name: str) -> bool:
    return inspect(bind).has_table(table_name)


def _has_column(bind: Connection, table_name: str, column_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        column["name"] == column_name
        for column in inspect(bind).get_columns(table_name)
    )


def _alter_postgresql_enum_columns_to_varchar(bind: Connection) -> None:
    if bind.dialect.name != "postgresql":
        return

    for table_name, column_name, enum_type in _ENUM_COLUMNS:
        if not _has_column(bind, table_name, column_name):
            continue
        op.alter_column(
            table_name,
            column_name,
            existing_type=postgresql.ENUM(name=enum_type),
            type_=sa.String(length=64),
            postgresql_using=f"{_quote_ident(column_name)}::text",
        )


def _normalize_enum_value_columns(bind: Connection) -> None:
    for table_name, column_name, _enum_type in _ENUM_COLUMNS:
        if not _has_column(bind, table_name, column_name):
            continue
        table = _quote_ident(table_name)
        column = _quote_ident(column_name)
        bind.execute(
            sa.text(
                f"UPDATE {table} "
                f"SET {column} = lower(CAST({column} AS TEXT)) "
                f"WHERE {column} IS NOT NULL"
            )
        )


def _uppercase_enum_value_columns(bind: Connection) -> None:
    for table_name, column_name, _enum_type in _ENUM_COLUMNS:
        if not _has_column(bind, table_name, column_name):
            continue
        table = _quote_ident(table_name)
        column = _quote_ident(column_name)
        bind.execute(
            sa.text(
                f"UPDATE {table} "
                f"SET {column} = upper(CAST({column} AS TEXT)) "
                f"WHERE {column} IS NOT NULL"
            )
        )


def _drop_postgresql_enum_types(bind: Connection) -> None:
    if bind.dialect.name != "postgresql":
        return

    for enum_type in _POSTGRES_ENUM_TYPES:
        bind.execute(sa.text(f"DROP TYPE IF EXISTS {_quote_ident(enum_type)}"))


def upgrade() -> None:
    bind = op.get_bind()
    _alter_postgresql_enum_columns_to_varchar(bind)
    _normalize_enum_value_columns(bind)
    _drop_postgresql_enum_types(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _uppercase_enum_value_columns(bind)
