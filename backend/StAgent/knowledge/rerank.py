"""Reranking contracts with a deterministic local floor and HTTP adapter."""

from __future__ import annotations

import re
from collections.abc import Sequence  # noqa: TC003 - runtime provider annotations are public
from typing import Any, Protocol

from StAgent.library.fts import cjk_lexical_terms


class RerankProvider(Protocol):
    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]: ...


def _terms(value: str) -> set[str]:
    """Return comparable lexical units for local reranking.

    Continuous CJK prose is not reliably split into useful words by the
    zero-dependency tokenizer. Reuse the bounded bigrams used by the SQLite
    retrieval floor, while retaining ordinary ASCII terms for mixed queries.
    """

    cjk_terms = set(cjk_lexical_terms(value))
    ascii_terms = set(re.findall(r"[A-Za-z0-9_]+", value.lower()))
    if cjk_terms:
        return cjk_terms | ascii_terms

    words = re.findall(r"[\w\u4e00-\u9fff]+", value.lower())
    return {word for word in words if word}


def _cjk_overlap(query_terms: tuple[str, ...], document: str) -> float:
    """Return the same bounded bigram-coverage signal as SQLite retrieval.

    ``cjk_lexical_terms`` is intentionally capped for a *query*. Applying that
    cap to an entire passage is incorrect: relevant terms near the end of a
    chunk vanish. Checking the bounded query terms directly against the full
    passage preserves the primary SQLite rank signal without a segmenter.
    """

    if not query_terms:
        return 0.0
    normalized_document = document.lower()
    return sum(term in normalized_document for term in query_terms) / len(query_terms)


class LexicalRerankProvider:
    """Always-available reranker used when no cross-encoder is configured."""

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]:
        cjk_query_terms = cjk_lexical_terms(query)
        query_terms = _terms(query) if not cjk_query_terms else set()
        scored: list[tuple[int, float]] = []
        for index, document in enumerate(documents):
            if cjk_query_terms:
                overlap = _cjk_overlap(cjk_query_terms, document)
            else:
                doc_terms = _terms(document)
                overlap = len(query_terms & doc_terms) / max(1, len(query_terms))
            phrase = 1.0 if query.lower() in document.lower() else 0.0
            scored.append((index, overlap * 0.8 + phrase * 0.2))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[: max(1, top_n)]


class HttpRerankProvider:
    """OpenAI/Cohere-compatible HTTP rerank endpoint."""

    def __init__(self, endpoint: str, *, api_key: str = "", timeout_seconds: float = 5.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]:
        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {"query": query, "documents": list(documents), "top_n": top_n}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
            data: Any = response.json()
        raw = data.get("results", data) if isinstance(data, dict) else data
        out: list[tuple[int, float]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if isinstance(index, int) and isinstance(score, (int, float)):
                out.append((index, float(score)))
        return out[: max(1, top_n)]


__all__ = ["HttpRerankProvider", "LexicalRerankProvider", "RerankProvider"]
