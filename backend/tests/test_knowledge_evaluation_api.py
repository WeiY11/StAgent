"""API-boundary coverage for knowledge retrieval evaluation metrics."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from StAgent.api.v1 import knowledge as knowledge_api
from StAgent.library import retrieval as retrieval_module
from StAgent.services.auth.deps import get_current_user_id

if TYPE_CHECKING:
    from StAgent.library.retrieval import SearchOptions


@pytest.mark.asyncio
async def test_evaluate_endpoint_reports_recall_and_precision_at_ten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP endpoint must request and score exactly its requested top ten."""

    options_seen: list[SearchOptions] = []

    class _Pipeline:
        async def search(self, options: SearchOptions) -> list[dict[str, str]]:
            options_seen.append(options)
            return [
                {"chunk_id": "a"},
                {"chunk_id": "noise-1"},
                {"chunk_id": "noise-2"},
                {"chunk_id": "b"},
                {"chunk_id": "noise-3"},
                {"chunk_id": "noise-4"},
                {"chunk_id": "noise-5"},
                {"chunk_id": "noise-6"},
                {"chunk_id": "noise-7"},
                {"chunk_id": "c"},
                {"chunk_id": "ignored-after-ten"},
            ]

    pipeline = _Pipeline()
    settings = SimpleNamespace(
        knowledge=SimpleNamespace(
            recall_at_10_target=0.90,
            ndcg_at_10_target=0.80,
            retrieval_p95_ms_target=5000.0,
            citation_accuracy_target=0.95,
            hallucination_rate_max=0.02,
        )
    )
    monkeypatch.setattr(
        retrieval_module,
        "get_knowledge_retrieval_pipeline",
        lambda: pipeline,
    )
    monkeypatch.setattr("StAgent.config.settings.get_settings", lambda: settings)

    user_id = uuid4()
    app = FastAPI()
    app.include_router(knowledge_api.router, prefix="/api/v1/knowledge")
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[knowledge_api.get_database_service] = lambda: object()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/knowledge/evaluate",
            json={
                "cases": [
                    {
                        "query": "top-ten",
                        "relevant_chunk_ids": ["a", "b", "c"],
                    }
                ],
                "k": 10,
                "query_rewrite": True,
                "rerank": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(options_seen) == 1
    options = options_seen[0]
    assert options.limit == 10
    assert options.user_id == user_id
    assert options.query_rewrite is True
    assert options.rerank is False
    assert payload["k"] == 10
    assert payload["recall_at_k"] == 1.0
    assert payload["precision_at_k"] == pytest.approx(0.30)
    assert payload["strict_gate_passed"] is False
