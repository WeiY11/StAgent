"""Best-effort tabular snapshots for uploaded CSV/Excel attachments.

:func:`build_tabular_snapshot` is a pure, synchronous helper: callers on the
event loop should run it through ``asyncio.to_thread``. It never raises — any
parse failure degrades to ``None`` so the upload flow is never blocked.
"""

from __future__ import annotations

import io
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

__all__ = ["build_tabular_snapshot"]

# Formats we attempt to parse, keyed by lowercase extension.
_CSV_EXTENSIONS = {".csv"}
_EXCEL_EXTENSIONS = {".xls", ".xlsx"}
_CSV_MIME_HINTS = ("csv",)
_EXCEL_MIME_HINTS = ("excel", "spreadsheet", "ms-excel")

# Guardrails: never parse more than this many rows / bytes.
_MAX_PARSE_ROWS = 200
_MAX_PARSE_BYTES = 5 * 1024 * 1024
_SKIP_PARSE_BYTES = 50 * 1024 * 1024
_SAMPLE_ROWS = 5

# Encodings tried in order for CSV payloads.
_CSV_ENCODINGS = ("utf-8-sig", "gbk", "latin-1")


def build_tabular_snapshot(
    raw: bytes, filename: str, content_type: str
) -> dict[str, Any] | None:
    """Build a lightweight schema snapshot for a tabular attachment.

    Args:
        raw: The full attachment payload.
        filename: Original upload filename (extension drives detection).
        content_type: Declared MIME type (fallback detection signal).

    Returns:
        A JSON-safe snapshot dict, a file-level info dict for oversized
        payloads, or ``None`` for non-tabular / unparseable inputs.
    """
    try:
        fmt = _detect_format(filename, content_type)
        if fmt is None:
            return None
        if len(raw) > _SKIP_PARSE_BYTES:
            return {
                "file_size": len(raw),
                "truncated": True,
                "hint": "large_file",
            }
        if fmt == "csv":
            return _snapshot_csv(raw)
        return _snapshot_excel(raw)
    except Exception:  # noqa: BLE001 - snapshots must never break uploads
        return None


def _detect_format(filename: str, content_type: str) -> str | None:
    """Classify the upload as ``"csv"``, ``"excel"``, or ``None``."""
    suffix = PureWindowsPath(PurePosixPath(filename or "").name).suffix.lower()
    if suffix in _CSV_EXTENSIONS:
        return "csv"
    if suffix in _EXCEL_EXTENSIONS:
        return "excel"
    mime = (content_type or "").lower()
    if any(hint in mime for hint in _CSV_MIME_HINTS):
        return "csv"
    if any(hint in mime for hint in _EXCEL_MIME_HINTS):
        return "excel"
    return None


def _snapshot_csv(raw: bytes) -> dict[str, Any] | None:
    import pandas as pd

    bytes_truncated = len(raw) > _MAX_PARSE_BYTES
    head = raw[:_MAX_PARSE_BYTES]
    if bytes_truncated:
        # Drop the (likely partial) trailing line after the byte cut.
        cut = head.rfind(b"\n")
        if cut > 0:
            head = head[: cut + 1]

    text = _decode_csv(head)
    if text is None:
        return None

    df = pd.read_csv(
        io.StringIO(text),
        nrows=_MAX_PARSE_ROWS,
        skip_blank_lines=True,
        on_bad_lines="skip",
    )
    if df.shape[1] == 0:
        return None

    # Newline count over the raw payload is a cheap whole-file estimate.
    newline_rows = max(raw.count(b"\n") - 1, 0)
    row_estimate = max(newline_rows, int(len(df)))
    truncated = bytes_truncated or row_estimate > len(df)
    return _snapshot_from_frame(df, row_estimate, truncated)


def _snapshot_excel(raw: bytes) -> dict[str, Any] | None:
    import pandas as pd

    df = pd.read_excel(io.BytesIO(raw), nrows=_MAX_PARSE_ROWS)
    if df.shape[1] == 0:
        return None
    truncated = len(df) >= _MAX_PARSE_ROWS
    return _snapshot_from_frame(df, int(len(df)), truncated)


def _decode_csv(head: bytes) -> str | None:
    for encoding in _CSV_ENCODINGS:
        try:
            return head.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _snapshot_from_frame(
    df: Any, row_count_estimate: int, truncated: bool
) -> dict[str, Any]:
    parsed_rows = int(len(df))
    columns: list[dict[str, Any]] = []
    for name in df.columns:
        series = df[name]
        null_count = int(series.isna().sum()) if parsed_rows else 0
        null_pct = round(null_count / parsed_rows * 100.0, 2) if parsed_rows else 0.0
        columns.append(
            {
                "name": str(name),
                "dtype": str(series.dtype),
                "null_pct": null_pct,
            }
        )

    sample_rows = [
        {str(k): _json_safe(v) for k, v in record.items()}
        for record in df.head(_SAMPLE_ROWS).to_dict(orient="records")
    ]
    return {
        "columns": columns,
        "row_count_estimate": int(row_count_estimate),
        "sample_rows": sample_rows,
        "truncated": bool(truncated),
    }


def _json_safe(value: Any) -> Any:
    """Coerce pandas/numpy scalars into JSON-serialisable primitives."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if value != value else value  # NaN -> None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (TypeError, ValueError, ImportError):
        pass
    if hasattr(value, "item"):  # numpy scalar
        try:
            return _json_safe(value.item())
        except Exception:  # noqa: BLE001
            return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)
