"""StAgent Agent SDK — the single, versioned public surface.

Every caller (chat API, background tasks, CLI, workflow nodes, sub-agent
delegation) interacts with the agent stack through this package.  The
public surface is intentionally narrow and semver-versioned so downstream
code can pin and upgrade with confidence.

Quick start::

    from StAgent.sdk import AgentRuntime, AgentBuilder

    runtime = AgentRuntime.from_service_manager(service_manager)
    result = await runtime.run("default_agent", "Summarise this PDF")

    # Define and register a custom agent:
    support = (
        AgentBuilder("support_agent")
        .variant("default_agent")
        .tools(allow=["web_search", "knowledge_*"])
        .build()
    )
    runtime.registry.register(support)

    # Stream events:
    async for event in runtime.stream("support_agent", "Help me"):
        print(event.type, event.data)
"""

# ── Definition / Builder / Registry (re-exported from runtime) ──────────
from StAgent.runtime.builder import AgentBuilder
from StAgent.runtime.context import RuntimeContext
from StAgent.runtime.definition import (
    AgentDefinition,
    MemoryPolicy,
    ModelPolicy,
    ToolPolicy,
)
from StAgent.runtime.registry import (
    AgentRegistry,
    get_agent_registry,
    load_agents_from_yaml,
    register_builtin_agents,
    reset_agent_registry,
)

# ── Runtime facade (re-exported from runtime) ───────────────────────────
from StAgent.runtime.runtime import AgentRuntime, get_delegation_runtime
from StAgent.sdk._version import __version__

# ── Events & Results ────────────────────────────────────────────────────
from StAgent.sdk.events import AgentEvent, AgentEventType, AgentResult

# ── Kernel ───────────────────────────────────────────────────────────────
from StAgent.sdk.kernel.checkpoint import (
    InMemoryCheckpointStore,
    SQLCheckpointStore,
    build_checkpoint_store,
    create_checkpoint,
)
from StAgent.sdk.kernel.loop import run_loop
from StAgent.sdk.kernel.state import RunState

# ── Protocols ───────────────────────────────────────────────────────────
from StAgent.sdk.protocols import (
    AssemblyRequest,
    AssemblyResult,
    Checkpoint,
    CheckpointStore,
    ContextAssembler,
    EpisodicStoreProtocol,
    LLMClient,
    MemoryStore,
    ProceduralStoreProtocol,
    Provider,
    RecallProvider,
    RunContext,
    SemanticStoreProtocol,
    ToolContext,
)

# ── Session ──────────────────────────────────────────────────────────────
from StAgent.sdk.session import AgentSession, ContextInspector, MemoryInspector

__all__ = [
    # version
    "__version__",
    # events
    "AgentEvent",
    "AgentEventType",
    "AgentResult",
    # protocols
    "AssemblyRequest",
    "AssemblyResult",
    "Checkpoint",
    "CheckpointStore",
    "ContextAssembler",
    "EpisodicStoreProtocol",
    "LLMClient",
    "MemoryStore",
    "ProceduralStoreProtocol",
    "Provider",
    "RecallProvider",
    "RunContext",
    "SemanticStoreProtocol",
    "ToolContext",
    # definition
    "AgentDefinition",
    "AgentBuilder",
    "AgentRegistry",
    "ToolPolicy",
    "ModelPolicy",
    "MemoryPolicy",
    "get_agent_registry",
    "load_agents_from_yaml",
    "register_builtin_agents",
    "reset_agent_registry",
    # runtime context
    "RuntimeContext",
    # session
    "AgentSession",
    "ContextInspector",
    "MemoryInspector",
    # kernel
    "InMemoryCheckpointStore",
    "SQLCheckpointStore",
    "build_checkpoint_store",
    "run_loop",
    "RunState",
    "create_checkpoint",
    # runtime facade
    "AgentRuntime",
    "get_delegation_runtime",
]
