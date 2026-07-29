"""``project_outline`` - lightweight code symbol and import index.

Parses Python, TypeScript, and JavaScript files without executing them so
the coding agent can navigate without reading entire modules.
"""

from __future__ import annotations

import configparser
import copy
import json
import posixpath
import re
import tomllib
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from leagent.code.parsing import (
    SUPPORTED_CODE_SUFFIXES,
    CodeLanguage,
    detect_code_language,
    parse_code_source,
)
from leagent.project.fs import (
    MAX_TEXT_FILE_BYTES,
    IgnoreMatcher,
    read_text_with_detection,
    resolve_in_project,
    select_project_root,
    walk_project,
)
from leagent.tools.base import BaseTool, ToolCategory, ToolContext

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)

_DEFAULT_GLOB = "**/*"
_DEFAULT_MAX_FILE_BYTES = 1 * 1024 * 1024
_OUTLINE_CACHE_MAX_ENTRIES = 512
_CONFIG_CACHE_MAX_ENTRIES = 128
_SUPPORTED_LANGUAGES: tuple[CodeLanguage, ...] = ("python", "typescript", "javascript")
_TS_JS_CONFIG_FILENAMES = ("tsconfig.json", "jsconfig.json")
_OUTLINE_EXTRA_IGNORE_DIRS: tuple[str, ...] = (
    ".nyc_output",
    ".vite",
    "coverage",
    "htmlcov",
    "playwright-report",
    "test-results",
)
_OUTLINE_EXTRA_IGNORES: tuple[str, ...] = tuple(
    f"{name}/" for name in _OUTLINE_EXTRA_IGNORE_DIRS
)
_TS_JS_CONFIG_EXTENDS_MAX_DEPTH = 5
_PACKAGE_JSON_FILENAME = "package.json"
_PYPROJECT_FILENAME = "pyproject.toml"
_SETUP_CFG_FILENAME = "setup.cfg"
_DEFAULT_PYTHON_SOURCE_ROOTS = ("src",)
_PYTHON_RESOLVE_SUFFIXES = (".py", ".pyw")
_TYPESCRIPT_RESOLVE_SUFFIXES = (
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".d.ts",
    ".d.mts",
    ".d.cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)
_JAVASCRIPT_RESOLVE_SUFFIXES = (
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".d.ts",
    ".d.mts",
    ".d.cts",
)
_CODE_RESOLVE_SUFFIXES = _PYTHON_RESOLVE_SUFFIXES + tuple(
    suffix
    for suffix in _TYPESCRIPT_RESOLVE_SUFFIXES + _JAVASCRIPT_RESOLVE_SUFFIXES
    if suffix not in _PYTHON_RESOLVE_SUFFIXES
)
_OUTLINE_CACHE: OrderedDict[tuple[str, int, int, CodeLanguage], dict[str, Any]] = OrderedDict()
_PYTHON_SOURCE_ROOT_CACHE: OrderedDict[
    tuple[str, tuple[str, int, int], tuple[str, int, int]], tuple[str, ...]
] = OrderedDict()
_PACKAGE_JSON_MAPPING_CACHE: OrderedDict[
    tuple[str, int, int], tuple[_TsJsPathMapping, ...]
] = OrderedDict()
_TS_JS_CONFIG_PAYLOAD_CACHE: OrderedDict[
    tuple[str, int, int], dict[str, Any] | None
] = OrderedDict()


@dataclass(frozen=True)
class _TsJsPathMapping:
    source_prefix: str
    source_suffix: str
    source_has_wildcard: bool
    target_prefix: str
    target_suffix: str


@dataclass(frozen=True)
class _TsJsResolutionConfig:
    base_url: str | None
    paths: tuple[_TsJsPathMapping, ...]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    escaped = re.escape(_normalize_glob_pattern(pattern))
    translated = (
        escaped.replace(r"\*\*/", "(?:.*/)?")
        .replace(r"/\*\*", "(?:/.*)?")
        .replace(r"\*\*", ".*")
        .replace(r"\*", "[^/]*")
        .replace(r"\?", "[^/]")
    )
    return re.compile(translated)


def _normalize_glob_pattern(pattern: str) -> str:
    value = pattern.strip().replace("\\", "/")
    while "//" in value:
        value = value.replace("//", "/")
    while value.startswith("./"):
        value = value[2:]
    return value or _DEFAULT_GLOB


class ProjectOutlineTool(BaseTool):
    """Return lightweight symbols and imports for code files in the project."""

    name = "project_outline"
    description = (
        "List functions, classes, methods, TypeScript declarations, import "
        "statements, and exports for Python/TypeScript/JavaScript files under "
        "the active project root. Use before `project_read` to locate symbols. "
        "Pass `path` as a single code file or a subdirectory; combine with "
        "`glob`, `languages`, `max_files`, and `max_file_bytes` to cap work "
        "on large trees. The response includes a bounded symbol_index for "
        "quick navigation by symbol name and a bounded import_index for "
        "dependency navigation, plus a bounded export_index for public API "
        "navigation. Python dependency resolution reads pyproject/setup.cfg "
        "source roots and src-layout fallbacks. TS/JS dependency resolution "
        "reads tsconfig/jsconfig baseUrl/paths, including local extends, plus "
        "package.json imports and self-reference exports; set "
        "`include_files=false` for a compact index-only response, or add "
        "`include_file_index=true` / `include_directory_index=true` for "
        "paged file- or directory-level summaries."
    )
    category = ToolCategory.CODE
    aliases = ["python_outline", "code_outline"]
    is_concurrency_safe = True
    is_read_only = True
    interrupt_behavior = "cancel"
    max_result_size_chars = 200_000
    timeout_sec = 45

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Project-relative code file or directory. "
                        "Defaults to the project root."
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Glob matched against project-relative paths "
                        f"(default `{_DEFAULT_GLOB}`). Windows path separators "
                        "are accepted."
                    ),
                    "default": _DEFAULT_GLOB,
                },
                "languages": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(_SUPPORTED_LANGUAGES),
                    },
                    "default": list(_SUPPORTED_LANGUAGES),
                    "description": (
                        "Optional language filter. Supported values: "
                        "python, typescript, javascript."
                    ),
                },
                "max_files": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 40,
                    "description": "Maximum number of code files to parse.",
                },
                "max_file_bytes": {
                    "type": "integer",
                    "minimum": 1024,
                    "maximum": MAX_TEXT_FILE_BYTES,
                    "default": _DEFAULT_MAX_FILE_BYTES,
                    "description": (
                        "Skip individual files larger than this byte limit "
                        "instead of reading and parsing them."
                    ),
                },
                "include_symbol_index": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Whether to include a bounded project-level symbol index "
                        "grouped by symbol name."
                    ),
                },
                "max_index_symbols": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2_000,
                    "default": 300,
                    "description": "Maximum symbol references to include in symbol_index.",
                },
                "symbol_index_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100_000,
                    "default": 0,
                    "description": (
                        "Number of matching symbol references to skip before "
                        "emitting symbol_index entries. Use with max_index_symbols "
                        "to page through large projects."
                    ),
                },
                "symbol_query": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring filter applied only "
                        "to symbol_index names, kinds, containers, decorators, "
                        "and bases. Windows path separators are normalized. "
                        "Summary and file parsing remain unchanged."
                    ),
                },
                "include_import_index": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Whether to include a bounded project-level dependency index "
                        "grouped by imported or re-exported module."
                    ),
                },
                "max_index_imports": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2_000,
                    "default": 300,
                    "description": "Maximum import references to include in import_index.",
                },
                "import_index_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100_000,
                    "default": 0,
                    "description": (
                        "Number of matching import references to skip before "
                        "emitting import_index entries. Use with max_index_imports "
                        "to page through large dependency indexes."
                    ),
                },
                "import_query": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring filter applied only "
                        "to import_index module names, imported names, aliases, "
                        "and resolved paths. Windows path separators are normalized. "
                        "Summary and file parsing remain unchanged."
                    ),
                },
                "include_export_index": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Whether to include a bounded project-level export index "
                        "grouped by exported name."
                    ),
                },
                "max_index_exports": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2_000,
                    "default": 300,
                    "description": "Maximum export references to include in export_index.",
                },
                "export_index_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100_000,
                    "default": 0,
                    "description": (
                        "Number of matching export references to skip before "
                        "emitting export_index entries. Use with max_index_exports "
                        "to page through public API indexes."
                    ),
                },
                "export_query": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring filter applied only "
                        "to export_index names, aliases, modules, declaration kinds, "
                        "and resolved paths. Windows path separators are normalized. "
                        "Summary and file parsing remain unchanged."
                    ),
                },
                "include_files": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Whether to include full per-file outlines in `files`. "
                        "Set false when summary and indexes are enough."
                    ),
                },
                "include_file_index": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Whether to include a compact per-file index with counts "
                        "and small previews. Useful with include_files=false."
                    ),
                },
                "max_file_index_entries": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                    "description": "Maximum file_index entries to include.",
                },
                "file_index_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100_000,
                    "default": 0,
                    "description": (
                        "Number of matching file_index entries to skip before "
                        "emitting compact file summaries."
                    ),
                },
                "file_query": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring filter applied only "
                        "to file_index paths, languages, symbol/import/export "
                        "previews, and diagnostics. Windows path separators are "
                        "normalized."
                    ),
                },
                "include_directory_index": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Whether to include compact per-directory counts and "
                        "small previews. Useful before narrowing path/glob."
                    ),
                },
                "max_directory_index_entries": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                    "description": "Maximum directory_index entries to include.",
                },
                "directory_index_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100_000,
                    "default": 0,
                    "description": (
                        "Number of matching directory_index entries to skip before "
                        "emitting compact directory summaries."
                    ),
                },
                "directory_index_depth": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "default": 0,
                    "description": (
                        "Directory grouping depth for directory_index. "
                        "0 keeps full directory paths; positive values group "
                        "by the first N path segments."
                    ),
                },
                "directory_query": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring filter applied only "
                        "to directory_index paths, languages, top previews, and "
                        "diagnostics. Windows path separators are normalized."
                    ),
                },
                "project_path": {
                    "type": "string",
                    "description": "Optional override of the active project root.",
                },
            },
            "additionalProperties": False,
        }

    def get_activity_description(self, params: dict[str, Any] | None = None) -> str | None:
        p = (params or {}).get("path")
        return f"Outline {p}" if p else "Outline project code"

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        root = select_project_root(
            context, explicit=params.get("project_path"),
        )
        rel = (params.get("path") or "").strip()
        if rel:
            resolved = resolve_in_project(root, rel, must_exist=True)
            target = resolved.abs_path
        else:
            target = root

        glob_pat = _normalize_glob_pattern(str(params.get("glob") or _DEFAULT_GLOB))
        languages = _normalize_languages(params.get("languages"))
        max_files = int(params.get("max_files") or 40)
        max_files = max(1, min(500, max_files))
        max_file_bytes = int(params.get("max_file_bytes") or _DEFAULT_MAX_FILE_BYTES)
        max_file_bytes = max(1024, min(MAX_TEXT_FILE_BYTES, max_file_bytes))
        include_symbol_index = bool(params.get("include_symbol_index", True))
        max_index_symbols = int(params.get("max_index_symbols") or 300)
        max_index_symbols = max(1, min(2_000, max_index_symbols))
        symbol_index_offset = _normalize_index_offset(params.get("symbol_index_offset"))
        symbol_query = _normalize_symbol_query(params.get("symbol_query"))
        include_import_index = bool(params.get("include_import_index", True))
        max_index_imports = int(params.get("max_index_imports") or 300)
        max_index_imports = max(1, min(2_000, max_index_imports))
        import_index_offset = _normalize_index_offset(params.get("import_index_offset"))
        import_query = _normalize_symbol_query(params.get("import_query"))
        include_export_index = bool(params.get("include_export_index", True))
        max_index_exports = int(params.get("max_index_exports") or 300)
        max_index_exports = max(1, min(2_000, max_index_exports))
        export_index_offset = _normalize_index_offset(params.get("export_index_offset"))
        export_query = _normalize_symbol_query(params.get("export_query"))
        include_files = bool(params.get("include_files", True))
        include_file_index = bool(params.get("include_file_index", False))
        max_file_index_entries = int(params.get("max_file_index_entries") or 100)
        max_file_index_entries = max(1, min(500, max_file_index_entries))
        file_index_offset = _normalize_index_offset(params.get("file_index_offset"))
        file_query = _normalize_symbol_query(params.get("file_query"))
        include_directory_index = bool(params.get("include_directory_index", False))
        max_directory_index_entries = int(
            params.get("max_directory_index_entries") or 100
        )
        max_directory_index_entries = max(1, min(500, max_directory_index_entries))
        directory_index_offset = _normalize_index_offset(
            params.get("directory_index_offset")
        )
        directory_index_depth = _normalize_directory_index_depth(
            params.get("directory_index_depth")
        )
        directory_query = _normalize_symbol_query(params.get("directory_query"))
        regex = _glob_to_regex(glob_pat)

        code_files: list[Path] = []
        truncated_walk = False

        if target.is_file():
            if target.suffix.lower() not in SUPPORTED_CODE_SUFFIXES:
                return {
                    "error": "project_outline only supports Python, TypeScript, and JavaScript files.",
                    "path": rel or ".",
                }
            try:
                target.relative_to(root)
            except ValueError:
                return {"error": "Path escapes project root."}
            rel_target = target.relative_to(root).as_posix()
            language = detect_code_language(rel_target)
            code_files = [target] if language in languages else []
        else:
            matcher = IgnoreMatcher(root, extra_ignores=_OUTLINE_EXTRA_IGNORES)
            for fpath in walk_project(target, matcher=matcher, max_files=50_000):
                if fpath.suffix.lower() not in SUPPORTED_CODE_SUFFIXES:
                    continue
                try:
                    rel_p = fpath.relative_to(root).as_posix()
                except ValueError:
                    continue
                if not regex.fullmatch(rel_p) and not regex.fullmatch(fpath.name):
                    continue
                language = detect_code_language(rel_p)
                if language not in languages:
                    continue
                if len(code_files) >= max_files:
                    truncated_walk = True
                    break
                code_files.append(fpath)

        code_files.sort(key=lambda p: p.relative_to(root).as_posix())

        outlines: list[dict[str, Any]] = []
        cache_hits = 0
        skipped = 0
        for fpath in code_files:
            rel_p = fpath.relative_to(root).as_posix()
            language = detect_code_language(rel_p)
            outline, meta = _outline_file(
                fpath,
                rel_p=rel_p,
                language=language,
                max_file_bytes=max_file_bytes,
            )
            cache_hits += 1 if meta.get("cache_hit") else 0
            skipped += 1 if outline.get("skipped") else 0
            outlines.append(outline)

        path_lookup: dict[str, str] | None = None
        ts_js_resolution: _TsJsResolutionConfig | None = None
        python_source_roots = _DEFAULT_PYTHON_SOURCE_ROOTS
        if include_import_index or include_export_index:
            path_lookup = _build_outline_path_lookup(outlines)
            ts_js_resolution = _load_ts_js_resolution_config(root)
            python_source_roots = _load_python_source_roots(root)
        logger.info(
            "project_outline",
            files=len(outlines),
            truncated=truncated_walk,
        )
        summary = _build_summary(
            outlines,
            cache_hits=cache_hits,
            skipped=skipped,
            truncated=truncated_walk,
        )
        result = {
            "glob": glob_pat,
            "languages": [lang for lang in _SUPPORTED_LANGUAGES if lang in languages],
            "max_files": max_files,
            "max_file_bytes": max_file_bytes,
            "include_symbol_index": include_symbol_index,
            "max_index_symbols": max_index_symbols,
            "symbol_index_offset": symbol_index_offset,
            "symbol_query": symbol_query,
            "include_import_index": include_import_index,
            "max_index_imports": max_index_imports,
            "import_index_offset": import_index_offset,
            "import_query": import_query,
            "include_export_index": include_export_index,
            "max_index_exports": max_index_exports,
            "export_index_offset": export_index_offset,
            "export_query": export_query,
            "include_files": include_files,
            "include_file_index": include_file_index,
            "max_file_index_entries": max_file_index_entries,
            "file_index_offset": file_index_offset,
            "file_query": file_query,
            "include_directory_index": include_directory_index,
            "max_directory_index_entries": max_directory_index_entries,
            "directory_index_offset": directory_index_offset,
            "directory_index_depth": directory_index_depth,
            "directory_query": directory_query,
            "count": len(outlines),
            "cache_hits": cache_hits,
            "skipped": skipped,
            "summary": summary,
            "truncated": truncated_walk,
        }
        if include_files:
            result["files"] = outlines
            result["files_omitted"] = False
        else:
            result["files_omitted"] = True
        if include_file_index:
            file_index, file_index_count, file_index_total, file_index_truncated = (
                _build_file_index(
                    outlines,
                    max_files=max_file_index_entries,
                    offset=file_index_offset,
                    file_query=file_query,
                )
            )
            result["file_index"] = file_index
            result["file_index_count"] = file_index_count
            result["file_index_total_matches"] = file_index_total
            result["file_index_truncated"] = file_index_truncated
            summary["indexed_files"] = file_index_count
            summary["file_index_total_matches"] = file_index_total
            summary["file_index_offset"] = file_index_offset
            summary["file_index_truncated"] = file_index_truncated
            if file_query is not None:
                summary["file_query"] = file_query
        if include_directory_index:
            directory_index, dir_index_count, dir_index_total, dir_index_truncated = (
                _build_directory_index(
                    outlines,
                    max_directories=max_directory_index_entries,
                    offset=directory_index_offset,
                    depth=directory_index_depth,
                    directory_query=directory_query,
                )
            )
            result["directory_index"] = directory_index
            result["directory_index_count"] = dir_index_count
            result["directory_index_total_matches"] = dir_index_total
            result["directory_index_truncated"] = dir_index_truncated
            summary["indexed_directories"] = dir_index_count
            summary["directory_index_total_matches"] = dir_index_total
            summary["directory_index_offset"] = directory_index_offset
            summary["directory_index_depth"] = directory_index_depth
            summary["directory_index_truncated"] = dir_index_truncated
            if directory_query is not None:
                summary["directory_query"] = directory_query
        if include_symbol_index:
            index, index_count, index_total, index_truncated = _build_symbol_index(
                outlines,
                max_symbols=max_index_symbols,
                offset=symbol_index_offset,
                symbol_query=symbol_query,
            )
            result["symbol_index"] = index
            result["symbol_index_count"] = index_count
            result["symbol_index_total_matches"] = index_total
            result["symbol_index_truncated"] = index_truncated
            summary["indexed_symbols"] = index_count
            summary["symbol_index_total_matches"] = index_total
            summary["symbol_index_offset"] = symbol_index_offset
            summary["symbol_index_truncated"] = index_truncated
            if symbol_query is not None:
                summary["symbol_query"] = symbol_query
        if include_import_index:
            import_index, import_count, import_total, import_truncated = _build_import_index(
                outlines,
                max_imports=max_index_imports,
                offset=import_index_offset,
                import_query=import_query,
                path_lookup=path_lookup,
                project_root=root,
                ts_js_resolution=ts_js_resolution,
                python_source_roots=python_source_roots,
            )
            result["import_index"] = import_index
            result["import_index_count"] = import_count
            result["import_index_total_matches"] = import_total
            result["import_index_truncated"] = import_truncated
            summary["indexed_imports"] = import_count
            summary["import_index_total_matches"] = import_total
            summary["import_index_offset"] = import_index_offset
            summary["import_index_truncated"] = import_truncated
            if import_query is not None:
                summary["import_query"] = import_query
        if include_export_index:
            export_index, export_count, export_total, export_truncated = _build_export_index(
                outlines,
                max_exports=max_index_exports,
                offset=export_index_offset,
                export_query=export_query,
                path_lookup=path_lookup,
                project_root=root,
                ts_js_resolution=ts_js_resolution,
                python_source_roots=python_source_roots,
            )
            result["export_index"] = export_index
            result["export_index_count"] = export_count
            result["export_index_total_matches"] = export_total
            result["export_index_truncated"] = export_truncated
            summary["indexed_exports"] = export_count
            summary["export_index_total_matches"] = export_total
            summary["export_index_offset"] = export_index_offset
            summary["export_index_truncated"] = export_truncated
            if export_query is not None:
                summary["export_query"] = export_query
        return result


def _normalize_languages(raw: Any) -> set[CodeLanguage]:
    if raw is None:
        return set(_SUPPORTED_LANGUAGES)
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        return set(_SUPPORTED_LANGUAGES)

    languages: set[CodeLanguage] = set()
    for value in values:
        if value in _SUPPORTED_LANGUAGES:
            languages.add(value)
    return languages or set(_SUPPORTED_LANGUAGES)


def _normalize_symbol_query(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip().replace("\\", "/")
    return value or None


def _normalize_index_offset(raw: Any) -> int:
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100_000, value))


def _normalize_directory_index_depth(raw: Any) -> int:
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(20, value))


def _config_file_fingerprint(path: Path) -> tuple[str, int, int]:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    try:
        stat = resolved.stat()
    except OSError:
        return str(resolved), -1, -1
    if not resolved.is_file():
        return str(resolved), -1, -1
    return str(resolved), int(stat.st_mtime_ns), int(stat.st_size)


def _trim_lru(cache: OrderedDict[Any, Any], max_entries: int) -> None:
    while len(cache) > max_entries:
        cache.popitem(last=False)


def _outline_file(
    fpath: Path,
    *,
    rel_p: str,
    language: CodeLanguage,
    max_file_bytes: int,
) -> tuple[dict[str, Any], dict[str, bool]]:
    try:
        stat = fpath.stat()
    except OSError as exc:
        return _error_outline(rel_p, language, str(exc)), {"cache_hit": False}

    size = int(stat.st_size)
    if size > max_file_bytes:
        return (
            {
                "path": rel_p,
                "language": language,
                "symbols": [],
                "imports": [],
                "exports": [],
                "diagnostics": [
                    {
                        "message": (
                            f"File size {size} exceeds max_file_bytes={max_file_bytes}; "
                            "narrow the path/glob or raise max_file_bytes."
                        ),
                        "severity": "warning",
                        "line": 1,
                        "column": 1,
                        "end_line": None,
                        "end_column": None,
                        "offset": None,
                        "code": "file_too_large",
                    }
                ],
                "line_count": 0,
                "char_count": 0,
                "size_bytes": size,
                "skipped": True,
                "skip_reason": "file_too_large",
            },
            {"cache_hit": False},
        )

    key = (str(fpath), int(stat.st_mtime_ns), size, language)
    cached = _OUTLINE_CACHE.get(key)
    if cached is not None:
        _OUTLINE_CACHE.move_to_end(key)
        return copy.deepcopy(cached), {"cache_hit": True}

    try:
        text, _enc = read_text_with_detection(fpath)
    except OSError as exc:
        return _error_outline(rel_p, language, str(exc)), {"cache_hit": False}

    outline = parse_code_source(text, path=rel_p, language=language)
    outline["size_bytes"] = size
    _OUTLINE_CACHE[key] = copy.deepcopy(outline)
    _OUTLINE_CACHE.move_to_end(key)
    while len(_OUTLINE_CACHE) > _OUTLINE_CACHE_MAX_ENTRIES:
        _OUTLINE_CACHE.popitem(last=False)
    return outline, {"cache_hit": False}


def _error_outline(rel_p: str, language: CodeLanguage, error: str) -> dict[str, Any]:
    return {
        "path": rel_p,
        "language": language,
        "error": error,
        "symbols": [],
        "imports": [],
        "exports": [],
        "diagnostics": [],
        "line_count": 0,
        "char_count": 0,
        "size_bytes": 0,
    }


def _build_outline_path_lookup(outlines: list[dict[str, Any]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for outline in outlines:
        path = outline.get("path")
        language = outline.get("language")
        if isinstance(path, str) and isinstance(language, str):
            lookup[path] = language
    return lookup


def _build_summary(
    outlines: list[dict[str, Any]],
    *,
    cache_hits: int,
    skipped: int,
    truncated: bool,
) -> dict[str, Any]:
    by_language: dict[str, int] = {}
    symbol_kinds: dict[str, int] = {}
    total_symbols = 0
    total_imports = 0
    total_exports = 0
    total_line_count = 0
    total_char_count = 0
    total_size_bytes = 0
    diagnostic_count = 0
    problem_files: list[str] = []
    largest_files: list[dict[str, Any]] = []

    for outline in outlines:
        language = str(outline.get("language") or "unknown")
        by_language[language] = by_language.get(language, 0) + 1

        symbols = outline.get("symbols") or []
        imports = outline.get("imports") or []
        exports = outline.get("exports") or []
        diagnostics = outline.get("diagnostics") or []
        total_symbols += len(symbols)
        total_imports += len(imports)
        total_exports += len(exports)
        diagnostic_count += len(diagnostics)
        total_line_count += _non_negative_int(outline.get("line_count"))
        total_char_count += _non_negative_int(outline.get("char_count"))
        size_bytes = _non_negative_int(outline.get("size_bytes"))
        total_size_bytes += size_bytes
        path = outline.get("path")
        if isinstance(path, str):
            largest_files.append(
                {
                    "path": path,
                    "language": language,
                    "size_bytes": size_bytes,
                    "line_count": _non_negative_int(outline.get("line_count")),
                    "char_count": _non_negative_int(outline.get("char_count")),
                }
            )

        for symbol in symbols:
            if not isinstance(symbol, dict):
                continue
            kind = str(symbol.get("kind") or "unknown")
            symbol_kinds[kind] = symbol_kinds.get(kind, 0) + 1

        if (
            (outline.get("error") or outline.get("parse_error") or outline.get("skipped") or diagnostics)
            and isinstance(path, str)
            and len(problem_files) < 25
        ):
            problem_files.append(path)

    largest_files.sort(
        key=lambda item: (
            int(item.get("size_bytes") or 0),
            str(item.get("path") or ""),
        ),
        reverse=True,
    )

    return {
        "files": len(outlines),
        "by_language": dict(sorted(by_language.items())),
        "symbols": total_symbols,
        "symbol_kinds": dict(sorted(symbol_kinds.items())),
        "imports": total_imports,
        "exports": total_exports,
        "line_count": total_line_count,
        "char_count": total_char_count,
        "size_bytes": total_size_bytes,
        "largest_files": largest_files[:5],
        "diagnostics": diagnostic_count,
        "problem_files": problem_files,
        "cache_hits": cache_hits,
        "skipped": skipped,
        "truncated": truncated,
    }


def _non_negative_int(raw: Any) -> int:
    return raw if isinstance(raw, int) and raw > 0 else 0


def _build_file_index(
    outlines: list[dict[str, Any]],
    *,
    max_files: int,
    offset: int = 0,
    file_query: str | None = None,
) -> tuple[list[dict[str, Any]], int, int, bool]:
    entries: list[dict[str, Any]] = []
    count = 0
    total_matches = 0
    query = file_query.casefold() if file_query else None

    for outline in sorted(outlines, key=lambda item: str(item.get("path") or "")):
        if query is not None and not _file_matches_query(outline, query):
            continue
        total_matches += 1
        if total_matches <= offset or count >= max_files:
            continue
        entries.append(_file_index_entry(outline))
        count += 1

    truncated = total_matches > offset + count
    return entries, count, total_matches, truncated


def _file_index_entry(outline: dict[str, Any]) -> dict[str, Any]:
    path = str(outline.get("path") or "")
    language = str(outline.get("language") or "unknown")
    symbols = [item for item in outline.get("symbols") or [] if isinstance(item, dict)]
    imports = [item for item in outline.get("imports") or [] if isinstance(item, dict)]
    exports = [item for item in outline.get("exports") or [] if isinstance(item, dict)]
    diagnostics = [
        item for item in outline.get("diagnostics") or [] if isinstance(item, dict)
    ]

    entry: dict[str, Any] = {
        "path": path,
        "language": language,
        "symbol_count": len(symbols),
        "import_count": len(imports),
        "export_count": len(exports),
        "diagnostic_count": len(diagnostics),
        "line_count": _non_negative_int(outline.get("line_count")),
        "char_count": _non_negative_int(outline.get("char_count")),
        "size_bytes": _non_negative_int(outline.get("size_bytes")),
    }
    if outline.get("skipped") is True:
        entry["skipped"] = True
    skip_reason = outline.get("skip_reason")
    if isinstance(skip_reason, str):
        entry["skip_reason"] = skip_reason
    if outline.get("parse_error"):
        entry["parse_error"] = True

    top_symbols = _file_index_top_symbols(symbols)
    if top_symbols:
        entry["top_symbols"] = top_symbols
    top_imports = _file_index_top_imports(imports)
    if top_imports:
        entry["top_imports"] = top_imports
    top_exports = _file_index_top_exports(exports)
    if top_exports:
        entry["top_exports"] = top_exports
    diagnostic_codes = _file_index_diagnostic_codes(diagnostics)
    if diagnostic_codes:
        entry["diagnostic_codes"] = diagnostic_codes
    return entry


def _build_directory_index(
    outlines: list[dict[str, Any]],
    *,
    max_directories: int,
    offset: int = 0,
    depth: int = 0,
    directory_query: str | None = None,
) -> tuple[list[dict[str, Any]], int, int, bool]:
    buckets: dict[str, dict[str, Any]] = {}
    for outline in sorted(outlines, key=lambda item: str(item.get("path") or "")):
        path = outline.get("path")
        if not isinstance(path, str):
            continue
        directory = _directory_index_path(path, depth=depth)
        bucket = buckets.setdefault(directory, _empty_directory_index_bucket(directory))
        _add_outline_to_directory_bucket(bucket, outline)

    entries: list[dict[str, Any]] = []
    count = 0
    total_matches = 0
    query = directory_query.casefold() if directory_query else None

    for directory in sorted(buckets):
        entry = _finalize_directory_index_bucket(buckets[directory])
        if query is not None and not _directory_matches_query(entry, query):
            continue
        total_matches += 1
        if total_matches <= offset or count >= max_directories:
            continue
        entries.append(entry)
        count += 1

    truncated = total_matches > offset + count
    return entries, count, total_matches, truncated


def _directory_index_path(path: str, *, depth: int = 0) -> str:
    directory = posixpath.dirname(path.replace("\\", "/"))
    if not directory:
        return "."
    if depth <= 0:
        return directory
    parts = [part for part in directory.split("/") if part]
    return "/".join(parts[:depth]) if parts else "."


def _empty_directory_index_bucket(directory: str) -> dict[str, Any]:
    return {
        "path": directory,
        "file_count": 0,
        "by_language": {},
        "symbol_count": 0,
        "symbol_kinds": {},
        "import_count": 0,
        "export_count": 0,
        "diagnostic_count": 0,
        "line_count": 0,
        "char_count": 0,
        "size_bytes": 0,
        "skipped_count": 0,
        "problem_file_count": 0,
        "problem_files": [],
        "top_symbols": [],
        "top_imports": [],
        "top_exports": [],
        "diagnostic_codes": [],
    }


def _add_outline_to_directory_bucket(
    bucket: dict[str, Any],
    outline: dict[str, Any],
) -> None:
    bucket["file_count"] += 1
    language = str(outline.get("language") or "unknown")
    by_language = bucket["by_language"]
    by_language[language] = by_language.get(language, 0) + 1

    symbols = [item for item in outline.get("symbols") or [] if isinstance(item, dict)]
    imports = [item for item in outline.get("imports") or [] if isinstance(item, dict)]
    exports = [item for item in outline.get("exports") or [] if isinstance(item, dict)]
    diagnostics = [
        item for item in outline.get("diagnostics") or [] if isinstance(item, dict)
    ]

    bucket["symbol_count"] += len(symbols)
    bucket["import_count"] += len(imports)
    bucket["export_count"] += len(exports)
    bucket["diagnostic_count"] += len(diagnostics)
    bucket["line_count"] += _non_negative_int(outline.get("line_count"))
    bucket["char_count"] += _non_negative_int(outline.get("char_count"))
    bucket["size_bytes"] += _non_negative_int(outline.get("size_bytes"))
    if outline.get("skipped") is True:
        bucket["skipped_count"] += 1

    path = outline.get("path")
    has_problem = bool(
        outline.get("error")
        or outline.get("parse_error")
        or outline.get("skipped")
        or diagnostics
    )
    if isinstance(path, str) and has_problem:
        bucket["problem_file_count"] += 1
        if len(bucket["problem_files"]) < 8:
            bucket["problem_files"].append(path)

    for symbol in symbols:
        kind = str(symbol.get("kind") or "unknown")
        symbol_kinds = bucket["symbol_kinds"]
        symbol_kinds[kind] = symbol_kinds.get(kind, 0) + 1
        if isinstance(path, str) and len(bucket["top_symbols"]) < 8:
            ref = _directory_symbol_ref(symbol, path)
            if ref is not None:
                bucket["top_symbols"].append(ref)

    for item in imports:
        for module in _import_index_modules(item):
            if not _append_unique_limited(bucket["top_imports"], module, 8):
                break

    for item in exports:
        for name in _export_index_names(item):
            if not _append_unique_limited(bucket["top_exports"], name, 8):
                break

    for code in _file_index_diagnostic_codes(diagnostics):
        if not _append_unique_limited(bucket["diagnostic_codes"], code, 8):
            break


def _directory_symbol_ref(symbol: dict[str, Any], path: str) -> dict[str, Any] | None:
    name = symbol.get("name")
    kind = symbol.get("kind")
    line = symbol.get("line")
    if not isinstance(name, str) or not isinstance(kind, str) or not isinstance(line, int):
        return None
    ref: dict[str, Any] = {"name": name, "kind": kind, "path": path, "line": line}
    container = symbol.get("container")
    if isinstance(container, str):
        ref["container"] = container
    return ref


def _finalize_directory_index_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "path": bucket["path"],
        "file_count": bucket["file_count"],
        "by_language": dict(sorted(bucket["by_language"].items())),
        "symbol_count": bucket["symbol_count"],
        "symbol_kinds": dict(sorted(bucket["symbol_kinds"].items())),
        "import_count": bucket["import_count"],
        "export_count": bucket["export_count"],
        "diagnostic_count": bucket["diagnostic_count"],
        "line_count": bucket["line_count"],
        "char_count": bucket["char_count"],
        "size_bytes": bucket["size_bytes"],
        "skipped_count": bucket["skipped_count"],
        "problem_file_count": bucket["problem_file_count"],
    }
    for key in (
        "problem_files",
        "top_symbols",
        "top_imports",
        "top_exports",
        "diagnostic_codes",
    ):
        values = bucket.get(key)
        if values:
            entry[key] = values
    return entry


def _directory_matches_query(entry: dict[str, Any], query: str) -> bool:
    for field in (entry.get("path"),):
        if isinstance(field, str) and query in field.casefold():
            return True

    languages = entry.get("by_language")
    if isinstance(languages, dict) and any(
        isinstance(language, str) and query in language.casefold()
        for language in languages
    ):
        return True

    for symbol in entry.get("top_symbols") or []:
        if not isinstance(symbol, dict):
            continue
        for key in ("name", "kind", "path", "container"):
            value = symbol.get(key)
            if isinstance(value, str) and query in value.casefold():
                return True

    for list_key in ("top_imports", "top_exports", "diagnostic_codes", "problem_files"):
        values = entry.get(list_key)
        if isinstance(values, list) and any(
            isinstance(value, str) and query in value.casefold()
            for value in values
        ):
            return True
    return False


def _file_index_top_symbols(symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for symbol in symbols:
        name = symbol.get("name")
        kind = symbol.get("kind")
        line = symbol.get("line")
        if not isinstance(name, str) or not isinstance(kind, str) or not isinstance(line, int):
            continue
        ref: dict[str, Any] = {"name": name, "kind": kind, "line": line}
        container = symbol.get("container")
        if isinstance(container, str):
            ref["container"] = container
        out.append(ref)
        if len(out) >= 8:
            break
    return out


def _file_index_top_imports(imports: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in imports:
        for module in _import_index_modules(item):
            _append_unique(values, module)
            if len(values) >= 8:
                return values
    return values


def _file_index_top_exports(exports: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in exports:
        for name in _export_index_names(item):
            _append_unique(values, name)
            if len(values) >= 8:
                return values
    return values


def _file_index_diagnostic_codes(diagnostics: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for diagnostic in diagnostics:
        code = diagnostic.get("code")
        if isinstance(code, str):
            _append_unique(values, code)
            if len(values) >= 8:
                return values
    return values


def _file_matches_query(outline: dict[str, Any], query: str) -> bool:
    for field in (
        outline.get("path"),
        outline.get("language"),
        outline.get("skip_reason"),
        outline.get("error"),
        outline.get("parse_error"),
    ):
        if isinstance(field, str) and query in field.casefold():
            return True

    for symbol in outline.get("symbols") or []:
        if isinstance(symbol, dict) and _symbol_matches_query(symbol, query):
            return True

    for item in outline.get("imports") or []:
        if isinstance(item, dict) and _file_import_matches_query(item, query):
            return True

    for item in outline.get("exports") or []:
        if isinstance(item, dict) and _file_export_matches_query(item, query):
            return True

    for diagnostic in outline.get("diagnostics") or []:
        if isinstance(diagnostic, dict) and _diagnostic_matches_query(diagnostic, query):
            return True
    return False


def _file_import_matches_query(item: dict[str, Any], query: str) -> bool:
    for module in _import_index_modules(item):
        if query in module.casefold():
            return True

    names = item.get("names")
    if isinstance(names, list) and any(
        isinstance(name, str) and query in name.casefold()
        for name in names
    ):
        return True

    aliases = item.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if not isinstance(alias, dict):
                continue
            for key in ("name", "asname"):
                value = alias.get(key)
                if isinstance(value, str) and query in value.casefold():
                    return True
    return False


def _file_export_matches_query(item: dict[str, Any], query: str) -> bool:
    for name in _export_index_names(item):
        if query in name.casefold():
            return True
    for field in (item.get("module"), item.get("declaration_kind"), item.get("kind")):
        if isinstance(field, str) and query in field.casefold():
            return True
    aliases = item.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if not isinstance(alias, dict):
                continue
            for key in ("name", "asname"):
                value = alias.get(key)
                if isinstance(value, str) and query in value.casefold():
                    return True
    return False


def _diagnostic_matches_query(diagnostic: dict[str, Any], query: str) -> bool:
    for field in (
        diagnostic.get("message"),
        diagnostic.get("severity"),
        diagnostic.get("code"),
    ):
        if isinstance(field, str) and query in field.casefold():
            return True
    return False


def _build_symbol_index(
    outlines: list[dict[str, Any]],
    *,
    max_symbols: int,
    offset: int = 0,
    symbol_query: str | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], int, int, bool]:
    index: dict[str, list[dict[str, Any]]] = {}
    count = 0
    total_matches = 0
    query = symbol_query.casefold() if symbol_query else None

    for outline in sorted(outlines, key=lambda item: str(item.get("path") or "")):
        path = outline.get("path")
        language = outline.get("language")
        if not isinstance(path, str) or not isinstance(language, str):
            continue
        symbols = outline.get("symbols") or []
        for symbol in symbols:
            if not isinstance(symbol, dict):
                continue
            name = symbol.get("name")
            kind = symbol.get("kind")
            line = symbol.get("line")
            if not isinstance(name, str) or not isinstance(kind, str) or not isinstance(line, int):
                continue
            if query is not None and not _symbol_matches_query(symbol, query):
                continue
            total_matches += 1
            if total_matches <= offset or count >= max_symbols:
                continue
            ref: dict[str, Any] = {
                "path": path,
                "language": language,
                "kind": kind,
                "line": line,
            }
            end_line = symbol.get("end_line")
            if isinstance(end_line, int):
                ref["end_line"] = end_line
            container = symbol.get("container")
            if isinstance(container, str):
                ref["container"] = container
            decorators = symbol.get("decorators")
            if isinstance(decorators, list):
                ref["decorators"] = [
                    decorator
                    for decorator in decorators
                    if isinstance(decorator, str)
                ]
            bases = symbol.get("bases")
            if isinstance(bases, list):
                ref["bases"] = [
                    base
                    for base in bases
                    if isinstance(base, str)
                ]
            if symbol.get("exported") is True:
                ref["exported"] = True
            index.setdefault(name, []).append(ref)
            count += 1

    truncated = total_matches > offset + count
    return dict(sorted(index.items())), count, total_matches, truncated


def _symbol_matches_query(symbol: dict[str, Any], query: str) -> bool:
    fields = [
        symbol.get("name"),
        symbol.get("kind"),
        symbol.get("container"),
    ]
    for field in fields:
        if isinstance(field, str) and query in field.casefold():
            return True

    for field_name in ("decorators", "bases"):
        values = symbol.get(field_name)
        if isinstance(values, list) and any(
            isinstance(value, str) and query in value.casefold()
            for value in values
        ):
            return True
    return False


def _build_import_index(
    outlines: list[dict[str, Any]],
    *,
    max_imports: int,
    offset: int = 0,
    import_query: str | None = None,
    path_lookup: dict[str, str] | None = None,
    project_root: Path | None = None,
    ts_js_resolution: _TsJsResolutionConfig | None = None,
    python_source_roots: tuple[str, ...] = _DEFAULT_PYTHON_SOURCE_ROOTS,
) -> tuple[dict[str, list[dict[str, Any]]], int, int, bool]:
    index: dict[str, list[dict[str, Any]]] = {}
    available_paths = path_lookup or {}
    count = 0
    total_matches = 0
    query = import_query.casefold() if import_query else None
    needs_resolved_query = (
        _dependency_query_needs_resolution(query)
        if query is not None
        else False
    )

    for outline in sorted(outlines, key=lambda item: str(item.get("path") or "")):
        path = outline.get("path")
        language = outline.get("language")
        if not isinstance(path, str) or not isinstance(language, str):
            continue
        for item, source in _dependency_index_entries(outline):
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            line = item.get("line")
            if not isinstance(kind, str) or not isinstance(line, int):
                continue
            for module in _import_index_modules(item):
                ref: dict[str, Any] | None = None
                if query is not None and not _import_item_matches_query(
                    module, item, query
                ):
                    if not needs_resolved_query:
                        continue
                    ref = _build_import_ref(
                        item,
                        module=module,
                        path=path,
                        language=language,
                        kind=kind,
                        line=line,
                        source=source,
                        path_lookup=available_paths,
                        project_root=project_root,
                        ts_js_resolution=ts_js_resolution,
                        python_source_roots=python_source_roots,
                    )
                    if not _import_ref_matches_query(module, ref, query):
                        continue
                total_matches += 1
                if total_matches <= offset or count >= max_imports:
                    continue
                if ref is None:
                    ref = _build_import_ref(
                        item,
                        module=module,
                        path=path,
                        language=language,
                        kind=kind,
                        line=line,
                        source=source,
                        path_lookup=available_paths,
                        project_root=project_root,
                        ts_js_resolution=ts_js_resolution,
                        python_source_roots=python_source_roots,
                    )
                index.setdefault(module, []).append(ref)
                count += 1

    truncated = total_matches > offset + count
    return dict(sorted(index.items())), count, total_matches, truncated


def _build_import_ref(
    item: dict[str, Any],
    *,
    module: str,
    path: str,
    language: str,
    kind: str,
    line: int,
    source: str,
    path_lookup: dict[str, str],
    project_root: Path | None = None,
    ts_js_resolution: _TsJsResolutionConfig | None = None,
    python_source_roots: tuple[str, ...] = _DEFAULT_PYTHON_SOURCE_ROOTS,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "path": path,
        "language": language,
        "kind": kind,
        "line": line,
        "source": source,
    }
    end_line = item.get("end_line")
    if isinstance(end_line, int):
        ref["end_line"] = end_line
    resolved = _resolve_dependency_path(
        source_path=path,
        source_language=language,
        module=module,
        path_lookup=path_lookup,
        project_root=project_root,
        ts_js_resolution=ts_js_resolution,
        python_source_roots=python_source_roots,
    )
    if resolved is not None:
        ref["resolved_path"] = resolved[0]
        ref["resolved_language"] = resolved[1]
    names = item.get("names")
    if isinstance(names, list):
        ref["names"] = [name for name in names if isinstance(name, str)]
    aliases = item.get("aliases")
    if isinstance(aliases, list):
        ref["aliases"] = [
            alias
            for alias in aliases
            if isinstance(alias, dict)
        ]
    resolved_names = _resolved_python_import_names(
        item,
        source_path=path,
        source_language=language,
        path_lookup=path_lookup,
        project_root=project_root,
        ts_js_resolution=ts_js_resolution,
        python_source_roots=python_source_roots,
    )
    if resolved_names:
        ref["resolved_names"] = resolved_names
    return ref


def _dependency_index_entries(
    outline: dict[str, Any],
) -> list[tuple[dict[str, Any], str]]:
    entries: list[tuple[dict[str, Any], str]] = [
        (item, "import")
        for item in outline.get("imports") or []
        if isinstance(item, dict)
    ]
    entries.extend(
        (item, "export")
        for item in outline.get("exports") or []
        if isinstance(item, dict) and isinstance(item.get("module"), str)
    )
    return entries


def _import_item_matches_query(
    module: str,
    item: dict[str, Any],
    query: str,
) -> bool:
    if query in module.casefold():
        return True

    names = item.get("names")
    if isinstance(names, list) and any(
        isinstance(name, str) and query in name.casefold()
        for name in names
    ):
        return True

    aliases = item.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if not isinstance(alias, dict):
                continue
            for key in ("name", "asname"):
                value = alias.get(key)
                if isinstance(value, str) and query in value.casefold():
                    return True

    return any(
        query in candidate_module.casefold()
        for candidate_module in _python_import_name_modules_for_query(item)
    )


def _python_import_name_modules_for_query(item: dict[str, Any]) -> list[str]:
    module = item.get("module")
    names = item.get("names")
    if item.get("kind") != "from" or not isinstance(module, str) or not module:
        return []
    if not isinstance(names, list):
        return []
    candidates: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name or name == "*" or "." in name:
            continue
        candidate = _python_import_name_module(module, name)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _dependency_query_needs_resolution(query: str) -> bool:
    return (
        "/" in query
        or "\\" in query
        or any(query.endswith(suffix) for suffix in SUPPORTED_CODE_SUFFIXES)
    )


def _import_ref_matches_query(module: str, ref: dict[str, Any], query: str) -> bool:
    for field in (module, ref.get("resolved_path")):
        if isinstance(field, str) and query in field.casefold():
            return True

    names = ref.get("names")
    if isinstance(names, list) and any(
        isinstance(name, str) and query in name.casefold()
        for name in names
    ):
        return True

    aliases = ref.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if not isinstance(alias, dict):
                continue
            for key in ("name", "asname"):
                value = alias.get(key)
                if isinstance(value, str) and query in value.casefold():
                    return True

    resolved_names = ref.get("resolved_names")
    if isinstance(resolved_names, list):
        for resolved_name in resolved_names:
            if not isinstance(resolved_name, dict):
                continue
            for key in (
                "name",
                "asname",
                "module",
                "resolved_path",
                "resolved_language",
            ):
                value = resolved_name.get(key)
                if isinstance(value, str) and query in value.casefold():
                    return True
    return False


def _resolved_python_import_names(
    item: dict[str, Any],
    *,
    source_path: str,
    source_language: str,
    path_lookup: dict[str, str],
    project_root: Path | None = None,
    ts_js_resolution: _TsJsResolutionConfig | None = None,
    python_source_roots: tuple[str, ...] = _DEFAULT_PYTHON_SOURCE_ROOTS,
) -> list[dict[str, Any]]:
    if source_language != "python" or item.get("kind") != "from":
        return []

    module = item.get("module")
    names = item.get("names")
    if not isinstance(module, str) or not module or not isinstance(names, list):
        return []

    aliases_by_name = _import_aliases_by_name(item)
    resolved_names: list[dict[str, Any]] = []
    for name in names:
        if not isinstance(name, str) or not name or name == "*" or "." in name:
            continue
        candidate_module = _python_import_name_module(module, name)
        if candidate_module is None:
            continue
        resolved = _resolve_dependency_path(
            source_path=source_path,
            source_language=source_language,
            module=candidate_module,
            path_lookup=path_lookup,
            project_root=project_root,
            ts_js_resolution=ts_js_resolution,
            python_source_roots=python_source_roots,
        )
        if resolved is None:
            continue
        resolved_ref = {
            "name": name,
            "module": candidate_module,
            "resolved_path": resolved[0],
            "resolved_language": resolved[1],
        }
        asname = aliases_by_name.get(name)
        if asname:
            resolved_ref["asname"] = asname
        resolved_names.append(resolved_ref)
    return resolved_names


def _python_import_name_module(module: str, name: str) -> str | None:
    if set(module) == {"."}:
        return f"{module}{name}"
    return f"{module}.{name}"


def _import_aliases_by_name(item: dict[str, Any]) -> dict[str, str]:
    aliases = item.get("aliases")
    if not isinstance(aliases, list):
        return {}

    by_name: dict[str, str] = {}
    for alias in aliases:
        if not isinstance(alias, dict):
            continue
        name = alias.get("name")
        asname = alias.get("asname")
        if isinstance(name, str) and isinstance(asname, str):
            by_name[name] = asname
    return by_name


def _build_export_index(
    outlines: list[dict[str, Any]],
    *,
    max_exports: int,
    offset: int = 0,
    export_query: str | None = None,
    path_lookup: dict[str, str] | None = None,
    project_root: Path | None = None,
    ts_js_resolution: _TsJsResolutionConfig | None = None,
    python_source_roots: tuple[str, ...] = _DEFAULT_PYTHON_SOURCE_ROOTS,
) -> tuple[dict[str, list[dict[str, Any]]], int, int, bool]:
    index: dict[str, list[dict[str, Any]]] = {}
    available_paths = path_lookup or {}
    count = 0
    total_matches = 0
    query = export_query.casefold() if export_query else None
    needs_resolved_query = (
        _dependency_query_needs_resolution(query)
        if query is not None
        else False
    )

    for outline in sorted(outlines, key=lambda item: str(item.get("path") or "")):
        path = outline.get("path")
        language = outline.get("language")
        if not isinstance(path, str) or not isinstance(language, str):
            continue
        exports = outline.get("exports") or []
        for item in exports:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            line = item.get("line")
            if not isinstance(kind, str) or not isinstance(line, int):
                continue
            for name in _export_index_names(item):
                ref: dict[str, Any] | None = None
                if query is not None and not _export_item_matches_query(
                    name, item, query
                ):
                    if not needs_resolved_query:
                        continue
                    ref = _build_export_ref(
                        item,
                        name=name,
                        path=path,
                        language=language,
                        kind=kind,
                        line=line,
                        path_lookup=available_paths,
                        project_root=project_root,
                        ts_js_resolution=ts_js_resolution,
                        python_source_roots=python_source_roots,
                    )
                    if not _export_ref_matches_query(name, ref, query):
                        continue
                total_matches += 1
                if total_matches <= offset or count >= max_exports:
                    continue
                if ref is None:
                    ref = _build_export_ref(
                        item,
                        name=name,
                        path=path,
                        language=language,
                        kind=kind,
                        line=line,
                        path_lookup=available_paths,
                        project_root=project_root,
                        ts_js_resolution=ts_js_resolution,
                        python_source_roots=python_source_roots,
                    )
                index.setdefault(name, []).append(ref)
                count += 1

    truncated = total_matches > offset + count
    return dict(sorted(index.items())), count, total_matches, truncated


def _build_export_ref(
    item: dict[str, Any],
    *,
    name: str,
    path: str,
    language: str,
    kind: str,
    line: int,
    path_lookup: dict[str, str],
    project_root: Path | None = None,
    ts_js_resolution: _TsJsResolutionConfig | None = None,
    python_source_roots: tuple[str, ...] = _DEFAULT_PYTHON_SOURCE_ROOTS,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "path": path,
        "language": language,
        "kind": kind,
        "line": line,
    }
    end_line = item.get("end_line")
    if isinstance(end_line, int):
        ref["end_line"] = end_line
    module = item.get("module")
    if isinstance(module, str):
        ref["module"] = module
        resolved = _resolve_dependency_path(
            source_path=path,
            source_language=language,
            module=module,
            path_lookup=path_lookup,
            project_root=project_root,
            ts_js_resolution=ts_js_resolution,
            python_source_roots=python_source_roots,
        )
        if resolved is not None:
            ref["resolved_path"] = resolved[0]
            ref["resolved_language"] = resolved[1]
    declaration_kind = item.get("declaration_kind")
    if isinstance(declaration_kind, str):
        ref["declaration_kind"] = declaration_kind
    aliases = _export_aliases_for_name(item, name)
    if aliases:
        ref["aliases"] = aliases
    return ref


def _export_aliases_for_name(item: dict[str, Any], name: str) -> list[dict[str, Any]]:
    aliases = item.get("aliases")
    if not isinstance(aliases, list):
        return []
    return [
        alias
        for alias in aliases
        if isinstance(alias, dict) and alias.get("asname") == name
    ]


def _export_item_matches_query(name: str, item: dict[str, Any], query: str) -> bool:
    for field in (
        name,
        item.get("module"),
        item.get("declaration_kind"),
        item.get("kind"),
    ):
        if isinstance(field, str) and query in field.casefold():
            return True

    aliases = item.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if not isinstance(alias, dict):
                continue
            for key in ("name", "asname"):
                value = alias.get(key)
                if isinstance(value, str) and query in value.casefold():
                    return True
    return False


def _export_ref_matches_query(name: str, ref: dict[str, Any], query: str) -> bool:
    for field in (
        name,
        ref.get("module"),
        ref.get("resolved_path"),
        ref.get("declaration_kind"),
    ):
        if isinstance(field, str) and query in field.casefold():
            return True

    aliases = ref.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if not isinstance(alias, dict):
                continue
            for key in ("name", "asname"):
                value = alias.get(key)
                if isinstance(value, str) and query in value.casefold():
                    return True
    return False


def _resolve_dependency_path(
    *,
    source_path: str,
    source_language: str,
    module: str,
    path_lookup: dict[str, str],
    project_root: Path | None = None,
    ts_js_resolution: _TsJsResolutionConfig | None = None,
    python_source_roots: tuple[str, ...] = _DEFAULT_PYTHON_SOURCE_ROOTS,
) -> tuple[str, str] | None:
    stems = _dependency_module_stems(
        source_path=source_path,
        source_language=source_language,
        module=module,
        ts_js_resolution=ts_js_resolution,
        python_source_roots=python_source_roots,
    )
    if not stems:
        return None

    for stem in stems:
        for candidate in _module_resolution_candidates(stem, source_language):
            language = path_lookup.get(candidate)
            if language is not None:
                return candidate, language
            if project_root is not None and _project_file_exists(project_root, candidate):
                return candidate, detect_code_language(candidate)
    return None


def _dependency_module_stems(
    *,
    source_path: str,
    source_language: str,
    module: str,
    ts_js_resolution: _TsJsResolutionConfig | None = None,
    python_source_roots: tuple[str, ...] = _DEFAULT_PYTHON_SOURCE_ROOTS,
) -> list[str]:
    stems: list[str] = []
    if source_language in {"typescript", "javascript"} and ts_js_resolution is not None:
        stems.extend(_ts_js_config_module_stems(module, ts_js_resolution))
    stem = _dependency_module_stem(source_path=source_path, module=module)
    if stem is not None:
        stems.append(stem)
        if source_language == "python":
            stems.extend(
                f"{source_root}/{stem}"
                for source_root in python_source_roots
                if source_root and not stem.startswith(f"{source_root}/")
            )
    return _unique_paths(stems)


def _load_python_source_roots(project_root: Path) -> tuple[str, ...]:
    cache_key = (
        str(project_root.resolve()),
        _config_file_fingerprint(project_root / _PYPROJECT_FILENAME),
        _config_file_fingerprint(project_root / _SETUP_CFG_FILENAME),
    )
    cached = _PYTHON_SOURCE_ROOT_CACHE.get(cache_key)
    if cached is not None:
        _PYTHON_SOURCE_ROOT_CACHE.move_to_end(cache_key)
        return cached

    roots: list[str] = []
    pyproject_path = project_root / _PYPROJECT_FILENAME
    if pyproject_path.is_file():
        try:
            text, _encoding = read_text_with_detection(pyproject_path)
            payload = tomllib.loads(text)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            logger.debug("project_outline_pyproject_unreadable", error=str(exc))
        else:
            if isinstance(payload, dict):
                roots.extend(_python_source_roots_from_pyproject(payload))
    roots.extend(_load_python_source_roots_from_setup_cfg(project_root))
    roots.extend(_DEFAULT_PYTHON_SOURCE_ROOTS)
    result = tuple(_unique_paths([root for root in roots if root]))
    _PYTHON_SOURCE_ROOT_CACHE[cache_key] = result
    _PYTHON_SOURCE_ROOT_CACHE.move_to_end(cache_key)
    _trim_lru(_PYTHON_SOURCE_ROOT_CACHE, _CONFIG_CACHE_MAX_ENTRIES)
    return result


def _python_source_roots_from_pyproject(payload: dict[str, Any]) -> list[str]:
    tool = payload.get("tool")
    if not isinstance(tool, dict):
        return []

    roots: list[str] = []
    setuptools = tool.get("setuptools")
    if isinstance(setuptools, dict):
        package_dir = setuptools.get("package-dir")
        if isinstance(package_dir, dict):
            _append_python_source_root(roots, package_dir.get(""))

    poetry = tool.get("poetry")
    if isinstance(poetry, dict):
        packages = poetry.get("packages")
        if isinstance(packages, list):
            for item in packages:
                if isinstance(item, dict):
                    _append_python_source_root(roots, item.get("from"))

    pdm = tool.get("pdm")
    if isinstance(pdm, dict):
        build = pdm.get("build")
        if isinstance(build, dict):
            _append_python_source_root(roots, build.get("package-dir"))

    hatch = tool.get("hatch")
    if isinstance(hatch, dict):
        roots.extend(_python_source_roots_from_hatch(hatch))

    return roots


def _python_source_roots_from_hatch(hatch: dict[str, Any]) -> list[str]:
    build = hatch.get("build")
    if not isinstance(build, dict):
        return []
    targets = build.get("targets")
    if not isinstance(targets, dict):
        return []
    wheel = targets.get("wheel")
    if not isinstance(wheel, dict):
        return []
    packages = wheel.get("packages")
    if not isinstance(packages, list):
        return []

    roots: list[str] = []
    for item in packages:
        if not isinstance(item, str):
            continue
        cleaned = _clean_config_project_path(item)
        if cleaned is None or "/" not in cleaned:
            continue
        _append_python_source_root(roots, posixpath.dirname(cleaned))
    return roots


def _append_python_source_root(roots: list[str], raw: Any) -> None:
    cleaned = _clean_config_project_path(raw)
    if cleaned:
        _append_unique(roots, cleaned)


def _load_python_source_roots_from_setup_cfg(project_root: Path) -> list[str]:
    setup_path = project_root / _SETUP_CFG_FILENAME
    if not setup_path.is_file():
        return []

    parser = configparser.ConfigParser(interpolation=None)
    try:
        text, _encoding = read_text_with_detection(setup_path)
        parser.read_string(text)
    except (OSError, UnicodeError, configparser.Error) as exc:
        logger.debug("project_outline_setup_cfg_unreadable", error=str(exc))
        return []

    roots: list[str] = []
    if parser.has_option("options", "package_dir"):
        _append_setup_cfg_package_dir_roots(roots, parser.get("options", "package_dir"))
    if parser.has_option("options.packages.find", "where"):
        for value in _split_setup_cfg_values(
            parser.get("options.packages.find", "where")
        ):
            _append_python_source_root(roots, value)
    return roots


def _append_setup_cfg_package_dir_roots(roots: list[str], raw: str) -> None:
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        if "=" in cleaned:
            package, root = [part.strip() for part in cleaned.split("=", 1)]
            if package:
                continue
            _append_python_source_root(roots, root)
            continue
        _append_python_source_root(roots, cleaned)


def _split_setup_cfg_values(raw: str) -> list[str]:
    values: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        cleaned = line.strip()
        if cleaned:
            values.append(cleaned)
    return values


def _load_ts_js_resolution_config(project_root: Path) -> _TsJsResolutionConfig | None:
    package_paths = _load_package_json_resolution_mappings(project_root)
    for filename in _TS_JS_CONFIG_FILENAMES:
        config_path = project_root / filename
        if not config_path.is_file():
            continue
        payload = _load_ts_js_config_payload(project_root, config_path)
        if not isinstance(payload, dict):
            return _config_from_paths(None, package_paths)
        compiler_options = payload.get("compilerOptions")
        if not isinstance(compiler_options, dict):
            return _config_from_paths(None, package_paths)
        base_url = _clean_config_project_path(compiler_options.get("baseUrl"))
        paths = [
            *_parse_ts_js_path_mappings(compiler_options.get("paths")),
            *package_paths,
        ]
        if base_url is None and not paths:
            return None
        return _TsJsResolutionConfig(base_url=base_url, paths=tuple(paths))
    return _config_from_paths(None, package_paths)


def _config_from_paths(
    base_url: str | None,
    paths: list[_TsJsPathMapping],
) -> _TsJsResolutionConfig | None:
    if base_url is None and not paths:
        return None
    return _TsJsResolutionConfig(base_url=base_url, paths=tuple(paths))


def _load_package_json_resolution_mappings(project_root: Path) -> list[_TsJsPathMapping]:
    package_path = project_root / _PACKAGE_JSON_FILENAME
    cache_key = _config_file_fingerprint(package_path)
    if cache_key in _PACKAGE_JSON_MAPPING_CACHE:
        _PACKAGE_JSON_MAPPING_CACHE.move_to_end(cache_key)
        return list(_PACKAGE_JSON_MAPPING_CACHE[cache_key])

    if not package_path.is_file():
        _PACKAGE_JSON_MAPPING_CACHE[cache_key] = ()
        _PACKAGE_JSON_MAPPING_CACHE.move_to_end(cache_key)
        _trim_lru(_PACKAGE_JSON_MAPPING_CACHE, _CONFIG_CACHE_MAX_ENTRIES)
        return []
    try:
        text, _encoding = read_text_with_detection(package_path)
        payload = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.debug("project_outline_package_json_unreadable", error=str(exc))
        _PACKAGE_JSON_MAPPING_CACHE[cache_key] = ()
        _PACKAGE_JSON_MAPPING_CACHE.move_to_end(cache_key)
        _trim_lru(_PACKAGE_JSON_MAPPING_CACHE, _CONFIG_CACHE_MAX_ENTRIES)
        return []
    if not isinstance(payload, dict):
        _PACKAGE_JSON_MAPPING_CACHE[cache_key] = ()
        _PACKAGE_JSON_MAPPING_CACHE.move_to_end(cache_key)
        _trim_lru(_PACKAGE_JSON_MAPPING_CACHE, _CONFIG_CACHE_MAX_ENTRIES)
        return []
    normalized: dict[str, list[str]] = {}
    normalized.update(_package_json_imports_paths(payload))
    normalized.update(_package_json_self_export_paths(payload))
    result = tuple(_parse_ts_js_path_mappings(normalized))
    _PACKAGE_JSON_MAPPING_CACHE[cache_key] = result
    _PACKAGE_JSON_MAPPING_CACHE.move_to_end(cache_key)
    _trim_lru(_PACKAGE_JSON_MAPPING_CACHE, _CONFIG_CACHE_MAX_ENTRIES)
    return list(result)


def _package_json_imports_paths(payload: dict[str, Any]) -> dict[str, list[str]]:
    imports = payload.get("imports")
    if not isinstance(imports, dict):
        return {}

    normalized: dict[str, list[str]] = {}
    for source, raw_target in imports.items():
        if not isinstance(source, str) or not source.startswith("#"):
            continue
        targets: list[str] = []
        for target in _package_json_import_targets(raw_target):
            if not target.strip().replace("\\", "/").startswith("./"):
                continue
            resolved = _resolve_config_path_value("", target, allow_wildcard=True)
            if resolved is not None:
                targets.append(resolved)
        if targets:
            normalized[source] = targets
    return normalized


def _package_json_self_export_paths(payload: dict[str, Any]) -> dict[str, list[str]]:
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        return {}
    package_name = name.strip()
    exports = payload.get("exports")
    if exports is None:
        return {}

    normalized: dict[str, list[str]] = {}
    for source, raw_target in _package_json_export_entries(exports):
        source_pattern = _package_export_source_pattern(package_name, source)
        if source_pattern is None:
            continue
        targets: list[str] = []
        for target in _package_json_import_targets(raw_target):
            if not target.strip().replace("\\", "/").startswith("./"):
                continue
            resolved = _resolve_config_path_value("", target, allow_wildcard=True)
            if resolved is not None:
                targets.append(resolved)
        if targets:
            normalized[source_pattern] = targets
    return normalized


def _package_json_export_entries(exports: Any) -> list[tuple[str, Any]]:
    if not isinstance(exports, dict):
        return [(".", exports)]
    if any(isinstance(key, str) and key.startswith(".") for key in exports):
        return [
            (key, value)
            for key, value in exports.items()
            if isinstance(key, str) and key.startswith(".")
        ]
    return [(".", exports)]


def _package_export_source_pattern(package_name: str, source: str) -> str | None:
    if source == ".":
        return package_name
    if source.startswith("./"):
        suffix = source[2:]
        return f"{package_name}/{suffix}" if suffix else package_name
    return None


def _package_json_import_targets(raw_target: Any) -> list[str]:
    if isinstance(raw_target, str):
        return [raw_target]
    if isinstance(raw_target, list):
        out: list[str] = []
        for item in raw_target:
            out.extend(_package_json_import_targets(item))
        return out
    if isinstance(raw_target, dict):
        out = []
        for value in raw_target.values():
            out.extend(_package_json_import_targets(value))
        return out
    return []


def _load_ts_js_config_payload(
    project_root: Path,
    config_path: Path,
    *,
    seen: frozenset[Path] = frozenset(),
    depth: int = 0,
) -> dict[str, Any] | None:
    try:
        resolved_path = config_path.resolve()
    except OSError:
        resolved_path = config_path
    if resolved_path in seen or depth > _TS_JS_CONFIG_EXTENDS_MAX_DEPTH:
        return None
    payload = _load_ts_js_config_payload_file(project_root, config_path)
    if payload is None:
        return None

    parent = _load_extended_ts_js_config_payload(
        project_root,
        config_path,
        payload.get("extends"),
        seen=seen | {resolved_path},
        depth=depth + 1,
    )
    if parent is None:
        return payload
    return _merge_ts_js_config_payloads(parent, payload)


def _load_ts_js_config_payload_file(
    project_root: Path,
    config_path: Path,
) -> dict[str, Any] | None:
    cache_key = _config_file_fingerprint(config_path)
    if cache_key in _TS_JS_CONFIG_PAYLOAD_CACHE:
        _TS_JS_CONFIG_PAYLOAD_CACHE.move_to_end(cache_key)
        cached = _TS_JS_CONFIG_PAYLOAD_CACHE[cache_key]
        return copy.deepcopy(cached)

    try:
        text, _encoding = read_text_with_detection(config_path)
        payload = json.loads(_strip_jsonc_for_config(text))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.debug(
            "project_outline_ts_js_config_unreadable",
            path=_display_config_path(project_root, config_path),
            error=str(exc),
        )
        _TS_JS_CONFIG_PAYLOAD_CACHE[cache_key] = None
        _TS_JS_CONFIG_PAYLOAD_CACHE.move_to_end(cache_key)
        _trim_lru(_TS_JS_CONFIG_PAYLOAD_CACHE, _CONFIG_CACHE_MAX_ENTRIES)
        return None
    if not isinstance(payload, dict):
        _TS_JS_CONFIG_PAYLOAD_CACHE[cache_key] = None
        _TS_JS_CONFIG_PAYLOAD_CACHE.move_to_end(cache_key)
        _trim_lru(_TS_JS_CONFIG_PAYLOAD_CACHE, _CONFIG_CACHE_MAX_ENTRIES)
        return None

    normalized = _normalize_ts_js_config_payload_paths(
        project_root, config_path, payload
    )
    _TS_JS_CONFIG_PAYLOAD_CACHE[cache_key] = copy.deepcopy(normalized)
    _TS_JS_CONFIG_PAYLOAD_CACHE.move_to_end(cache_key)
    _trim_lru(_TS_JS_CONFIG_PAYLOAD_CACHE, _CONFIG_CACHE_MAX_ENTRIES)
    return normalized


def _load_extended_ts_js_config_payload(
    project_root: Path,
    config_path: Path,
    raw_extends: Any,
    *,
    seen: frozenset[Path],
    depth: int,
) -> dict[str, Any] | None:
    if not isinstance(raw_extends, str):
        return None
    parent_path = _resolve_local_ts_js_config_extends(project_root, config_path, raw_extends)
    if parent_path is None or not parent_path.is_file():
        return None
    return _load_ts_js_config_payload(
        project_root,
        parent_path,
        seen=seen,
        depth=depth,
    )


def _resolve_local_ts_js_config_extends(
    project_root: Path,
    config_path: Path,
    raw_extends: str,
) -> Path | None:
    value = raw_extends.strip().replace("\\", "/")
    if (
        not value
        or not value.startswith(("./", "../"))
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
    ):
        return None
    try:
        candidate = (config_path.parent / value).resolve()
        candidate.relative_to(project_root)
    except (OSError, ValueError):
        return None
    candidates = [candidate]
    if candidate.suffix.lower() != ".json":
        candidates.append(candidate.with_suffix(candidate.suffix + ".json"))
        candidates.append(candidate / "tsconfig.json")
    for item in candidates:
        if item.is_file():
            return item
    return candidates[0] if candidates else None


def _merge_ts_js_config_payloads(
    parent: dict[str, Any],
    child: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(parent)
    merged.update({key: value for key, value in child.items() if key != "compilerOptions"})
    parent_options = parent.get("compilerOptions")
    child_options = child.get("compilerOptions")
    if isinstance(parent_options, dict) or isinstance(child_options, dict):
        merged_options: dict[str, Any] = {}
        if isinstance(parent_options, dict):
            merged_options.update(parent_options)
        if isinstance(child_options, dict):
            merged_options.update(child_options)
        merged["compilerOptions"] = merged_options
    return merged


def _normalize_ts_js_config_payload_paths(
    project_root: Path,
    config_path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    compiler_options = payload.get("compilerOptions")
    if not isinstance(compiler_options, dict):
        return payload

    config_dir = _config_dir_project_path(project_root, config_path)
    normalized_options = dict(compiler_options)
    base_url = _resolve_config_path_value(
        config_dir,
        compiler_options.get("baseUrl"),
        allow_wildcard=False,
    )
    if base_url is not None:
        normalized_options["baseUrl"] = base_url
    normalized_paths = _normalize_ts_js_paths_targets(
        config_dir,
        base_url,
        compiler_options.get("paths"),
    )
    if normalized_paths is not None:
        normalized_options["paths"] = normalized_paths

    normalized_payload = dict(payload)
    normalized_payload["compilerOptions"] = normalized_options
    return normalized_payload


def _normalize_ts_js_paths_targets(
    config_dir: str,
    base_url: str | None,
    raw_paths: Any,
) -> dict[str, list[str]] | None:
    if not isinstance(raw_paths, dict):
        return None

    origin = base_url if base_url is not None else config_dir
    normalized: dict[str, list[str]] = {}
    for raw_source, raw_targets in raw_paths.items():
        if not isinstance(raw_source, str):
            continue
        targets = raw_targets if isinstance(raw_targets, list) else [raw_targets]
        out_targets = [
            resolved
            for raw_target in targets
            if (
                resolved := _resolve_config_path_value(
                    origin,
                    raw_target,
                    allow_wildcard=True,
                )
            )
            is not None
        ]
        if out_targets:
            normalized[raw_source] = out_targets
    return normalized


def _config_dir_project_path(project_root: Path, config_path: Path) -> str:
    try:
        rel = config_path.parent.relative_to(project_root).as_posix()
    except ValueError:
        return ""
    return "" if rel == "." else rel


def _resolve_config_path_value(
    origin: str,
    raw_value: Any,
    *,
    allow_wildcard: bool,
) -> str | None:
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip().replace("\\", "/")
    if (
        not value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or (not allow_wildcard and "*" in value)
    ):
        return None
    cleaned = posixpath.normpath(posixpath.join(origin, value)).replace("\\", "/")
    if cleaned in {"", "."}:
        return ""
    if cleaned == ".." or cleaned.startswith("../"):
        return None
    return cleaned


def _display_config_path(project_root: Path, config_path: Path) -> str:
    try:
        return config_path.relative_to(project_root).as_posix()
    except ValueError:
        return str(config_path)


def _strip_jsonc_for_config(text: str) -> str:
    return _strip_json_trailing_commas(_strip_json_comments(text.lstrip("\ufeff")))


def _strip_json_comments(text: str) -> str:
    out: list[str] = []
    state = "normal"
    quote = ""
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "line_comment":
            if char in {"\n", "\r"}:
                out.append(char)
                state = "normal"
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and nxt == "/":
                state = "normal"
                index += 2
            else:
                if char in {"\n", "\r"}:
                    out.append(char)
                index += 1
            continue
        if state == "string":
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                state = "normal"
            index += 1
            continue

        if char in {"'", '"'}:
            quote = char
            out.append(char)
            state = "string"
            index += 1
            continue
        if char == "/" and nxt == "/":
            state = "line_comment"
            index += 2
            continue
        if char == "/" and nxt == "*":
            state = "block_comment"
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_json_trailing_commas(text: str) -> str:
    out: list[str] = []
    state = "normal"
    quote = ""
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if state == "string":
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                state = "normal"
            index += 1
            continue

        if char in {"'", '"'}:
            quote = char
            out.append(char)
            state = "string"
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in {"}", "]"}:
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _clean_config_project_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return None
    cleaned = posixpath.normpath(raw).replace("\\", "/")
    if cleaned in {"", "."}:
        return ""
    if cleaned == ".." or cleaned.startswith("../"):
        return None
    return cleaned


def _parse_ts_js_path_mappings(raw_paths: Any) -> list[_TsJsPathMapping]:
    if not isinstance(raw_paths, dict):
        return []

    mappings: list[_TsJsPathMapping] = []
    for raw_source, raw_targets in raw_paths.items():
        if not isinstance(raw_source, str):
            continue
        source_pattern = _split_ts_js_path_pattern(raw_source)
        if source_pattern is None:
            continue
        targets = raw_targets if isinstance(raw_targets, list) else [raw_targets]
        for raw_target in targets:
            if not isinstance(raw_target, str):
                continue
            target_pattern = _split_ts_js_path_pattern(raw_target)
            if target_pattern is None:
                continue
            mappings.append(
                _TsJsPathMapping(
                    source_prefix=source_pattern[0],
                    source_suffix=source_pattern[1],
                    source_has_wildcard=source_pattern[2],
                    target_prefix=target_pattern[0],
                    target_suffix=target_pattern[1],
                )
            )
    return mappings


def _split_ts_js_path_pattern(value: str) -> tuple[str, str, bool] | None:
    value = value.strip().replace("\\", "/")
    if not value or value.startswith("/") or value.count("*") > 1:
        return None
    if "*" not in value:
        return value, "", False
    prefix, suffix = value.split("*", 1)
    return prefix, suffix, True


def _ts_js_config_module_stems(
    module: str,
    config: _TsJsResolutionConfig,
) -> list[str]:
    value = module.strip().replace("\\", "/")
    if (
        not value
        or value.startswith(("./", "../", "."))
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)
    ):
        return []

    stems: list[str] = []
    for mapping in config.paths:
        matched = _ts_js_path_mapping_match(value, mapping)
        if matched is None:
            continue
        target = f"{mapping.target_prefix}{matched}{mapping.target_suffix}"
        stem = _clean_project_path(target)
        if stem is not None:
            stems.append(stem)
    if config.base_url is not None:
        stem = _join_config_base_url(config.base_url, value)
        if stem is not None:
            stems.append(stem)
    return _unique_paths(stems)


def _ts_js_path_mapping_match(module: str, mapping: _TsJsPathMapping) -> str | None:
    if not mapping.source_has_wildcard:
        return "" if module == mapping.source_prefix else None
    if not module.startswith(mapping.source_prefix):
        return None
    if mapping.source_suffix and not module.endswith(mapping.source_suffix):
        return None
    start = len(mapping.source_prefix)
    end = len(module) - len(mapping.source_suffix) if mapping.source_suffix else len(module)
    if end < start:
        return None
    return module[start:end]


def _join_config_base_url(base_url: str | None, target: str) -> str | None:
    clean_target = _clean_config_project_path(target)
    if clean_target is None:
        return None
    if base_url:
        return _clean_project_path(posixpath.join(base_url, clean_target))
    return _clean_project_path(clean_target)


def _dependency_module_stem(*, source_path: str, module: str) -> str | None:
    value = module.strip().replace("\\", "/")
    if not value or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        return None
    if value.startswith(("./", "../")):
        return _clean_project_path(posixpath.join(_project_dirname(source_path), value))
    if value.startswith("."):
        return _python_relative_module_stem(source_path=source_path, module=value)
    if "/" in value:
        return _clean_project_path(value)
    if "." in value:
        return _clean_project_path(value.replace(".", "/"))
    return _clean_project_path(value)


def _python_relative_module_stem(*, source_path: str, module: str) -> str | None:
    leading_dots = len(module) - len(module.lstrip("."))
    tail = module[leading_dots:].replace(".", "/")
    source_parts = [part for part in _project_dirname(source_path).split("/") if part]
    levels_up = max(0, leading_dots - 1)
    if levels_up > len(source_parts):
        return None
    base_parts = source_parts[: len(source_parts) - levels_up] if levels_up else source_parts
    base = "/".join(base_parts)
    if base and tail:
        return _clean_project_path(f"{base}/{tail}")
    return _clean_project_path(tail or base)


def _module_resolution_candidates(stem: str, source_language: str) -> list[str]:
    candidates: list[str] = []
    suffixes = _resolve_suffixes(source_language)
    stems = [stem]
    matched_suffix = _matched_resolve_suffix(stem)
    if matched_suffix:
        stems.append(stem[: -len(matched_suffix)])

    for base in _unique_paths(stems):
        has_suffix = _matched_resolve_suffix(base) is not None
        _append_unique(candidates, base)
        if has_suffix:
            continue
        for suffix in suffixes:
            _append_unique(candidates, f"{base}{suffix}")
        if source_language == "python":
            _append_unique(candidates, f"{base}/__init__.py")
        for suffix in suffixes:
            _append_unique(candidates, f"{base}/index{suffix}")
    return candidates


def _project_file_exists(project_root: Path, rel_path: str) -> bool:
    if _matched_resolve_suffix(rel_path) is None:
        return False
    try:
        return (project_root / rel_path).is_file()
    except OSError:
        return False


def _resolve_suffixes(source_language: str) -> tuple[str, ...]:
    if source_language == "python":
        preferred = _PYTHON_RESOLVE_SUFFIXES
    elif source_language == "typescript":
        preferred = _TYPESCRIPT_RESOLVE_SUFFIXES
    elif source_language == "javascript":
        preferred = _JAVASCRIPT_RESOLVE_SUFFIXES
    else:
        preferred = ()
    return tuple(_unique_paths([*preferred, *_CODE_RESOLVE_SUFFIXES]))


def _matched_resolve_suffix(path: str) -> str | None:
    for suffix in sorted(_CODE_RESOLVE_SUFFIXES, key=len, reverse=True):
        if path.endswith(suffix):
            return suffix
    return None


def _clean_project_path(path: str) -> str | None:
    if path.startswith("/"):
        return None
    cleaned = posixpath.normpath(path).replace("\\", "/")
    if cleaned in {"", ".", ".."} or cleaned.startswith("../"):
        return None
    return cleaned


def _project_dirname(path: str) -> str:
    return posixpath.dirname(path.replace("\\", "/"))


def _unique_paths(values: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for value in values:
        _append_unique(out, value)
    return out


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _append_unique_limited(values: list[str], value: str, limit: int) -> bool:
    if value and value not in values and len(values) < limit:
        values.append(value)
    return len(values) < limit


def _import_index_modules(item: dict[str, Any]) -> list[str]:
    module = item.get("module")
    names = item.get("names")
    if (
        item.get("kind") == "from"
        and isinstance(module, str)
        and module
        and set(module) == {"."}
        and isinstance(names, list)
    ):
        return [
            f"{module}{name}"
            for name in names
            if isinstance(name, str) and name
        ]
    if isinstance(module, str) and module:
        return [module]

    if not isinstance(names, list):
        return []
    return [name for name in names if isinstance(name, str) and name]


def _export_index_names(item: dict[str, Any]) -> list[str]:
    names = item.get("names")
    if not isinstance(names, list):
        return []
    return [name for name in names if isinstance(name, str) and name]
