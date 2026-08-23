"""Content quality gates for knowledge ingestion.

Runs at the file-processing service layer, deliberately not inside the doc
tools: the tools stay permissive so the agent can still read damaged files on
request, while anything that would enter the RAG index must pass these checks.

Two gates:

- :func:`assess_file_signature` — magic-byte check that a file's bytes match
  its claimed extension (a binary blob renamed to ``.pdf`` is rejected before
  any parser runs). Conservative: only rejects on a definite mismatch;
  extensions without a strong signature (txt/md/csv/code) are skipped.
- :func:`assess_extracted_text` — mojibake / binary-garbage check on the
  extracted text before it is chunked and indexed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# Text-quality thresholds. The absolute floors (_MIN_BAD_CHAR_COUNT,
# _MAX_NUL_COUNT) keep short documents with a few stray replacement chars from
# being rejected on ratio alone.
_MAX_REPLACEMENT_RATIO: Final[float] = 0.02
_MAX_CONTROL_RATIO: Final[float] = 0.05
_MIN_BAD_CHAR_COUNT: Final[int] = 20
_MAX_NUL_COUNT: Final[int] = 10

# C0/C1 control characters excluding \t \n \r (legitimate whitespace).
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
)

_SIGNATURE_HEAD_BYTES: Final[int] = 16


@dataclass(frozen=True, slots=True)
class ContentQualityVerdict:
    """Outcome of the extracted-text quality gate."""

    passed: bool
    reason: str = ""
    message: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


def _sig_pdf(head: bytes) -> bool:
    return head.startswith(b"%PDF")


def _sig_png(head: bytes) -> bool:
    return head.startswith(b"\x89PNG\r\n\x1a\n")


def _sig_jpg(head: bytes) -> bool:
    return head.startswith(b"\xff\xd8\xff")


def _sig_gif(head: bytes) -> bool:
    return head[:6] in (b"GIF87a", b"GIF89a")


def _sig_bmp(head: bytes) -> bool:
    return head.startswith(b"BM")


def _sig_webp(head: bytes) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _sig_tiff(head: bytes) -> bool:
    return head[:2] in (b"II", b"MM") and head[2:4] in (b"\x2a\x00", b"\x00\x2a")


def _sig_zip(head: bytes) -> bool:
    # OOXML documents are zip containers; accept any PK variant header.
    return head.startswith(b"PK")


def _sig_ole2(head: bytes) -> bool:
    return head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")


def _sig_rar(head: bytes) -> bool:
    return head.startswith(b"Rar!")


def _sig_7z(head: bytes) -> bool:
    return head.startswith(b"7z\xbc\xaf")


def _sig_gzip(head: bytes) -> bool:
    return head.startswith(b"\x1f\x8b")


# Strong byte signatures per extension. Anything absent here is skipped.
_SIGNATURES: Final[dict[str, Any]] = {
    ".pdf": _sig_pdf,
    ".png": _sig_png,
    ".jpg": _sig_jpg,
    ".jpeg": _sig_jpg,
    ".gif": _sig_gif,
    ".bmp": _sig_bmp,
    ".webp": _sig_webp,
    ".tif": _sig_tiff,
    ".tiff": _sig_tiff,
    ".zip": _sig_zip,
    ".docx": _sig_zip,
    ".docm": _sig_zip,
    ".xlsx": _sig_zip,
    ".xlsm": _sig_zip,
    ".pptx": _sig_zip,
    ".pptm": _sig_zip,
    ".doc": _sig_ole2,
    ".xls": _sig_ole2,
    ".ppt": _sig_ole2,
    ".rar": _sig_rar,
    ".7z": _sig_7z,
    ".gz": _sig_gzip,
    ".tgz": _sig_gzip,
}

_MIME_SUFFIXES: Final[dict[str, str]] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
    "application/zip": ".zip",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/x-rar-compressed": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
}


def _resolve_signature_suffix(
    path: Path,
    filename: str | None,
    mime_type: str | None,
) -> str | None:
    for candidate in (filename, path.name):
        if isinstance(candidate, str) and candidate.strip():
            suffix = Path(candidate).suffix.lower()
            if suffix in _SIGNATURES:
                return suffix
    if isinstance(mime_type, str):
        suffix = _MIME_SUFFIXES.get(mime_type.lower().strip())
        if suffix is not None:
            return suffix
    return None


def assess_file_signature(
    path: Path | str,
    *,
    filename: str | None = None,
    mime_type: str | None = None,
) -> str | None:
    """Return an error message when the file bytes contradict its type.

    ``None`` means the check passed or no strong signature applies to this
    extension (txt/md/csv/code files are never rejected here).
    """
    target = Path(path)
    suffix = _resolve_signature_suffix(target, filename, mime_type)
    if suffix is None:
        return None

    try:
        size = target.stat().st_size
        with target.open("rb") as fh:
            head = fh.read(_SIGNATURE_HEAD_BYTES)
    except OSError as exc:
        return f"cannot read file for signature check: {exc}"

    if size == 0:
        return f"file is empty but claims to be .{suffix.lstrip('.')}"
    if not _SIGNATURES[suffix](head):
        return (
            f"file content does not match its .{suffix.lstrip('.')} extension; "
            "the upload is corrupt or mislabelled"
        )
    return None


def _metadata_flags_binary(metadata: dict[str, Any] | None) -> bool:
    """Read the ``likely_binary`` signal text_processor reports on read."""
    if not isinstance(metadata, dict):
        return False
    decode = metadata.get("decode")
    if isinstance(decode, dict):
        detection = decode.get("detection")
        if isinstance(detection, dict) and detection.get("likely_binary") is True:
            return True
    return metadata.get("likely_binary") is True


def _gate_enabled() -> bool:
    from StAgent.config.settings import get_settings

    try:
        return bool(get_settings().knowledge.content_quality_gate_enabled)
    except Exception:  # noqa: BLE001 - fail open: never block on settings
        return True


def assess_extracted_text(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> ContentQualityVerdict:
    """Decide whether extracted text is safe to chunk and index."""
    total = max(len(text), 1)
    replacement_count = text.count("\ufffd")
    control_count = len(_CONTROL_CHARS.findall(text))
    nul_count = text.count("\x00")
    metrics: dict[str, Any] = {
        "chars": len(text),
        "replacement_count": replacement_count,
        "replacement_ratio": round(replacement_count / total, 6),
        "control_count": control_count,
        "control_ratio": round(control_count / total, 6),
        "nul_count": nul_count,
    }

    if not _gate_enabled():
        return ContentQualityVerdict(passed=True, metrics=metrics)

    if (
        replacement_count >= _MIN_BAD_CHAR_COUNT
        and replacement_count / total >= _MAX_REPLACEMENT_RATIO
    ):
        return ContentQualityVerdict(
            passed=False,
            reason="mojibake_content",
            message=(
                f"extracted text is {metrics['replacement_ratio']:.1%} unicode "
                "replacement characters (wrong encoding or binary content)"
            ),
            metrics=metrics,
        )
    if (
        control_count >= _MIN_BAD_CHAR_COUNT
        and control_count / total >= _MAX_CONTROL_RATIO
    ):
        return ContentQualityVerdict(
            passed=False,
            reason="binary_control_chars",
            message=(
                f"extracted text is {metrics['control_ratio']:.1%} raw control "
                "characters (binary content decoded as text)"
            ),
            metrics=metrics,
        )
    if nul_count >= _MAX_NUL_COUNT:
        return ContentQualityVerdict(
            passed=False,
            reason="nul_bytes",
            message=(
                f"extracted text contains {nul_count} NUL bytes "
                "(binary content decoded as text)"
            ),
            metrics=metrics,
        )
    if _metadata_flags_binary(metadata):
        return ContentQualityVerdict(
            passed=False,
            reason="binary_content",
            message="source was detected as binary but produced text output",
            metrics=metrics,
        )
    return ContentQualityVerdict(passed=True, metrics=metrics)
