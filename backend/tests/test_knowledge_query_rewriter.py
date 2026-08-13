"""Behavioral coverage for provider-backed knowledge query rewriting."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import leagent.knowledge.query as query_module


@pytest.mark.asyncio
async def test_llm_query_rewriter_parses_bounded_json_and_preserves_original() -> None:
    """A successful provider rewrite expands retrieval without changing the asked query."""

    class _Llm:
        async def complete(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(
                content=(
                    '{"standalone":"3GPP Release 19 NTN random access",'
                    '"expansions":["GNSS unavailable","PRACH timing compensation"]}'
                )
            )

    rewriter_cls = getattr(query_module, "LLMQueryRewriter", None)
    assert rewriter_cls is not None

    result = await rewriter_cls(_Llm()).rewrite(
        "How does Rel-19 handle it?",
        conversation=[
            {
                "role": "user",
                "content": "For NTN random access without GNSS, what does the standard say?",
            }
        ],
    )

    assert result.original == "How does Rel-19 handle it?"
    assert result.standalone == "3GPP Release 19 NTN random access"
    assert result.expansions == ("GNSS unavailable", "PRACH timing compensation")
    assert result.original in result.variants


@pytest.mark.asyncio
async def test_llm_query_rewriter_keeps_search_available_after_invalid_model_output() -> None:
    """Malformed provider output must not make knowledge search unavailable."""

    class _Llm:
        async def complete(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(content="I cannot produce the requested JSON")

    result = await query_module.LLMQueryRewriter(_Llm()).rewrite("NTN timing advance")

    assert result.original == "NTN timing advance"
    assert result.standalone == "NTN timing advance"
    assert result.expansions == ()


@pytest.mark.asyncio
async def test_llm_query_rewriter_disables_deepseek_thinking_for_json_output() -> None:
    """DeepSeek rewrites reserve completion tokens instead of spending them on reasoning."""

    class _Resolver:
        @staticmethod
        def resolve(_task: object) -> object:
            return SimpleNamespace(provider="deepseek")

    class _Llm:
        resolver = _Resolver()

        async def complete(self, *_args: object, **kwargs: object) -> object:
            if kwargs.get("thinking") == {"type": "disabled"}:
                return SimpleNamespace(content='{"standalone":"NTN timing advance Release 19","expansions":[]}')
            return SimpleNamespace(content="")

    result = await query_module.LLMQueryRewriter(_Llm()).rewrite("NTN timing advance")

    assert result.standalone == "NTN timing advance Release 19"
