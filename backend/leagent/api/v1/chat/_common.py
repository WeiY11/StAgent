"""Shared helpers for the chat API route submodules.

These small, cross-cutting helpers are imported by the per-route submodules
(``stream``, ``completions``, ``sessions``) and re-exported from
:mod:`leagent.api.v1.chat` for backward compatibility with existing callers
and tests that reference the legacy leading-underscore names.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from leagent.db import DatabaseService
from leagent.services.chat.projects import ChatProjectService


def _project_locked_error() -> HTTPException:
    return HTTPException(status_code=status.HTTP_423_LOCKED, detail="Project locked")


def _accumulate_workflow_extensions(existing_json: str | None, edata: dict[str, Any]) -> str | None:
    """Merge a workflow SSE payload into the accumulated message extensions.

    A single assistant turn may emit BOTH a DAG embed (``chat_workflow_embed_emit``)
    and a step card (``chat_workflow_emit``). They live under disjoint extension
    keys, so we accumulate rather than overwrite — otherwise the second emit would
    erase the first card on reload (and the DAG would disappear).
    """
    from leagent.chat_workflow.workflow_embed import build_extensions_payload

    acc: dict[str, Any] = {}
    if existing_json:
        try:
            parsed = json.loads(existing_json)
            if isinstance(parsed, dict):
                acc = parsed
        except json.JSONDecodeError:
            acc = {}

    spec = edata.get("spec")
    embed = edata.get("embed")
    if isinstance(embed, dict) and isinstance(embed.get("data"), dict):
        acc.update(
            build_extensions_payload(
                flow_data=embed["data"],
                digest=str(embed.get("digest") or ""),
                flow_id=str(embed["flow_id"]) if embed.get("flow_id") else None,
                title=str(embed.get("title") or "") or None,
                summary=str(embed.get("summary") or "") or None,
            )
        )
    elif isinstance(spec, dict):
        acc["chat_workflow"] = spec
        acc["chat_workflow_digest"] = edata.get("digest")
    else:
        return existing_json

    return json.dumps(acc, ensure_ascii=False)


async def _require_project_access(
    db: DatabaseService,
    *,
    project_id: UUID | None,
    user_id: UUID,
    unlock_token: str | None,
) -> None:
    try:
        await ChatProjectService(db).require_project_access(
            project_id,
            user_id=user_id,
            unlock_token=unlock_token,
        )
    except PermissionError as exc:
        if str(exc) == "Project locked":
            raise _project_locked_error() from exc
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from exc


async def _require_session_project_access(
    db: DatabaseService,
    *,
    session: Any,
    user_id: UUID,
    unlock_token: str | None,
) -> None:
    try:
        await ChatProjectService(db).require_session_project_access(
            session,
            user_id=user_id,
            unlock_token=unlock_token,
        )
    except PermissionError as exc:
        if str(exc) == "Project locked":
            raise _project_locked_error() from exc
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found") from exc
