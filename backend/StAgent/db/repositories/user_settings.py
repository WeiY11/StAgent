"""User settings repository (per-user key/value store).

Modeled on the other ``Db*`` repositories: a ``Protocol`` plus a
``DatabaseService``-backed implementation. ``user_id`` is compared as a
string (``user_settings.user_id`` is a plain string column, matching
``agent_traces``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlmodel import col, select

from StAgent.db.models.user_setting import UserSetting

if TYPE_CHECKING:
    from uuid import UUID

    from StAgent.db.service import DatabaseService


class UserSettingRepository(Protocol):
    """Protocol for the per-user settings store."""

    async def get(self, user_id: UUID, key: str) -> str | None:
        """Return the stored raw value for *key*, or ``None``."""
        ...

    async def set(self, user_id: UUID, key: str, value: str | None) -> None:
        """Upsert (or delete, when *value* is ``None``) the entry for *key*."""
        ...


class DbUserSettingRepository:
    """``DatabaseService``-backed :class:`UserSettingRepository`."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    async def get(self, user_id: UUID, key: str) -> str | None:
        async with self._db.session() as session:
            stmt = select(UserSetting).where(
                UserSetting.user_id == str(user_id),
                col(UserSetting.key) == key,
            )
            result = await session.exec(stmt)
            row = result.first()
            return row.value if row is not None else None

    async def set(self, user_id: UUID, key: str, value: str | None) -> None:
        async with self._db.session() as session:
            stmt = select(UserSetting).where(
                UserSetting.user_id == str(user_id),
                col(UserSetting.key) == key,
            )
            result = await session.exec(stmt)
            row = result.first()
            if value is None:
                if row is not None:
                    await session.delete(row)
                return
            if row is None:
                row = UserSetting(user_id=str(user_id), key=key, value=value)
            else:
                row.value = value
                row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(row)
            await session.flush()


__all__ = ["UserSettingRepository", "DbUserSettingRepository"]
