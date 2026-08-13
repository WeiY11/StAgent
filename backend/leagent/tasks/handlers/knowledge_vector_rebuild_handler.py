"""Durable handler for a local knowledge-vector candidate rebuild."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from leagent.db.models.task import TaskContext, TaskType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from leagent.services.service_manager import ServiceManager


class KnowledgeVectorRebuildTaskHandler:
    """Resume-safe task adapter around :class:`KnowledgeSemanticRebuilder`."""

    name = "knowledge_vector_rebuild_handler"
    task_type = TaskType.KNOWLEDGE_VECTOR_REBUILD

    def __init__(self, service_manager: ServiceManager) -> None:
        self._sm = service_manager

    async def spawn(
        self,
        task_ctx: TaskContext,
        params: dict[str, Any],
        session: AsyncSession,
    ) -> dict[str, Any]:
        rebuilder = self._sm.knowledge_semantic_rebuilder
        if rebuilder is None:
            raise RuntimeError("local_semantic_rebuild_unavailable")
        task_id = str(params.get("__task_db_id") or "") or None
        candidate = str(params.get("candidate_version") or "") or None
        return cast(
            "dict[str, Any]",
            await rebuilder.rebuild(
                task_ctx=task_ctx,
                session=session,
                candidate_version=candidate,
                task_id=task_id,
            ),
        )

    async def kill(self, task_id: str, session: AsyncSession) -> None:
        return None


__all__ = ["KnowledgeVectorRebuildTaskHandler"]
