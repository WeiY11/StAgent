"""Tests for FileProcessingService: routing, handler resolution, text extraction."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from StAgent.services.file_processing.service import (
    EXTENSION_HANDLER_MAP,
    FileProcessingService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service() -> FileProcessingService:
    return FileProcessingService()


# ---------------------------------------------------------------------------
# _resolve_handler
# ---------------------------------------------------------------------------


class TestResolveHandler:
    def test_pdf_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, None, None) == "pdf_reader"

    def test_docx_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.docx"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, None, None) == "word_reader"

    def test_doc_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "legacy.doc"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, None, None) == "word_reader"

    def test_xlsx_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "data.xlsx"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, None, None) == "excel_reader"

    def test_csv_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, None, None) == "csv_processor"

    def test_md_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "README.md"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, None, None) == "markdown_processor"

    def test_html_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "page.html"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, None, None) == "html_processor"

    def test_txt_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "notes.txt"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, None, None) == "text_processor"

    def test_zip_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "archive.zip"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, None, None) == "archive_manager"

    def test_fallback_to_mime_pdf(self, tmp_path: Path) -> None:
        f = tmp_path / "noextension"
        f.touch()
        svc = _make_service()
        handler = svc._resolve_handler(f, "application/pdf", None)
        assert handler == "pdf_reader"

    def test_fallback_to_mime_html(self, tmp_path: Path) -> None:
        f = tmp_path / "noextension"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, "text/html", None) == "html_processor"

    def test_fallback_to_original_name(self, tmp_path: Path) -> None:
        f = tmp_path / "upload_abc123"
        f.touch()
        svc = _make_service()
        handler = svc._resolve_handler(f, None, "report.pdf")
        assert handler == "pdf_reader"

    def test_unknown_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "file.xyz_unknown"
        f.touch()
        svc = _make_service()
        assert svc._resolve_handler(f, None, None) is None


# ---------------------------------------------------------------------------
# _extract_text_from_result
# ---------------------------------------------------------------------------


class TestExtractTextFromResult:
    def _svc(self) -> FileProcessingService:
        return FileProcessingService()

    def test_extracts_text_key(self) -> None:
        svc = self._svc()
        result = {"text": "hello world"}
        assert svc._extract_text_from_result("pdf_reader", result) == "hello world"

    def test_extracts_content_key(self) -> None:
        svc = self._svc()
        result = {"content": "some content"}
        assert svc._extract_text_from_result("text_processor", result) == "some content"

    def test_extracts_raw_text_key(self) -> None:
        svc = self._svc()
        result = {"raw_text": "raw content here"}
        assert svc._extract_text_from_result("html_processor", result) == "raw content here"

    def test_extracts_extracted_text_key(self) -> None:
        svc = self._svc()
        result = {"extracted_text": "extracted!"}
        assert svc._extract_text_from_result("pdf_reader", result) == "extracted!"

    def test_nested_data_dict(self) -> None:
        svc = self._svc()
        result = {"data": {"text": "nested text"}}
        assert svc._extract_text_from_result("pdf_reader", result) == "nested text"

    def test_non_dict_result_returns_none(self) -> None:
        svc = self._svc()
        assert svc._extract_text_from_result("pdf_reader", None) is None  # type: ignore[arg-type]

    def test_no_text_key_returns_none(self) -> None:
        svc = self._svc()
        assert svc._extract_text_from_result("pdf_reader", {"pages": [1, 2]}) is None

    def test_truncates_long_text(self) -> None:
        svc = self._svc()
        long_text = "a" * 600_000
        result = {"text": long_text}
        extracted = svc._extract_text_from_result("pdf_reader", result)
        assert extracted is not None
        assert len(extracted) <= 500_000


# ---------------------------------------------------------------------------
# process_file — integration tests with real files via mock registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestProcessFile:
    async def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        svc = _make_service()
        result = await svc.process_file("/nonexistent/path/to/file.txt")
        assert "error" in result
        assert result.get("extracted_text") is None

    async def test_unknown_extension_no_handler(self, tmp_path: Path) -> None:
        f = tmp_path / "file.xyz_unknown"
        f.write_text("content", encoding="utf-8")
        svc = _make_service()
        result = await svc.process_file(str(f))
        assert result.get("handler") is None

    async def test_process_txt_file(self, sample_txt: Path) -> None:
        svc = _make_service()
        with patch.object(svc, "_run_handler", new=AsyncMock(return_value={"text": "hello"})):
            result = await svc.process_file(str(sample_txt))
        assert result["handler"] == "text_processor"
        assert result["extracted_text"] == "hello"

    async def test_process_csv_file(self, sample_csv: Path) -> None:
        svc = _make_service()
        mock_data = {"rows": [[1, "a"], [2, "b"]], "text": "tabular data"}
        with patch.object(svc, "_run_handler", new=AsyncMock(return_value=mock_data)):
            result = await svc.process_file(str(sample_csv))
        assert result["handler"] == "csv_processor"

    async def test_process_with_mime_type_override(self, tmp_path: Path) -> None:
        f = tmp_path / "upload"
        f.write_text("some html content", encoding="utf-8")
        svc = _make_service()
        mock_data = {"content": "html content extracted"}
        with patch.object(svc, "_run_handler", new=AsyncMock(return_value=mock_data)):
            result = await svc.process_file(str(f), mime_type="text/html")
        assert result["handler"] == "html_processor"
        assert result["extracted_text"] == "html content extracted"

    async def test_handler_exception_returns_error(self, sample_txt: Path) -> None:
        svc = _make_service()
        with patch.object(svc, "_run_handler", side_effect=RuntimeError("tool failed")):
            result = await svc.process_file(str(sample_txt))
        assert "error" in result
        assert result.get("extracted_text") is None


@pytest.mark.asyncio
async def test_s3_source_is_staged_only_for_path_based_extraction() -> None:
    file_id = uuid4()

    class _Ref:
        filename = "evidence.txt"
        metadata = {"storage_path": "s3://kb/StAgent/evidence.txt"}

    class _FileService:
        async def resolve(self, requested_id):  # noqa: ANN001
            assert requested_id == file_id
            return _Ref()

        async def download(self, ref):  # noqa: ANN001
            assert isinstance(ref, _Ref)
            return b"remote evidence", "text/plain"

    class _ServiceManager:
        file_service = _FileService()

    path, staged_path = await FileProcessingService._materialize_processing_source(
        _ServiceManager(),
        file_id=str(file_id),
        storage_path="s3://kb/StAgent/evidence.txt",
        original_name="evidence.txt",
    )
    try:
        assert staged_path == path
        assert Path(path).read_bytes() == b"remote evidence"
    finally:
        if staged_path:
            os.unlink(staged_path)


@pytest.mark.asyncio
async def test_reindex_removes_stale_vectors_before_upserting_replacements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed document must not leave prior chunk vectors in Milvus."""
    file_id = uuid4()
    owner_id = uuid4()

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Chunks:
        async def replace_for_file(self, *_args: object) -> int:
            return 1

        async def list_ids_for_file(self, *_args: object) -> list[object]:
            return [uuid4()]

    class _Db:
        repositories = SimpleNamespace(document_chunks=_Chunks())

        def session(self) -> _Session:
            return _Session()

    operations: list[tuple[str, object]] = []

    class _VectorIndex:
        available = True

        async def delete_for_file(self, logical_id):  # noqa: ANN001
            operations.append(("delete", logical_id))
            return 1

        async def upsert_chunks(self, logical_id, *_args: object, **_kwargs: object) -> int:
            operations.append(("upsert", logical_id))
            return 1

    manager = SimpleNamespace(db=_Db())
    service = FileProcessingService()
    monkeypatch.setattr(
        "StAgent.services.service_manager.get_service_manager",
        lambda: manager,
    )

    async def load_file(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            user_id=owner_id,
            mime_type="text/plain",
            original_name="proof.txt",
            name="proof.txt",
        )

    monkeypatch.setattr("StAgent.db.sqlite_compat.load_entity_by_id", load_file)
    monkeypatch.setattr(
        "StAgent.knowledge.vector_index.get_knowledge_vector_index",
        lambda: _VectorIndex(),
    )
    monkeypatch.setattr(service, "_set_index_state", AsyncMock())
    monkeypatch.setattr(service, "_update_file_record", AsyncMock())

    await service._index_chunks(str(file_id), {"extracted_text": "fresh RAG evidence"})

    assert operations == [("delete", file_id), ("upsert", file_id)]


@pytest.mark.asyncio
async def test_unchanged_content_reindexes_when_index_version_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An embedding/index upgrade must not reuse an older ready projection."""
    file_id = uuid4()
    content_hash = "same-content-hash"

    class _Result:
        def __init__(self, value: object) -> None:
            self._value = value

        def first(self) -> object:
            return self._value

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def exec(self, statement: object) -> _Result:
            text = str(statement)
            if "file_contents" in text:
                return _Result(content_hash)
            if "document_chunks" in text:
                return _Result(uuid4())
            if "knowledge_index_states" in text:
                return _Result(
                    SimpleNamespace(
                        index_version="rag-v1",
                        status="ready",
                        content_hash=content_hash,
                    )
                )
            raise AssertionError(f"unexpected statement: {text}")

    class _Db:
        def session(self) -> _Session:
            return _Session()

    monkeypatch.setattr(
        "StAgent.services.service_manager.get_service_manager",
        lambda: SimpleNamespace(db=_Db()),
    )
    monkeypatch.setattr(
        "StAgent.db.sqlite_compat.uuid_column_equals",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "StAgent.config.settings.get_settings",
        lambda: SimpleNamespace(knowledge=SimpleNamespace(index_version="rag-v2")),
    )

    service = FileProcessingService()

    assert not await service._content_unchanged(str(file_id), content_hash)


@pytest.mark.asyncio
async def test_index_state_lookup_failure_does_not_mask_file_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort state reads must not turn their own logging into an error."""

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def exec(self, _statement: object) -> object:
            raise RuntimeError("index state table unavailable")

    class _Db:
        def session(self) -> _Session:
            return _Session()

    monkeypatch.setattr(
        "StAgent.services.service_manager.get_service_manager",
        lambda: SimpleNamespace(db=_Db()),
    )

    assert await FileProcessingService()._get_index_state(str(uuid4())) is None


@pytest.mark.asyncio
async def test_index_lifecycle_records_the_configured_index_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jobs and durable state must identify the same active index version."""
    file_id = uuid4()
    user_id = uuid4()
    enqueued: list[dict[str, object]] = []
    states: list[dict[str, object]] = []

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _IndexRepository:
        async def enqueue(self, *_args: object, **kwargs: object) -> str:
            enqueued.append(kwargs)
            return "job-1"

        async def upsert_state(self, *_args: object, **kwargs: object) -> None:
            states.append(kwargs)

    class _Db:
        repositories = SimpleNamespace(knowledge_index=_IndexRepository())

        def session(self) -> _Session:
            return _Session()

    async def load_file(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(user_id=user_id)

    monkeypatch.setattr(
        "StAgent.services.service_manager.get_service_manager",
        lambda: SimpleNamespace(db=_Db()),
    )
    monkeypatch.setattr("StAgent.db.sqlite_compat.load_entity_by_id", load_file)
    monkeypatch.setattr(
        "StAgent.config.settings.get_settings",
        lambda: SimpleNamespace(knowledge=SimpleNamespace(index_version="rag-v2")),
    )

    service = FileProcessingService()

    assert await service._enqueue_index_job(str(file_id), "hash") == "job-1"
    await service._set_index_state(str(file_id), "ready", content_hash="hash")

    assert enqueued == [{"content_hash": "hash", "index_version": "rag-v2"}]
    assert states[0]["index_version"] == "rag-v2"


# ---------------------------------------------------------------------------
# Extension map completeness
# ---------------------------------------------------------------------------


class TestExtensionMap:
    def test_common_extensions_present(self) -> None:
        required = [".pdf", ".docx", ".xlsx", ".csv", ".txt", ".html", ".md", ".zip"]
        for ext in required:
            assert ext in EXTENSION_HANDLER_MAP, f"{ext} missing from EXTENSION_HANDLER_MAP"

    def test_all_handlers_valid_names(self) -> None:
        valid = {
            "pdf_reader",
            "word_reader",
            "excel_reader",
            "csv_processor",
            "html_processor",
            "markdown_processor",
            "text_processor",
            "config_file",
            "archive_manager",
            "image_ocr",
        }
        for ext, handler in EXTENSION_HANDLER_MAP.items():
            assert handler in valid, f"Unknown handler {handler!r} for extension {ext}"
