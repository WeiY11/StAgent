"""Chat attachment persistence and reference resolution.

Two responsibilities:
  * :func:`attach_chat_files` — persist multipart uploads through
    ``SessionManager.attach_files`` and shape them for the SSE response.
  * :func:`resolve_request_attachment_paths` / :func:`merge_agent_attachment_paths`
    — turn mixed attachment references (id / filename / path) carried on a chat
    request into concrete storage paths the agent can read.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import HTTPException, UploadFile, status

from StAgent.api.v1.chat.paths import attachment_local_path_for_sse, dedupe_resolved_paths
from StAgent.file.attachment_context import (
    attachment_storage_basename,
    is_object_storage_path,
)

if TYPE_CHECKING:
    from uuid import UUID

logger = structlog.get_logger(__name__)

#: Attachment kinds rendered as inline assistant media (ChatGPT-style output).
_ASSISTANT_MEDIA_KINDS = {"image", "video", "model3d", "audio"}


def build_assistant_media_event(
    workspace_payload: Any,
    *,
    native_image_output: bool,
) -> dict[str, Any] | None:
    """Build an ``assistant_media`` SSE payload from workspace attachments.

    Filters the assistant's produced attachments down to renderable media so
    the frontend can show images / video / 3D inline within the message body
    rather than only as attachment cards. ``native_image_output`` reflects
    whether the active model's capability profile can itself emit image output
    (capability-routed), distinguishing model-native media from tool output.
    """
    if not isinstance(workspace_payload, dict):
        return None
    items = workspace_payload.get("attachments")
    if not isinstance(items, list):
        return None
    media: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").lower()
        ctype = str(item.get("content_type") or item.get("mime") or "").lower()
        if kind in _ASSISTANT_MEDIA_KINDS or ctype.startswith(("image/", "video/", "audio/")):
            media.append(item)
    if not media:
        return None
    return {"attachments": media, "native": bool(native_image_output)}


async def attach_chat_files(
    user_id: UUID,
    session_id: UUID,
    files: list[UploadFile],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    """Persist uploads via :meth:`SessionManager.attach_files`.

    Returns ``(attachment_rows, stored_paths, errors)``.
    """
    from StAgent.main import get_service_manager

    attachments_out: list[dict[str, Any]] = []
    stored_paths: list[str] = []
    errors: list[dict[str, str]] = []

    if not files:
        return attachments_out, stored_paths, errors

    try:
        sm = get_service_manager()
    except Exception:  # noqa: BLE001
        sm = None

    if sm is None or sm.session_manager is None:
        errors.extend(
            {
                "file": upload.filename or "unnamed",
                "error": "Session manager unavailable; file rejected",
            }
            for upload in files
        )
        return attachments_out, stored_paths, errors

    try:
        persisted = await sm.session_manager.attach_files(
            session_id,
            files,
            user_id=user_id,
        )
    except ValueError as exc:
        errors.append({"file": "(batch)", "error": str(exc)})
        return attachments_out, stored_paths, errors
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_attach_files_failed: %s", exc)
        errors.append({"file": "(batch)", "error": str(exc)})
        return attachments_out, stored_paths, errors

    for att in persisted:
        if att.storage_path:
            stored_paths.append(att.storage_path)
        row: dict[str, Any] = {
            "id": str(att.id),
            "filename": att.filename,
            "kind": att.kind,
            "content_type": att.content_type,
            "size": att.size,
            "preview_url": att.preview_url,
            "download_url": att.download_url,
        }
        lp = None
        if not is_object_storage_path(att.storage_path):
            lp = attachment_local_path_for_sse(att.storage_path)
        if lp:
            row["local_path"] = lp
        attachments_out.append(row)
    return attachments_out, stored_paths, errors


async def resolve_request_attachment_paths(
    session_id: UUID,
    attachment_refs: list[str] | None,
    *,
    user_id: UUID | None = None,
    reject_unknown: bool = True,
) -> list[str]:
    """Resolve persisted session attachment ids or names to local storage paths.

    Request attachment values are untrusted identifiers, never filesystem paths.
    ``reject_unknown`` is disabled only for workflow free-text inputs, where a
    non-attachment value is expected and must not grant access to a file.
    """
    if not attachment_refs:
        return []

    from StAgent.main import get_service_manager

    try:
        sm = get_service_manager()
    except Exception:  # noqa: BLE001
        sm = None

    if sm is None or sm.session_manager is None:
        if reject_unknown:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Attachment service unavailable; attachment references cannot be verified",
            )
        return []

    try:
        session_attachments = await sm.session_manager.list_attachments(
            session_id,
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_request_attachment_paths_failed: %s", exc)
        if reject_unknown:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Attachment references could not be verified",
            ) from exc
        return []

    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    by_basename: dict[str, str] = {}
    for att in session_attachments:
        if not att.storage_path:
            continue
        raw_storage_path = str(att.storage_path).strip()
        if is_object_storage_path(raw_storage_path):
            spath = raw_storage_path
        else:
            spath = str(Path(raw_storage_path).expanduser().resolve())
        by_id[str(att.id)] = spath
        if att.filename:
            by_name[att.filename.casefold()] = spath
        by_basename[attachment_storage_basename(spath).casefold()] = spath

    resolved: list[str] = []
    unknown_refs: list[str] = []
    for ref in attachment_refs:
        if not ref:
            continue
        key = str(ref).strip()
        if not key:
            continue
        mapped = (
            by_id.get(key)
            or by_name.get(key.casefold())
            or by_basename.get(attachment_storage_basename(key).casefold())
        )
        if mapped:
            resolved.append(mapped)
        else:
            unknown_refs.append(key)

    if unknown_refs and reject_unknown:
        logger.warning(
            "chat_attachment_reference_rejected",
            session_id=str(session_id),
            user_id=str(user_id) if user_id else None,
            count=len(unknown_refs),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Attachment references must belong to this chat session",
        )

    local_paths = [path for path in resolved if not is_object_storage_path(path)]
    remote_paths = [path for path in resolved if is_object_storage_path(path)]
    local_deduped = dedupe_resolved_paths(local_paths)
    return list(dict.fromkeys([*local_deduped, *remote_paths]))


def merge_agent_attachment_paths(
    base: list[str] | None,
    extra: list[str],
) -> list[str] | None:
    """Combine and de-duplicate two attachment-path lists (``None`` when empty)."""
    combined = (base or []) + extra
    if not combined:
        return None
    local_paths = [path for path in combined if not is_object_storage_path(path)]
    remote_paths = [path for path in combined if is_object_storage_path(path)]
    resolved = [*dedupe_resolved_paths(local_paths), *dict.fromkeys(remote_paths)]
    return resolved or None
