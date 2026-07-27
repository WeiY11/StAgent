"""Database enum column contracts."""

from __future__ import annotations

from enum import Enum
from importlib import import_module

import pytest
from sqlalchemy import Column, MetaData, String, Table, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects import postgresql
from sqlmodel import SQLModel, Session, create_engine

import leagent.db.models  # noqa: F401
from leagent.db.models.coding_project import (
    CodingProject,
    CodingProjectRuntimeKind,
    CodingProjectStatus,
)
from leagent.db.models.file import File, FileStatus, FileType, InboxState, LibraryScope
from leagent.db.models.flow import Flow, FlowStatus, FlowType
from leagent.db.models.message import Message, MessageRole, MessageStatus
from leagent.db.models.task import Task, TaskPriority, TaskStatus, TaskType


@pytest.mark.parametrize(
    ("model", "column_name", "enum_cls"),
    [
        (CodingProject, "runtime_kind", CodingProjectRuntimeKind),
        (CodingProject, "status", CodingProjectStatus),
        (File, "file_type", FileType),
        (File, "status", FileStatus),
        (File, "library_scope", LibraryScope),
        (File, "inbox_state", InboxState),
        (Flow, "status", FlowStatus),
        (Flow, "flow_type", FlowType),
        (Message, "role", MessageRole),
        (Message, "status", MessageStatus),
        (Task, "task_type", TaskType),
        (Task, "status", TaskStatus),
        (Task, "priority", TaskPriority),
    ],
)
def test_sqlmodel_enum_columns_store_values_as_non_native_varchar(
    model: type,
    column_name: str,
    enum_cls: type[Enum],
) -> None:
    column_type = model.__table__.c[column_name].type

    assert isinstance(column_type, SAEnum)
    assert column_type.enums == [str(member.value) for member in enum_cls]
    assert column_type.native_enum is False
    assert column_type.validate_strings is True
    assert column_type.compile(dialect=postgresql.dialect()).startswith("VARCHAR")


def test_no_native_enum_columns_registered_in_metadata() -> None:
    native_columns: list[str] = []

    for table in SQLModel.metadata.sorted_tables:
        for column in table.c:
            column_type = column.type
            if isinstance(column_type, SAEnum) and column_type.native_enum:
                native_columns.append(f"{table.name}.{column.name}")

    assert native_columns == []


def test_file_enum_values_are_stored_as_lowercase_values_in_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        row = File(
            name="doc.pdf",
            original_name="doc.pdf",
            file_type=FileType.DOCUMENT,
            status=FileStatus.PROCESSED,
            storage_path="/tmp/doc.pdf",
            library_scope=LibraryScope.KNOWLEDGE,
            inbox_state=InboxState.TRIAGED,
        )
        session.add(row)
        session.commit()

    with engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT file_type, status, library_scope, inbox_state "
                "FROM files LIMIT 1"
            )
        ).one()

    assert tuple(stored) == ("document", "processed", "knowledge", "triaged")


def test_enum_value_migration_normalizes_legacy_uppercase_sqlite_rows() -> None:
    migration = import_module("leagent.alembic.versions.0016_enum_values")
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    files = Table(
        "files",
        metadata,
        Column("file_type", String(64)),
        Column("status", String(64)),
        Column("library_scope", String(64)),
        Column("inbox_state", String(64)),
    )
    messages = Table(
        "messages",
        metadata,
        Column("role", String(64)),
        Column("status", String(64)),
    )
    tasks = Table(
        "tasks",
        metadata,
        Column("task_type", String(64)),
        Column("status", String(64)),
        Column("priority", String(64)),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            files.insert().values(
                file_type="DOCUMENT",
                status="PROCESSED",
                library_scope="KNOWLEDGE",
                inbox_state="TRIAGED",
            )
        )
        connection.execute(
            messages.insert().values(role="ASSISTANT", status="COMPLETED")
        )
        connection.execute(
            tasks.insert().values(
                task_type="WORKFLOW",
                status="RUNNING",
                priority="HIGH",
            )
        )

        migration._normalize_enum_value_columns(connection)

        file_row = connection.execute(
            text(
                "SELECT file_type, status, library_scope, inbox_state "
                "FROM files LIMIT 1"
            )
        ).one()
        message_row = connection.execute(
            text("SELECT role, status FROM messages LIMIT 1")
        ).one()
        task_row = connection.execute(
            text("SELECT task_type, status, priority FROM tasks LIMIT 1")
        ).one()

    assert tuple(file_row) == ("document", "processed", "knowledge", "triaged")
    assert tuple(message_row) == ("assistant", "completed")
    assert tuple(task_row) == ("workflow", "running", "high")
