"""Centralised service lifecycle manager"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from StAgent.services.base import ServiceState

if TYPE_CHECKING:
    from StAgent.config.settings import Settings
    from StAgent.cron.manager import CronManager
    from StAgent.cron.repository import JobRepository
    from StAgent.db.service import DatabaseService
    from StAgent.file.service import FileService
    from StAgent.llm.service import LLMService
    from StAgent.mcp.manager import MCPClientManager
    from StAgent.memory.agent_memory import AgentMemory
    from StAgent.memory.embeddings import EmbeddingProvider
    from StAgent.project.manager import CodingProjectManager
    from StAgent.runtime.context import RuntimeContext
    from StAgent.services.cache.service import CacheService
    from StAgent.services.canvas.service import CanvasService
    from StAgent.services.chat.service import ChatService
    from StAgent.services.event.manager import EventManager
    from StAgent.services.event.webhook import WebhookEventManager
    from StAgent.services.file_processing.service import FileProcessingService
    from StAgent.services.session.manager import SessionManager
    from StAgent.services.task_manager import TaskManager
    from StAgent.services.variable.service import VariableService
    from StAgent.workflow.services import WorkflowService

logger = logging.getLogger(__name__)


class ServiceManager:
    """Creates, starts, and stops all application services.

    Standalone local mode — no Redis, no Milvus, no MinIO, no auth.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        self._db: DatabaseService | None = None
        self._cache: CacheService | None = None
        self._redis: Any = None
        self._redis_client: Any = None
        self._redis_run_store_installed = False
        self._chat: ChatService | None = None
        self._canvas: CanvasService | None = None
        self._coding_projects: CodingProjectManager | None = None
        self._variable: VariableService | None = None
        self._event: EventManager | None = None
        self._webhook: WebhookEventManager | None = None

        self._llm: Any = None
        self._llm_service: LLMService | None = None
        self._mcp_manager: MCPClientManager | None = None
        self._cron: CronManager | None = None
        self._workflow_service: WorkflowService | None = None
        self._workflow_event_bus: Any = None
        self._file_service: FileService | None = None
        self._file_store: Any = None
        self._minio: Any = None
        self._file_processing: FileProcessingService | None = None
        self._rule_engine: Any = None
        self._task_manager: TaskManager | None = None
        self._session_manager: SessionManager | None = None
        self._agent_memory: AgentMemory | None = None
        self._runtime_context: RuntimeContext | None = None
        self._channel_manager: Any = None
        self._knowledge_embedding_status: Any = None
        self._knowledge_semantic_state: Any = None
        self._knowledge_semantic_rebuilder: Any = None
        self._knowledge_semantic_auto_rebuild = False
        self._knowledge_semantic_enqueue_lock = asyncio.Lock()
        self._knowledge_gc_task: asyncio.Task[None] | None = None

        self._started = False

    @property
    def db(self) -> DatabaseService | None:
        return self._db

    @property
    def auth(self) -> Any:
        return None

    @property
    def cache(self) -> CacheService | None:
        return self._cache

    @property
    def chat(self) -> ChatService | None:
        return self._chat

    @property
    def canvas(self) -> CanvasService | None:
        return self._canvas

    @property
    def variable(self) -> VariableService | None:
        return self._variable

    @property
    def coding_projects(self) -> CodingProjectManager | None:
        return self._coding_projects

    @property
    def job_queue(self) -> Any:
        return None

    @property
    def file_store(self) -> Any:
        return self._file_store

    @property
    def event(self) -> EventManager | None:
        return self._event

    @property
    def webhook(self) -> WebhookEventManager | None:
        return self._webhook

    @property
    def redis(self) -> Any:
        return self._redis

    @property
    def redis_client(self) -> Any:
        return self._redis_client

    @property
    def database_service(self) -> Any:
        return self._db

    @property
    def minio(self) -> Any:
        return self._minio

    @property
    def milvus(self) -> Any:
        return None

    @property
    def llm(self) -> Any:
        return self._llm

    @property
    def llm_service(self) -> LLMService | None:
        return self._llm_service

    @property
    def knowledge_semantic_rebuilder(self) -> Any:
        """Return the local semantic rebuild coordinator, if one is wired."""
        return self._knowledge_semantic_rebuilder

    @property
    def knowledge_semantic_status(self) -> dict[str, Any]:
        """Secret-free local semantic status for settings and knowledge UI."""
        if self._knowledge_semantic_state is not None:
            return cast("dict[str, Any]", self._knowledge_semantic_state.to_dict())
        semantic = self._effective_knowledge_semantic_config()
        return {
            "status": "off" if not bool(getattr(semantic, "enabled", False)) else "lexical_only",
            "active_version": None,
            "candidate_version": None,
            "model_id": getattr(semantic, "local_model_id", "") or None,
            "task_id": None,
            "task_progress": None,
            "task_progress_message": None,
            "document_count": 0,
            "chunk_count": 0,
            "capacity_warning": False,
            "error": None,
            "environment_managed": bool(getattr(semantic, "environment_managed", False)),
        }

    def _effective_knowledge_semantic_config(self) -> Any:
        """Overlay editable ``config.yaml`` unless an environment owns it."""
        semantic = self.settings.knowledge.semantic
        try:
            from StAgent.config.config import load_config

            persisted = load_config().knowledge.semantic
            values = {
                "enabled": persisted.enabled,
                "vector_backend": persisted.vector_backend,
                "local_model_id": persisted.local_model_id,
                "auto_reindex": persisted.auto_reindex,
            }
            env_fields = {
                "enabled": (
                    "StAgent_KNOWLEDGE__SEMANTIC__ENABLED",
                    "StAgent_KNOWLEDGE_SEMANTIC_ENABLED",
                ),
                "vector_backend": (
                    "StAgent_KNOWLEDGE__SEMANTIC__VECTOR_BACKEND",
                    "StAgent_KNOWLEDGE_SEMANTIC_VECTOR_BACKEND",
                ),
                "local_model_id": (
                    "StAgent_KNOWLEDGE__SEMANTIC__LOCAL_MODEL_ID",
                    "StAgent_KNOWLEDGE_SEMANTIC_LOCAL_MODEL_ID",
                ),
                "auto_reindex": (
                    "StAgent_KNOWLEDGE__SEMANTIC__AUTO_REINDEX",
                    "StAgent_KNOWLEDGE_SEMANTIC_AUTO_REINDEX",
                ),
            }
            managed = False
            for field, names in env_fields.items():
                if any(name in os.environ for name in names):
                    values[field] = getattr(semantic, field)
                    managed = True
            return type(
                "EffectiveSemanticConfig",
                (),
                {**values, "environment_managed": managed},
            )()
        except Exception:  # noqa: BLE001 - defaults preserve existing startup behavior
            return semantic

    @property
    def knowledge_retrieval_status(self) -> dict[str, Any]:
        """Return a secret-free summary of the optional RAG backends.

        A lexical-only deployment is a supported configuration.  The vector
        reason therefore explains whether the semantic plane is intentionally
        disabled, ready, or unavailable, without exposing credentials or
        endpoint URLs.
        """
        cfg = self.settings.knowledge
        vector = (
            self._knowledge_embedding_status.to_dict()
            if self._knowledge_embedding_status is not None
            else {
                "enabled": bool(getattr(cfg, "milvus_enabled", False) and getattr(cfg, "embedding_enabled", False)),
                "provider": getattr(cfg, "embedding_provider", "") or None,
                "model": getattr(cfg, "embedding_model", "") or None,
                "dimension": int(getattr(cfg, "embedding_dimension", 1024) or 1024),
                "reason": "not_initialized",
            }
        )
        return {
            "query_rewrite_enabled": bool(getattr(cfg, "query_rewrite_enabled", False)),
            "milvus_enabled": bool(getattr(cfg, "milvus_enabled", False)),
            "vector": vector,
            "opensearch_configured": bool(getattr(cfg, "opensearch_url", "")),
            "rerank_configured": bool(getattr(cfg, "rerank_endpoint", "")),
            "semantic": self.knowledge_semantic_status,
        }

    async def reload_llm_service(self) -> None:
        if self._llm_service is None:
            return
        try:
            from StAgent.llm.provider_config import reset_provider_config_service

            reset_provider_config_service()
            # Art image-generation stack reads the same providers.yaml; reset
            # its config + backend service so credential/preset edits apply
            # without a restart.
            try:
                from StAgent.llm.generation import reset_image_gen_config
                from StAgent.llm.generation.service import reset_generation_service

                reset_image_gen_config()
                reset_generation_service()
            except Exception:  # noqa: BLE001 - art stack reload is best-effort
                logger.debug("generation service reload skipped", exc_info=True)
            self._llm_service.reload()
            # Provider changes can alter whether the optional semantic route
            # is usable, so rebuild only the lightweight retrieval wiring.
            await self._start_knowledge_retrieval()
            logger.info(
                "LLMService reloaded with %d provider(s): %s",
                len(self._llm_service.list_providers()),
                self._llm_service.list_providers(),
            )
        except Exception:
            logger.warning("LLMService reload failed", exc_info=True)

    @property
    def mcp_manager(self) -> MCPClientManager | None:
        return self._mcp_manager

    @property
    def cron(self) -> CronManager | None:
        return self._cron

    @property
    def workflow_service(self) -> WorkflowService | None:
        return self._workflow_service

    @property
    def runtime_context(self) -> RuntimeContext:
        """Process-wide :class:`RuntimeContext` (lazy singleton).

        Central wiring point for agent execution: tool executor, hooks,
        checkpoint store, session manager, and LLM. Callers should prefer
        this over hand-constructing :class:`ToolExecutor` instances.
        """
        if self._runtime_context is None:
            from StAgent.runtime.context import RuntimeContext

            self._runtime_context = RuntimeContext.from_service_manager(self)
        return self._runtime_context

    @property
    def file_service(self) -> FileService | None:
        return self._file_service

    @property
    def file_processing(self) -> FileProcessingService | None:
        return self._file_processing

    @property
    def rule_engine(self) -> Any:
        return self._rule_engine

    @property
    def task_manager(self) -> TaskManager | None:
        return self._task_manager

    @property
    def task_registry(self) -> Any:
        return None

    @property
    def session_manager(self) -> SessionManager | None:
        return self._session_manager

    @property
    def agent_memory(self) -> AgentMemory | None:
        return self._agent_memory

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def channel_manager(self) -> Any:
        return self._channel_manager

    async def start_all(self) -> None:
        if self._started:
            logger.warning("Services already started")
            return

        logger.info("Starting services...")

        # Defense in depth: a launcher should already have validated the
        # database/worker combination, but re-check here so the process refuses
        # to bring up services under an unsafe topology (e.g. SQLite + workers>1).
        from StAgent.runtime.deployment_topology import validate_deployment_topology

        topology = validate_deployment_topology(
            database_backend=self.settings.database.backend,
            workers=self.settings.workers,
            redis_enabled=self.settings.redis.enabled,
        )
        for warning in topology.warnings:
            logger.warning("deployment_topology_warning: %s", warning)

        await self._start_database()
        await self._start_event()
        await self._start_cache()
        await self._start_chat()
        await self._start_canvas()
        await self._start_coding_projects()
        await self._start_variable()
        await self._start_llm()
        await self._start_knowledge_retrieval()
        await self._start_file_service()
        await self._start_session_manager()
        await self._start_agent_memory()
        await self._start_rules()
        await self._start_workflow_service()
        await self._start_cron()
        await self._start_task_manager()
        await self._start_file_processing()
        self._start_knowledge_gc()
        await self._start_channels()

        self._started = True
        logger.info("All services started successfully")

    async def stop_all(self) -> None:
        logger.info("Stopping services...")

        await self._stop_knowledge_gc()
        await self._stop_channels()

        if self._task_manager is not None:
            try:
                await self._task_manager.shutdown()
            except Exception:
                logger.warning("Task manager shutdown error", exc_info=True)

        if self._cron:
            try:
                await self._cron.stop()
            except Exception:
                logger.warning("Cron manager shutdown error", exc_info=True)

        if self._mcp_manager is not None:
            try:
                await self._mcp_manager.shutdown()
            except Exception:
                logger.warning("MCP manager shutdown error", exc_info=True)

        if self._webhook:
            try:
                await self._webhook.stop()
            except Exception:
                logger.warning("Webhook service shutdown error", exc_info=True)

        if self._coding_projects:
            try:
                from StAgent.project.manager import (
                    shutdown_coding_projects_service,
                )

                await shutdown_coding_projects_service()
            except Exception:
                logger.warning("Coding-projects service shutdown error", exc_info=True)

        if self._chat:
            try:
                await self._chat.stop()
            except Exception:
                logger.warning("Chat service shutdown error", exc_info=True)

        if self._llm_service:
            try:
                await self._llm_service.aclose()
            except Exception:
                logger.warning("LLM service shutdown error", exc_info=True)

        if self._minio is not None:
            try:
                close = getattr(self._minio, "close", None)
                if callable(close):
                    close()
            except Exception:
                logger.warning("Object storage client shutdown error", exc_info=True)
            finally:
                self._minio = None
                self._file_store = None

        if self._event:
            try:
                await self._event.stop()
            except Exception:
                logger.warning("Event service shutdown error", exc_info=True)

        if self._cache:
            try:
                await self._cache.stop()
            except Exception:
                logger.warning("Cache service shutdown error", exc_info=True)
        await self._shutdown_redis_runtime()

        if self._db:
            try:
                await self._db.dispose()
            except Exception:
                logger.warning("Database shutdown error", exc_info=True)

        self._started = False
        logger.info("All services stopped")

    async def _shutdown_redis_runtime(self) -> None:
        """Release shared-run infrastructure without changing the default store."""
        if self._redis_run_store_installed:
            from StAgent.runtime.run_store import reset_run_store

            reset_run_store()
            self._redis_run_store_installed = False
        if self._cache is None and self._redis is not None:
            try:
                close = getattr(self._redis, "aclose", None) or getattr(self._redis, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                logger.warning("Redis async client shutdown error", exc_info=True)
        if self._redis_client is not None:
            try:
                close = getattr(self._redis_client, "close", None)
                if callable(close):
                    close()
            except Exception:
                logger.warning("Redis sync client shutdown error", exc_info=True)
        self._redis = None
        self._redis_client = None

    async def health_check(self) -> dict[str, Any]:
        results: dict[str, Any] = {"healthy": True, "services": {}}
        services = [
            ("database", self._db),
            ("cache", self._cache),
            ("chat", self._chat),
            ("variable", self._variable),
            ("event", self._event),
        ]
        for name, service in services:
            if service is None:
                results["services"][name] = {"status": "not_initialized"}
                continue
            try:
                if hasattr(service, "health_check"):
                    check = await service.health_check()
                    results["services"][name] = check
                elif hasattr(service, "state"):
                    results["services"][name] = {
                        "status": service.state.value,
                        "healthy": service.state == ServiceState.READY,
                    }
                else:
                    results["services"][name] = {"status": "ok"}
            except Exception as e:
                results["services"][name] = {
                    "status": "error",
                    "error": str(e),
                    "healthy": False,
                }
                results["healthy"] = False
        knowledge = self.knowledge_retrieval_status
        vector_reason = str(knowledge["vector"].get("reason") or "not_initialized")
        results["services"]["knowledge_retrieval"] = {
            "status": ("ready" if vector_reason in {"ready", "disabled"} else vector_reason),
            # BM25 remains available when the optional vector plane is not.
            "healthy": True,
            **knowledge,
        }
        return results

    async def _start_database(self) -> None:
        try:
            from StAgent.db.service import init_database_service

            self._db = init_database_service(self.settings)
            await self._db.create_tables()
            logger.info(
                "Database service initialized (%s)",
                self.settings.database.backend,
            )
        except Exception:
            logger.exception("Database initialization failed")
            raise

    async def _start_event(self) -> None:
        try:
            from StAgent.services.event.manager import init_event_manager

            self._event = await init_event_manager(
                self.settings,
                keep_history=self.settings.debug,
                max_history=1000,
            )
            logger.info("Event manager initialized")
        except Exception:
            logger.warning("Event manager initialization skipped", exc_info=True)

    async def _start_cache(self) -> None:
        from StAgent.services.cache.service import init_cache_service

        if self.settings.redis.enabled:
            from StAgent.services.cache.redis_backend import build_redis_clients

            try:
                self._redis, self._redis_client = await build_redis_clients(self.settings.redis)
                self._cache = await init_cache_service(
                    self.settings,
                    redis_client=self._redis,
                    redis_prefix=self.settings.redis.prefix,
                )
            except Exception:
                logger.exception("Configured Redis runtime initialization failed")
                await self._shutdown_redis_runtime()
                raise
            if self.settings.workers > 1:
                from StAgent.runtime.redis_run_store import build_redis_run_store
                from StAgent.runtime.run_store import set_run_store

                set_run_store(
                    build_redis_run_store(
                        self._redis_client,
                        prefix=f"{self.settings.redis.prefix}:runs",
                        ttl_seconds=self.settings.redis.run_store_ttl_seconds,
                    )
                )
                self._redis_run_store_installed = True
        else:
            self._cache = await init_cache_service(self.settings)
        logger.info(
            "CacheService initialized (%s, max_items=%d)",
            "memory+redis" if self._cache.using_redis else "memory",
            self.settings.cache.max_items,
        )

    async def _start_knowledge_retrieval(self) -> None:
        """Wire lexical RAG plus either explicit local or legacy Milvus vectors."""
        from StAgent.knowledge.vector_index import set_knowledge_vector_index

        # The singleton may survive an in-process service reload.  Clear it
        # before resolving today's configuration so a prior vector backend
        # cannot remain active after semantic retrieval is disabled.
        set_knowledge_vector_index(None)
        self._knowledge_embedding_status = None
        self._knowledge_semantic_state = None
        self._knowledge_semantic_rebuilder = None
        self._knowledge_semantic_auto_rebuild = False
        if self._db is None:
            return
        try:
            from StAgent.library.retrieval import (
                KnowledgeRetrievalPipeline,
                set_knowledge_retrieval_pipeline,
            )

            cfg = self.settings.knowledge
            query_rewriter = None
            if cfg.query_rewrite_llm_enabled and self._llm_service is not None:
                from StAgent.knowledge.query import LLMQueryRewriter

                query_rewriter = LLMQueryRewriter(self._llm_service)
            semantic = self._effective_knowledge_semantic_config()
            local_enabled = bool(getattr(semantic, "enabled", False))
            # Wire BM25/FTS5 before optional vector initialization so a model,
            # extension, or sidecar failure can never remove lexical fallback.
            set_knowledge_retrieval_pipeline(
                KnowledgeRetrievalPipeline(
                    self._db,
                    cache=self._cache,
                    query_rewriter=query_rewriter,
                )
            )
            if local_enabled:
                await self._start_local_semantic_retrieval(semantic)
            else:
                # Milvus retains its existing explicit provider route.  Local
                # semantic mode never consults this resolver and therefore
                # cannot accidentally send embeddings to DeepSeek/chat LLMs.
                from StAgent.knowledge.embedding_runtime import resolve_knowledge_embedding_runtime

                embedding_runtime = resolve_knowledge_embedding_runtime(
                    self._llm_service,
                    cache=self._cache,
                    enabled=bool(getattr(cfg, "milvus_enabled", False) and getattr(cfg, "embedding_enabled", False)),
                    provider=getattr(cfg, "embedding_provider", ""),
                    model=getattr(cfg, "embedding_model", ""),
                    dimension=int(getattr(cfg, "embedding_dimension", 1024) or 1024),
                )
                self._knowledge_embedding_status = embedding_runtime.status
                if cfg.milvus_enabled and embedding_runtime.provider is not None:
                    from StAgent.knowledge.vector_index import KnowledgeVectorIndex
                    from StAgent.memory.vector import MilvusConnectionConfig

                    connection = MilvusConnectionConfig(
                        enabled=True,
                        host=cfg.milvus_host,
                        port=cfg.milvus_port,
                        uri=cfg.milvus_uri or None,
                        token=cfg.milvus_token or None,
                    )
                    set_knowledge_vector_index(
                        KnowledgeVectorIndex(
                            embeddings=embedding_runtime.provider,
                            connection=connection,
                            index_version=cfg.index_version,
                        )
                    )
            logger.info(
                "Knowledge retrieval initialized (opensearch=%s, milvus=%s, vector=%s, rerank=%s)",
                bool(cfg.opensearch_url),
                bool(cfg.milvus_enabled),
                self.knowledge_semantic_status["status"] if local_enabled else self._knowledge_embedding_status.reason,
                bool(cfg.rerank_endpoint),
            )
        except Exception:
            logger.warning("Knowledge retrieval initialization skipped", exc_info=True)

    async def _start_local_semantic_retrieval(self, semantic: Any) -> None:
        """Resolve only an administrator-selected offline managed model."""
        from StAgent.config.constants import StAgent_HOME
        from StAgent.knowledge.local_models import LocalModelRegistry
        from StAgent.knowledge.semantic_runtime import (
            KnowledgeSemanticRebuilder,
            SemanticRuntimeState,
            redact_semantic_error,
        )
        from StAgent.knowledge.sqlite_vec_index import SqliteVecKnowledgeIndex
        from StAgent.knowledge.vector_index import set_knowledge_vector_index
        from StAgent.memory.embeddings import LocalSentenceTransformerEmbeddingProvider

        state = SemanticRuntimeState(
            mode="lexical_only",
            model_id=str(getattr(semantic, "local_model_id", "") or "") or None,
            model_fingerprint=None,
            environment_managed=bool(getattr(semantic, "environment_managed", False)),
        )
        self._knowledge_semantic_state = state
        if self.settings.workers > 1:
            state.error = "local_backend_requires_single_worker_use_milvus"
            return
        backend_name = str(getattr(semantic, "vector_backend", "sqlite-vec") or "").strip().lower().replace("_", "-")
        if backend_name not in {"sqlite-vec", "sqlitevec", "local"}:
            state.error = "local_vector_backend_must_be_sqlite_vec"
            return
        if not state.model_id:
            state.error = "local_model_not_selected"
            return
        home = Path(os.getenv("StAgent_HOME", str(StAgent_HOME))).expanduser()
        try:
            registry = LocalModelRegistry(home)
            record = registry.get(state.model_id)
            if record is None or record.status != "ready" or not record.dimension:
                state.error = "local_model_not_ready"
                return
            state.model_fingerprint = record.fingerprint
            model_path = registry.models_dir / record.model_id
            if not model_path.is_dir() or model_path.is_symlink():
                state.error = "local_model_not_ready"
                return
            provider = LocalSentenceTransformerEmbeddingProvider(
                model_path,
                dimension=record.dimension,
                query_prompt=record.query_prompt,
                document_prompt=record.document_prompt,
                normalize=record.normalize,
            )
            # ``SqliteVecKnowledgeIndex`` has no model loading side effect.
            # Capture sidecar existence before construction to distinguish a
            # fresh opt-in installation from a persisted active projection.
            sidecar_existed = (home / "knowledge" / "vectors.sqlite3").exists()
            expected = f"semantic-{record.model_id.replace('local-', '')[:16]}-{record.fingerprint[:12]}"
            index = SqliteVecKnowledgeIndex(
                home,
                dimension=record.dimension,
                embeddings=provider,
                index_version=expected,
            )
            if not index.available:
                # A malformed sidecar header is recoverable projection damage,
                # not a reason to disable lexical retrieval. Preserve it for
                # diagnosis, then make a fresh empty sidecar that the durable
                # candidate task can rebuild. Missing sqlite-vec itself does
                # not match this branch and remains a clear degraded state.
                sidecar = home / "knowledge" / "vectors.sqlite3"
                extension_available = False
                try:
                    import sqlite_vec  # noqa: F401

                    extension_available = True
                except Exception:
                    # A missing/broken optional extension is a dependency
                    # state, not evidence that the user's sidecar is corrupt.
                    pass
                if sidecar.exists() and extension_available:
                    # A valid SQLite header does not guarantee a valid
                    # sqlite-vec payload. Preserve the complete sidecar set
                    # before retrying with a fresh projection.
                    quarantined = self._quarantine_vector_sidecar(sidecar)
                    if quarantined:
                        sidecar_existed = False
                        index = SqliteVecKnowledgeIndex(
                            home,
                            dimension=record.dimension,
                            embeddings=provider,
                            index_version=expected,
                        )
                        if index.available:
                            state.error = "sqlite_vec_sidecar_quarantined_rebuild_required"
                if not index.available:
                    state.mode = "degraded"
                    state.error = "sqlite_vec_unavailable"
                    return
            versions = await index.version_status()
            state.active_version = index.index_version
            persisted_candidate = next((key for key, value in versions.items() if value == "candidate"), None)
            state.candidate_version = persisted_candidate
            model_marker = f"semantic-{record.model_id.replace('local-', '')[:16]}-"
            active_matches_model = index.index_version == expected or index.index_version.startswith(model_marker)
            active_backend: Any = index
            active_can_serve = active_matches_model
            if not active_matches_model:
                # Preserve semantic service during a model switch when the
                # old managed model is still installed.  The active reader
                # gets its own offline provider; candidate writes use the new
                # model and never contaminate the old vector space.
                old_record = next(
                    (
                        candidate
                        for candidate in registry.list()
                        if candidate.status == "ready"
                        and candidate.dimension
                        and (
                            candidate.fingerprint[:12] in index.index_version
                            or index.index_version.startswith(
                                f"semantic-{candidate.model_id.replace('local-', '')[:16]}-"
                            )
                        )
                    ),
                    None,
                )
                if old_record is not None:
                    old_path = registry.models_dir / old_record.model_id
                    if old_path.is_dir() and not old_path.is_symlink():
                        old_provider = LocalSentenceTransformerEmbeddingProvider(
                            old_path,
                            dimension=int(old_record.dimension or 0),
                            query_prompt=old_record.query_prompt,
                            document_prompt=old_record.document_prompt,
                            normalize=old_record.normalize,
                        )
                        active_backend = SqliteVecKnowledgeIndex(
                            home,
                            dimension=int(old_record.dimension or 0),
                            embeddings=old_provider,
                            index_version=index.index_version,
                        )
                        active_can_serve = active_backend.available
            state.mode = "ready" if active_matches_model else "building"
            await self._refresh_semantic_counts(state)
            needs_rebuild = not sidecar_existed or not active_matches_model or bool(persisted_candidate)
            self._knowledge_semantic_auto_rebuild = bool(getattr(semantic, "auto_reindex", True) and needs_rebuild)
            if self._knowledge_semantic_auto_rebuild:
                state.mode = "building"
            elif not sidecar_existed:
                state.mode = "lexical_only"
                state.error = state.error or "semantic_rebuild_required"
            if active_can_serve:
                set_knowledge_vector_index(active_backend)

            def _replace_index(value: Any) -> None:
                set_knowledge_vector_index(value)

            self._knowledge_semantic_rebuilder = KnowledgeSemanticRebuilder(
                cast("DatabaseService", self._db),
                index,
                state,
                on_index_changed=_replace_index,
                active_backend=active_backend,
                serve_active=active_can_serve,
            )
        except Exception as exc:  # noqa: BLE001 - lexical retrieval remains valid
            state.mode = "degraded"
            state.error = redact_semantic_error(exc)
            logger.warning("local_semantic_startup_degraded", exc_info=True)

    @staticmethod
    def _quarantine_vector_sidecar(sidecar: Path) -> bool:
        """Move a damaged sidecar and SQLite journal siblings out of service."""
        if not sidecar.exists() or sidecar.is_symlink():
            return False
        marker = f"{int(time.time())}-{uuid.uuid4().hex[:10]}"
        destination = sidecar.with_name(f"{sidecar.name}.corrupt-{marker}")
        moved = False
        moved_files: list[tuple[Path, Path]] = []
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{sidecar}{suffix}")
            if source.is_symlink():
                logger.warning("semantic_sidecar_quarantine_refused_symlink", extra={"path": str(source)})
                return False
            if not source.exists():
                continue
            target = Path(f"{destination}{suffix}")
            try:
                os.replace(source, target)
            except OSError:
                for original, staged in reversed(moved_files):
                    try:
                        os.replace(staged, original)
                    except OSError:
                        logger.error("semantic_sidecar_quarantine_rollback_failed", exc_info=True)
                logger.warning("semantic_sidecar_quarantine_failed", exc_info=True)
                return False
            moved_files.append((source, target))
            moved = True
        return moved

    async def _refresh_semantic_counts(self, state: Any) -> None:
        """Read durable SQL corpus counts without consulting the sidecar."""
        if self._db is None:
            return
        try:
            from sqlalchemy import func
            from sqlmodel import select

            from StAgent.db.models.document_chunk import DocumentChunk

            async with self._db.session() as session:
                state.chunk_count = int((await session.exec(select(func.count(DocumentChunk.id)))).one() or 0)  # type: ignore[arg-type]
                state.capacity_warning = state.chunk_count >= 100_000
                state.document_count = int(
                    (await session.exec(select(func.count(func.distinct(DocumentChunk.file_id))))).one() or 0
                )
        except Exception:  # noqa: BLE001 - status still remains safe at zero
            logger.debug("semantic_corpus_count_skipped", exc_info=True)

    async def enqueue_knowledge_vector_rebuild(
        self,
        *,
        candidate_version: str | None = None,
        user_id: Any = None,
    ) -> str | None:
        """Persist a rebuild task; caller can use the existing task progress API."""
        if self._db is None or self._task_manager is None or self._knowledge_semantic_rebuilder is None:
            return None
        from StAgent.db.models.task import TaskType

        state = self._knowledge_semantic_state
        if state is None:
            return None
        async with self._knowledge_semantic_enqueue_lock:
            existing_task_id = await self._active_semantic_rebuild_task_id()
            if existing_task_id is not None:
                state.mode = "building"
                state.task_id = existing_task_id
                return existing_task_id
            candidate = (
                candidate_version
                or state.candidate_version
                or self._knowledge_semantic_rebuilder.new_candidate_version()
            )
            async with self._db.session() as session:
                task = await self._task_manager.create_task(
                    session,
                    name="knowledge-vector-rebuild",
                    task_type=TaskType.KNOWLEDGE_VECTOR_REBUILD,
                    description="Rebuild local semantic knowledge vectors",
                    user_id=user_id,
                    input_data={"candidate_version": candidate},
                    timeout_seconds=86_400,
                )
                state.mode = "building"
                state.candidate_version = candidate
                state.task_id = str(task.id)
                await self._task_manager.start_task(session, task, params={"candidate_version": candidate})
            return str(task.id)

    async def _start_chat(self) -> None:
        try:
            from StAgent.services.chat.service import init_chat_service

            if self._db:
                self._chat = await init_chat_service(self.settings, self._db, self._cache)
                logger.info("Chat service initialized")
        except Exception:
            logger.warning("Chat initialization skipped", exc_info=True)

    async def _start_canvas(self) -> None:
        try:
            from StAgent.services.canvas.service import init_canvas_service

            if self._db:
                self._canvas = await init_canvas_service(self.settings, self._db, self._chat)
                logger.info("Canvas service initialized")
        except Exception:
            logger.warning("Canvas initialization skipped", exc_info=True)

    async def _start_coding_projects(self) -> None:
        if not getattr(self.settings, "coding_projects", None):
            return
        if not self.settings.coding_projects.enabled:
            return
        try:
            from StAgent.project.manager import init_coding_projects_service

            if self._db is None:
                return
            self._coding_projects = await init_coding_projects_service(self.settings, self._db)
            logger.info("Coding-projects service initialized")
        except Exception:
            logger.warning("Coding-projects initialization skipped", exc_info=True)

    async def _start_variable(self) -> None:
        try:
            from StAgent.services.variable.service import init_variable_service

            if self._db:
                self._variable = await init_variable_service(self.settings, self._db, self._cache)
                logger.info("Variable service initialized")
        except Exception:
            logger.warning("Variable initialization skipped", exc_info=True)

    async def _start_llm(self) -> None:
        try:
            from StAgent.llm.service import LLMService

            self._llm_service = LLMService.from_settings()
            self._llm = {"tasks": self._llm_service.list_tasks()}
            logger.info("LLMService initialised with %d provider(s)", len(self._llm_service.list_providers()))
        except Exception:
            logger.warning("LLMService initialisation skipped", exc_info=True)

    async def _start_rules(self) -> None:
        try:
            from pathlib import Path

            from StAgent.config.constants import RULES_DIR
            from StAgent.rules.engine import RuleEngine

            raw = (self.settings.rules_directory or "").strip()
            rules_path = Path(raw).expanduser().resolve() if raw else RULES_DIR
            engine = RuleEngine(llm_service=self._llm_service)
            count = await engine.safe_load_directory(rules_path)
            self._rule_engine = engine
            logger.info("RuleEngine initialised: %d rule set(s) from %s", count, rules_path)
        except Exception:
            logger.warning("RuleEngine initialisation skipped", exc_info=True)

    async def _start_workflow_service(self) -> None:
        if not self._db:
            return
        try:
            from StAgent.workflow.engine import CacheServiceWorkflowProvider, WorkflowExecutor
            from StAgent.workflow.nodes import bootstrap as bootstrap_nodes
            from StAgent.workflow.registry import FlowWorkflowRegistry
            from StAgent.workflow.server.event_bus import InMemoryEventBus
            from StAgent.workflow.services import WorkflowService

            wf_settings = self.settings.workflow
            await bootstrap_nodes(
                custom_dirs=([wf_settings.custom_nodes_dir] if wf_settings.custom_nodes_dir else None)
            )

            event_bus = InMemoryEventBus()
            # Share the bus with the WS router (`get_event_bus(service_manager)`)
            # so publishers (executor progress handler) and subscribers
            # (WebSocket endpoints) use one transport.
            self._workflow_event_bus = event_bus

            tool_registry = None
            tool_executor = None
            try:
                from StAgent.tools.registry import get_registry as _get_tool_registry

                tool_registry = _get_tool_registry()
                tool_executor = self.runtime_context.executor
            except Exception:
                logger.warning("Tool registry/executor not available for workflow engine", exc_info=True)

            agent_runtime = None
            try:
                from StAgent.runtime import AgentRuntime

                agent_runtime = AgentRuntime(self.runtime_context)
            except Exception:
                logger.warning("Agent runtime not available for workflow engine", exc_info=True)

            from StAgent.workflow.state_store import build_workflow_state_store

            state_store = build_workflow_state_store(self._db)

            executor = WorkflowExecutor(
                tool_registry=tool_registry,
                tool_executor=tool_executor,
                llm_service=self._llm_service,
                review_service=None,
                workflow_registry=None,
                agent_runtime=agent_runtime,
                cache_mode=wf_settings.cache_mode,
                cache_options={
                    "lru_size": wf_settings.cache_lru_size,
                    "ram_mb": wf_settings.cache_ram_mb,
                },
                cache_provider=(CacheServiceWorkflowProvider(self._cache) if self._cache is not None else None),
                state_store=state_store,
            )

            async def _publish_to_event_bus(event: Any) -> None:
                try:
                    await event_bus.publish_event(event.prompt_id, event)
                except Exception:
                    logger.warning("Workflow event bus publish failed", exc_info=True)
                try:
                    if self._event is not None:
                        from StAgent.runtime.run_store import get_run_store

                        exec_run = get_run_store().get_by_prompt_id(getattr(event, "prompt_id", "") or "")
                        run_id = exec_run.run_id if exec_run else None
                        await self._event.bridge_workflow_progress_event(event, run_id=run_id)
                except Exception:
                    logger.debug("Workflow EventManager bridge failed", exc_info=True)
                # Observability plane: a third, independent bypass consumer.
                # ``on_event`` swallows its own errors; the guard here is belt and
                # braces so a Gantt problem can never surface as an execution one.
                try:
                    from StAgent.workflow.observability import get_observability_hub

                    get_observability_hub().on_event(event)
                except Exception:
                    logger.debug("Workflow observability hub bridge failed", exc_info=True)

            executor.register_progress_handler(_publish_to_event_bus)
            registry = FlowWorkflowRegistry(self._db)

            from StAgent.workflow.prompt_map import build_prompt_map

            prompt_map = build_prompt_map(None)

            self._workflow_service = WorkflowService(
                db=self._db,
                executor=executor,
                registry=registry,
                queue=None,
                prompt_map=prompt_map,
            )
            logger.info("WorkflowService initialized (async enqueue, no broker queue)")
        except Exception:
            logger.warning("WorkflowService initialization skipped", exc_info=True)

    async def _start_cron(self) -> None:
        try:
            from StAgent.cron import CronExecutor, CronHookManager, CronManager

            if self._db:
                from StAgent.cron.repository import DatabaseJobRepository

                repository: JobRepository = DatabaseJobRepository(self._db)
                await repository.initialize()
            else:
                from StAgent.cron import JsonJobRepository

                repository = JsonJobRepository("/tmp/StAgent/cron_jobs.json", auto_save_interval=30.0)
                await repository.initialize()

            workflow_executor = None
            workflow_registry = None
            if self._workflow_service is not None:
                workflow_executor = self._workflow_service._executor
                workflow_registry = self._workflow_service._registry

            executor = CronExecutor(
                workflow_executor=workflow_executor,
                workflow_registry=workflow_registry,
                workflow_service=self._workflow_service,
                default_timeout_sec=3600,
                max_concurrent_executions=10,
            )
            hook_manager = CronHookManager()
            self._cron = CronManager(
                repository=repository,
                executor=executor,
                hook_manager=hook_manager,
                timezone="UTC",
                redis_client=None,
            )
            await self._cron.start()
            logger.info("Cron manager started")
        except Exception:
            logger.warning("Cron manager initialization skipped", exc_info=True)

    async def _start_task_manager(self) -> None:
        try:
            from StAgent.services.task_manager import init_task_manager

            manager = init_task_manager(self.settings)
            if self._event is not None:
                manager.attach_event_manager(self._event)
            self._task_manager = manager
            try:
                from StAgent.tasks.registration import register_default_handlers

                registered = await register_default_handlers(self, manager)
                logger.info("TaskManager initialised with %d handler(s)", registered)
                if self._knowledge_semantic_auto_rebuild and not await self._has_active_semantic_rebuild_task():
                    await self.enqueue_knowledge_vector_rebuild(
                        candidate_version=getattr(self._knowledge_semantic_state, "candidate_version", None),
                    )
            except Exception:
                logger.warning("TaskManager handler registration skipped", exc_info=True)
        except Exception:
            logger.warning("TaskManager initialization skipped", exc_info=True)

    async def _has_active_semantic_rebuild_task(self) -> bool:
        """Avoid duplicating a durable rebuild that startup recovery will resume."""
        return await self._active_semantic_rebuild_task_id() is not None

    async def _active_semantic_rebuild_task_id(self) -> str | None:
        """Return an existing non-terminal rebuild task, if one is persisted."""
        if self._db is None:
            return None
        try:
            from sqlmodel import col, select

            from StAgent.db.models.task import Task, TaskStatus, TaskType

            async with self._db.session() as session:
                row = (
                    await session.exec(
                        select(Task.id)
                        .where(
                            Task.task_type == TaskType.KNOWLEDGE_VECTOR_REBUILD,
                            col(Task.status).in_([TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING]),
                        )
                        .limit(1)
                    )
                ).first()
            return str(row) if row is not None else None
        except Exception:  # noqa: BLE001 - auto enqueue remains best-effort
            logger.debug("semantic_rebuild_task_probe_failed", exc_info=True)
            return None

    async def _start_file_service(self) -> None:
        from StAgent.file.service import FileService

        if self.settings.object_storage.enabled:
            # Explicitly enabled external storage is an operational contract;
            # do not fall back to an invisible process-local file tree.
            from StAgent.file.storage.s3 import build_s3_storage_backend

            try:
                backend, client = build_s3_storage_backend(self.settings.object_storage)
            except Exception:
                logger.exception("Configured object storage initialization failed")
                raise
            backend_name = "s3"
            self._minio = client
            logger.info("FileService initialised (S3-compatible object storage)")
        else:
            try:
                from StAgent.file.storage.local import LocalStorageBackend

                backend = cast("Any", LocalStorageBackend(self.settings.files.upload_dir))
            except Exception:
                logger.warning("Local FileService initialization skipped", exc_info=True)
                return
            backend_name = "local"
            self._minio = None
            logger.info("FileService initialised (local storage)")

        self._file_store = backend
        self._file_service = FileService(
            default_backend=backend,
            default_backend_name=backend_name,
            cache=self._cache,
            database=self._db,
        )

    async def _start_file_processing(self) -> None:
        try:
            from StAgent.services.file_processing.service import FileProcessingService

            self._file_processing = FileProcessingService()
            logger.info("File processing service initialized")
        except Exception:
            logger.warning("File processing service initialization skipped", exc_info=True)

    _KNOWLEDGE_GC_BOOT_DELAY_MINUTES = 5.0

    def _start_knowledge_gc(self) -> None:
        """Spawn the periodic knowledge-base garbage collection loop."""
        interval_minutes = float(
            getattr(self.settings.knowledge, "gc_interval_minutes", 0.0) or 0.0
        )
        if self._db is None or interval_minutes <= 0:
            return
        coro = self._knowledge_gc_loop(interval_minutes)
        try:
            self._knowledge_gc_task = asyncio.create_task(coro, name="knowledge-gc")
        except RuntimeError:
            # No running loop (e.g. sync startup path): bind to the current
            # loop so it starts once the loop runs.
            loop = asyncio.get_event_loop()
            self._knowledge_gc_task = loop.create_task(coro, name="knowledge-gc")
        logger.info("Knowledge GC scheduled (every %.0f minutes)", interval_minutes)

    async def _knowledge_gc_loop(self, interval_minutes: float) -> None:
        """Run :class:`KnowledgeGarbageCollector` on a fixed interval.

        The first pass runs shortly after boot (bounded) so a restart can
        clean up orphans left by the previous process without waiting a
        full interval.
        """
        from StAgent.knowledge.gc import KnowledgeGarbageCollector

        collector = KnowledgeGarbageCollector(self._db)
        delay = min(self._KNOWLEDGE_GC_BOOT_DELAY_MINUTES, interval_minutes) * 60.0
        while True:
            await asyncio.sleep(delay)
            delay = interval_minutes * 60.0
            try:
                await collector.collect()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Knowledge GC run failed", exc_info=True)

    async def _stop_knowledge_gc(self) -> None:
        task = self._knowledge_gc_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("Knowledge GC task shutdown error", exc_info=True)
        finally:
            self._knowledge_gc_task = None

    async def _start_session_manager(self) -> None:
        try:
            from StAgent.services.session.manager import SessionManager

            if self._db is None:
                return
            self._session_manager = SessionManager(
                self.settings,
                database=self._db,
                file_service=self._file_service,
                cache=self._cache,
            )
            logger.info("SessionManager initialised (SQLite-only)")
        except Exception:
            logger.warning("SessionManager initialisation skipped", exc_info=True)

    async def _start_agent_memory(self) -> None:
        if self._db is None:
            return
        try:
            from StAgent.memory.agent_memory import AgentMemory
            from StAgent.memory.embeddings import (
                LLMServiceEmbeddingProvider,
                NullEmbeddingProvider,
            )
            from StAgent.memory.episodic import EpisodicStore
            from StAgent.memory.procedural import ProceduralStore
            from StAgent.memory.semantic import SemanticStore

            embedding_dim = int(getattr(self.settings.llm, "embedding_dim", 1024) or 1024)

            embeddings: EmbeddingProvider
            if self._llm_service is not None:
                embeddings = LLMServiceEmbeddingProvider(
                    self._llm_service,
                    cache=self._cache,
                    model=getattr(self.settings.llm, "embedding_model", None),
                    dimension=embedding_dim,
                )
            else:
                embeddings = NullEmbeddingProvider(dimension=embedding_dim)

            from StAgent.memory.vector import MilvusConnectionConfig

            vector_connection = MilvusConnectionConfig(enabled=False)

            episodic = EpisodicStore(
                database=self._db,
                embeddings=embeddings,
                vector_connection=vector_connection,
            )
            semantic = SemanticStore(
                database=self._db,
                embeddings=embeddings,
                vector_connection=vector_connection,
            )
            procedural = ProceduralStore(
                database=self._db,
                embeddings=embeddings,
                vector_connection=vector_connection,
            )
            self._agent_memory = AgentMemory(
                episodic=episodic,
                semantic=semantic,
                procedural=procedural,
                embeddings=embeddings,
                cache=self._cache,
            )
            logger.info("AgentMemory initialised (embedding_dim=%d, vector=off)", embedding_dim)
        except Exception:
            logger.warning("AgentMemory initialisation skipped", exc_info=True)

    async def _start_channels(self) -> None:
        """Start enabled messaging channels (currently Weixin inbound agent)."""
        try:
            import os

            from StAgent.config.config import load_config

            config = load_config()
            wx_cfg = config.channels.get("weixin")
            enabled = bool(wx_cfg and wx_cfg.enabled)
            if os.getenv("WEIXIN_CHANNEL_ENABLED", "").strip() == "1":
                enabled = True
            if not enabled:
                logger.info("Channels: weixin not enabled, skip")
                return
            await self.ensure_weixin_running()
        except Exception:
            logger.warning("Channel manager start skipped", exc_info=True)

    async def ensure_weixin_running(self) -> dict[str, Any]:
        """Start or hot-reload the Weixin long-poll channel (no process restart).

        Loads credentials from runtime ``config.yaml`` / env / disk account store.
        Safe to call after QR login from the API.
        """
        import os

        from StAgent.channels.agent_bridge import make_agent_process_handler
        from StAgent.channels.manager import ChannelManager
        from StAgent.channels.weixin import WeixinChannel
        from StAgent.channels.weixin.store import load_account
        from StAgent.config.config import load_config

        config = load_config()
        wx_cfg = config.channels.get("weixin")
        extra: dict[str, Any] = dict(wx_cfg.extra) if wx_cfg else {}
        account_id = str(extra.get("account_id") or "").strip()
        token = str((wx_cfg.token if wx_cfg else "") or extra.get("token") or "").strip()
        account_id = os.getenv("WEIXIN_ACCOUNT_ID", account_id).strip() or account_id
        token = os.getenv("WEIXIN_TOKEN", token).strip() or token

        if account_id and not token:
            persisted = load_account(account_id)
            if persisted:
                token = str(persisted.get("token") or "").strip()
                if persisted.get("base_url"):
                    extra.setdefault("base_url", persisted["base_url"])

        if not account_id or not token:
            raise RuntimeError(
                "Weixin credentials missing; complete QR login first (channels.weixin.token + extra.account_id)"
            )

        channel_config: dict[str, Any] = {
            "enabled": True,
            "token": token,
            "extra": {
                **extra,
                "account_id": account_id,
                "base_url": extra.get("base_url") or os.getenv("WEIXIN_BASE_URL", ""),
                "cdn_base_url": extra.get("cdn_base_url") or os.getenv("WEIXIN_CDN_BASE_URL", ""),
                "dm_policy": extra.get("dm_policy") or os.getenv("WEIXIN_DM_POLICY", "open"),
                "group_policy": extra.get("group_policy") or os.getenv("WEIXIN_GROUP_POLICY", "disabled"),
                "allow_from": extra.get("allow_from") or os.getenv("WEIXIN_ALLOWED_USERS", ""),
                "group_allow_from": extra.get("group_allow_from") or os.getenv("WEIXIN_GROUP_ALLOWED_USERS", ""),
            },
        }
        if not channel_config["extra"]["base_url"]:
            channel_config["extra"].pop("base_url", None)
        if not channel_config["extra"]["cdn_base_url"]:
            channel_config["extra"].pop("cdn_base_url", None)

        handler = make_agent_process_handler(self)
        weixin = WeixinChannel.from_config(channel_config, process_handler=handler)

        if self._channel_manager is None:
            manager = ChannelManager()
            manager.register(weixin)
            await manager.start_all()
            self._channel_manager = manager
        else:
            await self._channel_manager.replace_channel(weixin)

        # Keep config enabled so a later process restart also picks it up.
        if wx_cfg is not None:
            from StAgent.config.config import save_config

            wx_cfg.enabled = True
            wx_cfg.token = token
            wx_cfg.extra["account_id"] = account_id
            save_config(config)

        health = await weixin.health_check()
        logger.info("Weixin channel running account_id=%s", account_id[:12])
        return {
            "running": True,
            "account_id": account_id,
            "health": health,
        }

    async def stop_weixin_channel(self) -> dict[str, Any]:
        """Fully stop the Weixin poller and persist ``enabled=false``.

        Credentials (token / account_id) are kept so **启动监听** can resume
        without re-scanning. Process restart will not auto-start Weixin until
        start/login enables it again.
        """
        if self._channel_manager is not None:
            channel = self._channel_manager.get_channel("weixin")
            if channel is not None:
                try:
                    await channel.stop()
                except Exception:
                    logger.warning("Weixin channel stop error", exc_info=True)
                self._channel_manager.unregister("weixin")

        try:
            from StAgent.config.config import ChannelConfig, load_config, save_config

            config = load_config()
            if "weixin" not in config.channels:
                config.channels["weixin"] = ChannelConfig()
            config.channels["weixin"].enabled = False
            save_config(config)
        except Exception:
            logger.warning("Failed to persist Weixin enabled=false", exc_info=True)

        logger.info("Weixin channel fully stopped (enabled=false)")
        return self.weixin_runtime_status()

    def weixin_runtime_status(self) -> dict[str, Any]:
        """Return current Weixin poller status without I/O."""
        from StAgent.config.config import load_config

        config = load_config()
        wx_cfg = config.channels.get("weixin")
        account_id = ""
        enabled = False
        has_token = False
        if wx_cfg:
            enabled = bool(wx_cfg.enabled)
            account_id = str((wx_cfg.extra or {}).get("account_id") or "")
            has_token = bool(wx_cfg.token or (wx_cfg.extra or {}).get("token"))

        channel = None
        if self._channel_manager is not None:
            channel = self._channel_manager.get_channel("weixin")

        running = bool(
            channel is not None
            and getattr(channel, "_running", False)
            and not getattr(channel, "_session_expired", False)
        )
        return {
            "enabled": enabled,
            "configured": bool(account_id and has_token),
            "running": running,
            "account_id": account_id,
            "session_expired": bool(channel is not None and getattr(channel, "_session_expired", False)),
        }

    async def _stop_channels(self) -> None:
        if self._channel_manager is None:
            return
        try:
            await self._channel_manager.stop_all()
        except Exception:
            logger.warning("Channel manager shutdown error", exc_info=True)
        self._channel_manager = None


_service_manager: ServiceManager | None = None


def get_service_manager() -> ServiceManager:
    from StAgent.main import get_service_manager as _main_get_service_manager

    try:
        return _main_get_service_manager()
    except AssertionError:
        if _service_manager is None:
            raise RuntimeError("ServiceManager not initialized") from None
        return _service_manager


def init_service_manager(settings: Settings) -> ServiceManager:
    global _service_manager
    _service_manager = ServiceManager(settings)
    return _service_manager
