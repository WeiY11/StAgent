"""Single-process sqlite-vec projection for knowledge chunks.

The sidecar is intentionally disposable.  Failure to load or query it never
changes the primary document state and callers receive an empty vector result.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

from StAgent.knowledge.vector_backend import canonical_index_version, version_table_name

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from StAgent.library.chunking import Chunk
    from StAgent.memory.embeddings import EmbeddingProvider

logger = logging.getLogger(__name__)
_SQLITE_PARAMETER_BATCH = 800
_UPSERT_BATCH = 256


class SqliteVecKnowledgeIndex:
    """A local sqlite-vec knowledge index with blue/green version tables."""

    requires_allowed_file_ids = True

    def __init__(
        self,
        StAgent_home: str | Path,
        *,
        dimension: int,
        embeddings: EmbeddingProvider | None = None,
        index_version: str = "rag-v1",
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self._dimension = dimension
        self._embeddings = embeddings
        canonical = canonical_index_version(index_version, default="rag-v1")
        if canonical is None:
            raise ValueError("invalid index version")
        self._index_version = canonical
        root = Path(StAgent_home).expanduser().resolve()
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

        if self.path.parent.is_symlink():
            raise sqlite3.DatabaseError("knowledge vector directory must not be a symlink")
        if self.path.is_symlink():
            raise sqlite3.DatabaseError("knowledge vector sidecar must not be a symlink")
        for suffix in ("-wal", "-shm"):
            if Path(f"{self.path}{suffix}").is_symlink():
                raise sqlite3.DatabaseError("knowledge vector journal must not be a symlink")
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

    @contextmanager
    def _managed_connection(self) -> Iterator[sqlite3.Connection]:
        """Commit/rollback through SQLite's context manager and always close."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> bool:
        with self._init_lock:
            try:
                with self._managed_connection() as connection:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS knowledge_vector_versions "
                        "(singleton INTEGER PRIMARY KEY CHECK (singleton = 1), active_version TEXT NOT NULL)"
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS knowledge_vector_version_state "
                        "(version TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                    )
                    active = connection.execute(
                        "SELECT active_version FROM knowledge_vector_versions WHERE singleton = 1"
                    ).fetchone()
                    if active is None:
                        connection.execute(
                            "INSERT INTO knowledge_vector_versions(singleton, active_version) VALUES (1, ?)",
                            (self._index_version,),
                        )
                        connection.execute(
                            "UPDATE knowledge_vector_version_state SET status = 'retired' WHERE status = 'active'"
                        )
                        connection.execute(
                            "INSERT INTO knowledge_vector_version_state(version, status) VALUES (?, 'active') "
                            "ON CONFLICT(version) DO UPDATE SET status = 'active'",
                            (self._index_version,),
                        )
                    else:
                        persisted_active = canonical_index_version(str(active[0]))
                        if persisted_active is None:
                            raise ValueError("persisted index version is invalid")
                        self._index_version = persisted_active
                        active_table = version_table_name(self._index_version)
                        if (
                            connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                                (active_table,),
                            ).fetchone()
                            is None
                        ):
                            raise sqlite3.DatabaseError("active vector table is missing")
                        connection.execute(
                            "UPDATE knowledge_vector_version_state SET status = 'retired' "
                            "WHERE status = 'active' AND version != ?",
                            (self._index_version,),
                        )
                        connection.execute(
                            "INSERT INTO knowledge_vector_version_state(version, status) VALUES (?, 'active') "
                            "ON CONFLICT(version) DO UPDATE SET status = 'active'",
                            (self._index_version,),
                        )
                    self._ensure_table(connection, self._index_version)
                    self._verify_integrity(connection)
                return True
            except Exception:  # noqa: BLE001 - projection must never prevent RAG startup
                logger.warning("knowledge_sqlite_vec_unavailable", exc_info=True)
                return False

    def _ensure_table(self, connection: sqlite3.Connection, index_version: str) -> str:
        version = canonical_index_version(index_version)
        if version is None:
            raise ValueError("invalid index version")
        table = version_table_name(version)
        connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
            f"chunk_id TEXT PRIMARY KEY, embedding FLOAT[{self._dimension}], file_id TEXT)"
        )
        return table

    def _verify_integrity(self, connection: sqlite3.Connection) -> None:
        """Fail initialization when SQLite or any persisted vec table is damaged."""
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise sqlite3.DatabaseError("knowledge vector sidecar integrity check failed")
        table_names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'vec_%'"
        ).fetchall()
        for (table_name,) in table_names:
            # Touch each virtual table, including retired/candidate versions,
            # so a damaged projection is quarantined before it can be selected.
            connection.execute(f"SELECT count(*) FROM {table_name}").fetchone()

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
        version = canonical_index_version(index_version, default=self._index_version)
        if version is None:
            return 0
        try:
            rows = [
                (str(chunk_id), self._normalized(vector, self._dimension), str(file_id)) for chunk_id, vector in vectors
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

        with self._managed_connection() as connection:
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
        index_version: str | None = None,
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
                chunk_ids[index]
                if chunk_ids and index < len(chunk_ids)
                else uuid5(NAMESPACE_URL, f"StAgent:knowledge:{file_id}:{index}"),
                vector,
            )
            for index, vector in enumerate(vectors)
            if index < len(chunks)
        ]
        return await self.upsert_vectors(file_id, pairs, index_version=index_version)

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
        version = canonical_index_version(index_version, default=self._index_version)
        if version is None:
            return 0
        async with self._write_lock:
            try:
                return await asyncio.to_thread(self._delete_sync, version, str(file_id))
            except Exception:  # noqa: BLE001
                logger.warning("knowledge_sqlite_vec_delete_degraded", exc_info=True)
                self._available = False
                return 0

    def _delete_sync(self, version: str, file_id: str) -> int:
        try:
            with self._managed_connection() as connection:
                result = connection.execute(f"DELETE FROM {version_table_name(version)} WHERE file_id = ?", (file_id,))
                return max(0, result.rowcount)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise

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
            version = canonical_index_version(index_version, default=self._index_version)
            if version is None:
                return []
            return await asyncio.to_thread(
                self._search_sync,
                version,
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

    def _search_sync(
        self, version: str, vector: list[float], limit: int, file_ids: list[str]
    ) -> list[tuple[str, float]]:
        import sqlite_vec

        best: dict[str, float] = {}
        try:
            with self._managed_connection() as connection:
                table = version_table_name(version)
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
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise
        return sorted(best.items(), key=lambda item: (-item[1], item[0]))[:limit]

    async def begin_version(self, index_version: str) -> bool:
        version = canonical_index_version(index_version)
        if not self.available or version is None or version == self._index_version:
            return False
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._begin_sync, version)
                return True
            except Exception:  # noqa: BLE001
                logger.warning("knowledge_sqlite_vec_begin_degraded", exc_info=True)
                return False

    def _begin_sync(self, version: str) -> None:
        with self._managed_connection() as connection:
            active = connection.execute(
                "SELECT active_version FROM knowledge_vector_versions WHERE singleton = 1"
            ).fetchone()
            if active and str(active[0]) == version:
                raise ValueError("active index version cannot be a candidate")
            self._ensure_table(connection, version)
            connection.execute(
                "INSERT INTO knowledge_vector_version_state(version, status) VALUES (?, 'candidate') "
                "ON CONFLICT(version) DO UPDATE SET status = 'candidate'",
                (version,),
            )

    async def activate_version(self, index_version: str) -> bool:
        version = canonical_index_version(index_version)
        if not self.available or version is None or version == self._index_version:
            return False
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._activate_sync, version)
                self._index_version = version
                return True
            except Exception:  # noqa: BLE001
                logger.warning("knowledge_sqlite_vec_activate_degraded", exc_info=True)
                return False

    def _activate_sync(self, version: str) -> None:
        with self._managed_connection() as connection:
            candidate = connection.execute(
                "SELECT status FROM knowledge_vector_version_state WHERE version = ?", (version,)
            ).fetchone()
            if candidate is None or candidate[0] != "candidate":
                raise ValueError("index version is not a persisted candidate")
            connection.execute(
                "UPDATE knowledge_vector_versions SET active_version = ? WHERE singleton = 1", (version,)
            )
            connection.execute(
                "UPDATE knowledge_vector_version_state SET status = 'retired' WHERE status = 'active' AND version != ?",
                (version,),
            )
            connection.execute(
                "INSERT INTO knowledge_vector_version_state(version, status) VALUES (?, 'active') "
                "ON CONFLICT(version) DO UPDATE SET status = 'active'",
                (version,),
            )

    async def discard_version(self, index_version: str) -> bool:
        version = canonical_index_version(index_version)
        if not self.available or version is None or version == self._index_version:
            return False
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._discard_sync, version)
                return True
            except Exception:  # noqa: BLE001
                logger.warning("knowledge_sqlite_vec_discard_degraded", exc_info=True)
                return False

    def _discard_sync(self, version: str) -> None:
        with self._managed_connection() as connection:
            connection.execute(f"DROP TABLE IF EXISTS {version_table_name(version)}")
            connection.execute("DELETE FROM knowledge_vector_version_state WHERE version = ?", (version,))

    async def version_status(self) -> dict[str, str]:
        """Return durable active/candidate/retired state for rebuild orchestration."""
        if not self.available:
            return {}
        try:
            return await asyncio.to_thread(self._version_status_sync)
        except Exception:  # noqa: BLE001
            logger.warning("knowledge_sqlite_vec_version_status_degraded", exc_info=True)
            return {}

    def _version_status_sync(self) -> dict[str, str]:
        with self._managed_connection() as connection:
            rows = connection.execute("SELECT version, status FROM knowledge_vector_version_state").fetchall()
        return {str(version): str(status) for version, status in rows}


__all__ = ["SqliteVecKnowledgeIndex"]
