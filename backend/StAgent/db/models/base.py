"""Base SQLModel classes with common fields."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Enum as SAEnum
from sqlmodel import Column, Field, SQLModel

if TYPE_CHECKING:
    from enum import Enum as PyEnum


def _naive_utc_now() -> datetime:
    """UTC "now" as a naive datetime, matching PostgreSQL ``timestamp`` columns.

    Timezone-aware values on ORM inserts against ``DateTime()`` (no tz) break
    asyncpg encoding; see :func:`naive_utc_for_db_column` and
    ``session.store``'s chat timestamp normalization for the symmetric path.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def naive_utc_for_db_column(dt: datetime | None) -> datetime | None:
    """Coerce a datetime for SQL ``DateTime()`` columns without time zone.

    PostgreSQL ``timestamp without time zone`` and asyncpg expect naive
    parameters. If *dt* is timezone-aware, convert to UTC then strip
    ``tzinfo``. Naive values are returned unchanged (callers treat them as
    UTC wall time matching stored rows).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def enum_value_column(
    enum_cls: type[PyEnum],
    *,
    name: str | None = None,
    nullable: bool = False,
    index: bool = False,
) -> Column[Any]:
    """SQLAlchemy column that persists a string Enum's values, not names."""
    return Column(
        SAEnum(
            enum_cls,
            name=name or enum_cls.__name__.lower(),
            values_callable=lambda members: [str(member.value) for member in members],
            native_enum=False,
            validate_strings=True,
            create_constraint=False,
        ),
        nullable=nullable,
        index=index,
    )


class TimestampMixin(SQLModel):
    """Mixin providing created_at and updated_at timestamps."""

    created_at: datetime = Field(default_factory=_naive_utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=_naive_utc_now, nullable=False)


class UUIDMixin(SQLModel):
    """Mixin providing UUID primary key."""

    id: UUID = Field(default_factory=uuid4, primary_key=True)


class TenantMixin(SQLModel):
    """Optional multi-tenant scoping.

    ``workspace_id`` is intentionally nullable + indexed: existing rows carry
    ``NULL`` and are implicitly owned by the user's personal workspace; new
    rows populate it from the ``TenantContext`` contextvar. All repository
    queries that extend the tenant-scoped repository pattern
    filter on this column automatically.
    """

    workspace_id: UUID | None = Field(default=None, nullable=True, index=True)


class BaseModel(UUIDMixin, TimestampMixin, SQLModel):
    """Base model with UUID and timestamps."""



class TenantBaseModel(UUIDMixin, TimestampMixin, TenantMixin, SQLModel):
    """Base model for tenant-scoped entities."""



class SoftDeleteMixin(SQLModel):
    """Mixin for soft delete functionality."""

    is_deleted: bool = Field(default=False, nullable=False)
    deleted_at: datetime | None = Field(default=None, nullable=True)


def utc_now() -> datetime:
    """Return current UTC time as a naive datetime (convention: UTC wall time)."""
    return _naive_utc_now()
