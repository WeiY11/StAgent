"""Database settings sub-module."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_", populate_by_name=True)

    driver: str = "sqlite+aiosqlite"
    sqlite_path: str = ""
    echo: bool = False
    database_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "database_url",
            "DB_DATABASE_URL",
            "DATABASE_URL",
        ),
    )
    pool_size: int = 5
    max_overflow: int = 10
    fail_fast_migrations: bool = False

    @property
    def backend(self) -> Literal["sqlite", "postgresql", "other"]:
        """Normalized database backend derived from url/driver scheme."""
        raw = (self.database_url or self.driver or "").strip().lower()
        scheme = raw.split(":", 1)[0].split("+", 1)[0]
        if scheme == "sqlite":
            return "sqlite"
        if scheme in {"postgres", "postgresql"}:
            return "postgresql"
        return "other"

    @property
    def is_sqlite(self) -> bool:
        return self.backend == "sqlite"

    @property
    def is_postgresql(self) -> bool:
        return self.backend == "postgresql"

    @model_validator(mode="after")
    def validate_database_target(self) -> DatabaseSettings:
        if not self.database_url and not self.driver.lower().startswith("sqlite"):
            raise ValueError("database_url is required for non-SQLite drivers")
        return self

    def _sqlite_file_path(self) -> str:
        from leagent.config.constants import LEAGENT_HOME

        raw = (self.sqlite_path or "").strip()
        if raw:
            return str(Path(raw).expanduser().resolve())
        return str((LEAGENT_HOME / "leagent.db").resolve())

    def _resolved_sqlite_path(self) -> Path:
        """Absolute SQLite file path with parent dirs created."""
        p = Path(self._sqlite_file_path())
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def url(self) -> str:
        if self.database_url:
            return self.database_url
        path = self._resolved_sqlite_path().as_posix()
        return f"{self.driver}:///{path}"

    @property
    def sync_url(self) -> str:
        if self.database_url:
            return (
                self.database_url.replace("+asyncpg", "")
                .replace("+aiopg", "")
                .replace("+aiosqlite", "")
            )
        path = self._resolved_sqlite_path().as_posix()
        return f"sqlite:///{path}"
