"""End-to-end contracts for a fully isolated public retrieval benchmark run."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from StAgent.knowledge.evaluation_dataset import parse_public_retrieval_dataset
from StAgent.knowledge.evaluation_runner import run_isolated_public_retrieval_evaluation

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_isolated_runner_writes_auditable_lexical_retrieval_results(
    tmp_path: Path,
) -> None:
    """The runner imports, evaluates, and records an isolated public corpus."""

    long_passage = "火星轨道导航" * 200
    dataset = parse_public_retrieval_dataset(
        dataset_id="example/public-retrieval",
        revision="revision-1",
        corpus_rows=[
            {"_id": "passage-a", "text": long_passage},
            {"_id": "passage-b", "text": "海洋气候观测浮标记录海温。"},
            {"_id": "passage-c", "text": "城市交通信号优化降低拥堵。"},
        ],
        query_rows=[
            {"_id": "query-a", "text": "火星轨道导航"},
            {"_id": "query-b", "text": "海洋气候观测"},
        ],
        qrel_rows=[
            {"query-id": "query-a", "corpus-id": "passage-a"},
            {"query-id": "query-b", "corpus-id": "passage-b"},
        ],
    )
    run_dir = tmp_path / "runs" / "isolated-run"

    result = await run_isolated_public_retrieval_evaluation(dataset, run_dir)

    assert run_dir.is_dir()
    assert (run_dir / "evaluation.sqlite3").is_file()
    assert (run_dir / "blobs").is_dir()
    assert (run_dir / "chunk_mapping.jsonl").is_file()
    assert (run_dir / "results.json").is_file()
    assert result["isolation"]["vector_embedding"] == "disabled"
    assert result["isolation"]["query_rewrite"] == "disabled"
    assert (
        result["mapping_validation"].items()
        >= {
            "corpus_passages": 3,
            "files": 3,
            "chunks": 3,
            "duplicate_chunk_ids": 0,
            "chunking_strategy": "one-passage-one-chunk-v1",
        }.items()
    )
    assert len(result["mapping_validation"]["mapping_sha256"]) == 64
    assert result["mapping_validation"]["index_version"]
    assert result["answer_accuracy"] is None
    assert set(result["configurations"]) == {"lexical_baseline", "lexical_rerank"}

    persisted = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert persisted["mapping_validation"] == result["mapping_validation"]
    for name, configuration in persisted["configurations"].items():
        assert configuration["rerank"] is (name == "lexical_rerank")
        assert configuration["metrics"]["at_5"]["k"] == 5
        assert configuration["metrics"]["at_10"]["k"] == 10
        assert configuration["query_count"] == 2
        case_path = run_dir / configuration["case_records_file"]
        assert case_path.is_file()
        assert len(case_path.read_text(encoding="utf-8").splitlines()) == 2

    mapping_rows = (run_dir / "chunk_mapping.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(mapping_rows) == 3
    assert max(json.loads(row)["text_char_count"] for row in mapping_rows) == len(long_passage)
