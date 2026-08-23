"""LLM tool-argument JSON parsing, repair, and recovery.

Extracted from :mod:`StAgent.tools.executor` so the executor can focus on
dispatch/orchestration. This module owns the tolerant parsing pipeline used
for model-emitted ``function.arguments`` and ``__raw__`` payloads:

* BOM / code-fence stripping and trailing-comma repair
* control-character and interior-quote escaping inside JSON strings
* double-encoded JSON-string unwrapping
* stray closing-delimiter and truncated-stream salvage
* per-tool argument recovery (``code_execution``, ``project_*``,
  ``canvas_publish``, ``emit_ui_tree`` / ``emit_ui_patch``,
  ``tool_argument_blob``)
* model-actionable parse-error formatting

All symbols remain importable from :mod:`StAgent.tools.executor` (re-exported
there) for backward compatibility.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from StAgent.tools.base import BaseTool


def _strip_json_code_fence(raw: str) -> str:
    """Remove optional markdown code fences around JSON payloads."""
    text = _strip_json_prefix(raw).strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if match:
        return _strip_json_prefix(match.group(1)).strip()
    return text


def _strip_json_prefix(raw: str) -> str:
    """Trim BOM and invisible prefix characters that commonly precede JSON."""
    return raw.lstrip("\ufeff\u200b\u200c\u200d\u2060")


def _repair_trailing_commas(raw: str) -> str:
    """Remove trailing commas before object/array terminators outside strings."""
    out: list[str] = []
    in_string = False
    escaped = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == ",":
            j = i + 1
            while j < len(raw) and raw[j].isspace():
                j += 1
            if j < len(raw) and raw[j] in "}]":
                i += 1
                continue

        out.append(ch)
        i += 1
    return "".join(out)


def _escape_control_chars_in_json_strings(raw: str) -> str:
    """Escape raw control characters that appear inside JSON strings."""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in raw:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            if ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
                continue
            out.append(ch)
            continue

        out.append(ch)
        if ch == '"':
            in_string = True
    return "".join(out)


def _is_json_string_terminator_after_quote(raw: str, quote_pos: int) -> bool:
    """True when the quote at *quote_pos* likely ends a JSON string key/value."""
    j = quote_pos + 1
    while j < len(raw) and raw[j] in " \t\n\r":
        j += 1
    if j >= len(raw):
        return True
    return raw[j] in ",}]:"


def _escape_unescaped_quotes_in_json_strings(raw: str) -> str:
    """Escape interior ASCII ``"`` that LLMs leave unescaped inside string values.

    Heuristic: a ``"`` inside a JSON string is a terminator only when the next
    non-whitespace character is ``,`` ``}`` ``]`` or ``:``; otherwise it is
    written as ``\\\"``. Fixes common CJK prose such as ``偶尔"忘记"用户`` in
    ``slides_generate`` / ``document_generate`` bodies.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue

        if ch == "\\":
            out.append(ch)
            escaped = True
            i += 1
            continue

        if ch == '"':
            if _is_json_string_terminator_after_quote(raw, i):
                out.append(ch)
                in_string = False
            else:
                out.append('\\"')
            i += 1
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def _candidate_json_texts(raw: str) -> list[str]:
    """Return increasingly repaired JSON candidates, preserving order."""
    candidates: list[str] = []

    def add(value: str) -> None:
        if value not in candidates:
            candidates.append(value)

    add(raw)
    add(_strip_json_prefix(raw).strip())
    fenced = _strip_json_code_fence(raw)
    add(fenced)
    for value in candidates:
        repaired = _repair_trailing_commas(value)
        add(repaired)
        escaped = _escape_control_chars_in_json_strings(value)
        add(escaped)
        add(_repair_trailing_commas(escaped))
        # Quote repair before control-char escape so interior " keep us in-string
        # across raw newlines (CJK ``"忘记"`` in deck/document bodies).
        quoted = _escape_unescaped_quotes_in_json_strings(value)
        add(quoted)
        quoted_ctrl = _escape_control_chars_in_json_strings(quoted)
        add(quoted_ctrl)
        add(_repair_trailing_commas(quoted))
        add(_repair_trailing_commas(quoted_ctrl))
    return candidates


def _loads_json_dict(candidate: str) -> dict[str, Any] | None:
    """Parse a JSON object candidate, including double-encoded objects."""
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, str):
        for nested in _candidate_json_texts(parsed):
            try:
                parsed = json.loads(nested)
            except json.JSONDecodeError:
                continue
            break
        else:
            return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _try_json_dict_raw_decode_trailing_junk(candidate: str) -> dict[str, Any] | None:
    """When ``json.loads`` fails with trailing garbage, accept a leading object if junk is only ``]``/``}``."""
    text = candidate.strip()
    try:
        obj, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    rest = text[end:].strip()
    if not rest:
        return obj
    if all(ch in "]}" for ch in rest):
        return obj
    return None


def _try_repair_superfluous_closing_delimiter(candidate: str, *, max_deletions: int = 3) -> dict[str, Any] | None:
    """Drop a few stray ``]``/``}`` near decode errors (common LLM nesting mistakes)."""
    stripped = candidate.strip()

    def parse_or_error(value: str) -> tuple[dict[str, Any] | None, int | None]:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            pos = getattr(e, "pos", None)
            return None, pos if isinstance(pos, int) else None
        return (parsed if isinstance(parsed, dict) else None), None

    parsed, pos = parse_or_error(stripped)
    if parsed is not None:
        return parsed
    queue: list[tuple[str, int | None, int]] = [(stripped, pos, 0)]
    seen = {stripped}
    window = 24
    while queue:
        text, err_pos, deletions = queue.pop(0)
        if err_pos is None or deletions >= max_deletions:
            continue
        lo = max(0, err_pos - window)
        hi = min(len(text), err_pos + window + 1)
        for i in range(lo, hi):
            if text[i] not in "]}":
                continue
            cand = text[:i] + text[i + 1 :]
            if cand in seen:
                continue
            seen.add(cand)
            parsed, next_pos = parse_or_error(cand)
            if parsed is not None:
                return parsed
            queue.append((cand, next_pos, deletions + 1))
    return None


def _find_json_string_end(raw: str, start: int) -> int | None:
    """Find the end quote for a JSON string by normal escape rules."""
    escaped = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            return i
    return None


def _extract_json_string_value(raw: str, key: str) -> tuple[str, int, int] | None:
    """Extract a normally escaped JSON string value for *key*."""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"', raw)
    if not match:
        return None
    value_start = match.end()
    value_end = _find_json_string_end(raw, value_start)
    if value_end is None:
        return None
    try:
        value = json.loads(f'"{raw[value_start:value_end]}"')
    except json.JSONDecodeError:
        return None
    return value, value_start, value_end


def _decode_partial_json_string_body(body: str) -> str:
    """Decode a JSON string body that may have been cut before its closing quote."""
    trimmed = body
    while trimmed.endswith("\\"):
        trimmed = trimmed[:-1]
    try:
        return json.loads(f'"{trimmed}"')
    except json.JSONDecodeError:
        return (
            trimmed.replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )


def _extract_unterminated_json_string_field(
    raw: str,
    field: str,
) -> tuple[str, int, int] | None:
    """Extract ``field`` when max_tokens cut the value mid-string (no closing quote)."""
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"', raw)
    if not match:
        return None
    value_start = match.end()
    if _find_json_string_end(raw, value_start) is not None:
        return None
    body = raw[value_start:]
    if len(body.strip()) < 8:
        return None
    return _decode_partial_json_string_body(body), value_start, len(raw) - 1


def _extract_malformed_quoted_field(
    raw: str,
    field: str,
    allowed_next_keys: tuple[str, ...],
) -> tuple[str, int, int] | None:
    """Best-effort extraction when *field*'s JSON string value contains raw newlines.

    Models occasionally emit multiline text inside a JSON string without
    escaping newlines or inner quotes. The heuristic finds a closing ``"``
    whose suffix resumes the outer object (comma + next known key, or ``}``).
    """
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"', raw)
    if not match:
        return None
    value_start = match.end()
    escaped = False
    fallback_end: int | None = None
    for i in range(value_start, len(raw)):
        ch = raw[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch != '"':
            continue

        suffix = raw[i + 1 :]
        stripped = suffix.lstrip()
        if stripped.startswith("}"):
            fallback_end = i
            continue
        if stripped.startswith(","):
            after_comma = stripped[1:].lstrip()
            if any(after_comma.startswith(f'"{key}"') for key in allowed_next_keys):
                return raw[value_start:i], value_start, i
    if fallback_end is not None:
        return raw[value_start:fallback_end], value_start, fallback_end
    return None


def _extract_malformed_source(raw: str) -> tuple[str, int, int] | None:
    """Best-effort extraction for ``code_execution`` ``source`` in broken JSON."""
    return _extract_malformed_quoted_field(
        raw,
        "source",
        (
            "inputs",
            "timeout_sec",
            "memory_bytes",
            "files",
            "reset_workspace",
        ),
    )


def _recover_code_execution_args(raw: str) -> dict[str, Any] | None:
    """Recover code_execution args when only the outer JSON is malformed."""
    text = _strip_json_code_fence(raw)
    source_info = _extract_json_string_value(text, "source") or _extract_malformed_source(text)
    if source_info is None:
        return None
    source, _, source_end = source_info
    recovered: dict[str, Any] = {"source": source}

    suffix = text[source_end + 1 :].strip()
    if suffix.startswith(","):
        suffix = "{" + suffix[1:]
        parsed_suffix = _loads_json_dict(_repair_trailing_commas(suffix))
        if parsed_suffix:
            for key in (
                "inputs",
                "timeout_sec",
                "memory_bytes",
                "files",
                "reset_workspace",
                "source_blob_id",
            ):
                if key in parsed_suffix:
                    recovered[key] = parsed_suffix[key]

    return recovered


def _recover_project_write_args(raw: str) -> dict[str, Any] | None:
    """Recover ``project_write`` args when ``content`` breaks outer JSON."""
    text = _strip_json_code_fence(raw)
    if '"path"' not in text or '"content"' not in text:
        return None
    path_info = _extract_json_string_value(text, "path")
    if path_info is None:
        return None
    path, _, _ = path_info
    if not path.strip():
        return None
    content_info = _extract_json_string_value(text, "content") or _extract_malformed_quoted_field(
        text,
        "content",
        ("overwrite", "create_parents", "project_path"),
    )
    if content_info is None:
        return None
    content, _, content_end = content_info
    recovered: dict[str, Any] = {"path": path, "content": content}
    suffix = text[content_end + 1 :].strip()
    if suffix.startswith(","):
        suffix = "{" + suffix[1:]
        parsed_suffix = _loads_json_dict(_repair_trailing_commas(suffix))
        if parsed_suffix:
            for key in ("overwrite", "create_parents", "project_path"):
                if key in parsed_suffix:
                    recovered[key] = parsed_suffix[key]
    return recovered


def _recover_project_edit_args(raw: str) -> dict[str, Any] | None:
    """Recover ``project_edit`` args when ``new_string`` breaks outer JSON."""
    text = _strip_json_code_fence(raw)
    if '"path"' not in text or '"new_string"' not in text:
        return None
    path_info = _extract_json_string_value(text, "path")
    if path_info is None:
        return None
    path, _, _ = path_info
    if not path.strip():
        return None
    old_info = _extract_json_string_value(text, "old_string")
    new_info = _extract_json_string_value(text, "new_string") or _extract_malformed_quoted_field(
        text,
        "new_string",
        ("project_path",),
    )
    if new_info is None:
        return None
    new_string, _, new_end = new_info
    recovered: dict[str, Any] = {"path": path, "new_string": new_string}
    if old_info is not None:
        recovered["old_string"] = old_info[0]
    suffix = text[new_end + 1 :].strip()
    if suffix.startswith(","):
        suffix = "{" + suffix[1:]
        parsed_suffix = _loads_json_dict(_repair_trailing_commas(suffix))
        if parsed_suffix:
            for key in ("project_path",):
                if key in parsed_suffix:
                    recovered[key] = parsed_suffix[key]
    return recovered


def _recover_project_apply_patch_args(raw: str) -> dict[str, Any] | None:
    """Recover ``project_apply_patch`` args when ``diff`` breaks outer JSON."""
    text = _strip_json_code_fence(raw)
    if '"diff"' not in text:
        return None
    diff_info = _extract_json_string_value(text, "diff") or _extract_malformed_quoted_field(
        text,
        "diff",
        ("project_path",),
    )
    if diff_info is None:
        return None
    diff_text, _, diff_end = diff_info
    if not diff_text.strip():
        return None
    recovered: dict[str, Any] = {"diff": diff_text}
    suffix = text[diff_end + 1 :].strip()
    if suffix.startswith(","):
        suffix = "{" + suffix[1:]
        parsed_suffix = _loads_json_dict(_repair_trailing_commas(suffix))
        if parsed_suffix and "project_path" in parsed_suffix:
            recovered["project_path"] = parsed_suffix["project_path"]
    return recovered


def _recover_canvas_publish_args(raw: str) -> dict[str, Any] | None:
    """Recover ``canvas_publish`` args when ``html`` / ``html_files`` breaks outer JSON."""
    text = _strip_json_code_fence(raw)
    if '"title"' not in text or '"mode"' not in text:
        return None
    if (
        '"html"' not in text
        and '"html_blob_id"' not in text
        and '"html_files"' not in text
        and '"html_files_blob_id"' not in text
        and '"html_paths"' not in text
    ):
        return None
    title_info = _extract_json_string_value(text, "title")
    if title_info is None:
        return None
    title, _, _ = title_info
    mode_info = _extract_json_string_value(text, "mode")
    if mode_info is None:
        return None
    mode, _, _ = mode_info
    recovered: dict[str, Any] = {"title": title, "mode": mode}
    sid_info = _extract_json_string_value(text, "session_id")
    if sid_info is not None:
        recovered["session_id"] = sid_info[0]

    html_info = _extract_json_string_value(text, "html") or _extract_malformed_quoted_field(
        text,
        "html",
        (
            "embed_url",
            "ui_snapshot",
            "canvas_id",
            "message_id",
            "open_in_panel",
            "html_blob_id",
            "html_files",
            "html_files_blob_id",
            "html_paths",
            "html_bundle_entry",
        ),
    ) or _extract_unterminated_json_string_field(text, "html")
    end_pos: int | None = None
    if html_info is not None:
        html_body, _, html_end = html_info
        recovered["html"] = html_body
        end_pos = html_end
    else:
        files_obj = _extract_json_object_value(text, "html_files")
        if files_obj is not None:
            recovered["html_files"] = {
                str(k): str(v) for k, v in files_obj.items() if isinstance(v, str)
            }
            # Approximate end for suffix parse — after the object key region.
            match = re.search(r'"html_files"\s*:\s*', text)
            if match is not None:
                try:
                    _, obj_end = json.JSONDecoder().raw_decode(
                        text[match.end() :].lstrip()
                    )
                    end_pos = match.end() + len(text[match.end() :]) - len(
                        text[match.end() :].lstrip()
                    ) + obj_end - 1
                except json.JSONDecodeError:
                    end_pos = match.end()
        else:
            for key in ("html_blob_id", "html_files_blob_id"):
                blob_info = _extract_json_string_value(text, key)
                if blob_info is not None:
                    bid, _, bid_end = blob_info
                    recovered[key] = bid
                    end_pos = bid_end
                    break
            if end_pos is None:
                # Thin path: html_paths array
                paths_match = re.search(r'"html_paths"\s*:\s*', text)
                if paths_match is not None:
                    try:
                        parsed_paths, paths_end = json.JSONDecoder().raw_decode(
                            text[paths_match.end() :].lstrip()
                        )
                        if isinstance(parsed_paths, list) and parsed_paths:
                            recovered["html_paths"] = [str(p) for p in parsed_paths]
                            pad = len(text[paths_match.end() :]) - len(
                                text[paths_match.end() :].lstrip()
                            )
                            end_pos = paths_match.end() + pad + paths_end - 1
                    except json.JSONDecodeError:
                        return None
                else:
                    return None

    if end_pos is None:
        return None

    entry_info = _extract_json_string_value(text, "html_bundle_entry")
    if entry_info is not None:
        recovered["html_bundle_entry"] = entry_info[0]

    suffix = text[end_pos + 1 :].strip()
    if suffix.startswith(","):
        suffix = "{" + suffix[1:]
        parsed_suffix = _loads_json_dict(_repair_trailing_commas(suffix))
        if parsed_suffix:
            for key in (
                "embed_url",
                "ui_snapshot",
                "canvas_id",
                "message_id",
                "open_in_panel",
                "html_blob_id",
                "html",
                "html_files",
                "html_files_blob_id",
                "html_paths",
                "html_bundle_entry",
            ):
                if key in parsed_suffix and key not in recovered:
                    recovered[key] = parsed_suffix[key]
    return recovered


def _extract_json_object_value(raw: str, key: str) -> dict[str, Any] | None:
    """Extract a complete object value for *key* even when the outer object is malformed."""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*', raw)
    if not match:
        return None
    value_start = match.end()
    while value_start < len(raw) and raw[value_start].isspace():
        value_start += 1
    if value_start >= len(raw) or raw[value_start] != "{":
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(raw[value_start:])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _close_truncated_json_object(raw: str) -> str | None:
    """Close an otherwise well-formed JSON object prefix after provider truncation.

    This is intentionally conservative: it only appends a missing string quote
    and the delimiters already implied by the parsed prefix. It does not create
    missing keys, values, or commas.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()

    if not in_string and not stack:
        return None
    suffix = ""
    if in_string:
        if escaped:
            suffix += "\\"
        suffix += '"'
    suffix += "".join(reversed(stack))
    return text + suffix


def _close_truncated_json_array(raw: str) -> str | None:
    """Close a truncated JSON array prefix after provider truncation."""
    text = raw.strip()
    if not text.startswith("["):
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()

    if not in_string and not stack:
        return None
    suffix = ""
    if in_string:
        if escaped:
            suffix += "\\"
        suffix += '"'
    suffix += "".join(reversed(stack))
    return text + suffix


def _try_parse_closed_json_prefix(text: str) -> Any | None:
    """Bracket-close a truncated object/array prefix and parse when possible."""
    stripped = text.strip()
    if not stripped:
        return None
    closed: str | None = None
    if stripped.startswith("{"):
        closed = _close_truncated_json_object(stripped)
    elif stripped.startswith("["):
        closed = _close_truncated_json_array(stripped)
    if closed is None:
        return None
    for candidate in (closed, _repair_trailing_commas(closed)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _salvage_truncated_json_prefix(raw: str, *, max_trim: int = 4000) -> Any | None:
    """Trim a truncated JSON prefix until bracket-closing yields valid parse."""
    text = raw.strip()
    if not text or text[0] not in "{[":
        return None
    min_len = max(1, len(text) - max_trim)
    for end in range(len(text), min_len - 1, -1):
        prefix = text[:end].rstrip()
        if not prefix:
            continue
        while prefix and prefix[-1] in ",:":
            prefix = prefix[:-1].rstrip()
        parsed = _try_parse_closed_json_prefix(prefix)
        if parsed is not None:
            return parsed
    return None


def _looks_like_stream_truncation(raw: str) -> bool:
    """Heuristic: provider cut the JSON stream mid-token (not a mid-string syntax error)."""
    text = raw.rstrip()
    if len(text) < 2:
        return False
    if text[-1] in "{[,:":
        return True
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
    if in_string:
        return True
    try:
        json.loads(text)
        return False
    except json.JSONDecodeError as exc:
        pos = getattr(exc, "pos", None)
        if isinstance(pos, int) and pos >= max(0, len(text) - 8):
            return True
        return False


def _salvage_truncated_json_after_key(raw: str, key: str) -> Any | None:
    """Salvage the JSON value for *key* when the stream was truncated mid-value."""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*', raw)
    if not match:
        return None
    value_start = match.end()
    while value_start < len(raw) and raw[value_start].isspace():
        value_start += 1
    if value_start >= len(raw):
        return None
    fragment = raw[value_start:]
    if not _looks_like_stream_truncation(fragment):
        return None
    return _salvage_truncated_json_prefix(fragment)


def _extract_truncated_string_value(raw: str, key: str) -> str | None:
    """Extract a string value for *key* even when the closing quote is missing (truncated output)."""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"', raw)
    if not match:
        return None
    value_start = match.end()
    value_end = _find_json_string_end(raw, value_start)
    if value_end is not None:
        try:
            return json.loads(f'"{raw[value_start:value_end]}"')
        except json.JSONDecodeError:
            return raw[value_start:value_end]
    # Truncated: take everything after the opening quote, stripping trailing whitespace
    return raw[value_start:].rstrip()


def _recover_tool_argument_blob_args(raw: str) -> dict[str, Any] | None:
    """Recover ``tool_argument_blob`` append args when ``chunk`` breaks outer JSON.

    When the LLM inlines HTML/JSX in ``chunk``, unescaped double quotes
    break the tool-call JSON. We extract the chunk content and re-encode
    it as ``chunk_base64`` so downstream processing never sees raw markup
    in a JSON string.

    Also handles truncated ``chunk_base64`` values where the output token
    limit cut the string before the closing quote — we take whatever base64
    was emitted and let downstream decode it tolerantly.
    """
    text = _strip_json_code_fence(raw)
    low = text.lower()
    if '"append"' not in low and "'append'" not in low:
        if '"create_and_finalize"' not in low:
            return None
    if '"blob_id"' not in text and '"chunk"' not in text and '"chunk_base64"' not in text:
        if '"create_and_finalize"' not in low:
            return None

    action_info = _extract_json_string_value(text, "action")
    action = action_info[0] if action_info else ""
    if action not in ("append", "create_and_finalize"):
        return None

    blob_id_info = _extract_json_string_value(text, "blob_id")
    blob_id = blob_id_info[0] if blob_id_info else ""

    b64_info = _extract_json_string_value(text, "chunk_base64")
    if b64_info is not None:
        recovered: dict[str, Any] = {"action": action, "chunk_base64": b64_info[0]}
        if blob_id:
            recovered["blob_id"] = blob_id
        return recovered

    # Truncated chunk_base64: the string was cut off before the closing quote
    truncated_b64 = _extract_truncated_string_value(text, "chunk_base64")
    if truncated_b64 and len(truncated_b64) > 8:
        recovered = {"action": action, "chunk_base64": truncated_b64}
        if blob_id:
            recovered["blob_id"] = blob_id
        return recovered

    chunk_info = (
        _extract_json_string_value(text, "chunk")
        or _extract_malformed_quoted_field(
            text,
            "chunk",
            ("blob_id", "action", "chunk_base64"),
        )
    )
    if chunk_info is None:
        return None
    chunk_text = chunk_info[0]
    if not chunk_text:
        return None

    recovered: dict[str, Any] = {"action": action, "chunk": chunk_text}
    if blob_id:
        recovered["blob_id"] = blob_id
    return recovered


def _recover_emit_ui_tree_args(raw: str) -> dict[str, Any] | None:
    """Recover ``emit_ui_tree`` args when the outer JSON is malformed or truncated.

    Handles three scenarios:
    1. Outer JSON broken but the ``tree`` object value is complete (``raw_decode``).
    2. Outer + tree both truncated: close the brackets on the tree substring.
    3. Outer closed but inner tree still truncated: extract and close tree.
    """
    for candidate in _candidate_json_texts(_strip_json_code_fence(raw)):
        tree = _extract_json_object_value(candidate, "tree")

        if tree is None:
            closed = _close_truncated_json_object(candidate)
            if closed is not None:
                parsed = _loads_json_dict(closed)
                if parsed is not None and isinstance(parsed.get("tree"), dict):
                    tree = parsed["tree"]
                else:
                    tree = _extract_json_object_value(closed, "tree")

        if tree is None:
            tree = _extract_and_close_truncated_tree(candidate)

        if tree is None and _looks_like_stream_truncation(candidate):
            outer = _salvage_truncated_json_prefix(candidate)
            if isinstance(outer, dict):
                inner = outer.get("tree")
                if isinstance(inner, dict):
                    tree = inner
                elif "root" in outer or "schemaVersion" in outer:
                    tree = outer

        if tree is None:
            continue
        recovered: dict[str, Any] = {"tree": tree}
        canvas_info = _extract_json_string_value(candidate, "canvas_id")
        if canvas_info is not None:
            recovered["canvas_id"] = canvas_info[0]
        return recovered
    return None


def _extract_and_close_truncated_tree(raw: str) -> dict[str, Any] | None:
    """Extract the ``"tree"`` value from *raw* even when it is truncated mid-stream."""
    match = re.search(r'"tree"\s*:\s*', raw)
    if not match:
        return None
    value_start = match.end()
    while value_start < len(raw) and raw[value_start].isspace():
        value_start += 1
    if value_start >= len(raw) or raw[value_start] != "{":
        return None
    tree_fragment = raw[value_start:]

    closed = _close_truncated_json_object(tree_fragment)
    if closed is not None:
        try:
            parsed = json.loads(_repair_trailing_commas(closed))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    for wrapped in (tree_fragment, f'{{"tree":{tree_fragment}}}'):
        repaired = _try_repair_superfluous_closing_delimiter(wrapped, max_deletions=16)
        if isinstance(repaired, dict):
            inner = repaired.get("tree")
            if isinstance(inner, dict):
                return inner

    salvaged = _salvage_truncated_json_after_key(raw, "tree")
    return salvaged if isinstance(salvaged, dict) else None


def _recover_emit_ui_patch_args(raw: str) -> dict[str, Any] | None:
    """Recover ``emit_ui_patch`` args when outer JSON is malformed."""
    for candidate in _candidate_json_texts(_strip_json_code_fence(raw)):
        parsed = _loads_json_dict(candidate)
        if parsed is None:
            parsed = _try_json_dict_raw_decode_trailing_junk(candidate)
        if parsed is None:
            parsed = _try_repair_superfluous_closing_delimiter(candidate, max_deletions=16)
        if isinstance(parsed, dict) and isinstance(parsed.get("patches"), list):
            return {
                k: v
                for k, v in parsed.items()
                if k in ("patches", "canvas_id", "seq") and v is not None
            }

        patches = _salvage_truncated_json_after_key(candidate, "patches")
        if isinstance(patches, list) and patches:
            return {"patches": patches}

        if _looks_like_stream_truncation(candidate):
            outer = _salvage_truncated_json_prefix(candidate)
            if isinstance(outer, dict) and isinstance(outer.get("patches"), list):
                return {
                    k: v
                    for k, v in outer.items()
                    if k in ("patches", "canvas_id", "seq") and v is not None
                }
    return None


def _try_parse_raw_tool_args(raw: str) -> dict[str, Any] | None:
    """Parse a raw arguments string into a dict when possible."""
    for candidate in _candidate_json_texts(raw):
        parsed = _loads_json_dict(candidate)
        if parsed is not None:
            return parsed
        parsed = _try_json_dict_raw_decode_trailing_junk(candidate)
        if parsed is not None:
            return parsed
        parsed = _try_repair_superfluous_closing_delimiter(candidate)
        if parsed is not None:
            return parsed
    return (
        _recover_tool_argument_blob_args(raw)
        or _recover_emit_ui_patch_args(raw)
        or _recover_emit_ui_tree_args(raw)
        or _recover_canvas_publish_args(raw)
        or _recover_project_write_args(raw)
        or _recover_project_edit_args(raw)
        or _recover_project_apply_patch_args(raw)
        or _recover_code_execution_args(raw)
    )


def parse_tool_arguments_str(raw: str) -> dict[str, Any] | None:
    """Parse LLM ``function.arguments`` text into a parameter dict when possible.

    Uses the same recovery as the executor ``__raw__`` path: BOM trim, optional
    markdown code fences, trailing-comma repair, interior-quote repair (CJK
    ``"word"`` in string values), control-character escaping, double-encoded
    JSON-string wrappers, :func:`json.JSONDecoder.raw_decode` when only stray
    ``]``/``}`` trail a complete object, and a narrow single-character deletion
    near decode errors (common with over-nested ``emit_ui_tree`` /
    ``canvas_publish`` payloads).
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return _try_parse_raw_tool_args(stripped)


def strict_json_loads_error(raw: str) -> json.JSONDecodeError | None:
    """Return the error from a strict :func:`json.loads` of *raw*, or ``None`` if it parses."""
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        return e
    return None


def format_tool_arguments_json_error(
    raw: str,
    strict_err: json.JSONDecodeError | None = None,
    *,
    tool_name: str | None = None,
) -> str:
    """Return a bounded, model-actionable parse error.

    Generic tools keep the historical ``Malformed tool arguments JSON`` prefix
    for tests and log correlation. ``code_execution`` uses plain language: the
    usual failure mode is truncated streaming or quote-heavy inlined ``source``,
    not "invalid JSON syntax" in the abstract.
    """
    base = "Malformed tool arguments JSON"
    if not raw:
        return base
    err = strict_err or strict_json_loads_error(raw)
    if err is None:
        return base
    if tool_name == "code_execution" and '"source"' in raw:
        return _format_code_execution_raw_args_error(raw, err)
    if tool_name == "tool_argument_blob":
        low = raw.lower()
        if "append" in low and ("chunk" in low or "chunk_base64" in low):
            return _format_tool_argument_blob_raw_args_error(raw, err)

    pos = getattr(err, "pos", None)
    if not isinstance(pos, int):
        msg = f"{base}: {err.msg}"
    else:
        start = max(0, pos - 80)
        end = min(len(raw), pos + 80)
        snippet = raw[start:end].replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
        msg = (
            f"{base}: {err.msg} at line {err.lineno} column {err.colno}. "
            "Retry with strict JSON and correctly escaped string values. "
            f"Near: {snippet[:180]}"
        )
    return msg + _tool_json_parse_recovery_hint(tool_name, raw)


def _format_code_execution_raw_args_error(raw: str, err: json.JSONDecodeError) -> str:
    """Explain ``code_execution`` ``__raw__`` failures without blaming abstract JSON skill."""
    trimmed = raw.rstrip()
    incomplete_object = not trimmed.endswith("}")
    unterminated = "Unterminated string" in err.msg
    likely_truncated = incomplete_object and (unterminated or len(raw) > 800)

    parts = [
        "`code_execution` did not run: the tool-call JSON could not be decoded. "
        "The runtime usually auto-recovers inline `source`, but this call "
        "could not be salvaged.",
    ]
    if likely_truncated:
        parts.append(
            "The call appears truncated (JSON never reaches a closing `}` or "
            "closing quote on `source`)."
        )
    parts.append(
        "Retry with proper JSON escaping (escape `\"` as `\\\"` and newlines "
        "as `\\n`). If it fails again, use `tool_argument_blob` + `source_blob_id`."
    )
    return " ".join(parts)


def _format_tool_argument_blob_raw_args_error(raw: str, err: json.JSONDecodeError) -> str:
    """Explain ``tool_argument_blob`` ``__raw__`` failures (HTML-in-chunk JSON breaks)."""
    return (
        "`tool_argument_blob` did not run: the tool call is not valid JSON. "
        "For webpage HTML, prefer a single `canvas_publish(mode=html, html=...)` "
        "call; the runtime can auto-recover malformed inline HTML and publish it "
        "without this multi-step blob flow. If you are intentionally continuing "
        "blob staging, raw HTML in `chunk` often breaks parsing because JSON "
        "string literals cannot contain raw double quotes from attributes unless "
        "every quote is escaped correctly. "
        f"Parse error: {err.msg}. "
        "For blob staging, retry using `chunk_base64` (standard base64 of UTF-8 "
        "bytes, no data: URL prefix) instead of `chunk`."
    )


def _tool_json_parse_recovery_hint(tool_name: str | None, raw: str) -> str:
    """Append tool-specific guidance so the model retries with a safer transport."""
    if not tool_name:
        return ""
    if tool_name == "code_execution" and '"source"' in raw:
        return ""
    if tool_name in ("emit_ui_tree", "emit_ui_patch"):
        return " Prefer a smaller `tree` or incremental `emit_ui_patch`."
    if tool_name == "canvas_publish":
        return (
            " The runtime usually auto-recovers inline `html` and auto-stages it "
            "when large. Use short `/api/v1/files/{id}/preview` image URLs (no tokens). "
            "If this fails again, try `html_files` (map of path → source) "
            "or `tool_argument_blob` + `html_blob_id` as a last resort."
        )
    if tool_name in ("project_write", "project_apply_patch", "project_edit"):
        return (
            " The runtime usually auto-recovers inline content. "
            "If this fails again, use `*_blob_id` from `tool_argument_blob`."
        )
    if tool_name in ("slides_generate", "document_generate", "checklist_generate"):
        return (
            " Escape ASCII double quotes in titles/bodies as \\\" "
            "(or use 「」 / 『』). Newlines must be \\n."
        )
    if tool_name == "tool_argument_blob":
        return (
            " For webpage HTML, prefer direct `canvas_publish(html=...)`; "
            "for intentional blob append use plain `chunk` (not base64) when possible."
        )
    return ""


def normalize_tool_parameters(
    parameters: dict[str, Any],
    tool: BaseTool | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Normalize tool parameters and recover `__raw__` payloads when possible.

    When *tool* is provided, its :meth:`~BaseTool.recover_raw_args` is
    tried as a final fallback after the generic recovery pipeline.
    """
    params = dict(parameters or {})
    raw_payload = params.get("__raw__")
    if not isinstance(raw_payload, str):
        return params, None

    parsed = _try_parse_raw_tool_args(raw_payload)
    if parsed is not None:
        return parsed, None

    if tool is not None:
        try:
            tool_parsed = tool.recover_raw_args(raw_payload)
        except Exception:  # noqa: BLE001
            tool_parsed = None
        if tool_parsed is not None:
            return tool_parsed, None

    error = "Malformed tool arguments JSON"
    return params, error
