"""Files settings sub-module."""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from leagent.config.constants import MAX_UPLOAD_SIZE_BYTES


def _default_upload_dir() -> str:
    from leagent.config.constants import UPLOAD_DIR

    return str(UPLOAD_DIR)


def _default_task_output_dir() -> str:
    from leagent.config.constants import TASK_OUTPUT_DIR

    return str(TASK_OUTPUT_DIR)


class FilesSettings(BaseSettings):
    """Uploaded file storage for chat session attachments."""

    model_config = SettingsConfigDict(env_prefix="FILES_")

    upload_dir: str = Field(default_factory=_default_upload_dir)
    tool_argument_blob_persist: bool = Field(
        default=False,
        description=(
            "When true, finalized tool_argument_blob payloads are written under "
            "upload_dir/tool-argument-blobs/ and survive process restarts until consumed."
        ),
    )
    knowledge_storage_dir: str = Field(
        default="",
        validation_alias=AliasChoices("LEAGENT_KNOWLEDGE_DIR", "FILES_KNOWLEDGE_STORAGE_DIR"),
    )
    max_upload_bytes: int = Field(default=MAX_UPLOAD_SIZE_BYTES)
    preview_ttl_seconds: int = 15 * 60
    signed_url_secret: str = ""
    projects_allowed_roots: str = ""

    def resolved_knowledge_storage_dir(self) -> str:
        from leagent.config.constants import KNOWLEDGE_DIR

        raw = (self.knowledge_storage_dir or "").strip()
        if raw:
            return str(Path(raw).expanduser().resolve(strict=False))
        return str(KNOWLEDGE_DIR.resolve(strict=False))
