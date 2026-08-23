"""Database models for StAgent (standalone local deployment)."""

from StAgent.db.models.agent_checkpoint import AgentCheckpoint
from StAgent.db.models.agent_memory import (
    AgentEpisode,
    AgentFact,
    AgentProcedure,
)
from StAgent.db.models.agent_trace import (
    AgentTrace,
    AgentTraceExperiment,
    AgentTraceSpan,
)
from StAgent.db.models.approval_decision import ApprovalDecisionLog
from StAgent.db.models.base import (
    BaseModel,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDMixin,
    utc_now,
)
from StAgent.db.models.canvas import (
    CanvasContentType,
    CanvasDocument,
)
from StAgent.db.models.change_review import ChangeReview
from StAgent.db.models.chat_project import ChatProject
from StAgent.db.models.coding_project import (
    CodingProject,
    CodingProjectCreate,
    CodingProjectRead,
    CodingProjectRuntimeKind,
    CodingProjectStatus,
    CodingProjectUpdate,
)
from StAgent.db.models.complaint import (
    Complaint,
    ComplaintCreate,
    ComplaintRead,
    ComplaintUpdate,
)
from StAgent.db.models.cron import (
    CronExecutionModel,
    CronJobModel,
    CronJobRead,
)
from StAgent.db.models.document_chunk import (
    DocumentChunk,
    DocumentChunkRead,
)
from StAgent.db.models.file import (
    File,
    FileCreate,
    FileRead,
    FileStatus,
    FileType,
    FileUpdate,
    InboxState,
    LibraryScope,
)
from StAgent.db.models.file_content import (
    FileContent,
    FileContentRead,
)
from StAgent.db.models.knowledge_index import (
    KnowledgeIndexJob,
    KnowledgeIndexJobStatus,
    KnowledgeIndexState,
    KnowledgeIndexStateRead,
    KnowledgeIndexStatus,
)
from StAgent.db.models.flow import (
    Flow,
    FlowCreate,
    FlowRead,
    FlowStatus,
    FlowType,
    FlowUpdate,
    FlowVersion,
)
from StAgent.db.models.folder import (
    Folder,
    FolderCreate,
    FolderProjectUpdate,
    FolderRead,
    FolderUpdate,
)
from StAgent.db.models.identity_stub import (
    UserStub,
    WorkspaceStub,
)
from StAgent.db.models.knowledge_acl import (
    KnowledgeAccessScope,
    KnowledgeDocumentGrant,
    KnowledgeSpace,
    KnowledgeSpaceCreate,
    KnowledgeSpaceMember,
    KnowledgeSpaceMemberRead,
    KnowledgeSpaceMemberRole,
    KnowledgeSpaceRead,
    WorkspaceMember,
    WorkspaceMemberRole,
)
from StAgent.db.models.llm_request_log import LLMRequestLog
from StAgent.db.models.message import (
    ChatSession,
    Message,
    MessageCreate,
    MessageRead,
    MessageRole,
    MessageStatus,
    SessionCreate,
    SessionRead,
)
from StAgent.db.models.mission import (
    Mission,
    MissionAutonomy,
    MissionStep,
    MissionStepKind,
    MissionStepStatus,
    MissionStatus,
    MissionVerifyLevel,
)
from StAgent.db.models.pet_project import (
    PetProject,
    PetProjectFile,
)
from StAgent.db.models.task import (
    Task,
    TaskContext,
    TaskCreate,
    TaskPriority,
    TaskRead,
    TaskStatus,
    TaskType,
    TaskUpdate,
    create_task_state_base,
    generate_task_id,
    is_terminal_task_status,
)
from StAgent.db.models.user_setting import UserSetting
from StAgent.db.models.workflow_execution import (
    WorkflowExecution,
    WorkflowExecutionRead,
)
from StAgent.db.models.workflow_state_snapshot import WorkflowStateSnapshot

__all__ = [
    # Agent checkpoints
    "AgentCheckpoint",
    # Approval audit
    "ApprovalDecisionLog",
    # Change reviews (worktree review queue)
    "ChangeReview",
    # Agent memory
    "AgentEpisode",
    "AgentFact",
    "AgentProcedure",
    # Identity stubs
    "UserStub",
    "WorkspaceStub",
    # Knowledge ACL
    "KnowledgeAccessScope",
    "KnowledgeDocumentGrant",
    "KnowledgeSpace",
    "KnowledgeSpaceCreate",
    "KnowledgeSpaceMember",
    "KnowledgeSpaceMemberRead",
    "KnowledgeSpaceMemberRole",
    "KnowledgeSpaceRead",
    "WorkspaceMember",
    "WorkspaceMemberRole",
    # Cron
    "CronJobModel",
    "CronExecutionModel",
    "CronJobRead",
    # Workflow execution
    "WorkflowExecution",
    "WorkflowExecutionRead",
    "WorkflowStateSnapshot",
    # Base
    "BaseModel",
    "TimestampMixin",
    "UUIDMixin",
    "SoftDeleteMixin",
    "utc_now",
    # Flow
    "Flow",
    "FlowVersion",
    "FlowStatus",
    "FlowType",
    "FlowCreate",
    "FlowUpdate",
    "FlowRead",
    # Canvas
    "CanvasContentType",
    "CanvasDocument",
    # Document chunks (knowledge storage layer)
    "DocumentChunk",
    "DocumentChunkRead",
    # Message
    "Message",
    "ChatSession",
    "MessageRole",
    "MessageStatus",
    "MessageCreate",
    "MessageRead",
    "SessionCreate",
    "SessionRead",
    "LLMRequestLog",
    # Agent traces (debug/eval)
    "AgentTrace",
    "AgentTraceSpan",
    "AgentTraceExperiment",
    # Mission (goal-scoped autonomous execution)
    "Mission",
    "MissionStep",
    "MissionStatus",
    "MissionAutonomy",
    "MissionVerifyLevel",
    "MissionStepKind",
    "MissionStepStatus",
    # Task
    "Task",
    "TaskContext",
    "TaskStatus",
    "TaskPriority",
    "TaskType",
    "TaskCreate",
    "TaskUpdate",
    "TaskRead",
    "is_terminal_task_status",
    "generate_task_id",
    "create_task_state_base",
    # File
    "File",
    "FileType",
    "FileStatus",
    "FileCreate",
    "FileUpdate",
    "FileRead",
    "LibraryScope",
    "InboxState",
    # Knowledge indexing lifecycle
    "KnowledgeIndexJob",
    "KnowledgeIndexJobStatus",
    "KnowledgeIndexState",
    "KnowledgeIndexStateRead",
    "KnowledgeIndexStatus",
    # Pet Space
    "PetProject",
    "PetProjectFile",
    # Chat Projects
    "ChatProject",
    # Folder
    "Folder",
    "FolderCreate",
    "FolderUpdate",
    "FolderProjectUpdate",
    "FolderRead",
    # Coding Project
    "CodingProject",
    "CodingProjectCreate",
    "CodingProjectRead",
    "CodingProjectRuntimeKind",
    "CodingProjectStatus",
    "CodingProjectUpdate",
    # Complaint
    "Complaint",
    "ComplaintCreate",
    "ComplaintUpdate",
    "ComplaintRead",
    # User settings (per-user key/value store)
    "UserSetting",
]
