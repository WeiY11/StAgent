"""Deterministic and provider-backed query rewriting for knowledge search."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class QueryRewriteResult:
    """Bounded rewrite output; ACLs are never changed by this layer."""

    original: str
    standalone: str
    expansions: tuple[str, ...] = ()

    @property
    def variants(self) -> tuple[str, ...]:
        out: list[str] = []
        for value in (self.standalone, *self.expansions, self.original):
            value = re.sub(r"\s+", " ", value or "").strip()
            if value and value not in out:
                out.append(value)
        return tuple(out[:4])


class QueryRewriter(Protocol):
    async def rewrite(
        self,
        query: str,
        *,
        conversation: list[dict[str, str]] | None = None,
    ) -> QueryRewriteResult: ...


class DeterministicQueryRewriter:
    """Safe local rewrite with no invented synonyms or permissions."""

    async def rewrite(
        self,
        query: str,
        *,
        conversation: list[dict[str, str]] | None = None,
    ) -> QueryRewriteResult:
        original = re.sub(r"\s+", " ", query or "").strip()
        latest_user = ""
        for turn in reversed(conversation or []):
            if str(turn.get("role", "")).lower() == "user":
                latest_user = re.sub(r"\s+", " ", str(turn.get("content", ""))).strip()
                break
        standalone = original
        if latest_user and len(original) < 12 and latest_user != original:
            standalone = f"{latest_user} {original}".strip()
        # Keep only explicit quoted/parenthesized terms as expansions.  This
        # improves lexical recall without allowing a model to widen ACL scope.
        terms = re.findall(r"[\"“”']([^\"“”']{2,80})[\"“”']", original)
        expansions = tuple(term.strip() for term in terms if term.strip() != standalone)[:3]
        return QueryRewriteResult(original=original, standalone=standalone, expansions=expansions)


class CallableQueryRewriter:
    """Adapter for an injected LLM rewrite callable with strict bounds."""

    def __init__(self, call: Any, fallback: QueryRewriter | None = None) -> None:
        self._call = call
        self._fallback = fallback or DeterministicQueryRewriter()

    async def rewrite(
        self,
        query: str,
        *,
        conversation: list[dict[str, str]] | None = None,
    ) -> QueryRewriteResult:
        fallback = await self._fallback.rewrite(query, conversation=conversation)
        try:
            raw = await self._call(query, conversation=conversation)
            if isinstance(raw, dict):
                standalone = str(raw.get("standalone") or fallback.standalone).strip()
                expansions_raw = raw.get("expansions")
                expansions = tuple(
                    str(item).strip()
                    for item in (expansions_raw if isinstance(expansions_raw, list) else [])
                    if str(item).strip()
                )[:3]
                return QueryRewriteResult(
                    original=fallback.original,
                    standalone=standalone[:1000],
                    expansions=expansions,
                )
        except Exception:  # noqa: BLE001
            pass
        return fallback


__all__ = [
    "CallableQueryRewriter",
    "DeterministicQueryRewriter",
    "QueryRewriteResult",
    "QueryRewriter",
]
