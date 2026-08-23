"""Context source: knowledge base injection.

When the user query is relevant to the knowledge base (detected by keyword
heuristics or harness opt-in), this source searches the knowledge retrieval
pipeline and injects the top results as a context attachment.

This complements the ``knowledge_search`` tool (which is agent-initiated)
by proactively surfacing relevant documents without requiring an explicit
tool call — similar to how the ``recall`` source injects memory entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from StAgent.context.sources import SOURCE_REGISTRY
from StAgent.context.types import (
    AttachmentKind,
    ContextBlock,
    ContextScope,
    RenderTarget,
)

if TYPE_CHECKING:
    from StAgent.context.sources.base import ResolveContext

logger = structlog.get_logger(__name__)

#: Keywords that indicate a knowledge-related query.
_KNOWLEDGE_HINTS = (
    "知识库",
    "文档",
    "查阅",
    "资料",
    "参考",
    "检索",
    "knowledge",
    "document",
    "reference",
    "lookup",
    "according to",
    "based on",
)

# Generic words such as "document" are useful retrieval hints, but they also
# occur in ordinary authoring requests. Only explicit KB/source-bound requests
# must fail closed when no evidence is found.
_STRICT_KNOWLEDGE_HINTS = (
    "知识库",
    "文档库",
    "资料库",
    "knowledge base",
    "knowledge",
    "based on",
    "according to",
    "@knowledge:",
)

#: Maximum characters of knowledge context to inject.
_MAX_CONTEXT_CHARS = 3000
#: Maximum number of search results to include.
_MAX_RESULTS = 5


class KnowledgeContextSource:
    """Injects relevant knowledge base snippets as a context attachment."""

    id = "knowledge_context"
    kind = "state"
    scope = ContextScope.TURN
    # A knowledge answer without retrieval evidence is a safety problem, not
    # an optional convenience block. Keep this small guard above ordinary
    # recall/context material when the turn budget is tight.
    priority = 1100
    weight = 0.7
    render_target = RenderTarget.ATTACHMENT_USER

    def invalidation_key(self, ctx: ResolveContext) -> str:
        return f"kb_ctx:{ctx.query[:100]}:{ctx.user_id}:{ctx.session_id}"

    async def resolve(self, ctx: ResolveContext) -> ContextBlock | None:
        try:
            if not ctx.query or not ctx.user_id:
                return None

            # Relevance gate: only inject when the query looks knowledge-related.
            if not await self._is_knowledge_relevant(ctx):
                return None

            # Search the knowledge base.
            from StAgent.library.retrieval import (
                KnowledgeRetrievalPipeline,
                SearchOptions,
                get_knowledge_retrieval_pipeline,
            )

            pipeline = get_knowledge_retrieval_pipeline()
            if pipeline is None:
                # Try to construct from database service.
                try:
                    from StAgent.db import get_database_service

                    db = get_database_service()
                    if db is None:
                        return (
                            self._unavailable_evidence_block("database_unavailable")
                            if self._requires_evidence(ctx)
                            else None
                        )
                    pipeline = KnowledgeRetrievalPipeline(db)
                except Exception:  # noqa: BLE001
                    return (
                        self._unavailable_evidence_block("retrieval_unavailable")
                        if self._requires_evidence(ctx)
                        else None
                    )

            options = SearchOptions(
                query=ctx.query,
                user_id=ctx.user_id,
                limit=_MAX_RESULTS,
                library_scope="knowledge",
            )
            results = await pipeline.search(options)

            if not results:
                return (
                    self._unavailable_evidence_block("no_matching_evidence")
                    if self._requires_evidence(ctx)
                    else None
                )

            # Build context block.
            lines: list[str] = []
            selected_results: list[object] = []
            total_chars = 0
            for r in results:
                snippet = (r.snippet or r.text[:300]).strip()
                if not snippet:
                    continue
                entry = f"- [K{len(lines) + 1}] [{r.file_name}] {snippet}"
                if total_chars + len(entry) > _MAX_CONTEXT_CHARS:
                    break
                lines.append(entry)
                selected_results.append(r)
                total_chars += len(entry)

            if not lines:
                return None

            body_content = "\n".join(lines)
            signature = ContextBlock.content_signature(self.id, body_content)
            from StAgent.knowledge.evidence import build_evidence

            evidence = build_evidence(selected_results, strict=True)
            body = (
                f'<attachment kind="{AttachmentKind.CONTEXT.value}"'
                f' source="knowledge_base"'
                f' signature="{signature}">\n'
                f"Relevant knowledge base documents:\n"
                f"{body_content}\n"
                "Use only claims supported by this attachment. If your answer relies on it, "
                "include at least one valid [K#] reference; if the evidence is insufficient, say so.\n"
                f"</attachment>"
            )

            return ContextBlock(
                source_id=self.id,
                kind=self.kind,
                render_target=self.render_target,
                body=body,
                tokens=ContextBlock.approx_tokens(body),
                cost=len(body),
                signature=signature,
                priority=self.priority,
                weight=self.weight,
                metadata={"knowledge_evidence": evidence},
            )
        except Exception:
            logger.warning("knowledge_context_source_failed", exc_info=True)
            return (
                self._unavailable_evidence_block("retrieval_failed")
                if self._requires_evidence(ctx)
                else None
            )

    def _unavailable_evidence_block(self, reason: str) -> ContextBlock:
        """Return a small fail-closed block when retrieval lacks evidence."""
        from StAgent.knowledge.evidence import EvidenceBundle, EvidenceStatus

        status = (
            EvidenceStatus.INSUFFICIENT
            if reason == "no_matching_evidence"
            else EvidenceStatus.DEGRADED
        )
        evidence = EvidenceBundle(status, (), reason=reason)
        try:
            from StAgent.utils.metrics import get_metrics

            get_metrics().record_knowledge_evidence(status.value)
        except Exception:  # noqa: BLE001 - observability must not block refusal
            pass
        body = (
            f'<attachment kind="{AttachmentKind.CONTEXT.value}" '
            'source="knowledge_base" evidence="unavailable">\n'
            "No verifiable knowledge-base evidence was retrieved for this request. "
            "Do not answer from background knowledge. State that the knowledge base "
            "does not currently support a verified answer, or use knowledge_search "
            "with a more focused query.\n"
            "</attachment>"
        )
        signature = ContextBlock.content_signature(self.id, body)
        return ContextBlock(
            source_id=self.id,
            kind=self.kind,
            render_target=self.render_target,
            body=body,
            tokens=ContextBlock.approx_tokens(body),
            cost=len(body),
            signature=signature,
            priority=self.priority,
            weight=self.weight,
            metadata={"knowledge_evidence": evidence},
        )

    @staticmethod
    async def _is_knowledge_relevant(ctx: ResolveContext) -> bool:
        """Check if the query is knowledge-related."""
        # Harness opt-in via template_vars.
        tv = ctx.template_vars or {}
        if tv.get("knowledge_context") or tv.get("use_knowledge_base"):
            return True

        # Workflow hint match.
        if ctx.workflow_hint:
            low_hint = ctx.workflow_hint.lower()
            if any(h in low_hint for h in _KNOWLEDGE_HINTS):
                return True

        # Query keyword heuristic.
        if ctx.query:
            low_query = ctx.query.lower()
            if any(h in low_query for h in _KNOWLEDGE_HINTS):
                return True

        # Naming a session attachment (filename or stem) is an implicit
        # retrieval request: the user is pointing at a specific document.
        if await KnowledgeContextSource._query_names_session_attachment(ctx):
            return True

        return False

    @staticmethod
    async def _query_names_session_attachment(ctx: ResolveContext) -> bool:
        """Whether the query explicitly names a file attached to this session."""
        if not ctx.query or ctx.session_manager is None or ctx.session_id is None:
            return False
        query = ctx.query.lower()
        try:
            attachments = await ctx.session_manager.list_attachments(
                ctx.session_id, user_id=ctx.user_id
            )
        except Exception:  # noqa: BLE001 - gating must never break the turn
            return False
        for attachment in attachments:
            filename = (getattr(attachment, "filename", "") or "").strip()
            if not filename:
                continue
            if filename.lower() in query:
                return True
            stem = filename.rsplit(".", 1)[0].strip().lower()
            if len(stem) >= 3 and stem in query:
                return True
        return False

    @staticmethod
    def _requires_evidence(ctx: ResolveContext) -> bool:
        """Whether a retrieval miss must block background-knowledge answers."""
        tv = ctx.template_vars or {}
        if tv.get("knowledge_context") or tv.get("use_knowledge_base"):
            return True
        candidates = (ctx.query or "", ctx.workflow_hint or "")
        return any(
            hint in candidate.lower()
            for candidate in candidates
            for hint in _STRICT_KNOWLEDGE_HINTS
        )


SOURCE_REGISTRY["knowledge_context"] = KnowledgeContextSource
