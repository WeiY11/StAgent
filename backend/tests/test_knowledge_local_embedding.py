"""Focused tests for the optional local SentenceTransformers provider."""

from __future__ import annotations

import math
import sys
from pathlib import Path  # noqa: TC003
from types import SimpleNamespace

import pytest

from leagent.memory.embeddings import LocalSentenceTransformerEmbeddingProvider


class _Model:
    def __init__(self, *_: object, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, list[str], dict[str, object]]] = []

    def encode_query(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        self.calls.append(("query", texts, kwargs))
        return [[1.0, 2.0] for _ in texts]

    def encode_document(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        self.calls.append(("document", texts, kwargs))
        return [[3.0, 4.0] for _ in texts]


def _install_fake(monkeypatch: pytest.MonkeyPatch, model: type[_Model] = _Model) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=model))


@pytest.mark.asyncio
async def test_provider_loads_only_on_first_embedding_and_routes_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake(monkeypatch)
    model_path = tmp_path / "model"
    model_path.mkdir()
    provider = LocalSentenceTransformerEmbeddingProvider(model_path, dimension=2, normalize=True)
    assert provider._model is None

    assert await provider.embed_query("query") == [1.0, 2.0]
    assert await provider.embed_documents(["doc"]) == [[3.0, 4.0]]
    model = provider._model
    assert model is not None
    assert model.kwargs == {"local_files_only": True, "trust_remote_code": False}
    assert [call[0] for call in model.calls] == ["query", "document"]


@pytest.mark.asyncio
async def test_provider_never_falls_back_to_remote_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _NoDownload:
        def __init__(self, *_: object, **kwargs: object) -> None:
            assert kwargs["local_files_only"] is True
            assert kwargs["trust_remote_code"] is False

        def encode(self, *_: object, **__: object) -> list[list[float]]:
            return [[1.0, 2.0]]

    _install_fake(monkeypatch, _NoDownload)
    model_path = tmp_path / "model"
    model_path.mkdir()
    provider = LocalSentenceTransformerEmbeddingProvider(model_path, dimension=2)
    assert await provider.embed_one("offline") == [1.0, 2.0]


@pytest.mark.asyncio
async def test_provider_rejects_dimension_mismatch_and_nonfinite_vectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Broken(_Model):
        def encode_document(self, texts: list[str], **kwargs: object) -> list[list[float]]:
            return [[math.nan, 2.0] for _ in texts]

    _install_fake(monkeypatch, _Broken)
    model_path = tmp_path / "model"
    model_path.mkdir()
    provider = LocalSentenceTransformerEmbeddingProvider(model_path, dimension=2)
    with pytest.raises(RuntimeError, match="nonfinite"):
        await provider.embed_documents(["bad"])

    class _WrongDimension(_Model):
        def encode_query(self, texts: list[str], **kwargs: object) -> list[list[float]]:
            return [[1.0, 2.0, 3.0] for _ in texts]

    _install_fake(monkeypatch, _WrongDimension)
    provider = LocalSentenceTransformerEmbeddingProvider(model_path, dimension=2)
    with pytest.raises(RuntimeError, match="dimension"):
        await provider.embed_query("bad")
