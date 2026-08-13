"""Session attachment paths and lookup tables for ToolContext.extra.

Mirrors the logic previously embedded in AgentController so chat workflow
step runs, agents, and tests share one implementation.
"""

from __future__ import annotations

import copy
import re
import tempfile
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

logger = structlog.get_logger(__name__)

_STORED_UUID_PREFIX = re.compile(
    r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_(.+)$",
    re.IGNORECASE,
)
_S3_STORAGE_PATH_RE = re.compile(r"^s3://", re.IGNORECASE)
_SAFE_TEMP_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,16}$")


def is_object_storage_path(storage_path: str | None) -> bool:
    """Return whether a persisted path is an S3 object URI, never a local path."""
    return bool(_S3_STORAGE_PATH_RE.match(str(storage_path or "").strip()))


def attachment_storage_basename(storage_path: str) -> str:
    """Return a display basename without treating an object URI as a local path."""
    raw = str(storage_path or "").strip()
    if is_object_storage_path(raw):
        return raw.rstrip("/").rsplit("/", 1)[-1]
    return Path(raw).name


def _safe_staging_suffix(attachment: Any) -> str:
    filename = str(getattr(attachment, "filename", "") or "")
    suffix = Path(filename).suffix
    return suffix if _SAFE_TEMP_SUFFIX_RE.fullmatch(suffix) else ""


def _local_attachment_path(storage_path: str) -> str | None:
    """Normalize a local attachment path, rejecting non-local values."""
    raw = str(storage_path or "").strip()
    if not raw or is_object_storage_path(raw):
        return None
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            return None
        return str(candidate.resolve())
    except OSError:
        return None


def _dedupe_local_paths(paths: list[str]) -> list[str]:
    """Normalize local paths while preserving order and omitting invalid inputs."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = _local_attachment_path(raw)
        if path is None or path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return normalized


def _copy_attachment_with_storage_path(attachment: Any, storage_path: str) -> Any:
    """Clone a session attachment without mutating durable session state."""
    try:
        copied = copy.copy(attachment)
        copied.storage_path = storage_path
        return copied
    except Exception:  # noqa: BLE001 - heterogeneous legacy attachment rows
        from types import SimpleNamespace

        return SimpleNamespace(
            id=getattr(attachment, "id", ""),
            session_id=getattr(attachment, "session_id", None),
            filename=getattr(attachment, "filename", ""),
            storage_path=storage_path,
            content_type=getattr(attachment, "content_type", "application/octet-stream"),
            kind=getattr(attachment, "kind", "other"),
            size=getattr(attachment, "size", 0),
            sha256=getattr(attachment, "sha256", ""),
            extra=getattr(attachment, "extra", {}),
        )


def _configured_file_service(
    session_manager: Any,
    file_service: Any | None,
) -> Any | None:
    """Find an existing configured FileService without creating a local fallback."""
    if file_service is not None:
        return file_service
    configured = getattr(session_manager, "_file_service", None)
    if configured is not None:
        return configured
    try:
        from leagent.main import get_service_manager

        manager = get_service_manager()
        return getattr(manager, "file_service", None)
    except Exception:  # noqa: BLE001 - remote attachments fail closed below
        return None


def _file_service_resolves_object_storage_reference(
    file_service: Any,
    ref: Any,
    storage_path: str,
) -> bool:
    """Require a registered backend to recognise the persisted object URI.

    ``FileService.ref_from_persisted_record`` intentionally falls back to its
    default backend for legacy paths. That fallback is useful for local rows,
    but accepting it for an S3 URI could turn an unavailable object backend
    into a local-file read. Object attachments therefore require an explicit
    backend URI round trip before download.
    """
    try:
        backend_name = getattr(ref, "backend_name", None)
        get_backend = getattr(file_service, "get_backend", None)
        if not isinstance(backend_name, str) or not backend_name or not callable(get_backend):
            return False
        backend = get_backend(backend_name)
        key_from_storage_path = getattr(backend, "key_from_storage_path", None)
        return callable(key_from_storage_path) and key_from_storage_path(storage_path) is not None
    except Exception:  # noqa: BLE001 - unavailable/misconfigured backend is not a grant
        return False


async def _stage_object_storage_attachment(
    attachment: Any,
    *,
    session_id: UUID,
    user_id: UUID | None,
    file_service: Any | None,
    staged_paths: list[Path],
) -> str | None:
    """Materialize one already-authorised session object into a temporary file."""
    storage_path = str(getattr(attachment, "storage_path", "") or "").strip()
    if file_service is None:
        logger.warning(
            "session_attachment_object_storage_skipped",
            file_id=str(getattr(attachment, "id", "")),
            reason="file_service_unavailable",
        )
        return None

    staged_path: Path | None = None
    try:
        ref = file_service.ref_from_persisted_record(
            file_id=attachment.id,
            filename=str(getattr(attachment, "filename", "") or "attachment"),
            storage_path=storage_path,
            content_type=getattr(attachment, "content_type", None),
            size=getattr(attachment, "size", None),
            checksum=getattr(attachment, "sha256", None),
            user_id=user_id,
            session_id=session_id,
            library_scope="workspace",
        )
        if not _file_service_resolves_object_storage_reference(
            file_service,
            ref,
            storage_path,
        ):
            raise RuntimeError("object storage backend cannot resolve attachment URI")
        raw, _content_type = await file_service.download(ref)
        if not isinstance(raw, bytes):
            raise TypeError("session attachment object storage returned non-bytes content")
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="leagent-session-attachment-",
            suffix=_safe_staging_suffix(attachment),
            delete=False,
        ) as staged:
            staged_path = Path(staged.name)
            staged.write(raw)
        staged_paths.append(staged_path)
        return str(staged_path.resolve())
    except Exception:  # noqa: BLE001 - an unreadable object must not become a path grant
        if staged_path is not None:
            with suppress(OSError):
                staged_path.unlink()
        logger.warning(
            "session_attachment_object_storage_materialization_failed",
            file_id=str(getattr(attachment, "id", "")),
            exc_info=True,
        )
        return None


@dataclass(slots=True)
class SessionAttachmentPathLease:
    """Turn-scoped local paths and attachment rows safe to hand to tools."""

    session_attachments: list[Any]
    paths: list[str]
    storage_path_aliases: dict[str, str]

    def materialize_extra_paths(self, extra_paths: list[str] | None) -> list[str]:
        """Map extra refs while preserving unrelated local paths.

        An unlisted object URI is retained for a separately-owned lease (such
        as a knowledge-context file). It is not downloaded here: only a URI
        listed in ``storage_path_aliases`` can trigger this session's
        FileService materialization.
        """
        combined: list[str] = []
        for raw in extra_paths or []:
            value = str(raw or "").strip()
            if not value:
                continue
            mapped = self.storage_path_aliases.get(value)
            if mapped is not None:
                combined.append(mapped)
            else:
                combined.append(value)
        return combined

    def all_paths_with_extra(self, extra_paths: list[str] | None) -> list[str]:
        """Return all session paths plus mapped extra local paths."""
        local_paths: list[str] = []
        remote_paths: list[str] = []
        for path in [*self.paths, *self.materialize_extra_paths(extra_paths)]:
            if is_object_storage_path(path):
                remote_paths.append(path)
            else:
                local_paths.append(path)
        return [*_dedupe_local_paths(local_paths), *dict.fromkeys(remote_paths)]


class MaterializedSessionAttachmentManager:
    """Read-only current-turn view that keeps prompt context off raw S3 URIs."""

    def __init__(
        self,
        session_manager: Any,
        *,
        session_id: UUID,
        user_id: UUID | None,
        attachments: list[Any],
    ) -> None:
        self._session_manager = session_manager
        self._session_id = session_id
        self._user_id = user_id
        self._attachments = list(attachments)

    async def list_attachments(
        self,
        session_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> list[Any]:
        """Return staged rows only for the same authorised turn identity."""
        if str(session_id) == str(self._session_id):
            if user_id is not None and self._user_id is not None and str(user_id) != str(self._user_id):
                return []
            return list(self._attachments)
        attachments = await self._session_manager.list_attachments(
            session_id,
            user_id=user_id,
        )
        return list(attachments or [])

    def build_attachment_manifest(self, attachments: Any) -> str:
        """Delegate formatting while retaining the materialized attachment rows."""
        manifest = self._session_manager.build_attachment_manifest(attachments)
        return manifest if isinstance(manifest, str) else ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session_manager, name)


def materialized_session_attachment_manager(
    session_manager: Any | None,
    *,
    session_id: UUID,
    user_id: UUID | None,
    attachments: list[Any],
) -> Any | None:
    """Create a turn-local session-manager overlay for engine context sources."""
    if session_manager is None:
        return None
    return MaterializedSessionAttachmentManager(
        session_manager,
        session_id=session_id,
        user_id=user_id,
        attachments=attachments,
    )


@asynccontextmanager
async def materialize_session_attachment_paths(
    session_manager: Any | None,
    session_id: UUID,
    *,
    user_id: UUID | None = None,
    file_service: Any | None = None,
    extra_paths: list[str] | None = None,
) -> AsyncIterator[SessionAttachmentPathLease]:
    """Lease current-session attachment paths for a full agent turn.

    Local paths retain their former normalisation behavior. S3-backed rows are
    downloaded only after ``SessionManager.list_attachments(session_id,
    user_id=...)`` authorises them, then removed in ``finally`` after the
    agent turn completes, fails, or is cancelled.
    """
    staged_paths: list[Path] = []
    try:
        if session_manager is None:
            extra_local_paths: list[str] = []
            extra_remote_paths: list[str] = []
            for raw in extra_paths or []:
                value = str(raw or "").strip()
                if not value:
                    continue
                if is_object_storage_path(value):
                    extra_remote_paths.append(value)
                else:
                    extra_local_paths.append(value)
            yield SessionAttachmentPathLease(
                [],
                [
                    *_dedupe_local_paths(extra_local_paths),
                    *dict.fromkeys(extra_remote_paths),
                ],
                {},
            )
            return

        try:
            loaded_attachments = await session_manager.list_attachments(
                session_id,
                user_id=user_id,
            )
            attachments = list(loaded_attachments or [])
        except Exception as exc:  # noqa: BLE001 - session auth/load failure fails closed
            logger.warning("session_attachments_load_failed", error=str(exc))
            attachments = []

        service: Any | None = file_service
        service_resolved = file_service is not None
        materialized_attachments: list[Any] = []
        local_paths: list[str] = []
        aliases: dict[str, str] = {}
        staged_by_storage_path: dict[str, str] = {}

        for attachment in attachments:
            original_path = str(getattr(attachment, "storage_path", "") or "").strip()
            if not original_path:
                continue
            attachment_session_id = getattr(attachment, "session_id", None)
            if attachment_session_id is None or str(attachment_session_id) != str(session_id):
                logger.warning(
                    "session_attachment_session_mismatch",
                    attachment_id=str(getattr(attachment, "id", "")),
                    session_id=str(session_id),
                )
                continue

            if is_object_storage_path(original_path):
                if not service_resolved:
                    service = _configured_file_service(session_manager, None)
                    service_resolved = True
                staged = staged_by_storage_path.get(original_path)
                if staged is None:
                    staged = await _stage_object_storage_attachment(
                        attachment,
                        session_id=session_id,
                        user_id=user_id,
                        file_service=service,
                        staged_paths=staged_paths,
                    )
                    if staged is not None:
                        staged_by_storage_path[original_path] = staged
                if staged is None:
                    continue
                materialized_attachments.append(
                    _copy_attachment_with_storage_path(attachment, staged),
                )
                local_paths.append(staged)
                aliases[original_path] = staged
                continue

            local_path = _local_attachment_path(original_path)
            if local_path is None:
                logger.warning(
                    "session_attachment_path_not_local",
                    attachment_id=str(getattr(attachment, "id", "")),
                    value=original_path,
                )
                continue
            materialized_attachments.append(
                _copy_attachment_with_storage_path(attachment, local_path),
            )
            local_paths.append(local_path)
            aliases[original_path] = local_path

        lease = SessionAttachmentPathLease(
            session_attachments=materialized_attachments,
            paths=_dedupe_local_paths(local_paths),
            storage_path_aliases=aliases,
        )
        yield SessionAttachmentPathLease(
            session_attachments=lease.session_attachments,
            paths=lease.all_paths_with_extra(extra_paths),
            storage_path_aliases=lease.storage_path_aliases,
        )
    finally:
        for staged_path in reversed(staged_paths):
            with suppress(OSError):
                staged_path.unlink()


def normalize_attachment_paths(paths: list[str]) -> list[str]:
    """Normalize and de-duplicate attachment paths for tool context."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        if is_object_storage_path(raw):
            logger.warning("attachment_object_storage_not_materialized", value=raw)
            continue
        resolved = _local_attachment_path(raw)
        if resolved is None:
            logger.warning("attachment_path_not_absolute", value=raw)
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        normalized.append(resolved)
    return normalized


def attachment_aliases_from_path(storage_path: str) -> set[str]:
    aliases: set[str] = set()
    name = attachment_storage_basename(storage_path)
    if not name:
        return aliases
    aliases.add(name)
    stem = Path(name).stem
    if stem:
        aliases.add(stem)
    if "_" in name:
        suffix = name.split("_", 1)[1]
        if suffix:
            aliases.add(suffix)
            suffix_stem = Path(suffix).stem
            if suffix_stem:
                aliases.add(suffix_stem)
    return aliases


def attachment_aliases(attachment: Any) -> set[str]:
    aliases: set[str] = set()
    filename = str(getattr(attachment, "filename", "") or "").strip()
    if filename:
        aliases.add(filename)
        stem = Path(filename).stem
        if stem:
            aliases.add(stem)
    storage_path = str(getattr(attachment, "storage_path", "") or "").strip()
    if storage_path:
        aliases.update(attachment_aliases_from_path(storage_path))
    return aliases


def normalise_attachment_alias(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def build_attachment_lookup(
    *,
    session_attachments: list[Any],
    normalized_attachments: list[str],
) -> dict[str, dict[str, str]]:
    """Build attachment lookup tables keyed by id and normalized name."""
    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}

    for att in session_attachments:
        storage_path = getattr(att, "storage_path", "")
        if not isinstance(storage_path, str) or not storage_path:
            continue
        normalized_path = normalize_attachment_paths([storage_path])
        if not normalized_path:
            continue
        resolved_path = normalized_path[0]

        att_id = str(getattr(att, "id", "") or "").strip()
        if att_id:
            by_id.setdefault(att_id, resolved_path)

        for alias in attachment_aliases(att):
            key = normalise_attachment_alias(alias)
            if key:
                by_name.setdefault(key, resolved_path)

    for resolved_path in normalized_attachments:
        for alias in attachment_aliases_from_path(resolved_path):
            key = normalise_attachment_alias(alias)
            if key:
                by_name.setdefault(key, resolved_path)

    for resolved_path in normalized_attachments:
        base = attachment_storage_basename(resolved_path)
        m = _STORED_UUID_PREFIX.match(base)
        if m:
            by_id.setdefault(m.group(1).lower(), resolved_path)
            suffix = m.group(2)
            if suffix:
                synthetic = str(Path("/__wa_virtual__") / suffix)
                for alias in attachment_aliases_from_path(synthetic):
                    key = normalise_attachment_alias(alias)
                    if key:
                        by_name.setdefault(key, resolved_path)

    lookup: dict[str, dict[str, str]] = {}
    if by_id:
        lookup["by_id"] = by_id
    if by_name:
        lookup["by_name"] = by_name
    return lookup


def build_tool_extra_for_attachment_paths(
    session_attachments: list[Any],
    merged_paths: list[str],
) -> dict[str, Any]:
    """Return ``attachments`` / ``attachment_lookup`` entries for ToolContext.extra.

    *merged_paths* should be the deduplicated union of session storage paths
    and any extra paths (e.g. knowledge files merged for this turn).
    """
    normalized = normalize_attachment_paths(merged_paths)
    tool_extra: dict[str, Any] = {}
    if normalized:
        tool_extra["attachments"] = normalized
    lookup = build_attachment_lookup(
        session_attachments=session_attachments,
        normalized_attachments=normalized,
    )
    if lookup:
        tool_extra["attachment_lookup"] = lookup
    return tool_extra


async def tool_extra_for_chat_session(
    session_manager: Any,
    session_id: UUID,
    *,
    user_id: UUID | None = None,
    extra_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Load session attachments and build tool ``extra`` for that chat session."""
    try:
        session_attachments = await session_manager.list_attachments(
            session_id,
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_attachments_load_failed", error=str(exc))
        return {}

    session_paths = [att.storage_path for att in session_attachments if getattr(att, "storage_path", None)]
    merged = list(session_paths)
    if extra_paths:
        for path in extra_paths:
            if path and path not in merged:
                merged.append(path)

    return build_tool_extra_for_attachment_paths(session_attachments, merged)
