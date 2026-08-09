"""Central knowledge-document authorization predicates.

PostgreSQL/SQLModel callers use :func:`knowledge_file_access_clause`; the
SQLite FTS paths use the equivalent parameterized SQL fragment so ACLs are
applied before ranking rather than after a candidate limit.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4  # noqa: TC003 - public helper annotations are runtime-facing

import sqlalchemy as sa
from sqlalchemy import and_, exists, or_
from sqlmodel import select

from leagent.db.models.file import File, KnowledgeAccessScope
from leagent.db.models.knowledge_acl import (
    KnowledgeDocumentGrant,
    KnowledgeSpace,
    KnowledgeSpaceMember,
    KnowledgeSpaceMemberRole,
    WorkspaceMember,
    WorkspaceMemberRole,
)
from leagent.db.sqlite_compat import (
    session_dialect_name,
    sqlite_parent_id_text,
    uuid_column_equals,
    uuid_columns_equal,
)

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession


def knowledge_file_access_clause(session: AsyncSession, user_id: UUID | None) -> sa.ColumnElement[bool]:
    """Return the SQL predicate for a user's readable knowledge documents."""
    if user_id is None:
        return sa.true()

    owner = uuid_column_equals(session, File.user_id, user_id)
    org_member = exists(
        sa.select(1)
        .select_from(WorkspaceMember)
        .where(
            uuid_columns_equal(session, WorkspaceMember.workspace_id, File.workspace_id),
            uuid_column_equals(session, WorkspaceMember.user_id, user_id),
        )
    )
    space_member = exists(
        sa.select(1)
        .select_from(KnowledgeSpaceMember)
        .join(
            KnowledgeSpace,
            uuid_columns_equal(
                session,
                KnowledgeSpace.id,
                KnowledgeSpaceMember.space_id,
            ),
        )
        .where(
            uuid_columns_equal(session, KnowledgeSpaceMember.space_id, File.knowledge_space_id),
            uuid_column_equals(session, KnowledgeSpaceMember.user_id, user_id),
            KnowledgeSpace.is_deleted == False,  # noqa: E712
            uuid_columns_equal(session, KnowledgeSpace.workspace_id, File.workspace_id),
        )
    )
    grant = exists(
        sa.select(1)
        .select_from(KnowledgeDocumentGrant)
        .where(
            uuid_columns_equal(session, KnowledgeDocumentGrant.file_id, File.id),
            uuid_column_equals(session, KnowledgeDocumentGrant.user_id, user_id),
            KnowledgeDocumentGrant.permission.in_(["read", "write", "admin"]),
        )
    )

    scope = File.access_scope
    return or_(
        owner,
        and_(
            scope == KnowledgeAccessScope.SPACE,
            org_member,
            space_member,
        ),
        and_(
            scope == KnowledgeAccessScope.RESTRICTED,
            grant,
        ),
    )


def knowledge_file_write_clause(session: AsyncSession, user_id: UUID | None) -> sa.ColumnElement[bool]:
    """Return the predicate for users allowed to mutate/index a document."""
    if user_id is None:
        return sa.false()

    owner = uuid_column_equals(session, File.user_id, user_id)
    org_member = exists(
        sa.select(1)
        .select_from(WorkspaceMember)
        .where(
            uuid_columns_equal(session, WorkspaceMember.workspace_id, File.workspace_id),
            uuid_column_equals(session, WorkspaceMember.user_id, user_id),
            WorkspaceMember.role.in_([WorkspaceMemberRole.OWNER, WorkspaceMemberRole.ADMIN]),
        )
    )
    space_editor = exists(
        sa.select(1)
        .select_from(KnowledgeSpaceMember)
        .join(
            KnowledgeSpace,
            uuid_columns_equal(session, KnowledgeSpace.id, KnowledgeSpaceMember.space_id),
        )
        .join(
            WorkspaceMember,
            uuid_columns_equal(session, WorkspaceMember.workspace_id, KnowledgeSpace.workspace_id),
        )
        .where(
            uuid_columns_equal(session, KnowledgeSpaceMember.space_id, File.knowledge_space_id),
            uuid_column_equals(session, KnowledgeSpaceMember.user_id, user_id),
            KnowledgeSpaceMember.role.in_([
                KnowledgeSpaceMemberRole.MANAGER,
                KnowledgeSpaceMemberRole.EDITOR,
            ]),
            uuid_column_equals(session, WorkspaceMember.user_id, user_id),
            WorkspaceMember.role.in_([
                WorkspaceMemberRole.OWNER,
                WorkspaceMemberRole.ADMIN,
                WorkspaceMemberRole.MEMBER,
            ]),
            KnowledgeSpace.is_deleted == False,  # noqa: E712
        )
    )
    grant = exists(
        sa.select(1)
        .select_from(KnowledgeDocumentGrant)
        .where(
            uuid_columns_equal(session, KnowledgeDocumentGrant.file_id, File.id),
            uuid_column_equals(session, KnowledgeDocumentGrant.user_id, user_id),
            KnowledgeDocumentGrant.permission.in_(["write", "admin"]),
        )
    )
    return or_(owner, and_(org_member, File.workspace_id.is_not(None)), space_editor, grant)


SQLITE_KNOWLEDGE_ACCESS_SQL = """
(
    :user_id_dashed IS NULL
    OR lower(replace(CAST(f.user_id AS TEXT), '-', '')) = lower(:user_id_hex)
    OR (
        (f.access_scope = 'space' OR f.access_scope = 'SPACE')
        AND EXISTS (
            SELECT 1
            FROM knowledge_space_members ksm
            JOIN knowledge_spaces ks
              ON lower(replace(CAST(ks.id AS TEXT), '-', ''))
               = lower(replace(CAST(f.knowledge_space_id AS TEXT), '-', ''))
            JOIN workspace_members wm
              ON lower(replace(CAST(wm.workspace_id AS TEXT), '-', ''))
               = lower(replace(CAST(ks.workspace_id AS TEXT), '-', ''))
            WHERE lower(replace(CAST(ksm.space_id AS TEXT), '-', ''))
                    = lower(replace(CAST(f.knowledge_space_id AS TEXT), '-', ''))
              AND lower(replace(CAST(ksm.user_id AS TEXT), '-', '')) = lower(:user_id_hex)
              AND lower(replace(CAST(wm.user_id AS TEXT), '-', '')) = lower(:user_id_hex)
              AND ks.is_deleted = 0
        )
    )
    OR (
        (f.access_scope = 'restricted' OR f.access_scope = 'RESTRICTED')
        AND EXISTS (
            SELECT 1
            FROM knowledge_document_grants kdg
            WHERE lower(replace(CAST(kdg.file_id AS TEXT), '-', ''))
                    = lower(replace(CAST(f.id AS TEXT), '-', ''))
              AND lower(replace(CAST(kdg.user_id AS TEXT), '-', '')) = lower(:user_id_hex)
              AND kdg.permission IN ('read', 'write', 'admin')
        )
    )
)
"""


def sqlite_access_params(user_id: UUID | None) -> dict[str, str | None]:
    """Return bind parameters consumed by :data:`SQLITE_KNOWLEDGE_ACCESS_SQL`."""
    dashed = str(user_id) if user_id is not None else None
    return {
        "user_id_dashed": dashed,
        "user_id_hex": dashed.replace("-", "") if dashed else None,
    }


async def ensure_personal_knowledge_space(db: object, user_id: UUID) -> UUID:
    """Ensure the user's organization, membership, and private default space.

    Existing deployments have a workspace identity stub but no ACL rows.  The
    first knowledge write upgrades that identity lazily and returns a stable
    default space, so old upload APIs do not need a breaking migration step.
    """
    from leagent.db.models.identity_stub import WorkspaceStub

    async with db.session() as session:  # type: ignore[attr-defined]
        sqlite = session_dialect_name(session) == "sqlite"
        workspace = (
            await session.exec(
                select(WorkspaceStub).where(uuid_column_equals(session, WorkspaceStub.id, user_id))
            )
        ).first()
        if workspace is None:
            if sqlite:
                await session.execute(
                    sa.text("INSERT INTO workspaces (id) VALUES (:id)"),
                    {"id": str(user_id)},
                )
            else:
                workspace = WorkspaceStub(id=user_id)
                session.add(workspace)
                await session.flush()
        workspace_db_id = (
            await sqlite_parent_id_text(session, "workspaces", user_id)
            if sqlite
            else str(user_id)
        )
        user_db_id = (
            await sqlite_parent_id_text(session, "users", user_id)
            if sqlite
            else str(user_id)
        )

        member = (
            await session.exec(
                select(WorkspaceMember).where(
                    uuid_column_equals(session, WorkspaceMember.workspace_id, user_id),
                    uuid_column_equals(session, WorkspaceMember.user_id, user_id),
                )
            )
        ).first()
        if member is None:
            if sqlite:
                now = datetime.utcnow()
                await session.execute(
                    sa.text(
                        "INSERT INTO workspace_members "
                        "(created_at, updated_at, id, workspace_id, user_id, role) "
                        "VALUES (:created_at, :updated_at, :id, :workspace_id, :user_id, :role)"
                    ),
                    {
                        "created_at": now,
                        "updated_at": now,
                        "id": str(uuid4()),
                        "workspace_id": workspace_db_id,
                        "user_id": user_db_id,
                        "role": WorkspaceMemberRole.OWNER.value,
                    },
                )
            else:
                session.add(
                    WorkspaceMember(
                        workspace_id=user_id,
                        user_id=user_id,
                        role=WorkspaceMemberRole.OWNER,
                    )
                )
                await session.flush()

        space = (
            await session.exec(
                select(KnowledgeSpace).where(
                    uuid_column_equals(session, KnowledgeSpace.workspace_id, user_id),
                    KnowledgeSpace.slug == "personal",
                    KnowledgeSpace.is_deleted == False,  # noqa: E712
                )
            )
        ).first()
        if space is None:
            if sqlite:
                space_id = uuid4()
                now = datetime.utcnow()
                await session.execute(
                    sa.text(
                        "INSERT INTO knowledge_spaces "
                        "(created_at, updated_at, id, workspace_id, name, slug, "
                        "default_access_scope, is_default, is_deleted) VALUES "
                        "(:created_at, :updated_at, :id, :workspace_id, :name, :slug, "
                        ":default_access_scope, :is_default, :is_deleted)"
                    ),
                    {
                        "created_at": now,
                        "updated_at": now,
                        "id": str(space_id),
                        "workspace_id": workspace_db_id,
                        "name": "Personal",
                        "slug": "personal",
                        "default_access_scope": KnowledgeAccessScope.PRIVATE.value,
                        "is_default": 1,
                        "is_deleted": 0,
                    },
                )
                space = (
                    await session.exec(
                        select(KnowledgeSpace).where(
                            uuid_column_equals(session, KnowledgeSpace.id, space_id)
                        )
                    )
                ).first()
            else:
                space = KnowledgeSpace(
                    workspace_id=user_id,
                    name="Personal",
                    slug="personal",
                    default_access_scope=KnowledgeAccessScope.PRIVATE,
                    is_default=True,
                )
                session.add(space)
                await session.flush()
        if space is None:
            raise RuntimeError("personal knowledge space could not be created")

        space_member = (
            await session.exec(
                select(KnowledgeSpaceMember).where(
                    uuid_column_equals(session, KnowledgeSpaceMember.space_id, space.id),
                    uuid_column_equals(session, KnowledgeSpaceMember.user_id, user_id),
                )
            )
        ).first()
        if space_member is None:
            if sqlite:
                space_db_id = await sqlite_parent_id_text(session, "knowledge_spaces", space.id)
                now = datetime.utcnow()
                await session.execute(
                    sa.text(
                        "INSERT INTO knowledge_space_members "
                        "(created_at, updated_at, id, space_id, user_id, role) "
                        "VALUES (:created_at, :updated_at, :id, :space_id, :user_id, :role)"
                    ),
                    {
                        "created_at": now,
                        "updated_at": now,
                        "id": str(uuid4()),
                        "space_id": space_db_id,
                        "user_id": user_db_id,
                        "role": KnowledgeSpaceMemberRole.MANAGER.value,
                    },
                )
            else:
                session.add(
                    KnowledgeSpaceMember(
                        space_id=space.id,
                        user_id=user_id,
                        role=KnowledgeSpaceMemberRole.MANAGER,
                    )
                )
        return space.id


__all__ = [
    "SQLITE_KNOWLEDGE_ACCESS_SQL",
    "knowledge_file_access_clause",
    "knowledge_file_write_clause",
    "ensure_personal_knowledge_space",
    "sqlite_access_params",
]
