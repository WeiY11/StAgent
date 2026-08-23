"""Agent memory package for StAgent.

This package hosts the cognitive memory stack used by the agent runtime —
**not** the conversation transcript (that lives in
:mod:`StAgent.services.session`).

The module is organised around three kinds of long-lived memory, plus a
short-lived scratchpad and a retrieval pipeline:

* :class:`~StAgent.memory.episodic.EpisodicStore` — "what happened in
  past turns": one row per user/assistant turn with a distilled summary.
* :class:`~StAgent.memory.semantic.SemanticStore` — stable facts and
  preferences the agent should remember about the user / workspace.
* :class:`~StAgent.memory.procedural.ProceduralStore` — outcomes of
  structured procedures (tool chains, workflows) keyed by a canonical
  signature, so the agent can re-use what worked before.
* :class:`~StAgent.memory.working_scratchpad.WorkingScratchpad` —
  ephemeral, per-task Redis scratchpad (tool history, reasoning notes).
* :class:`~StAgent.memory.recall.RetrievalPipeline` — hybrid
  semantic / lexical recall over the three cognitive stores.
* :class:`~StAgent.memory.agent_memory.AgentMemory` — the public façade
  the agent runtime consumes. Everything else is an implementation detail.

All of the above are built incrementally by the tasks in the
``session-manager-redis-upgrade`` plan. This ``__init__`` re-exports them
as they land so downstream callers keep a single import site.
"""

from __future__ import annotations

from StAgent.memory.agent_memory import AgentMemory, RecallHandle
from StAgent.memory.embeddings import (
    EmbeddingProvider,
    LLMServiceEmbeddingProvider,
    NullEmbeddingProvider,
)
from StAgent.memory.episodic import EpisodicStore
from StAgent.memory.formation import (
    FormationDecision,
    FormationPolicy,
    FormationTarget,
    TriggerKind,
    TurnObservation,
)
from StAgent.memory.procedural import ProceduralStore, build_signature
from StAgent.memory.protocols import RecallProvider
from StAgent.memory.recall import RecallOptions, RetrievalPipeline
from StAgent.memory.semantic import SemanticStore
from StAgent.memory.types import (
    Episode,
    Fact,
    MemoryKind,
    Procedure,
    RecallBundle,
    RecallEntry,
)
from StAgent.memory.working_scratchpad import ToolInvocation, WorkingScratchpad

__all__ = [
    "AgentMemory",
    "EmbeddingProvider",
    "Episode",
    "EpisodicStore",
    "Fact",
    "FormationDecision",
    "FormationPolicy",
    "FormationTarget",
    "LLMServiceEmbeddingProvider",
    "MemoryKind",
    "NullEmbeddingProvider",
    "Procedure",
    "ProceduralStore",
    "RecallBundle",
    "RecallEntry",
    "RecallHandle",
    "RecallOptions",
    "RecallProvider",
    "RetrievalPipeline",
    "SemanticStore",
    "ToolInvocation",
    "TriggerKind",
    "TurnObservation",
    "WorkingScratchpad",
    "build_signature",
]

from StAgent.memory.fake import (
    FakeAgentMemory,
    FakeEpisodicStore,
    FakeProceduralStore,
    FakeSemanticStore,
)

__all__ += [
    "FakeAgentMemory",
    "FakeEpisodicStore",
    "FakeProceduralStore",
    "FakeSemanticStore",
]

try:
    from StAgent.memory.compact import build_autocompact, build_microcompact

    __all__ += ["build_autocompact", "build_microcompact"]
except ImportError:  # pragma: no cover
    pass
