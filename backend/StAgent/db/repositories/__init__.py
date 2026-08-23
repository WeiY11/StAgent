"""Per-domain persistence repositories.

Repositories are the seam between application/service code and the database.
They encapsulate SQLModel queries so handlers and services depend on a narrow,
typed interface (modeled on :class:`StAgent.cron.repository.JobRepository`)
rather than embedding inline ``select(...)`` statements.

Each repository is defined as a ``Protocol`` plus a concrete ``Db*`` implementation
that wraps a :class:`~StAgent.db.service.DatabaseService`. The :class:`Repositories`
aggregator provides lazy access to all of them from a single ``DatabaseService``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from StAgent.db.repositories.agent_checkpoint import (
    CheckpointRepository,
    DbCheckpointRepository,
)
from StAgent.db.repositories.chat import ChatRepository, DbChatRepository
from StAgent.db.repositories.coding_projects import (
    CodingProjectRepository,
    DbCodingProjectRepository,
)
from StAgent.db.repositories.complaints import (
    ComplaintRepository,
    DbComplaintRepository,
)
from StAgent.db.repositories.document_chunks import (
    DbDocumentChunkRepository,
    DocumentChunkRepository,
)
from StAgent.db.repositories.files import DbFileRepository, FileRepository
from StAgent.db.repositories.flows import DbFlowRepository, FlowRepository
from StAgent.db.repositories.folders import DbFolderRepository, FolderRepository
from StAgent.db.repositories.messages import (
    DbMessageRepository,
    MessageRepository,
)
from StAgent.db.repositories.missions import DbMissionRepository, MissionRepository
from StAgent.db.repositories.knowledge_index import (
    DbKnowledgeIndexRepository,
    KnowledgeIndexRepository,
)
from StAgent.db.repositories.tasks import DbTaskRepository, TaskRepository
from StAgent.db.repositories.user_settings import (
    DbUserSettingRepository,
    UserSettingRepository,
)
from StAgent.db.repositories.workflow_executions import (
    DbWorkflowExecutionRepository,
    WorkflowExecutionRepository,
)

if TYPE_CHECKING:
    from StAgent.db.service import DatabaseService


class Repositories:
    """Lazy accessor bundling all per-domain repositories for a database."""

    def __init__(self, db: DatabaseService) -> None:
        self._db = db
        self._files: DbFileRepository | None = None
        self._document_chunks: DbDocumentChunkRepository | None = None
        self._tasks: DbTaskRepository | None = None
        self._chat: DbChatRepository | None = None
        self._checkpoints: DbCheckpointRepository | None = None
        self._workflow_executions: DbWorkflowExecutionRepository | None = None
        self._messages: DbMessageRepository | None = None
        self._flows: DbFlowRepository | None = None
        self._folders: DbFolderRepository | None = None
        self._coding_projects: DbCodingProjectRepository | None = None
        self._complaints: DbComplaintRepository | None = None
        self._knowledge_index: DbKnowledgeIndexRepository | None = None
        self._missions: DbMissionRepository | None = None
        self._user_settings: DbUserSettingRepository | None = None

    @property
    def files(self) -> DbFileRepository:
        if self._files is None:
            self._files = DbFileRepository(self._db)
        return self._files

    @property
    def document_chunks(self) -> DbDocumentChunkRepository:
        if self._document_chunks is None:
            self._document_chunks = DbDocumentChunkRepository(self._db)
        return self._document_chunks

    @property
    def tasks(self) -> DbTaskRepository:
        if self._tasks is None:
            self._tasks = DbTaskRepository(self._db)
        return self._tasks

    @property
    def chat(self) -> DbChatRepository:
        if self._chat is None:
            self._chat = DbChatRepository(self._db)
        return self._chat

    @property
    def checkpoints(self) -> DbCheckpointRepository:
        if self._checkpoints is None:
            self._checkpoints = DbCheckpointRepository(self._db)
        return self._checkpoints

    @property
    def workflow_executions(self) -> DbWorkflowExecutionRepository:
        if self._workflow_executions is None:
            self._workflow_executions = DbWorkflowExecutionRepository(self._db)
        return self._workflow_executions

    @property
    def messages(self) -> DbMessageRepository:
        if self._messages is None:
            self._messages = DbMessageRepository(self._db)
        return self._messages

    @property
    def flows(self) -> DbFlowRepository:
        if self._flows is None:
            self._flows = DbFlowRepository(self._db)
        return self._flows

    @property
    def folders(self) -> DbFolderRepository:
        if self._folders is None:
            self._folders = DbFolderRepository(self._db)
        return self._folders

    @property
    def coding_projects(self) -> DbCodingProjectRepository:
        if self._coding_projects is None:
            self._coding_projects = DbCodingProjectRepository(self._db)
        return self._coding_projects

    @property
    def complaints(self) -> DbComplaintRepository:
        if self._complaints is None:
            self._complaints = DbComplaintRepository(self._db)
        return self._complaints

    @property
    def knowledge_index(self) -> DbKnowledgeIndexRepository:
        if self._knowledge_index is None:
            self._knowledge_index = DbKnowledgeIndexRepository(self._db)
        return self._knowledge_index

    @property
    def missions(self) -> DbMissionRepository:
        if self._missions is None:
            self._missions = DbMissionRepository(self._db)
        return self._missions

    @property
    def user_settings(self) -> DbUserSettingRepository:
        if self._user_settings is None:
            self._user_settings = DbUserSettingRepository(self._db)
        return self._user_settings


__all__ = [
    "Repositories",
    "ChatRepository",
    "DbChatRepository",
    "CheckpointRepository",
    "DbCheckpointRepository",
    "CodingProjectRepository",
    "DbCodingProjectRepository",
    "ComplaintRepository",
    "DbComplaintRepository",
    "FileRepository",
    "DbFileRepository",
    "DocumentChunkRepository",
    "DbDocumentChunkRepository",
    "FlowRepository",
    "DbFlowRepository",
    "FolderRepository",
    "DbFolderRepository",
    "MessageRepository",
    "DbMessageRepository",
    "TaskRepository",
    "DbTaskRepository",
    "WorkflowExecutionRepository",
    "DbWorkflowExecutionRepository",
    "KnowledgeIndexRepository",
    "DbKnowledgeIndexRepository",
    "MissionRepository",
    "DbMissionRepository",
    "UserSettingRepository",
    "DbUserSettingRepository",
]
