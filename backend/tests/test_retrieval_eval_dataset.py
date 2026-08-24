"""Offline contracts for public retrieval-evaluation dataset conversion."""

from __future__ import annotations

import pytest

from StAgent.knowledge.evaluation_dataset import (
    build_evaluation_cases,
    parse_public_retrieval_dataset,
)


def test_public_rows_map_to_one_chunk_per_passage_evaluation_cases() -> None:
    """Document qrels become chunk qrels only under an explicit 1:1 map."""

    dataset = parse_public_retrieval_dataset(
        dataset_id="example/nanomiracl",
        revision="revision-1",
        corpus_rows=[
            {"_id": "doc-a", "text": "第一段语料"},
            {"_id": "doc-b", "text": "第二段语料"},
        ],
        query_rows=[
            {"_id": "query-1", "text": "第二段是什么？"},
        ],
        qrel_rows=[
            {"query-id": "query-1", "corpus-id": "doc-b"},
        ],
    )

    cases = build_evaluation_cases(
        dataset,
        source_to_chunk_ids={
            "doc-a": ["chunk-a"],
            "doc-b": ["chunk-b"],
        },
    )

    assert dataset.dataset_id == "example/nanomiracl"
    assert dataset.revision == "revision-1"
    assert len(dataset.corpus) == 2
    assert len(dataset.queries) == 1
    assert len(dataset.qrels) == 1
    assert cases[0].case_id == "query-1"
    assert cases[0].query == "第二段是什么？"
    assert cases[0].relevant_chunk_ids == frozenset({"chunk-b"})


def test_public_rows_reject_qrel_pointing_to_unknown_corpus() -> None:
    """Broken source qrels must fail before they can distort recall."""

    with pytest.raises(ValueError, match="qrel 0 references unknown corpus id 'missing-doc'"):
        parse_public_retrieval_dataset(
            dataset_id="example/dataset",
            revision="revision-1",
            corpus_rows=[{"_id": "doc-a", "text": "语料"}],
            query_rows=[{"_id": "query-1", "text": "问题"}],
            qrel_rows=[{"query-id": "query-1", "corpus-id": "missing-doc"}],
        )


def test_evaluation_cases_reject_multi_chunk_passage_mapping() -> None:
    """A document-level qrel cannot be silently expanded into many chunks."""

    dataset = parse_public_retrieval_dataset(
        dataset_id="example/dataset",
        revision="revision-1",
        corpus_rows=[{"_id": "doc-a", "text": "语料"}],
        query_rows=[{"_id": "query-1", "text": "问题"}],
        qrel_rows=[{"query-id": "query-1", "corpus-id": "doc-a"}],
    )

    with pytest.raises(ValueError, match="doc-a must map to exactly one chunk"):
        build_evaluation_cases(
            dataset,
            source_to_chunk_ids={"doc-a": ["chunk-a-1", "chunk-a-2"]},
        )
