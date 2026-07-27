"""Lightweight in-process BM25 (Okapi) ranker for the lexical recall path.

The default (SQLite) deployment has no full-text-search server, so lexical
recall historically fell back to a substring ``ILIKE '%query%'`` scan that
returns every match with a *flat* score ordered only by recency. That has two
costs:

1. **Quality** — a document that merely contains the query substring somewhere
   ranks the same as one that is genuinely *about* the query terms.
2. **Wasted work** — the candidate pool is truncated by recency before any
   relevance consideration, so strong keyword matches created earlier are
   dropped silently.

This module provides a dependency-free BM25 ranker that re-scores the lexical
candidate pool by term relevance so the strongest keyword matches surface
first. Tokenization is English-word + CJK-bigram aware so it works for the
mixed Chinese/English content common in this codebase's memory stores.

The ranker is pure Python over already-loaded :class:`RecallEntry` objects —
it issues no additional database queries and adds only ``O(pool × tokens)``
in-memory work, which is negligible next to the SQL scan it complements.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from leagent.memory.types import RecallEntry

# ASCII alphanumeric runs become word tokens; everything else (punctuation,
# whitespace) is a separator. CJK is handled separately at the character level.
_WORD_RE = re.compile(r"[a-z0-9]+")

# CJK Unified Ideographs (basic + Extension A). A character in this range is
# indexed as a unigram and paired with its neighbour as a bigram so that both
# single-character and compound-term queries match.
_CJK_RANGE = (0x4E00, 0x9FFF)
_CJK_EXT_A = (0x3400, 0x4DBF)

# Standard Okapi BM25 tuning constants.
_K1 = 1.2
_B = 0.75


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        _CJK_RANGE[0] <= code <= _CJK_RANGE[1]
        or _CJK_EXT_A[0] <= code <= _CJK_EXT_A[1]
    )


def tokenize(text: str) -> list[str]:
    """Tokenize mixed English/CJK text into a flat term list.

    English/digit runs are lowercased word tokens; CJK characters yield
    unigrams plus overlapping bigrams. Returns an empty list for blank input.
    """
    if not text:
        return []
    lowered = text.lower()
    tokens: list[str] = _WORD_RE.findall(lowered)

    cjk_run: list[str] = []
    for ch in lowered:
        if _is_cjk(ch):
            cjk_run.append(ch)
        else:
            if cjk_run:
                tokens.extend(_cjk_tokens(cjk_run))
                cjk_run = []
    if cjk_run:
        tokens.extend(_cjk_tokens(cjk_run))
    return tokens


def _cjk_tokens(chars: list[str]) -> list[str]:
    """Unigrams + overlapping bigrams for a run of CJK characters."""
    out = list(chars)
    if len(chars) >= 2:
        out.extend(a + b for a, b in zip(chars, chars[1:], strict=False))
    return out


class BM25Ranker:
    """Okapi BM25 scorer over a fixed corpus of documents.

    Build once per query with the candidate texts, then call :meth:`score`
    (or :meth:`score_tokens` with pre-tokenized input) for each document.
    IDF is computed over the candidate pool itself — appropriate for reranking
    a small, already-filtered lexical candidate set.
    """

    __slots__ = ("_avgdl", "_df", "_idf", "_n")

    def __init__(self, documents: list[str]) -> None:
        self._n = len(documents)
        df: Counter[str] = Counter()
        total_len = 0
        for doc in documents:
            terms = set(tokenize(doc))
            total_len += len(terms)
            for term in terms:
                df[term] += 1
        self._df = df
        self._avgdl = (total_len / self._n) if self._n else 0.0
        # Smoothed IDF, floored at 0 so ubiquitous terms never subtract score.
        self._idf = {
            term: max(0.0, math.log(1.0 + (self._n - freq + 0.5) / (freq + 0.5)))
            for term, freq in df.items()
        }

    def score(self, document: str, query: str) -> float:
        """BM25 score of ``document`` against ``query`` (raw, unbounded)."""
        return self.score_tokens(tokenize(document), tokenize(query))

    def score_tokens(self, doc_tokens: list[str], query_tokens: list[str]) -> float:
        """BM25 score from pre-tokenized document and query terms."""
        if not doc_tokens or not query_tokens or self._avgdl <= 0.0:
            return 0.0
        tf = Counter(doc_tokens)
        dl = len(doc_tokens)
        norm = 1.0 - _B + _B * dl / self._avgdl
        total = 0.0
        for term in query_tokens:
            idf = self._idf.get(term)
            if not idf:
                continue
            f = tf.get(term, 0)
            if f:
                total += idf * (f * (_K1 + 1.0)) / (f + _K1 * norm)
        return total


def rerank_entries(
    entries: list[RecallEntry],
    query: str,
    *,
    rescale_to: float | None = None,
) -> list[RecallEntry]:
    """Sort ``entries`` by BM25 relevance to ``query`` (most relevant first).

    Each entry's ``score`` is replaced with its BM25 score. When
    ``rescale_to`` is given, scores are min-max normalised over the batch and
    multiplied by ``rescale_to`` — useful for keeping lexical scores inside a
    band comparable to the flat scores they replace (e.g. ``0.5``). Entries are
    returned in descending score order; the input list is not mutated.
    """
    if not entries:
        return []
    query_tokens = tokenize(query)
    if not query_tokens:
        return list(entries)

    ranker = BM25Ranker([e.text for e in entries])
    scored: list[tuple[float, RecallEntry]] = [
        (ranker.score_tokens(tokenize(e.text), query_tokens), e) for e in entries
    ]

    if rescale_to is not None:
        max_score = max((s for s, _ in scored), default=0.0)
        if max_score > 0.0:
            scored = [(s / max_score * rescale_to, e) for s, e in scored]

    scored.sort(key=lambda pair: pair[0], reverse=True)
    out: list[RecallEntry] = []
    for score, entry in scored:
        entry.score = score
        out.append(entry)
    return out


__all__ = ["BM25Ranker", "rerank_entries", "tokenize"]
