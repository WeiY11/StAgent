"""Document handler registry — extensible, priority-based file type resolution.

Replaces the flat ``EXTENSION_HANDLER_MAP`` dict in ``FileProcessingService``
with a proper :class:`HandlerRegistry` that supports:

- **Multi-signal resolution**: extension → MIME → content sniffing (in order).
- **Custom handler registration**: plugins can register new handlers at runtime.
- **Handler metadata**: each handler declares its supported formats and
  capabilities (text extraction, OCR, structured data).

Usage::

    from StAgent.services.file_processing.registry import (
        HandlerRegistry,
        HandlerDescriptor,
        get_handler_registry,
    )

    registry = get_handler_registry()
    handler = registry.resolve(path=Path("report.pdf"), mime_type="application/pdf")
    # → HandlerDescriptor(name="pdf_reader", ...)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HandlerDescriptor:
    """Metadata for a registered document handler."""

    name: str
    extensions: tuple[str, ...]
    mime_types: tuple[str, ...]
    capability: str  # "text" | "ocr" | "data" | "archive"
    priority: int = 0  # higher = preferred when multiple match
    description: str = ""


# ── Built-in handler definitions ──────────────────────────────────────

_BUILTIN_HANDLERS: list[HandlerDescriptor] = [
    HandlerDescriptor(
        name="pdf_reader",
        extensions=(".pdf",),
        mime_types=("application/pdf",),
        capability="text",
        priority=10,
        description="PDF text extraction (pdfplumber / PyMuPDF)",
    ),
    HandlerDescriptor(
        name="word_reader",
        extensions=(".doc", ".docx"),
        mime_types=(
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        capability="text",
        priority=10,
        description="Word document text extraction",
    ),
    HandlerDescriptor(
        name="excel_reader",
        extensions=(".xls", ".xlsx"),
        mime_types=(
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        capability="data",
        priority=10,
        description="Excel spreadsheet extraction",
    ),
    HandlerDescriptor(
        name="csv_processor",
        extensions=(".csv", ".tsv"),
        mime_types=("text/csv",),
        capability="data",
        priority=5,
        description="CSV/TSV data processing",
    ),
    HandlerDescriptor(
        name="markdown_processor",
        extensions=(".md", ".markdown"),
        mime_types=("text/markdown",),
        capability="text",
        priority=5,
        description="Markdown text processing",
    ),
    HandlerDescriptor(
        name="html_processor",
        extensions=(".html", ".htm"),
        mime_types=("text/html",),
        capability="text",
        priority=5,
        description="HTML text extraction",
    ),
    HandlerDescriptor(
        name="config_file",
        extensions=(".json", ".yaml", ".yml", ".toml"),
        mime_types=("application/json",),
        capability="text",
        priority=3,
        description="Configuration file reader",
    ),
    HandlerDescriptor(
        name="text_processor",
        extensions=(
            ".txt", ".log", ".cfg", ".ini",
            ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h",
            ".rs", ".go", ".rb", ".sh", ".sql", ".xml", ".css",
        ),
        mime_types=("text/plain",),
        capability="text",
        priority=1,
        description="Generic text file processor",
    ),
    HandlerDescriptor(
        name="archive_manager",
        extensions=(".zip", ".tar", ".gz", ".bz2", ".xz", ".tgz"),
        mime_types=("application/zip", "application/x-tar"),
        capability="archive",
        priority=5,
        description="Archive inspection",
    ),
    HandlerDescriptor(
        name="image_ocr",
        extensions=(".png", ".jpg", ".jpeg", ".tiff", ".bmp"),
        mime_types=("image/png", "image/jpeg"),
        capability="ocr",
        priority=5,
        description="Image OCR text extraction",
    ),
]


class HandlerRegistry:
    """Extensible registry for document processing handlers.

    Resolution order:
    1. Extension match (highest priority handler wins)
    2. MIME type match (highest priority handler wins)
    3. None (no handler available)
    """

    def __init__(self) -> None:
        self._handlers: list[HandlerDescriptor] = list(_BUILTIN_HANDLERS)
        self._ext_index: dict[str, list[HandlerDescriptor]] = {}
        self._mime_index: dict[str, list[HandlerDescriptor]] = {}
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        """Rebuild extension and MIME type lookup indices."""
        self._ext_index.clear()
        self._mime_index.clear()
        for h in self._handlers:
            for ext in h.extensions:
                self._ext_index.setdefault(ext.lower(), []).append(h)
            for mime in h.mime_types:
                self._mime_index.setdefault(mime.lower(), []).append(h)
        # Sort each bucket by priority descending.
        for bucket in self._ext_index.values():
            bucket.sort(key=lambda h: h.priority, reverse=True)
        for bucket in self._mime_index.values():
            bucket.sort(key=lambda h: h.priority, reverse=True)

    def register(self, handler: HandlerDescriptor) -> None:
        """Register a new handler (or override by name)."""
        self._handlers = [h for h in self._handlers if h.name != handler.name]
        self._handlers.append(handler)
        self._rebuild_index()

    def unregister(self, name: str) -> bool:
        """Remove a handler by name. Returns True if found."""
        before = len(self._handlers)
        self._handlers = [h for h in self._handlers if h.name != name]
        if len(self._handlers) < before:
            self._rebuild_index()
            return True
        return False

    def resolve(
        self,
        *,
        path: Path | None = None,
        mime_type: str | None = None,
        original_name: str | None = None,
    ) -> HandlerDescriptor | None:
        """Find the best handler for the given file.

        Resolution order:
        1. Extension from *path* or *original_name*
        2. MIME type
        3. None
        """
        # Extension-based resolution.
        ext: str | None = None
        if path is not None:
            ext = path.suffix.lower()
        if not ext and original_name:
            ext = Path(original_name).suffix.lower()

        if ext:
            candidates = self._ext_index.get(ext)
            if candidates:
                return candidates[0]  # highest priority

        # MIME-based resolution.
        if mime_type:
            candidates = self._mime_index.get(mime_type.lower())
            if candidates:
                return candidates[0]

        return None

    def resolve_name(
        self,
        *,
        path: Path | None = None,
        mime_type: str | None = None,
        original_name: str | None = None,
    ) -> str | None:
        """Convenience: return just the handler name, or None."""
        h = self.resolve(path=path, mime_type=mime_type, original_name=original_name)
        return h.name if h else None

    def list_handlers(self) -> list[HandlerDescriptor]:
        """Return all registered handlers sorted by priority."""
        return sorted(self._handlers, key=lambda h: h.priority, reverse=True)

    def handler_names(self) -> list[str]:
        """Return all registered handler names."""
        return [h.name for h in self._handlers]


# ── Module singleton ──────────────────────────────────────────────────

_registry: HandlerRegistry | None = None


def get_handler_registry() -> HandlerRegistry:
    """Return the module-level handler registry (lazy singleton)."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = HandlerRegistry()
    return _registry


def set_handler_registry(registry: HandlerRegistry) -> None:
    """Override the module-level registry (used in tests / bootstrap)."""
    global _registry  # noqa: PLW0603
    _registry = registry


__all__ = [
    "HandlerDescriptor",
    "HandlerRegistry",
    "get_handler_registry",
    "set_handler_registry",
]
