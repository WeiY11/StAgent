"""Per-user settings store (key/value, JSON-encoded).

Small scoped store for user-level preferences that don't warrant dedicated
columns. Keys are namespaced dotted strings (e.g. ``cost_monthly_budget_usd``)
and values are stored as JSON ``Text`` so numbers, strings and structured
payloads share one schema.
"""

from __future__ import annotations

from sqlalchemy import Column, Text
from sqlmodel import Field, UniqueConstraint

from StAgent.db.models.base import BaseModel


class UserSetting(BaseModel, table=True):
    """One settings entry per (user, key)."""

    __tablename__ = "user_settings"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_settings_user_key"),)

    user_id: str = Field(index=True, max_length=100)
    key: str = Field(index=True, max_length=200)
    value: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


__all__ = ["UserSetting"]
