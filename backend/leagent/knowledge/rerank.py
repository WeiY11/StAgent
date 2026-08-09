"""Reranking contracts with a deterministic local floor and HTTP adapter."""

from __future__ import annotations

import re
from collections.abc import Sequence  # noqa: TC003 - runtime provider annotations are public
from typing import Any, Protocol


class RerankProvider(Protocol):
    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]: ...


def _terms(value: str) -> set[str]:
    words = re.findall(r"[\w\u4e00-\u9fff]+", value.lower())
    return {word for word in words if word}


class LexicalRerankProvider:
    """Always-available reranker used when no cross-encoder is configured."""

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]:
        query_terms = _terms(query)
        scored: list[tuple[int, float]] = []
        for index, document in enumerate(documents):
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
