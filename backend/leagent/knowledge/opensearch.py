"""Optional OpenSearch lexical backend for quality-profile retrieval."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from leagent.db.repositories.document_chunks import ChunkHit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from leagent.library.chunking import Chunk


def _exact_field_filter(field: str, value: str) -> dict[str, Any]:
    """Match both explicit keyword fields and dynamic ``text.keyword`` fields."""
    return {
        "bool": {
            "should": [
                {"term": {field: value}},
                {"term": {f"{field}.keyword": value}},
            ],
            "minimum_should_match": 1,
        }
    }


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

    def _headers(self, *, ndjson: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"ApiKey {self.api_key}"} if self.api_key else {}
        if ndjson:
            headers["Content-Type"] = "application/x-ndjson"
        return headers

    async def upsert_chunks(
        self,
        file_id: UUID,
        file_name: str,
        chunks: Sequence[Chunk],
        *,
        chunk_ids: Sequence[UUID],
        index_version: str = "rag-v1",
    ) -> int:
        """Write the current chunk projection through OpenSearch bulk API.

        OpenSearch is an accelerator, not the authorization source of truth;
        the SQL retrieval path still validates every returned file.  A failed
        remote write is allowed to degrade indexing to the local lexical
        projection.
        """
        if not chunks:
            return 0
        operations: list[str] = []
        for index, chunk in enumerate(chunks):
            if index >= len(chunk_ids):
                break
            chunk_id = chunk_ids[index]
            operations.append(
                json.dumps(
                    {"index": {"_index": self.index, "_id": str(chunk_id)}},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            operations.append(
                json.dumps(
                    {
                        "chunk_id": str(chunk_id),
                        "file_id": str(file_id),
                        "file_name": file_name,
                        "seq": chunk.seq,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                        "text": chunk.text,
                        "snippet": chunk.text[:400],
                        "index_version": index_version,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        if not operations:
            return 0

        import httpx

        body = "\n".join(operations) + "\n"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.endpoint}/_bulk",
                content=body,
                headers=self._headers(ndjson=True),
            )
            response.raise_for_status()
            payload = response.json()
        items = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return 0
        return sum(
            1
            for item in items
            if isinstance(item, dict)
            and any(
                isinstance(operation, dict)
                and 200 <= int(operation.get("status", 500)) < 300
                for operation in item.values()
            )
        )

    async def delete_for_file(
        self,
        file_id: UUID,
        *,
        index_version: str = "rag-v1",
    ) -> int:
        """Remove stale remote chunks for one file and index version."""
        import httpx

        payload = {
            "query": {
                "bool": {
                    "filter": [
                        _exact_field_filter("file_id", str(file_id)),
                        _exact_field_filter("index_version", index_version),
                    ]
                }
            }
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.endpoint}/{self.index}/_delete_by_query",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()
        deleted = data.get("deleted", 0) if isinstance(data, dict) else 0
        return int(deleted) if isinstance(deleted, (int, float)) else 0

    async def search(self, query: str, *, limit: int = 50, index_version: str = "rag-v1") -> list[ChunkHit]:
        import httpx

        body: dict[str, Any] = {
            "size": max(1, min(limit, 200)),
            "track_total_hits": False,
            "query": {
                "bool": {
                    "must": [{"multi_match": {"query": query, "fields": ["text^2", "heading", "file_name"]}}],
                    "filter": [_exact_field_filter("index_version", index_version)],
                }
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.endpoint}/{self.index}/_search",
                json=body,
                headers=self._headers(),
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
