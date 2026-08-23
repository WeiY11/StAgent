"""Context management for the agent runtime.

Typed sources, cost-function budgeting, and attachments-first rendering.
The :class:`ContextManager` is the single entry point for per-turn
context assembly.
"""

from StAgent.context.artifact_error_tracker import ArtifactErrorTracker
from StAgent.context.budget import MinimiseResult, minimise
from StAgent.context.cache import SourceCache
from StAgent.context.file_state import FileState
from StAgent.context.ledger import ContextLedger, LedgerRow
from StAgent.context.manager import ContextManager
from StAgent.context.recipe import ContextRecipe, RecipeEntry, get_recipe
from StAgent.context.sources.base import ContextSource, ResolveContext
from StAgent.context.types import (
    AttachmentKind,
    ContextBlock,
    ContextScope,
    EnvironmentSnapshot,
    FileReadRecord,
    ProjectMemoryOrigin,
    ProjectMemorySource,
    RenderTarget,
    TurnContext,
    WorkingSetEntry,
)
from StAgent.context.working_set import WorkingSet

__all__ = [
    "ArtifactErrorTracker",
    "AttachmentKind",
    "ContextBlock",
    "ContextLedger",
    "ContextManager",
    "ContextRecipe",
    "ContextScope",
    "ContextSource",
    "EnvironmentSnapshot",
    "FileReadRecord",
    "FileState",
    "LedgerRow",
    "MinimiseResult",
    "ProjectMemoryOrigin",
    "ProjectMemorySource",
    "RecipeEntry",
    "RenderTarget",
    "ResolveContext",
    "SourceCache",
    "TurnContext",
    "WorkingSet",
    "WorkingSetEntry",
    "get_recipe",
    "minimise",
]
