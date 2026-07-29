"""Context source: knowledge base injection.

When the user query is relevant to the knowledge base (detected by keyword
heuristics or harness opt-in), this source searches the knowledge retrieval
pipeline and injects the top results as a context attachment.

This complements the ``knowledge_search`` tool (which is agent-initiated)
by proactively surfacing relevant documents without requiring an explicit
tool call — similar to how the ``recall`` source injects memory entries.
"""

from __future__ import annotations

import structlog

from leagent.context.sources import SOURCE_REGISTRY
from leagent.context.sources.base import ResolveContext
from leagent.context.types import (
    AttachmentKind,
    ContextBlock,
    ContextScope,
    RenderTarget,
)

logger = structlog.get_logger(__name__)

#: Keywords that indicate a knowledge-related query.
_KNOWLEDGE_HINTS = (
    "知识库",
    "文档",
    "查阅",
    "资料",
    "参考",
    "knowledge",
    "document",
    "reference",
    "lookup",
    "according to",
    "based on",
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
    priority = 480  # slightly below recall (500)
    weight = 0.7
    render_target = RenderTarget.ATTACHMENT_USER

    def invalidation_key(self, ctx: ResolveContext) -> str:
        return f"kb_ctx:{ctx.query[:100]}:{ctx.user_id}"

    async def resolve(self, ctx: ResolveContext) -> ContextBlock | None:
        try:
            if not ctx.query or not ctx.user_id:
                return None

            # Relevance gate: only inject when the query looks knowledge-related.
            if not self._is_knowledge_relevant(ctx):
                return None

            # Search the knowledge base.
            from leagent.library.retrieval import (
                KnowledgeRetrievalPipeline,
                SearchOptions,
                get_knowledge_retrieval_pipeline,
            )

            pipeline = get_knowledge_retrieval_pipeline()
            if pipeline is None:
                # Try to construct from database service.
                try:
                    from leagent.db import get_database_service

                    db = get_database_service()
                    if db is None:
                        return None
                    pipeline = KnowledgeRetrievalPipeline(db)
                except Exception:  # noqa: BLE001
                    return None

            options = SearchOptions(
                query=ctx.query,
                user_id=ctx.user_id,
                limit=_MAX_RESULTS,
                library_scope="knowledge",
            )
            results = await pipeline.search(options)

            if not results:
                return None

            # Build context block.
            lines: list[str] = []
            total_chars = 0
            for r in results:
                snippet = (r.snippet or r.text[:300]).strip()
                if not snippet:
                    continue
                entry = f"- [{r.file_name}] {snippet}"
                if total_chars + len(entry) > _MAX_CONTEXT_CHARS:
                    break
                lines.append(entry)
                total_chars += len(entry)

            if not lines:
                return None

            body_content = "\n".join(lines)
            signature = ContextBlock.content_signature(self.id, body_content)
            body = (
                f'<attachment kind="{AttachmentKind.CONTEXT.value}"'
                f' source="knowledge_base"'
                f' signature="{signature}">\n'
                f"Relevant knowledge base documents:\n"
                f"{body_content}\n"
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
            )
        except Exception:
            logger.warning("knowledge_context_source_failed", exc_info=True)
            return None

    @staticmethod
    def _is_knowledge_relevant(ctx: ResolveContext) -> bool:
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

        return False


SOURCE_REGISTRY["knowledge_context"] = KnowledgeContextSource
