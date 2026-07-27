"""Unit tests for the in-process BM25 ranker (:mod:`leagent.memory.bm25`).

Covers the mixed English/CJK tokenizer, the Okapi BM25 scorer, and the
``rerank_entries`` helper that the recall pipeline uses to order lexical
candidates by relevance.
"""

from __future__ import annotations

from uuid import uuid4

from leagent.memory.bm25 import BM25Ranker, rerank_entries, tokenize
from leagent.memory.types import MemoryKind, RecallEntry


def _entry(text: str, score: float = 0.4) -> RecallEntry:
    return RecallEntry(
        kind=MemoryKind.EPISODIC,
        text=text,
        score=score,
        source_id=uuid4(),
        metadata={},
    )


class TestTokenize:
    def test_english_words_lowercased(self) -> None:
        assert tokenize("Edge Computing OFFLOADING") == [
            "edge",
            "computing",
            "offloading",
        ]

    def test_punctuation_is_separator(self) -> None:
        assert tokenize("hello, world! foo-bar") == ["hello", "world", "foo", "bar"]

    def test_digits_kept(self) -> None:
        assert tokenize("model v2 release") == ["model", "v2", "release"]

    def test_cjk_unigrams_and_bigrams(self) -> None:
        tokens = tokenize("边缘计算")
        # unigrams
        for ch in "边缘计算":
            assert ch in tokens
        # overlapping bigrams
        assert "边缘" in tokens
        assert "缘计" in tokens
        assert "计算" in tokens

    def test_mixed_english_cjk(self) -> None:
        tokens = tokenize("边缘计算 edge computing")
        assert "edge" in tokens
        assert "computing" in tokens
        assert "边缘" in tokens

    def test_blank_input(self) -> None:
        assert tokenize("") == []
        assert tokenize("   ") == []


class TestBM25Ranker:
    def test_relevant_doc_outranks_unrelated(self) -> None:
        docs = [
            "edge computing task offloading for vehicles",
            "recipe for chocolate cake baking",
            "edge offloading latency optimization",
        ]
        ranker = BM25Ranker(docs)
        query = "edge computing offloading"
        scores = [ranker.score(d, query) for d in docs]
        assert scores[0] > scores[1]
        assert scores[2] > scores[1]

    def test_cjk_query_matches_cjk_doc(self) -> None:
        docs = ["边缘计算任务卸载", "巧克力蛋糕烘焙食谱", "边缘节点卸载优化"]
        ranker = BM25Ranker(docs)
        scores = [ranker.score(d, "边缘计算 卸载") for d in docs]
        assert scores[0] > scores[1]
        assert scores[2] > scores[1]

    def test_empty_corpus_scores_zero(self) -> None:
        ranker = BM25Ranker([])
        assert ranker.score("anything", "query") == 0.0

    def test_no_term_overlap_scores_zero(self) -> None:
        ranker = BM25Ranker(["alpha beta gamma"])
        assert ranker.score("alpha beta", "zeta eta") == 0.0


class TestRerankEntries:
    def test_orders_by_relevance(self) -> None:
        weak = _entry("recipe for chocolate cake baking")
        strong = _entry("edge computing task offloading for vehicles")
        mid = _entry("edge offloading latency optimization")

        out = rerank_entries([weak, strong, mid], "edge computing offloading")
        assert out[0].text == strong.text
        assert out[-1].text == weak.text

    def test_rescale_bounds_max_score(self) -> None:
        entries = [
            _entry("edge computing task offloading"),
            _entry("unrelated chocolate cake"),
        ]
        out = rerank_entries(entries, "edge computing", rescale_to=0.5)
        assert out[0].score <= 0.5 + 1e-9
        assert out[0].score > out[1].score

    def test_empty_query_preserves_order(self) -> None:
        a = _entry("alpha")
        b = _entry("beta")
        out = rerank_entries([a, b], "   ")
        assert [e.text for e in out] == ["alpha", "beta"]

    def test_empty_entries(self) -> None:
        assert rerank_entries([], "query") == []
