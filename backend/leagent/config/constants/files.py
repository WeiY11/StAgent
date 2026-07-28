"""Supported file types and upload limits."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Supported file types
# ---------------------------------------------------------------------------

SUPPORTED_DOCUMENT_TYPES = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv",
    ".ppt", ".pptx", ".txt", ".md", ".rtf",
}
SUPPORTED_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}
SUPPORTED_ARCHIVE_TYPES = {".zip", ".tar", ".gz", ".rar", ".7z"}
ALL_SUPPORTED_TYPES = SUPPORTED_DOCUMENT_TYPES | SUPPORTED_IMAGE_TYPES | SUPPORTED_ARCHIVE_TYPES

MAX_UPLOAD_SIZE_MB = 1024
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
