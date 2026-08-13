"""Deterministic scaling benchmark for the local sqlite-vec projection."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from leagent.knowledge.sqlite_vec_index import SqliteVecKnowledgeIndex


def _vector(row: int, dimension: int) -> list[float]:
    """Build a deterministic sparse vector without retaining the full corpus."""
    values = [0.0] * dimension
    values[row % dimension] = 1.0
    values[(row * 17 + 3) % dimension] += 0.5
    return values


async def run_benchmark(*, rows: int, dimension: int, batch_size: int, limit: int) -> dict[str, int | float | bool]:
    """Insert a corpus in bounded batches, run an ACL-filtered query, and report timings."""
    if rows <= 0 or dimension <= 0 or batch_size <= 0 or limit <= 0:
        raise ValueError("rows, dimension, batch_size, and limit must be positive")

    with tempfile.TemporaryDirectory(prefix="leagent-sqlite-vec-bench-") as temp_dir:
        root = Path(temp_dir)
        index = SqliteVecKnowledgeIndex(root, dimension=dimension, index_version="benchmark-v1")
        if not index.available:
            raise RuntimeError("sqlite-vec projection is unavailable")

        file_ids: list[UUID] = []
        inserted = 0
        insert_started = time.perf_counter()
        for offset in range(0, rows, batch_size):
            file_id = uuid5(NAMESPACE_URL, f"leagent-benchmark-file:{offset // batch_size}")
            file_ids.append(file_id)
            vectors = [
                (
                    uuid5(NAMESPACE_URL, f"leagent-benchmark-chunk:{row}"),
                    _vector(row, dimension),
                )
                for row in range(offset, min(offset + batch_size, rows))
            ]
            inserted += await index.upsert_vectors(file_id, vectors)
        insert_seconds = time.perf_counter() - insert_started

        query_started = time.perf_counter()
        hits = await index.search(
            _vector(rows // 2, dimension),
            limit=limit,
            allowed_file_ids=file_ids,
        )
        query_seconds = time.perf_counter() - query_started
        database_bytes = index.path.stat().st_size

        return {
            "available": index.available,
            "rows_requested": rows,
            "rows_inserted": inserted,
            "dimension": dimension,
            "batch_size": batch_size,
            "allowed_file_ids": len(file_ids),
            "hits": len(hits),
            "insert_seconds": round(insert_seconds, 3),
            "rows_per_second": round(inserted / insert_seconds, 1),
            "query_seconds": round(query_seconds, 3),
            "database_bytes": database_bytes,
        }


def main() -> None:
    """Parse CLI arguments and print one machine-readable result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--dimension", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    result = asyncio.run(
        run_benchmark(
            rows=args.rows,
            dimension=args.dimension,
            batch_size=args.batch_size,
            limit=args.limit,
        )
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
