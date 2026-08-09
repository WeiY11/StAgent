"""Optional OpenSearch lexical backend for quality-profile retrieval."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from leagent.db.repositories.document_chunks import ChunkHit


class OpenSearchKnowledgeIndex:
    """Small HTTP adapter; PostgreSQL remains the ACL/source-of-truth layer."""

    def __init__(
        self,
        endpoint: str,
        *,
        index: str = "knowledge_chunks",
        api_key: str = "",
        timeout_seconds: float = 5.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.index = index
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str, *, limit: int = 50, index_version: str = "rag-v1") -> list[ChunkHit]:
        import httpx

        headers = {"Authorization": f"ApiKey {self.api_key}"} if self.api_key else {}
        body: dict[str, Any] = {
            "size": max(1, min(limit, 200)),
            "track_total_hits": False,
            "query": {
                "bool": {
                    "must": [{"multi_match": {"query": query, "fields": ["text^2", "heading", "file_name"]}}],
                    "filter": [{"term": {"index_version": index_version}}],
                }
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.endpoint}/{self.index}/_search",
                json=body,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        hits: list[ChunkHit] = []
        raw_hits = payload.get("hits", {}).get("hits", []) if isinstance(payload, dict) else []
        for item in raw_hits if isinstance(raw_hits, list) else []:
            if not isinstance(item, dict):
                continue
            source = item.get("_source") or {}
            try:
                hits.append(
                    ChunkHit(
                        chunk_id=UUID(str(source.get("chunk_id") or item.get("_id"))),
                        file_id=UUID(str(source["file_id"])),
                        file_name=str(source.get("file_name") or ""),
                        seq=int(source.get("seq") or 0),
                        start_offset=int(source.get("start_offset") or 0),
                        end_offset=int(source.get("end_offset") or 0),
                        text=str(source.get("text") or ""),
                        snippet=str(source.get("snippet") or source.get("text") or "")[:400],
                        score=float(item.get("_score") or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return hits


__all__ = ["OpenSearchKnowledgeIndex"]
