"""Single-process sqlite-vec projection for knowledge chunks.

The sidecar is intentionally disposable.  Failure to load or query it never
changes the primary document state and callers receive an empty vector result.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

from leagent.knowledge.vector_backend import version_table_name

if TYPE_CHECKING:
    from collections.abc import Sequence

    from leagent.library.chunking import Chunk
    from leagent.memory.embeddings import EmbeddingProvider

logger = logging.getLogger(__name__)
_SQLITE_PARAMETER_BATCH = 800
_UPSERT_BATCH = 256


class SqliteVecKnowledgeIndex:
    """A local sqlite-vec knowledge index with blue/green version tables."""

    requires_allowed_file_ids = True

    def __init__(
        self,
        leagent_home: str | Path,
        *,
        dimension: int,
        embeddings: EmbeddingProvider | None = None,
        index_version: str = "rag-v1",
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self._dimension = dimension
        self._embeddings = embeddings
        self._index_version = (index_version or "rag-v1").strip() or "rag-v1"
        root = Path(leagent_home).expanduser().resolve()
        knowledge = root / "knowledge"
        knowledge.mkdir(parents=True, exist_ok=True)
        self.path = knowledge / "vectors.sqlite3"
        self._write_lock = asyncio.Lock()
        self._init_lock = Lock()
        self._available = self._initialize()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def index_version(self) -> str:
        return self._index_version

    def _connect(self) -> sqlite3.Connection:
        import sqlite_vec

        if self.path.exists() and self.path.stat().st_size:
            with self.path.open("rb") as sidecar:
                if sidecar.read(16) != b"SQLite format 3\x00":
                    raise sqlite3.DatabaseError("knowledge vector sidecar header is invalid")
        connection = sqlite3.connect(self.path, timeout=20, check_same_thread=False)
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> bool:
        with self._init_lock:
            try:
                with self._connect() as connection:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS knowledge_vector_versions "
                        "(singleton INTEGER PRIMARY KEY CHECK (singleton = 1), active_version TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO knowledge_vector_versions(singleton, active_version) VALUES (1, ?) ",
                        (self._index_version,),
                    )
                    active = connection.execute(
                        "SELECT active_version FROM knowledge_vector_versions WHERE singleton = 1"
                    ).fetchone()
                    if active and active[0]:
                        self._index_version = str(active[0])
                    self._ensure_table(connection, self._index_version)
                return True
            except Exception:  # noqa: BLE001 - projection must never prevent RAG startup
                logger.warning("knowledge_sqlite_vec_unavailable", exc_info=True)
                return False

    def _ensure_table(self, connection: sqlite3.Connection, index_version: str) -> str:
        table = version_table_name(index_version)
        connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
            f"chunk_id TEXT PRIMARY KEY, embedding FLOAT[{self._dimension}], file_id TEXT)"
        )
        return table

    @staticmethod
    def _normalized(vector: Sequence[float], dimension: int) -> list[float]:
        if len(vector) != dimension:
            raise ValueError("embedding dimension mismatch")
        values = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("embedding must be finite")
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            raise ValueError("embedding must not be zero")
        return [value / norm for value in values]

    async def upsert_vectors(
        self,
        file_id: UUID,
        vectors: Sequence[tuple[UUID | str, Sequence[float]]],
        *,
        index_version: str | None = None,
    ) -> int:
        """Persist precomputed vectors; used by rebuild workers and tests."""
        if not self.available or not vectors:
            return 0
        version = index_version or self._index_version
        try:
            rows = [
                (str(chunk_id), self._normalized(vector, self._dimension), str(file_id))
                for chunk_id, vector in vectors
            ]
        except (TypeError, ValueError):
            return 0
        async with self._write_lock:
            try:
                return await asyncio.to_thread(self._upsert_sync, version, rows)
            except Exception:  # noqa: BLE001
                logger.warning("knowledge_sqlite_vec_upsert_degraded", exc_info=True)
                self._available = False
                return 0

    def _upsert_sync(self, version: str, rows: list[tuple[str, list[float], str]]) -> int:
        import sqlite_vec

        with self._connect() as connection:
            table = self._ensure_table(connection, version)
            for start in range(0, len(rows), _UPSERT_BATCH):
                batch = [
                    (chunk_id, sqlite_vec.serialize_float32(vector), file_id)
                    for chunk_id, vector, file_id in rows[start : start + _UPSERT_BATCH]
                ]
                connection.executemany(
                    f"INSERT OR REPLACE INTO {table}(chunk_id, embedding, file_id) VALUES (?, ?, ?)",
                    batch,
                )
        return len(rows)

    async def upsert_chunks(
        self,
        file_id: UUID,
        user_id: UUID | None,
        chunks: Sequence[Chunk],
        *,
        chunk_ids: Sequence[UUID] | None = None,
    ) -> int:
        """Embed chunks then upsert; uploader identity is deliberately unused."""
        del user_id
        if not self.available or not chunks or self._embeddings is None:
            return 0
        try:
            vectors = await self._embeddings.embed([chunk.text for chunk in chunks])
        except Exception:  # noqa: BLE001
            logger.warning("knowledge_sqlite_vec_embed_degraded", exc_info=True)
            return 0
        pairs = [
            (
                chunk_ids[index] if chunk_ids and index < len(chunk_ids) else uuid5(NAMESPACE_URL, f"leagent:knowledge:{file_id}:{index}"),
                vector,
            )
            for index, vector in enumerate(vectors)
            if index < len(chunks)
        ]
        return await self.upsert_vectors(file_id, pairs)

    async def embed_query(self, query: str) -> list[float]:
        """Encode a query without allowing a model library network download."""
        if self._embeddings is None:
            raise RuntimeError("local embedding provider is not configured")
        embed_query = getattr(self._embeddings, "embed_query", None)
        if embed_query is not None:
            vector = await embed_query(query)
        else:
            vector = await self._embeddings.embed_one(query)
        return self._normalized(vector, self._dimension)

    async def delete_for_file(self, file_id: UUID, *, index_version: str | None = None) -> int:
        if not self.available:
            return 0
        async with self._write_lock:
            try:
                return await asyncio.to_thread(self._delete_sync, index_version or self._index_version, str(file_id))
            except Exception:  # noqa: BLE001
                logger.warning("knowledge_sqlite_vec_delete_degraded", exc_info=True)
                self._available = False
                return 0

    def _delete_sync(self, version: str, file_id: str) -> int:
        with self._connect() as connection:
            table = self._ensure_table(connection, version)
            result = connection.execute(f"DELETE FROM {table} WHERE file_id = ?", (file_id,))
            return max(0, result.rowcount)

    async def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int = 20,
        allowed_file_ids: Sequence[UUID],
        index_version: str | None = None,
    ) -> list[tuple[str, float]]:
        """Search only an explicitly ACL-filtered set of files, fail closed otherwise."""
        if not self.available or not allowed_file_ids or limit <= 0:
            return []
        try:
            vector = self._normalized(query_vector, self._dimension)
            return await asyncio.to_thread(
                self._search_sync,
                index_version or self._index_version,
                vector,
                max(1, limit),
                [str(file_id) for file_id in allowed_file_ids],
            )
        except Exception:  # noqa: BLE001
            logger.warning("knowledge_sqlite_vec_search_degraded", exc_info=True)
            self._available = False
            return []

    async def search_vector(self, query_vector: Sequence[float], **kwargs: object) -> list[tuple[str, float]]:
        """Protocol spelling retained for retrieval adapters."""
        return await self.search(query_vector, **kwargs)  # type: ignore[arg-type]

    def _search_sync(self, version: str, vector: list[float], limit: int, file_ids: list[str]) -> list[tuple[str, float]]:
        import sqlite_vec

        best: dict[str, float] = {}
        with self._connect() as connection:
            table = self._ensure_table(connection, version)
            # vec0 metadata IN filtering is parameterized; batching avoids SQLite's bind limit.
            for start in range(0, len(file_ids), _SQLITE_PARAMETER_BATCH):
                batch = file_ids[start : start + _SQLITE_PARAMETER_BATCH]
                marks = ", ".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT chunk_id, distance FROM {table} "
                    f"WHERE embedding MATCH ? AND k = ? AND file_id IN ({marks})",
                    (sqlite_vec.serialize_float32(vector), limit, *batch),
                ).fetchall()
                for chunk_id, distance in rows:
                    # Unit-normalized vectors make squared L2 distance exactly 2(1-cosine).
                    score = max(-1.0, min(1.0, 1.0 - (float(distance) ** 2) / 2.0))
                    best[str(chunk_id)] = max(best.get(str(chunk_id), -1.0), score)
        return sorted(best.items(), key=lambda item: (-item[1], item[0]))[:limit]

    async def begin_version(self, index_version: str) -> bool:
        if not self.available or not index_version.strip():
            return False
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._begin_sync, index_version)
                return True
            except Exception:  # noqa: BLE001
                logger.warning("knowledge_sqlite_vec_begin_degraded", exc_info=True)
                return False

    def _begin_sync(self, version: str) -> None:
        with self._connect() as connection:
            self._ensure_table(connection, version)

    async def activate_version(self, index_version: str) -> bool:
        if not self.available:
            return False
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._activate_sync, index_version)
                self._index_version = index_version
                return True
            except Exception:  # noqa: BLE001
                logger.warning("knowledge_sqlite_vec_activate_degraded", exc_info=True)
                return False

    def _activate_sync(self, version: str) -> None:
        with self._connect() as connection:
            self._ensure_table(connection, version)
            connection.execute("UPDATE knowledge_vector_versions SET active_version = ? WHERE singleton = 1", (version,))

    async def discard_version(self, index_version: str) -> bool:
        if not self.available or index_version == self._index_version:
            return False
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._discard_sync, index_version)
                return True
            except Exception:  # noqa: BLE001
                logger.warning("knowledge_sqlite_vec_discard_degraded", exc_info=True)
                return False

    def _discard_sync(self, version: str) -> None:
        with self._connect() as connection:
            connection.execute(f"DROP TABLE IF EXISTS {version_table_name(version)}")


__all__ = ["SqliteVecKnowledgeIndex"]
