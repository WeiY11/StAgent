"""Process-wide mission runner registry — the API-facing wiring layer.

Owns one asyncio task per active :class:`~leagent.db.models.mission.Mission`
and assembles the :class:`~leagent.mission.runner.MissionRunner` seams from
the :class:`~leagent.services.service_manager.ServiceManager`:

- ``DbMissionRepository`` for persistence,
- ``LLMMissionPlanner`` for plan generation / replan,
- ``KernelSegmentExecutor`` for real-model segments,
- ``HeuristicVerifier`` (``verify_level=none`` → ``AlwaysPassVerifier``).

The runner's ``event_sink`` output is bridged onto the ``EventManager`` bus as
``mission.*`` lifecycle events. Process-local by design — same deployment
boundaries as ``ExecutionRunRegistry`` (single worker / sticky sessions).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from leagent.db.models.mission import Mission, MissionVerifyLevel
from leagent.db.repositories.missions import DbMissionRepository
from leagent.mission.runner import (
    AlwaysPassVerifier,
    MissionPlanner,
    MissionRunner,
    MissionVerifier,
    SegmentExecutor,
)
from leagent.mission.verifier import HeuristicVerifier
from leagent.utils.logging import get_logger

if TYPE_CHECKING:
    from leagent.db.repositories.missions import MissionRepository
    from leagent.db.service import DatabaseService
    from leagent.services.service_manager import ServiceManager

logger = get_logger(__name__)

#: Runner ``event_sink`` names → typed ``mission.*`` event types.
_EVENT_TYPE_BY_NAME: dict[str, Any] = {}


def _build_event_map() -> dict[str, Any]:
    from leagent.services.event.manager import EventType

    return {
        "mission_planned": EventType.MISSION_PLANNED,
        "mission_plan_rejected": EventType.MISSION_PLAN_REJECTED,
        "mission_step_started": EventType.MISSION_STEP_STARTED,
        "mission_step_verifying": EventType.MISSION_STEP_VERIFYING,
        "mission_step_verified": EventType.MISSION_STEP_VERIFIED,
        "mission_step_verify_failed": EventType.MISSION_STEP_VERIFY_FAILED,
        "mission_step_awaiting_approval": EventType.MISSION_STEP_AWAITING_APPROVAL,
        "mission_step_approved": EventType.MISSION_STEP_APPROVED,
        "mission_step_passed": EventType.MISSION_STEP_PASSED,
        "mission_step_failed": EventType.MISSION_STEP_FAILED,
        "mission_replanned": EventType.MISSION_REPLANNED,
        "mission_segment_continued": EventType.MISSION_SEGMENT_CONTINUED,
        "mission_resumed": EventType.MISSION_RESUMED,
        "mission_paused": EventType.MISSION_PAUSED,
        "mission_completed": EventType.MISSION_COMPLETED,
        "mission_failed": EventType.MISSION_FAILED,
        "mission_aborted": EventType.MISSION_ABORTED,
        "mission_expired": EventType.MISSION_EXPIRED,
    }


PlannerFactory = Callable[[Mission], MissionPlanner]
ExecutorFactory = Callable[[Mission], SegmentExecutor]
VerifierFactory = Callable[[Mission], MissionVerifier]


class _FactoryPlanner:
    """Per-mission planner adapter over a :class:`PlannerFactory`."""

    def __init__(self, factory: PlannerFactory) -> None:
        self._factory = factory

    async def plan(self, mission: Mission) -> Any:
        return await self._factory(mission).plan(mission)

    async def replan(self, mission: Mission, failed_step: Any) -> Any | None:
        impl = self._factory(mission)
        replan = getattr(impl, "replan", None)
        if replan is None:
            return None
        return await replan(mission, failed_step)


class _FactoryExecutor:
    """Per-mission executor adapter over an :class:`ExecutorFactory`."""

    def __init__(self, factory: ExecutorFactory) -> None:
        self._factory = factory

    async def execute(self, *, mission: Mission, step: Any, prompt: str, attempt: int) -> Any:
        return await self._factory(mission).execute(
            mission=mission, step=step, prompt=prompt, attempt=attempt
        )


class _FactoryVerifier:
    """Per-mission verifier adapter over a :class:`VerifierFactory`."""

    def __init__(self, factory: VerifierFactory) -> None:
        self._factory = factory

    async def verify(self, *, mission: Mission, step: Any, result: Any) -> Any:
        return await self._factory(mission).verify(mission=mission, step=step, result=result)


class MissionService:
    """Registry + factory for mission runner tasks.

    Args:
        service_manager: Wired services; used to build the default planner /
            executor / event sink. Optional so tests can inject factories only.
        db: Database service backing the mission repository (defaults to
            ``service_manager.db``).
        planner_factory / executor_factory / verifier_factory: Optional
            overrides (per-mission) for the runner seams — the test seams.
    """

    def __init__(
        self,
        *,
        service_manager: ServiceManager | None = None,
        db: DatabaseService | None = None,
        planner_factory: PlannerFactory | None = None,
        executor_factory: ExecutorFactory | None = None,
        verifier_factory: VerifierFactory | None = None,
    ) -> None:
        self._sm = service_manager
        self._db = db or (service_manager.db if service_manager is not None else None)
        if self._db is None:
            raise ValueError("MissionService requires a DatabaseService (db= or service_manager)")
        self._store: MissionRepository = DbMissionRepository(self._db)
        self._planner_factory = planner_factory
        self._executor_factory = executor_factory
        self._verifier_factory = verifier_factory
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    @property
    def store(self) -> MissionRepository:
        """Shared mission repository (also used directly by the API layer)."""
        return self._store

    def is_running(self, mission_id: UUID) -> bool:
        task = self._tasks.get(mission_id)
        return task is not None and not task.done()

    async def ensure_running(self, mission: Mission) -> bool:
        """Drive *mission* in a background task (idempotent).

        Returns True when a new task was started, False when one is already
        active for this mission. ``MissionRunner.run`` is itself idempotent:
        it plans ``PLANNING`` missions and resumes ``PAUSED`` ones.
        """
        if self.is_running(mission.id):
            return False
        runner = self._build_runner()
        task = asyncio.create_task(self._drive(runner, mission.id), name=f"mission-{mission.id}")
        self._tasks[mission.id] = task
        return True

    async def _drive(self, runner: MissionRunner, mission_id: UUID) -> None:
        from leagent.db.models.mission import MissionStatus

        try:
            mission = await self._store.get(mission_id)
            if mission is None:
                logger.warning("mission_drive_missing", mission_id=str(mission_id))
                return
            await runner.run(mission)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leave a mission stuck RUNNING
            logger.error("mission_drive_failed", mission_id=str(mission_id), error=str(exc))
            try:
                await self._store.update_status(
                    mission_id, MissionStatus.FAILED, error=f"runner crashed: {exc}"
                )
            except Exception:
                logger.debug("mission_drive_status_write_failed", exc_info=True)
        finally:
            self._tasks.pop(mission_id, None)

    async def pause(self, mission_id: UUID) -> Mission | None:
        """Pause a running mission (graceful: the run loop exits at step end)."""
        mission = await self._store.get(mission_id)
        if mission is None:
            return None
        runner = self._build_runner()
        return await runner.pause(mission)

    async def resume(self, mission_id: UUID) -> Mission | None:
        """Resume a paused mission and (re)start its background task."""
        mission = await self._store.get(mission_id)
        if mission is None:
            return None
        runner = self._build_runner()
        mission = await runner.resume(mission)
        await self.ensure_running(mission)
        return mission

    async def abort(self, mission_id: UUID) -> Mission | None:
        """Abort the mission and cancel any active background task."""
        mission = await self._store.get(mission_id)
        if mission is None:
            return None
        runner = self._build_runner()
        mission = await runner.abort(mission)
        task = self._tasks.get(mission_id)
        if task is not None and not task.done():
            task.cancel()
        return mission

    async def approve_step(self, mission_id: UUID, step_no: int, *, approved: bool, note: str = "") -> Mission | None:
        """Approve/reject a ``WAITING_APPROVAL`` step (re-runs when paused)."""
        mission = await self._store.get(mission_id)
        if mission is None:
            return None
        runner = self._build_runner()
        mission = await runner.approve_step(mission, step_no, approved=approved, note=note)
        # ``step_confirm`` missions pause while waiting; approval flips them
        # back to RUNNING, so re-drive the loop.
        from leagent.db.models.mission import MissionStatus

        if mission.status == MissionStatus.RUNNING:
            await self.ensure_running(mission)
        return mission

    # ------------------------------------------------------------------
    # Runner assembly
    # ------------------------------------------------------------------

    def _build_runner(self) -> MissionRunner:
        return MissionRunner(
            store=self._store,
            planner=_FactoryPlanner(self._make_planner),
            executor=_FactoryExecutor(self._make_executor),
            verifier=_FactoryVerifier(self._make_verifier),
            event_sink=self._event_sink,
        )

    def _make_planner(self, mission: Mission) -> MissionPlanner:
        if self._planner_factory is not None:
            return self._planner_factory(mission)
        from leagent.mission.planner import LLMMissionPlanner

        llm = getattr(self._sm, "llm_service", None) if self._sm is not None else None
        if llm is None:
            raise RuntimeError("LLMService unavailable — mission planning cannot run")
        return LLMMissionPlanner(llm, store=self._store)

    def _make_executor(self, mission: Mission) -> SegmentExecutor:
        if self._executor_factory is not None:
            return self._executor_factory(mission)
        from leagent.mission.segment import KernelSegmentExecutor

        return KernelSegmentExecutor(
            service_manager=self._sm,
            agent_id=mission.agent_id or "default_agent",
        )

    def _make_verifier(self, mission: Mission) -> MissionVerifier:
        if self._verifier_factory is not None:
            return self._verifier_factory(mission)
        if mission.verify_level == MissionVerifyLevel.NONE:
            return AlwaysPassVerifier()
        return HeuristicVerifier()

    async def _event_sink(self, event: str, data: dict[str, Any]) -> None:
        """Bridge runner ``mission_*`` emits onto the EventManager bus."""
        global _EVENT_TYPE_BY_NAME
        if not _EVENT_TYPE_BY_NAME:
            _EVENT_TYPE_BY_NAME = _build_event_map()
        etype = _EVENT_TYPE_BY_NAME.get(event)
        em = getattr(self._sm, "event", None) if self._sm is not None else None
        if em is None or etype is None:
            return
        mission_id = str(data.get("mission_id") or "")
        try:
            await em.publish_mission_lifecycle(etype, mission_id=mission_id or None, data=data)
        except Exception:
            logger.debug("mission_event_sink_failed", event=event, exc_info=True)


_mission_service: MissionService | None = None


def get_mission_service() -> MissionService:
    """Return the process-wide :class:`MissionService` (lazy singleton)."""
    global _mission_service
    if _mission_service is None:
        from leagent.services.service_manager import get_service_manager

        _mission_service = MissionService(service_manager=get_service_manager())
    return _mission_service


def set_mission_service(service: MissionService | None) -> None:
    """Replace the process-wide service (lifespan teardown / tests)."""
    global _mission_service
    _mission_service = service


__all__ = [
    "MissionService",
    "get_mission_service",
    "set_mission_service",
]
