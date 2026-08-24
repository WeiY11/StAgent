"""Contracts for explicit public retrieval-data preparation."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prepare_retrieval_eval.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepare_retrieval_eval", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["prepare_retrieval_eval"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_prepare_writes_validated_pinned_nanomiracl_assets(tmp_path: Path) -> None:
    """Downloader writes only corpus, queries, qrels, and a checksum manifest."""

    tool = _load_tool()
    pages: dict[tuple[str, int], dict[str, Any]] = {
        ("corpus", 0): {
            "rows": [
                {"row": {"_id": "doc-a", "text": "第一段语料"}},
                {"row": {"_id": "doc-b", "text": "第二段语料"}},
            ],
            "num_rows_total": 2,
        },
        ("queries", 0): {
            "rows": [{"row": {"_id": "query-1", "text": "第二段是什么？"}}],
            "num_rows_total": 1,
        },
        ("qrels", 0): {
            "rows": [{"row": {"query-id": "query-1", "corpus-id": "doc-b"}}],
            "num_rows_total": 1,
        },
    }
    requests: list[tuple[str, int, int]] = []

    async def fetch_page(config: str, offset: int, length: int) -> dict[str, Any]:
        requests.append((config, offset, length))
        return pages[(config, offset)]

    output_dir = tmp_path / "nanomiracl-zh"
    result = await tool.prepare_nanomiracl_zh(
        output_dir,
        fetch_page=fetch_page,
        destination_root=tmp_path,
    )

    assert result["dataset_id"] == "hotchpotch/NanoMIRACL"
    assert result["dataset_revision"] == tool.NANOMIRACL_ZH_REVISION
    assert result["counts"] == {"corpus": 2, "queries": 1, "qrels": 1}
    assert requests == [("corpus", 0, 100), ("queries", 0, 100), ("qrels", 0, 100)]
    assert (output_dir / "bm25.jsonl").exists() is False

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_id"] == "hotchpotch/NanoMIRACL"
    assert manifest["dataset_revision"] == tool.NANOMIRACL_ZH_REVISION
    assert manifest["source"]["data_card"].endswith(f"/{tool.NANOMIRACL_ZH_REVISION}/README.md")
    assert manifest["qrel_level"] == "passage"
    assert manifest["files"]["corpus.jsonl"]["rows"] == 2
    assert len(manifest["files"]["corpus.jsonl"]["sha256"]) == 64

    corpus_rows = [json.loads(line) for line in (output_dir / "corpus.jsonl").read_text(encoding="utf-8").splitlines()]
    assert corpus_rows == [
        {"source_id": "doc-a", "text": "第一段语料"},
        {"source_id": "doc-b", "text": "第二段语料"},
    ]
    assert tool.verify_nanomiracl_zh(output_dir) == manifest


@pytest.mark.asyncio
async def test_verify_rejects_tampered_prepared_file(tmp_path: Path) -> None:
    """Manifest verification detects a changed public corpus file before use."""

    tool = _load_tool()
    pages: dict[tuple[str, int], dict[str, Any]] = {
        ("corpus", 0): {
            "rows": [{"row": {"_id": "doc-a", "text": "原始语料"}}],
            "num_rows_total": 1,
        },
        ("queries", 0): {
            "rows": [{"row": {"_id": "query-1", "text": "问题"}}],
            "num_rows_total": 1,
        },
        ("qrels", 0): {
            "rows": [{"row": {"query-id": "query-1", "corpus-id": "doc-a"}}],
            "num_rows_total": 1,
        },
    }

    async def fetch_page(config: str, offset: int, _length: int) -> dict[str, Any]:
        return pages[(config, offset)]

    output_dir = tmp_path / "nanomiracl-zh"
    await tool.prepare_nanomiracl_zh(
        output_dir,
        fetch_page=fetch_page,
        destination_root=tmp_path,
    )
    (output_dir / "corpus.jsonl").write_text(
        '{"source_id":"doc-a","text":"被篡改"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SHA-256 mismatch for corpus.jsonl"):
        tool.verify_nanomiracl_zh(output_dir)


@pytest.mark.asyncio
async def test_prepare_refuses_to_overwrite_existing_dataset(tmp_path: Path) -> None:
    """An existing target is never replaced by an automatic redownload."""

    tool = _load_tool()
    output_dir = tmp_path / "existing-dataset"
    output_dir.mkdir()
    requests: list[tuple[str, int, int]] = []

    async def fetch_page(config: str, offset: int, length: int) -> dict[str, Any]:
        requests.append((config, offset, length))
        return {"rows": [], "num_rows_total": 0}

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        await tool.prepare_nanomiracl_zh(
            output_dir,
            fetch_page=fetch_page,
            destination_root=tmp_path,
        )

    assert requests == []


@pytest.mark.asyncio
async def test_prepare_rejects_output_outside_allowed_download_tree(tmp_path: Path) -> None:
    """The CLI helper cannot be redirected to an arbitrary filesystem location."""

    tool = _load_tool()
    requests: list[tuple[str, int, int]] = []

    async def fetch_page(config: str, offset: int, length: int) -> dict[str, Any]:
        requests.append((config, offset, length))
        return {"rows": [], "num_rows_total": 0}

    with pytest.raises(ValueError, match="must be inside the retrieval evaluation download tree"):
        await tool.prepare_nanomiracl_zh(
            tmp_path.parent / "outside-download-tree",
            fetch_page=fetch_page,
            destination_root=tmp_path,
        )

    assert requests == []
