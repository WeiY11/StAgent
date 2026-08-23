"""Content-quality gates for RAG ingestion: signature + mojibake defenses.

Covers ``StAgent.services.file_processing.content_quality`` and its wiring
into ``FileProcessingService``, plus the markdown/html encoding-detection
upgrade that keeps GBK documents out of the replacement-character swamp.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from StAgent.services.file_processing.content_quality import (
    assess_extracted_text,
    assess_file_signature,
)
from StAgent.services.file_processing.service import FileProcessingService
from StAgent.tools.doc.html_processor import _read_file as read_html
from StAgent.tools.doc.markdown_processor import _read_text as read_markdown

if TYPE_CHECKING:
    from pathlib import Path

_PDF_HEAD = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
_PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_BINARY_GARBAGE = bytes(range(256)) * 4


# ---------------------------------------------------------------------------
# assess_extracted_text
# ---------------------------------------------------------------------------


class TestAssessExtractedText:
    def test_clean_bilingual_text_passes(self) -> None:
        text = "# 项目说明\n\n" + "知识库摄取管线。Knowledge ingestion pipeline.\n" * 50
        verdict = assess_extracted_text(text)
        assert verdict.passed
        assert verdict.metrics["replacement_count"] == 0

    def test_few_replacement_chars_pass(self) -> None:
        text = "正文内容。" * 1000 + "\ufffd" * 3
        assert assess_extracted_text(text).passed

    def test_short_text_high_ratio_passes_on_absolute_floor(self) -> None:
        # 40 chars, 4 replacement chars = 10% ratio but count < 20 floor.
        text = "ok text " * 5 + "\ufffd" * 4
        assert assess_extracted_text(text).passed

    def test_heavy_replacement_rejected(self) -> None:
        text = "锟斤拷" * 30 + "\ufffd" * 200
        verdict = assess_extracted_text(text)
        assert not verdict.passed
        assert verdict.reason == "mojibake_content"
        assert verdict.metrics["replacement_count"] >= 200

    def test_control_chars_rejected(self) -> None:
        text = ("line\n" + "\x01\x02\x03\x04\x05" * 8) * 4
        verdict = assess_extracted_text(text)
        assert not verdict.passed
        assert verdict.reason == "binary_control_chars"

    def test_nul_bytes_rejected(self) -> None:
        text = "readable text\n" + "\x00" * 12
        verdict = assess_extracted_text(text)
        assert not verdict.passed
        assert verdict.reason == "nul_bytes"

    def test_metadata_likely_binary_rejected(self) -> None:
        text = "could be gb18030-decoded binary garbage with no U+FFFD"
        metadata = {"decode": {"detection": {"likely_binary": True}}}
        verdict = assess_extracted_text(text, metadata=metadata)
        assert not verdict.passed
        assert verdict.reason == "binary_content"

    def test_gate_disabled_via_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "StAgent.config.settings.get_settings",
            lambda: SimpleNamespace(
                knowledge=SimpleNamespace(content_quality_gate_enabled=False)
            ),
        )
        text = "\ufffd" * 500
        assert assess_extracted_text(text).passed


# ---------------------------------------------------------------------------
# assess_file_signature
# ---------------------------------------------------------------------------


class TestAssessFileSignature:
    def test_valid_pdf_passes(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(_PDF_HEAD + b"1 0 obj")
        assert assess_file_signature(f) is None

    def test_binary_renamed_pdf_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(_BINARY_GARBAGE)
        error = assess_file_signature(f)
        assert error is not None and ".pdf" in error

    def test_empty_pdf_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.touch()
        assert assess_file_signature(f) is not None

    def test_text_files_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "notes.txt"
        f.write_bytes(_BINARY_GARBAGE)
        assert assess_file_signature(f) is None

    def test_valid_png_passes(self, tmp_path: Path) -> None:
        f = tmp_path / "img.png"
        f.write_bytes(_PNG_HEAD)
        assert assess_file_signature(f) is None

    def test_docx_requires_zip_header(self, tmp_path: Path) -> None:
        good = tmp_path / "good.docx"
        good.write_bytes(b"PK\x03\x04" + b"\x00" * 12)
        assert assess_file_signature(good) is None

        bad = tmp_path / "bad.docx"
        bad.write_bytes(_BINARY_GARBAGE)
        assert assess_file_signature(bad) is not None

    def test_legacy_doc_requires_ole2_header(self, tmp_path: Path) -> None:
        f = tmp_path / "old.doc"
        f.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8)
        assert assess_file_signature(f) is None

        bad = tmp_path / "broken.doc"
        bad.write_bytes(_BINARY_GARBAGE)
        assert assess_file_signature(bad) is not None

    def test_mime_fallback_without_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "upload_abc123"
        f.write_bytes(_BINARY_GARBAGE)
        error = assess_file_signature(f, mime_type="application/pdf")
        assert error is not None

    def test_original_name_suffix_wins(self, tmp_path: Path) -> None:
        f = tmp_path / "staged.bin"
        f.write_bytes(_PDF_HEAD)
        assert assess_file_signature(f, filename="report.pdf") is None


# ---------------------------------------------------------------------------
# process_file wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestProcessFileSignatureGate:
    async def test_renamed_pdf_rejected_before_handler(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "doc.pdf"
        f.write_bytes(_BINARY_GARBAGE)
        svc = FileProcessingService()
        handler = AsyncMock(return_value={"text": "should not run"})
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(svc, "_run_handler", handler)
            result = await svc.process_file(str(f))
        assert result.get("error")
        assert result.get("extracted_text") is None
        handler.assert_not_awaited()

    async def test_txt_processing_unaffected(self, tmp_path: Path) -> None:
        f = tmp_path / "notes.txt"
        f.write_text("plain utf-8 notes", encoding="utf-8")
        svc = FileProcessingService()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                svc, "_run_handler", AsyncMock(return_value={"text": "hello"})
            )
            result = await svc.process_file(str(f))
        assert result["handler"] == "text_processor"
        assert result["extracted_text"] == "hello"


# ---------------------------------------------------------------------------
# process_and_update_db quality gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mojibake_text_degrades_without_indexing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    file_id = uuid4()
    source = tmp_path / "garbled.txt"
    source.write_text("placeholder", encoding="utf-8")

    svc = FileProcessingService()
    updates: list[dict[str, object]] = []
    states: list[dict[str, object]] = []
    index_chunks = AsyncMock()

    monkeypatch.setattr(
        "StAgent.services.service_manager.get_service_manager",
        lambda: SimpleNamespace(db=SimpleNamespace()),
    )
    monkeypatch.setattr(svc, "_get_index_state", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_enqueue_index_job", AsyncMock(return_value=None))
    monkeypatch.setattr(
        svc,
        "_materialize_processing_source",
        AsyncMock(return_value=(str(source), None)),
    )
    monkeypatch.setattr(
        svc,
        "process_file",
        AsyncMock(
            return_value={
                "extracted_text": "\ufffd" * 300 + "trailing",
                "metadata": {},
                "handler": "text_processor",
            }
        ),
    )

    async def record_update(_file_id: str, **kwargs: object) -> None:
        updates.append(kwargs)

    async def record_state(_file_id: str, status: str, **kwargs: object) -> None:
        states.append({"status": status, **kwargs})

    monkeypatch.setattr(svc, "_update_file_record", record_update)
    monkeypatch.setattr(svc, "_set_index_state", record_state)
    monkeypatch.setattr(svc, "_upsert_file_content", AsyncMock())
    monkeypatch.setattr(svc, "_index_chunks", index_chunks)

    result = await svc.process_and_update_db(str(file_id), str(source))

    assert result["_quality_rejected"] == "mojibake_content"
    from StAgent.db.models.file import FileStatus

    assert updates[-1]["status"] is FileStatus.PROCESSED
    assert updates[-1]["is_indexed"] is False
    assert states[-1]["status"] == "degraded"
    assert states[-1]["last_error"] == "content_quality:mojibake_content"
    index_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_clean_text_still_reaches_indexing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    file_id = uuid4()
    source = tmp_path / "clean.txt"
    source.write_text("placeholder", encoding="utf-8")

    svc = FileProcessingService()
    index_chunks = AsyncMock(return_value={"_index_chunk_count": 2})

    monkeypatch.setattr(
        "StAgent.services.service_manager.get_service_manager",
        lambda: SimpleNamespace(db=SimpleNamespace()),
    )
    monkeypatch.setattr(svc, "_get_index_state", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_content_unchanged", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_update_file_record", AsyncMock())
    monkeypatch.setattr(svc, "_set_index_state", AsyncMock())
    monkeypatch.setattr(svc, "_upsert_file_content", AsyncMock())
    monkeypatch.setattr(svc, "_index_chunks", index_chunks)

    async def fake_enqueue_job(_fid: str, _hash: str) -> None:
        return None

    monkeypatch.setattr(svc, "_enqueue_index_job", fake_enqueue_job)
    monkeypatch.setattr(
        svc,
        "_materialize_processing_source",
        AsyncMock(return_value=(str(source), None)),
    )
    monkeypatch.setattr(
        svc,
        "process_file",
        AsyncMock(
            return_value={
                "extracted_text": "干净的可检索文本 clean indexable text",
                "metadata": {},
                "handler": "text_processor",
            }
        ),
    )

    result = await svc.process_and_update_db(str(file_id), str(source))

    assert "_quality_rejected" not in result
    index_chunks.assert_awaited_once()


# ---------------------------------------------------------------------------
# markdown / html encoding detection
# ---------------------------------------------------------------------------


class TestMarkdownEncodingDetection:
    def test_gbk_markdown_decodes_correctly(self, tmp_path: Path) -> None:
        expected = "# 中文标题\n\n这是 GBK 编码的中文内容。\n"
        f = tmp_path / "notes.md"
        f.write_bytes(expected.encode("gbk"))
        decoded = read_markdown(f)
        assert decoded == expected
        assert "\ufffd" not in decoded

    def test_utf8_markdown_unchanged(self, tmp_path: Path) -> None:
        expected = "# UTF-8 标题\n\n正文。\n"
        f = tmp_path / "notes.md"
        f.write_text(expected, encoding="utf-8")
        assert read_markdown(f) == expected

    def test_bom_markdown_handled(self, tmp_path: Path) -> None:
        expected = "# BOM 文档\n"
        f = tmp_path / "notes.md"
        f.write_bytes(b"\xef\xbb\xbf" + expected.encode("utf-8"))
        assert read_markdown(f).lstrip("\ufeff") == expected


class TestHtmlEncodingDetection:
    def test_gbk_html_decodes_correctly(self, tmp_path: Path) -> None:
        html = "<html><body><h1>中文标题</h1><p>GBK 编码的段落。</p></body></html>"
        f = tmp_path / "page.html"
        f.write_bytes(html.encode("gbk"))
        decoded = read_html(f)
        assert "中文标题" in decoded
        assert "\ufffd" not in decoded

    def test_utf8_html_unchanged(self, tmp_path: Path) -> None:
        html = "<html><body><p>UTF-8 段落</p></body></html>"
        f = tmp_path / "page.html"
        f.write_text(html, encoding="utf-8")
        assert read_html(f) == html
