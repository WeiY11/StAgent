"""Smoke tests for the ``project_*`` coding-agent toolbox.

Each tool is exercised against a fresh temp project so we verify the
end-to-end path: ``project_path`` → sandbox allow-listing →
``ProjectFS`` resolution → tool execution.

The tests stay deliberately narrow: they confirm the tools work on
happy-path inputs and that the sandbox refuses obvious escapes
(``../`` traversal, paths outside the configured root). Heavy
behavioural tests live closer to ``_fs.py`` and the diff applicator
internals.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from leagent.tools.base import ToolContext, ToolResult

if TYPE_CHECKING:
    from pathlib import Path


def _ctx(project: Path) -> ToolContext:
    """Build a tool context that authorises the temp project root."""
    return ToolContext(
        user_id="u1",
        session_id="s1",
        extra={"project_roots": [str(project)]},
    )


async def _run(tool: Any, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return await tool.run(params, ctx)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Scaffold a tiny project with a Python module + tests folder."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def greet(name: str) -> str:\n    return f\"hello {name}\"\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "util.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from src.app import greet\n\n"
        "def test_greet():\n    assert greet(\"world\") == \"hello world\"\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "# Demo project\n", encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_project_read_returns_line_numbered_content(project: Path) -> None:
    from leagent.project.tools.read import ProjectReadTool

    tool = ProjectReadTool()
    result = await _run(tool, {"path": "src/app.py"}, _ctx(project))
    assert result.success, result.error
    data = result.data
    assert data["path"] == "src/app.py"
    assert "1|def greet" in data["content"]
    assert data["total_lines"] == 2
    assert data["start_line"] == 1
    assert data["end_line"] == 2


@pytest.mark.asyncio
async def test_project_read_rejects_escape(project: Path) -> None:
    from leagent.project.tools.read import ProjectReadTool

    outside = project.parent / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    tool = ProjectReadTool()
    result = await _run(tool, {"path": "../outside.txt"}, _ctx(project))
    assert not result.success
    assert "outside" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_project_write_then_edit_then_read(project: Path) -> None:
    from leagent.project.tools.edit import ProjectEditTool
    from leagent.project.tools.read import ProjectReadTool
    from leagent.project.tools.write import ProjectWriteTool

    write = ProjectWriteTool()
    edit = ProjectEditTool()
    read = ProjectReadTool()

    res = await _run(write, {
        "path": "src/feature.py",
        "content": "VALUE = 1\n",
    }, _ctx(project))
    assert res.success, res.error
    assert (project / "src" / "feature.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert res.data["created"] is True

    res2 = await _run(write, {
        "path": "src/feature.py",
        "content": "VALUE = 2\n",
    }, _ctx(project))
    assert res2.success
    assert "already exists" in (res2.data.get("error") or "")

    res3 = await _run(edit, {
        "path": "src/feature.py",
        "old_string": "VALUE = 1",
        "new_string": "VALUE = 42",
    }, _ctx(project))
    assert res3.success, res3.error
    assert res3.data["replacements"] == 1
    assert (project / "src" / "feature.py").read_text(encoding="utf-8").startswith("VALUE = 42")

    res4 = await _run(read, {"path": "src/feature.py"}, _ctx(project))
    assert res4.success
    assert "VALUE = 42" in res4.data["content"]


@pytest.mark.asyncio
async def test_project_edit_requires_uniqueness(project: Path) -> None:
    """Multiple matches without ``replace_all`` must fail loudly."""
    from leagent.project.tools.edit import ProjectEditTool
    from leagent.project.tools.write import ProjectWriteTool

    await _run(
        ProjectWriteTool(),
        {"path": "dup.txt", "content": "x\nx\n"},
        _ctx(project),
    )
    res = await _run(
        ProjectEditTool(),
        {"path": "dup.txt", "old_string": "x", "new_string": "y"},
        _ctx(project),
    )
    assert res.success  # tool-level success: returns structured error
    assert "occurs 2 times" in (res.data.get("error") or "")

    res2 = await _run(
        ProjectEditTool(),
        {
            "path": "dup.txt",
            "old_string": "x",
            "new_string": "y",
            "replace_all": True,
        },
        _ctx(project),
    )
    assert res2.success and res2.data.get("replacements") == 2


@pytest.mark.asyncio
async def test_project_edit_no_match_includes_patch_hint(project: Path) -> None:
    from leagent.project.tools.edit import ProjectEditTool

    res = await _run(
        ProjectEditTool(),
        {
            "path": "src/app.py",
            "old_string": "def greet(wrong: str) -> str:",
            "new_string": "def greet(name: str) -> str:",
        },
        _ctx(project),
    )
    assert res.success
    assert res.data.get("error")
    hint = res.data.get("patch_hint") or {}
    assert hint.get("closest_matches")


@pytest.mark.asyncio
async def test_project_grep_finds_match(project: Path) -> None:
    from leagent.project.tools.grep import ProjectGrepTool

    tool = ProjectGrepTool()
    result = await _run(tool, {"pattern": "def greet"}, _ctx(project))
    assert result.success, result.error
    matches = result.data.get("matches") or []
    files = result.data.get("files_with_matches") or []
    # Either ripgrep or python backend is fine; both should locate the
    # symbol in src/app.py.
    if matches:
        assert any(m["path"] == "src/app.py" for m in matches)
    assert "src/app.py" in files


@pytest.mark.asyncio
async def test_project_glob_lists_python_files(project: Path) -> None:
    from leagent.project.tools.glob import ProjectGlobTool

    result = await _run(
        ProjectGlobTool(),
        {"pattern": "**/*.py"},
        _ctx(project),
    )
    assert result.success, result.error
    files = [f["path"] for f in result.data["files"]]
    assert "src/app.py" in files
    assert "src/util.py" in files
    assert "tests/test_app.py" in files


@pytest.mark.asyncio
async def test_project_tree_renders_layout(project: Path) -> None:
    from leagent.project.tools.tree import ProjectTreeTool

    result = await _run(ProjectTreeTool(), {"max_depth": 3}, _ctx(project))
    assert result.success, result.error
    tree = result.data["tree"]
    assert "src/" in tree
    assert "tests/" in tree
    assert "README.md" in tree


@pytest.mark.asyncio
async def test_project_tree_missing_root_is_not_retried() -> None:
    from leagent.project.tools.tree import ProjectTreeTool

    result = await _run(
        ProjectTreeTool(),
        {"max_depth": 3},
        ToolContext(user_id="u1", session_id="s1"),
    )

    assert not result.success
    assert "No project root configured" in (result.error or "")
    assert result.metadata.get("attempts") == 1


@pytest.mark.asyncio
async def test_project_apply_patch_creates_and_modifies(project: Path) -> None:
    from leagent.project.tools.patch import ProjectApplyPatchTool

    diff = (
        "--- /dev/null\n"
        "+++ b/src/new_module.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+CONST = 7\n"
        "+other = 8\n"
    )
    result = await _run(
        ProjectApplyPatchTool(),
        {"diff": diff},
        _ctx(project),
    )
    assert result.success, result.error
    files = result.data["files"]
    assert files == [
        {"path": "src/new_module.py", "is_new": True, "is_deleted": False}
    ]
    assert (project / "src" / "new_module.py").read_text(encoding="utf-8").startswith(
        "CONST = 7"
    )


@pytest.mark.asyncio
async def test_project_shell_runs_python(project: Path) -> None:
    """Curated shell can execute ``python -c`` inside the project root."""
    from leagent.project.tools.shell import ProjectShellTool

    result = await _run(
        ProjectShellTool(),
        {
            "argv": ["python", "-c", "print('ok'); import os; print(os.getcwd())"],
            "timeout_sec": 30,
        },
        _ctx(project),
    )
    assert result.success, result.error
    data = result.data
    assert data["returncode"] == 0
    assert "ok" in data["stdout"]
    # cwd line must reside inside the project root.
    cwd_line = data["stdout"].strip().splitlines()[-1]
    assert str(project) in cwd_line or cwd_line == str(project)


@pytest.mark.asyncio
async def test_project_shell_blocks_unknown_binary(
    project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curated argv rejects unknown binaries when free-shell mode is off."""
    monkeypatch.setenv("LEAGENT_CODING_AGENT_FREE_SHELL", "0")
    from leagent.project.tools.shell import ProjectShellTool

    result = await _run(
        ProjectShellTool(),
        {"argv": ["definitely_not_real_42"]},
        _ctx(project),
    )
    assert not result.success
    assert result.data is not None
    assert "whitelist" in (result.error or "").lower() or "whitelist" in str(
        result.data.get("error") or ""
    ).lower()


@pytest.mark.asyncio
async def test_project_outline_single_file(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/app.py"},
        _ctx(project),
    )
    assert result.success, result.error
    files = result.data["files"]
    assert len(files) == 1
    f0 = files[0]
    assert f0["path"] == "src/app.py"
    assert f0["language"] == "python"
    names = {s["name"] for s in f0["symbols"]}
    assert "greet" in names


@pytest.mark.asyncio
async def test_project_outline_syntax_error(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "broken.py").write_text("def x(\n", encoding="utf-8")
    result = await _run(
        ProjectOutlineTool(),
        {"path": "broken.py"},
        _ctx(project),
    )
    assert result.success, result.error
    f0 = result.data["files"][0]
    assert f0.get("parse_error")


@pytest.mark.asyncio
async def test_project_outline_root_glob(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    result = await _run(
        ProjectOutlineTool(),
        {"glob": "**/*.py", "max_files": 10},
        _ctx(project),
    )
    assert result.success, result.error
    paths = {f["path"] for f in result.data["files"]}
    assert "src/app.py" in paths
    assert "tests/test_app.py" in paths


@pytest.mark.asyncio
async def test_project_outline_max_files_truncated_only_when_more_matches(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    exact_result = await _run(
        ProjectOutlineTool(),
        {"glob": "**/*.py", "max_files": 3},
        _ctx(project),
    )

    assert exact_result.success, exact_result.error
    assert exact_result.data["count"] == 3
    assert exact_result.data["truncated"] is False
    assert exact_result.data["summary"]["truncated"] is False

    truncated_result = await _run(
        ProjectOutlineTool(),
        {"glob": "**/*.py", "max_files": 2},
        _ctx(project),
    )

    assert truncated_result.success, truncated_result.error
    assert truncated_result.data["count"] == 2
    assert truncated_result.data["truncated"] is True
    assert truncated_result.data["summary"]["truncated"] is True


@pytest.mark.asyncio
async def test_project_outline_accepts_windows_style_glob(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    result = await _run(
        ProjectOutlineTool(),
        {"glob": "src\\*.py", "max_files": 10},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["glob"] == "src/*.py"
    paths = {f["path"] for f in result.data["files"]}
    assert paths == {"src/app.py", "src/util.py"}


@pytest.mark.asyncio
async def test_project_outline_accepts_windows_style_recursive_glob(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "nested").mkdir()
    (project / "src" / "nested" / "widget.ts").write_text(
        "export const widget = () => null;\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"glob": "src\\**\\*.ts", "max_files": 10},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["glob"] == "src/**/*.ts"
    paths = {f["path"] for f in result.data["files"]}
    assert paths == {"src/nested/widget.ts"}


@pytest.mark.asyncio
async def test_project_outline_default_includes_ts_js(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "widget.ts").write_text(
        "export interface Props { name: string }\n"
        "export const buildWidget = () => ({ ok: true });\n",
        encoding="utf-8",
    )
    (project / "src" / "view.jsx").write_text(
        "export default function View() { return null; }\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"max_files": 20},
        _ctx(project),
    )

    assert result.success, result.error
    by_path = {item["path"]: item for item in result.data["files"]}
    assert by_path["src/app.py"]["language"] == "python"
    assert by_path["src/widget.ts"]["language"] == "typescript"
    assert by_path["src/view.jsx"]["language"] == "javascript"
    ts_symbols = {item["name"] for item in by_path["src/widget.ts"]["symbols"]}
    assert {"Props", "buildWidget"} <= ts_symbols
    summary = result.data["summary"]
    assert summary["files"] == result.data["count"]
    assert summary["by_language"]["python"] >= 3
    assert summary["by_language"]["typescript"] == 1
    assert summary["by_language"]["javascript"] == 1
    assert summary["symbol_kinds"]["function"] >= 1
    assert summary["imports"] >= 1
    assert summary["line_count"] == sum(item["line_count"] for item in result.data["files"])
    assert summary["char_count"] == sum(item["char_count"] for item in result.data["files"])
    assert summary["size_bytes"] == sum(item["size_bytes"] for item in result.data["files"])
    assert summary["largest_files"]
    assert summary["largest_files"] == sorted(
        summary["largest_files"],
        key=lambda item: (item["size_bytes"], item["path"]),
        reverse=True,
    )
    assert result.data["symbol_index"]["greet"][0]["path"] == "src/app.py"
    assert result.data["symbol_index"]["Props"][0]["language"] == "typescript"
    assert result.data["summary"]["indexed_symbols"] == result.data["symbol_index_count"]
    assert result.data["symbol_index_truncated"] is False
    assert result.data["import_index"]["src.app"][0]["path"] == "tests/test_app.py"
    assert result.data["summary"]["indexed_imports"] == result.data["import_index_count"]
    assert result.data["import_index_truncated"] is False
    assert result.data["export_index"]["Props"][0]["path"] == "src/widget.ts"
    assert result.data["export_index"]["buildWidget"][0]["declaration_kind"] == "const"
    assert result.data["summary"]["indexed_exports"] == result.data["export_index_count"]
    assert result.data["export_index_truncated"] is False


@pytest.mark.asyncio
async def test_project_outline_skips_common_generated_dirs_by_default(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    generated_files = [
        project / "node_modules" / "pkg" / "index.ts",
        project / "dist" / "bundle.js",
        project / "build" / "generated.py",
        project / ".next" / "server" / "page.js",
        project / ".vite" / "deps" / "react.js",
        project / "coverage" / "lcov-report" / "index.js",
        project / "playwright-report" / "trace.js",
        project / "test-results" / "run.js",
        project / "htmlcov" / "index.py",
        project / ".nyc_output" / "coverage.js",
    ]
    for path in generated_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export const generated = true;\n", encoding="utf-8")
    (project / "src" / "visible.ts").write_text(
        "export const visible = true;\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"max_files": 50},
        _ctx(project),
    )

    assert result.success, result.error
    paths = {item["path"] for item in result.data["files"]}
    assert "src/visible.ts" in paths
    assert paths.isdisjoint(
        {path.relative_to(project).as_posix() for path in generated_files}
    )


@pytest.mark.asyncio
async def test_project_outline_parses_explicit_file_inside_generated_dir(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    generated = project / "coverage" / "report.ts"
    generated.parent.mkdir()
    generated.write_text(
        "export function inspectCoverage() { return true; }\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "coverage/report.ts"},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["count"] == 1
    assert result.data["files"][0]["path"] == "coverage/report.ts"
    assert result.data["symbol_index"]["inspectCoverage"][0]["path"] == "coverage/report.ts"


@pytest.mark.asyncio
async def test_project_outline_indexes_anonymous_default_exports(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "anonymous-default.jsx").write_text(
        "export default () => null;\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/anonymous-default.jsx",
            "include_files": False,
            "symbol_query": "default",
            "export_query": "default",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    assert result.data["symbol_index"] == {
        "default": [
            {
                "path": "src/anonymous-default.jsx",
                "language": "javascript",
                "kind": "function",
                "line": 1,
                "exported": True,
            }
        ]
    }
    assert result.data["export_index"] == {
        "default": [
            {
                "path": "src/anonymous-default.jsx",
                "language": "javascript",
                "kind": "default",
                "line": 1,
                "end_line": 1,
            }
        ]
    }


@pytest.mark.asyncio
async def test_project_outline_default_includes_modern_module_suffixes(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "entry.mjs").write_text(
        "export function start() { return true; }\n",
        encoding="utf-8",
    )
    (project / "src" / "config.cjs").write_text(
        "const fs = require('node:fs');\nmodule.exports = { fs };\n",
        encoding="utf-8",
    )
    (project / "src" / "types.mts").write_text(
        "export interface ModernOptions { enabled: boolean }\n",
        encoding="utf-8",
    )
    (project / "src" / "compat.cts").write_text(
        "export type CompatMode = 'strict' | 'loose';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"max_files": 20},
        _ctx(project),
    )

    assert result.success, result.error
    by_path = {item["path"]: item for item in result.data["files"]}
    assert by_path["src/entry.mjs"]["language"] == "javascript"
    assert by_path["src/config.cjs"]["language"] == "javascript"
    assert by_path["src/types.mts"]["language"] == "typescript"
    assert by_path["src/compat.cts"]["language"] == "typescript"
    assert result.data["symbol_index"]["start"][0]["path"] == "src/entry.mjs"
    assert result.data["import_index"]["node:fs"][0]["path"] == "src/config.cjs"
    assert result.data["export_index"]["ModernOptions"][0]["path"] == "src/types.mts"
    assert result.data["export_index"]["CompatMode"][0]["path"] == "src/compat.cts"


@pytest.mark.asyncio
async def test_project_outline_languages_filter(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "widget.ts").write_text(
        "export const buildWidget = () => ({ ok: true });\n",
        encoding="utf-8",
    )
    (project / "src" / "view.js").write_text(
        "export function view() { return null; }\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"languages": ["typescript"], "max_files": 20},
        _ctx(project),
    )

    assert result.success, result.error
    paths = {f["path"] for f in result.data["files"]}
    assert paths == {"src/widget.ts"}
    assert result.data["languages"] == ["typescript"]


@pytest.mark.asyncio
async def test_project_outline_symbol_index_can_be_disabled(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/app.py", "include_symbol_index": False},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["include_symbol_index"] is False
    assert "symbol_index" not in result.data
    assert "indexed_symbols" not in result.data["summary"]


@pytest.mark.asyncio
async def test_project_outline_import_index_can_be_disabled(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/app.py", "include_import_index": False},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["include_import_index"] is False
    assert "import_index" not in result.data
    assert "indexed_imports" not in result.data["summary"]


@pytest.mark.asyncio
async def test_project_outline_export_index_can_be_disabled(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/app.py", "include_export_index": False},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["include_export_index"] is False
    assert "export_index" not in result.data
    assert "indexed_exports" not in result.data["summary"]


@pytest.mark.asyncio
async def test_project_outline_skips_dependency_resolution_when_indexes_disabled(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leagent.project.tools import outline as outline_module
    from leagent.project.tools.outline import ProjectOutlineTool

    def fail_if_called(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("dependency resolution config should not be loaded")

    monkeypatch.setattr(outline_module, "_build_outline_path_lookup", fail_if_called)
    monkeypatch.setattr(outline_module, "_load_ts_js_resolution_config", fail_if_called)
    monkeypatch.setattr(outline_module, "_load_python_source_roots", fail_if_called)

    result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "include_file_index": True,
            "include_directory_index": True,
            "include_import_index": False,
            "include_export_index": False,
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "import_index" not in result.data
    assert "export_index" not in result.data
    assert result.data["file_index"]
    assert result.data["directory_index"]


@pytest.mark.asyncio
async def test_project_outline_can_emit_compact_file_index(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "broken.py").write_text(
        "def broken(:\n    pass\n",
        encoding="utf-8",
    )
    (project / "src" / "widget.ts").write_text(
        "import { add } from './util';\n"
        "export function Widget() {\n"
        "  return add(1, 2);\n"
        "}\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src",
            "include_files": False,
            "include_file_index": True,
            "max_file_index_entries": 2,
            "file_index_offset": 1,
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["include_files"] is False
    assert result.data["files_omitted"] is True
    assert "files" not in result.data
    assert result.data["include_file_index"] is True
    assert result.data["file_index_count"] == 2
    assert result.data["file_index_total_matches"] == 4
    assert result.data["file_index_truncated"] is True
    assert [item["path"] for item in result.data["file_index"]] == [
        "src/broken.py",
        "src/util.py",
    ]
    broken_entry = result.data["file_index"][0]
    assert broken_entry["diagnostic_count"] == 1
    assert broken_entry["parse_error"] is True
    assert broken_entry["diagnostic_codes"] == ["python_syntax_error"]
    assert result.data["summary"]["indexed_files"] == 2
    assert result.data["summary"]["file_index_total_matches"] == 4
    assert result.data["summary"]["file_index_offset"] == 1

    query_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src",
            "include_files": False,
            "include_file_index": True,
            "file_query": "Widget",
        },
        _ctx(project),
    )

    assert query_result.success, query_result.error
    assert query_result.data["file_index_total_matches"] == 1
    assert query_result.data["file_index_count"] == 1
    assert query_result.data["file_index_truncated"] is False
    widget_entry = query_result.data["file_index"][0]
    assert widget_entry["path"] == "src/widget.ts"
    assert widget_entry["top_symbols"] == [
        {"name": "Widget", "kind": "function", "line": 2}
    ]
    assert widget_entry["top_imports"] == ["./util"]
    assert widget_entry["top_exports"] == ["Widget"]
    assert query_result.data["summary"]["file_query"] == "Widget"

    path_query_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src",
            "include_files": False,
            "include_file_index": True,
            "file_query": "src\\widget.ts",
        },
        _ctx(project),
    )

    assert path_query_result.success, path_query_result.error
    assert path_query_result.data["file_query"] == "src/widget.ts"
    assert path_query_result.data["file_index_total_matches"] == 1
    assert path_query_result.data["file_index"][0]["path"] == "src/widget.ts"


@pytest.mark.asyncio
async def test_project_outline_can_emit_compact_directory_index(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "models").mkdir()
    (project / "src" / "models" / "user.ts").write_text(
        "export type User = { id: string };\n",
        encoding="utf-8",
    )
    (project / "src" / "ui").mkdir()
    (project / "src" / "ui" / "button.ts").write_text(
        "export class Button {}\n",
        encoding="utf-8",
    )
    (project / "tests" / "broken").mkdir()
    (project / "tests" / "broken" / "bad.py").write_text(
        "def bad(:\n    pass\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "include_directory_index": True,
            "max_directory_index_entries": 2,
            "directory_index_offset": 1,
            "max_files": 20,
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["include_directory_index"] is True
    assert result.data["directory_index_count"] == 2
    assert result.data["directory_index_total_matches"] == 5
    assert result.data["directory_index_truncated"] is True
    assert [item["path"] for item in result.data["directory_index"]] == [
        "src/models",
        "src/ui",
    ]
    model_entry = result.data["directory_index"][0]
    assert model_entry["file_count"] == 1
    assert model_entry["by_language"] == {"typescript": 1}
    assert model_entry["top_exports"] == ["User"]
    assert result.data["summary"]["indexed_directories"] == 2
    assert result.data["summary"]["directory_index_total_matches"] == 5
    assert result.data["summary"]["directory_index_offset"] == 1

    symbol_query_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "include_directory_index": True,
            "directory_query": "Button",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert symbol_query_result.success, symbol_query_result.error
    assert symbol_query_result.data["directory_index_total_matches"] == 1
    assert symbol_query_result.data["directory_index_count"] == 1
    assert symbol_query_result.data["directory_index"][0]["path"] == "src/ui"
    assert symbol_query_result.data["directory_index"][0]["top_symbols"] == [
        {
            "name": "Button",
            "kind": "class",
            "path": "src/ui/button.ts",
            "line": 1,
        }
    ]
    assert symbol_query_result.data["summary"]["directory_query"] == "Button"

    diagnostic_query_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "include_directory_index": True,
            "directory_query": "python_syntax_error",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert diagnostic_query_result.success, diagnostic_query_result.error
    assert diagnostic_query_result.data["directory_index_total_matches"] == 1
    diagnostic_entry = diagnostic_query_result.data["directory_index"][0]
    assert diagnostic_entry["path"] == "tests/broken"
    assert diagnostic_entry["diagnostic_count"] == 1
    assert diagnostic_entry["problem_files"] == ["tests/broken/bad.py"]
    assert diagnostic_entry["diagnostic_codes"] == ["python_syntax_error"]


@pytest.mark.asyncio
async def test_project_outline_directory_index_can_group_by_depth(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "models").mkdir()
    (project / "src" / "models" / "user.ts").write_text(
        "export type User = { id: string };\n",
        encoding="utf-8",
    )
    (project / "src" / "ui").mkdir()
    (project / "src" / "ui" / "button.ts").write_text(
        "export class Button {}\n",
        encoding="utf-8",
    )

    grouped_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src",
            "include_files": False,
            "include_directory_index": True,
            "directory_index_depth": 1,
            "directory_query": "src",
            "include_import_index": False,
            "include_export_index": False,
            "max_files": 20,
        },
        _ctx(project),
    )

    assert grouped_result.success, grouped_result.error
    assert grouped_result.data["directory_index_depth"] == 1
    assert grouped_result.data["directory_index_total_matches"] == 1
    assert grouped_result.data["summary"]["directory_index_depth"] == 1
    entry = grouped_result.data["directory_index"][0]
    assert entry["path"] == "src"
    assert entry["file_count"] == 4
    assert entry["by_language"] == {"python": 2, "typescript": 2}
    assert entry["symbol_count"] == 4
    assert {symbol["name"] for symbol in entry["top_symbols"]} == {
        "Button",
        "User",
        "add",
        "greet",
    }

    full_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src",
            "include_files": False,
            "include_directory_index": True,
            "directory_query": "src",
            "include_import_index": False,
            "include_export_index": False,
            "max_files": 20,
        },
        _ctx(project),
    )

    assert full_result.success, full_result.error
    assert full_result.data["directory_index_depth"] == 0
    assert full_result.data["directory_index_total_matches"] == 3
    assert {entry["path"] for entry in full_result.data["directory_index"]} == {
        "src",
        "src/models",
        "src/ui",
    }

    windows_path_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src",
            "include_files": False,
            "include_directory_index": True,
            "directory_query": "src\\ui",
            "include_import_index": False,
            "include_export_index": False,
            "max_files": 20,
        },
        _ctx(project),
    )

    assert windows_path_result.success, windows_path_result.error
    assert windows_path_result.data["directory_query"] == "src/ui"
    assert windows_path_result.data["directory_index_total_matches"] == 1
    assert windows_path_result.data["directory_index"][0]["path"] == "src/ui"


@pytest.mark.asyncio
async def test_project_outline_directory_index_caps_top_previews(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "many").mkdir()
    for index in range(10):
        suffix = f"{index:02d}"
        (project / "src" / "many" / f"item_{suffix}.ts").write_text(
            f"import dep{suffix} from './dep{suffix}';\n"
            f"export const value{suffix} = dep{suffix};\n",
            encoding="utf-8",
        )

    result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "include_directory_index": True,
            "directory_query": "src/many",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["directory_index_total_matches"] == 1
    entry = result.data["directory_index"][0]
    assert entry["path"] == "src/many"
    assert entry["file_count"] == 10
    assert entry["import_count"] == 10
    assert entry["export_count"] == 10
    assert entry["top_imports"] == [f"./dep{index:02d}" for index in range(8)]
    assert entry["top_exports"] == [f"value{index:02d}" for index in range(8)]
    assert len(entry["top_imports"]) == 8
    assert len(entry["top_exports"]) == 8


@pytest.mark.asyncio
async def test_project_outline_can_omit_files_and_filter_symbol_index(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "widget.ts").write_text(
        "export interface WidgetProps { name: string }\n"
        "export const buildWidget = () => ({ ok: true });\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "symbol_query": "widget",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["include_files"] is False
    assert result.data["files_omitted"] is True
    assert "files" not in result.data
    assert result.data["count"] >= 1
    assert result.data["symbol_query"] == "widget"
    assert result.data["summary"]["symbol_query"] == "widget"
    assert set(result.data["symbol_index"]) == {"WidgetProps", "buildWidget"}
    assert result.data["symbol_index"]["buildWidget"][0]["path"] == "src/widget.ts"


@pytest.mark.asyncio
async def test_project_outline_symbol_index_includes_wrapped_ts_functions(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "components.tsx").write_text(
        "import React, { memo } from 'react';\n"
        "export const MemoPanel = memo(() => null);\n"
        "export const RefButton = React.forwardRef<HTMLButtonElement, Props>(\n"
        "  (props, ref) => null,\n"
        ");\n"
        "const ids = items.map((item) => item.id);\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/components.tsx",
            "include_files": False,
            "symbol_query": "Panel",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["symbol_index"] == {
        "MemoPanel": [
            {
                "path": "src/components.tsx",
                "language": "typescript",
                "kind": "function",
                "line": 2,
                "exported": True,
            }
        ]
    }

    all_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/components.tsx",
            "include_files": False,
            "symbol_query": "Button",
        },
        _ctx(project),
    )

    assert all_result.success, all_result.error
    assert set(all_result.data["symbol_index"]) == {"RefButton"}
    assert all_result.data["symbol_index"]["RefButton"][0]["kind"] == "function"


@pytest.mark.asyncio
async def test_project_outline_symbol_index_includes_exported_ts_values(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "config.ts").write_text(
        "export const appConfig = defineConfig({ mode: 'spa' });\n"
        "export let currentMode = 'dev';\n"
        "const internal = 1;\n"
        "export const makeConfig = () => ({ mode: currentMode });\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/config.ts",
            "include_files": False,
            "symbol_query": "constant",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["symbol_index"] == {
        "appConfig": [
            {
                "path": "src/config.ts",
                "language": "typescript",
                "kind": "constant",
                "line": 1,
                "exported": True,
            }
        ]
    }

    variable_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/config.ts",
            "include_files": False,
            "symbol_query": "current",
        },
        _ctx(project),
    )

    assert variable_result.success, variable_result.error
    assert variable_result.data["symbol_index"]["currentMode"][0]["kind"] == "variable"

    function_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/config.ts",
            "include_files": False,
            "symbol_query": "makeConfig",
        },
        _ctx(project),
    )

    assert function_result.success, function_result.error
    assert function_result.data["symbol_index"]["makeConfig"][0]["kind"] == "function"


@pytest.mark.asyncio
async def test_project_outline_marks_python_all_symbols_as_exported(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "public_api.py").write_text(
        "__all__ = ['PublicWorker', 'public_build']\n"
        "\n"
        "class PublicWorker:\n"
        "    def run(self):\n"
        "        return 'ok'\n"
        "\n"
        "def public_build():\n"
        "    return PublicWorker()\n"
        "\n"
        "def hidden():\n"
        "    return None\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/public_api.py", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    index = result.data["symbol_index"]
    assert index["PublicWorker"][0]["exported"] is True
    assert index["public_build"][0]["exported"] is True
    assert "exported" not in index["PublicWorker.run"][0]
    assert "exported" not in index["hidden"][0]
    assert set(result.data["export_index"]) == {"PublicWorker", "public_build"}


@pytest.mark.asyncio
async def test_project_outline_symbol_index_includes_python_all_values(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "public_values.py").write_text(
        "VERSION = '1.0'\n"
        "runtime_config = {}\n"
        "hidden = object()\n"
        "__all__ = ['VERSION', 'runtime_config']\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/public_values.py",
            "include_files": False,
            "symbol_query": "constant",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["symbol_index"] == {
        "VERSION": [
            {
                "path": "src/public_values.py",
                "language": "python",
                "kind": "constant",
                "line": 1,
                "end_line": 1,
                "exported": True,
            }
        ]
    }

    variable_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/public_values.py",
            "include_files": False,
            "symbol_query": "runtime",
        },
        _ctx(project),
    )

    assert variable_result.success, variable_result.error
    assert variable_result.data["symbol_index"]["runtime_config"][0]["kind"] == "variable"
    assert variable_result.data["symbol_index"]["runtime_config"][0]["exported"] is True
    assert "hidden" not in variable_result.data["symbol_index"]


@pytest.mark.asyncio
async def test_project_outline_symbol_index_includes_python_decorators(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "settings.py").write_text(
        "from functools import cached_property\n"
        "\n"
        "class Settings:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return 'ok'\n"
        "\n"
        "    @value.setter\n"
        "    def value(self, next_value):\n"
        "        self._value = next_value\n"
        "\n"
        "    @staticmethod\n"
        "    def parse(raw):\n"
        "        return raw.strip()\n"
        "\n"
        "    @cached_property\n"
        "    def token(self):\n"
        "        return 'token'\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/settings.py",
            "include_files": False,
            "symbol_query": "Settings.",
        },
        _ctx(project),
    )

    assert result.success, result.error
    index = result.data["symbol_index"]
    assert {ref["kind"] for ref in index["Settings.value"]} == {"getter", "setter"}
    assert {tuple(ref["decorators"]) for ref in index["Settings.value"]} == {
        ("property",),
        ("value.setter",),
    }
    assert index["Settings.parse"][0]["kind"] == "method"
    assert index["Settings.parse"][0]["decorators"] == ["staticmethod"]
    assert index["Settings.token"][0]["kind"] == "getter"
    assert index["Settings.token"][0]["decorators"] == ["cached_property"]


@pytest.mark.asyncio
async def test_project_outline_symbol_index_includes_class_bases(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "models.py").write_text(
        "class UserModel(BaseModel, mixins.Timestamped):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (project / "src" / "view-model.ts").write_text(
        "export interface ViewProps extends BaseProps, WithTheme {}\n"
        "export class ViewModel extends BaseView implements Renderable {}\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "symbol_query": "Model",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert result.success, result.error
    index = result.data["symbol_index"]
    assert index["UserModel"][0]["bases"] == ["BaseModel", "mixins.Timestamped"]
    assert index["ViewModel"][0]["bases"] == ["BaseView", "Renderable"]

    props_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/view-model.ts",
            "include_files": False,
            "symbol_query": "Props",
        },
        _ctx(project),
    )

    assert props_result.success, props_result.error
    assert props_result.data["symbol_index"]["ViewProps"][0]["bases"] == [
        "BaseProps",
        "WithTheme",
    ]


@pytest.mark.asyncio
async def test_project_outline_symbol_query_matches_metadata(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "models.py").write_text(
        "class UserModel(BaseModel):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (project / "src" / "dashboard.ts").write_text(
        "@Component({ selector: 'app-dashboard' })\n"
        "export class Dashboard {}\n",
        encoding="utf-8",
    )

    base_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "symbol_query": "BaseModel",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert base_result.success, base_result.error
    assert set(base_result.data["symbol_index"]) == {"UserModel"}
    assert base_result.data["symbol_index"]["UserModel"][0]["bases"] == ["BaseModel"]
    assert base_result.data["summary"]["symbol_query"] == "BaseModel"

    decorator_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "symbol_query": "Component",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert decorator_result.success, decorator_result.error
    assert set(decorator_result.data["symbol_index"]) == {"Dashboard"}
    assert decorator_result.data["symbol_index"]["Dashboard"][0]["decorators"] == [
        "Component"
    ]


@pytest.mark.asyncio
async def test_project_outline_symbol_index_includes_typescript_accessors(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "settings.ts").write_text(
        "class Settings {\n"
        "  get value(): string {\n"
        "    return 'ok';\n"
        "  }\n"
        "  set value(next: string) {\n"
        "    void next;\n"
        "  }\n"
        "  get #secret(): string {\n"
        "    return 'private';\n"
        "  }\n"
        "  #compute(): string {\n"
        "    return this.#secret;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/settings.ts",
            "include_files": False,
            "symbol_query": "settings.value",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    refs = result.data["symbol_index"]["Settings.value"]
    assert {ref["kind"] for ref in refs} == {"getter", "setter"}
    assert {ref["line"] for ref in refs} == {2, 5}

    private_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/settings.ts",
            "include_files": False,
            "symbol_query": "#",
        },
        _ctx(project),
    )

    assert private_result.success, private_result.error
    assert set(private_result.data["symbol_index"]) == {
        "Settings.#compute",
        "Settings.#secret",
    }
    assert private_result.data["symbol_index"]["Settings.#secret"][0]["kind"] == "getter"


@pytest.mark.asyncio
async def test_project_outline_symbol_index_includes_typescript_decorators(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "dashboard.ts").write_text(
        "@Component({\n"
        "  selector: 'app-dashboard',\n"
        "})\n"
        "export class Dashboard {\n"
        "  @Trace()\n"
        "  load(): void {}\n"
        "}\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/dashboard.ts",
            "include_files": False,
            "symbol_query": "Dashboard",
        },
        _ctx(project),
    )

    assert result.success, result.error
    index = result.data["symbol_index"]
    assert index["Dashboard"][0]["decorators"] == ["Component"]
    assert index["Dashboard"][0]["exported"] is True
    assert index["Dashboard.load"][0]["decorators"] == ["Trace"]


@pytest.mark.asyncio
async def test_project_outline_symbol_index_omits_local_function_symbols(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "local.js").write_text(
        "export function outer() {\n"
        "  const localArrow = () => true;\n"
        "  function nested() { return false; }\n"
        "  return localArrow();\n"
        "}\n"
        "class Store {\n"
        "  run() {\n"
        "    const localMethodHelper = () => true;\n"
        "    return localMethodHelper();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/local.js",
            "include_files": False,
            "symbol_query": "local",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    assert result.data["symbol_index"] == {}
    assert result.data["symbol_index_count"] == 0


@pytest.mark.asyncio
async def test_project_outline_can_omit_files_and_filter_import_index(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "widget.ts").write_text(
        "import React, { useMemo as memo } from 'react';\n"
        "import { helper } from './helper';\n"
        "export const buildWidget = () => helper;\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "import_query": "react",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["include_files"] is False
    assert result.data["files_omitted"] is True
    assert "files" not in result.data
    assert result.data["import_query"] == "react"
    assert result.data["summary"]["import_query"] == "react"
    assert set(result.data["import_index"]) == {"react"}
    assert result.data["import_index"]["react"][0]["path"] == "src/widget.ts"
    assert result.data["import_index"]["react"][0]["names"] == ["memo", "React"]
    assert result.data["import_index"]["react"][0]["aliases"] == [
        {"name": "useMemo", "asname": "memo"}
    ]

    alias_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "import_query": "useMemo",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert alias_result.success, alias_result.error
    assert set(alias_result.data["import_index"]) == {"react"}
    assert alias_result.data["import_index"]["react"][0]["names"] == ["memo", "React"]


@pytest.mark.asyncio
async def test_project_outline_import_query_avoids_resolving_raw_misses(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leagent.project.tools import outline as outline_module
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "import_search.ts").write_text(
        "\n".join(
            f"import dep{index:02d} from './dep{index:02d}';"
            for index in range(6)
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    def count_resolution(**kwargs: Any) -> None:
        calls.append(str(kwargs.get("module")))

    monkeypatch.setattr(outline_module, "_resolve_dependency_path", count_resolution)

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/import_search.ts",
            "include_files": False,
            "include_export_index": False,
            "import_query": "dep03",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert set(result.data["import_index"]) == {"./dep03"}
    assert calls == ["./dep03"]


@pytest.mark.asyncio
async def test_project_outline_indexes_import_export_end_lines(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "spans.ts").write_text(
        "import {\n"
        "  User,\n"
        "  Role as PublicRole,\n"
        "} from './types';\n"
        "export {\n"
        "  User as PublicUser,\n"
        "  PublicRole,\n"
        "} from './types';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/spans.ts", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    refs = result.data["import_index"]["./types"]
    assert {ref["source"] for ref in refs} == {"import", "export"}
    import_ref = next(ref for ref in refs if ref["source"] == "import")
    export_dependency_ref = next(ref for ref in refs if ref["source"] == "export")
    assert import_ref["line"] == 1
    assert import_ref["end_line"] == 4
    assert export_dependency_ref["line"] == 5
    assert export_dependency_ref["end_line"] == 8

    export_ref = result.data["export_index"]["PublicUser"][0]
    assert export_ref["line"] == 5
    assert export_ref["end_line"] == 8


@pytest.mark.asyncio
async def test_project_outline_resolves_project_dependency_paths(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "pkg").mkdir()
    (project / "src" / "views").mkdir()
    (project / "src" / "helper.ts").write_text(
        "export const helper = () => true;\n",
        encoding="utf-8",
    )
    (project / "src" / "views" / "index.ts").write_text(
        "export const route = '/';\n",
        encoding="utf-8",
    )
    (project / "src" / "index.ts").write_text(
        "import { helper } from './helper';\n"
        "export { helper as publicHelper } from './helper';\n"
        "import { route } from './views';\n",
        encoding="utf-8",
    )
    (project / "src" / "pkg" / "consumer.py").write_text(
        "from ..app import greet\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"include_files": False, "max_files": 20},
        _ctx(project),
    )

    assert result.success, result.error
    app_ref = result.data["import_index"]["src.app"][0]
    assert app_ref["resolved_path"] == "src/app.py"
    assert app_ref["resolved_language"] == "python"

    relative_app_ref = result.data["import_index"]["..app"][0]
    assert relative_app_ref["path"] == "src/pkg/consumer.py"
    assert relative_app_ref["resolved_path"] == "src/app.py"
    assert relative_app_ref["resolved_language"] == "python"

    helper_refs = result.data["import_index"]["./helper"]
    assert {ref["source"] for ref in helper_refs} == {"import", "export"}
    assert {ref["resolved_path"] for ref in helper_refs} == {"src/helper.ts"}
    assert {ref["resolved_language"] for ref in helper_refs} == {"typescript"}

    views_ref = result.data["import_index"]["./views"][0]
    assert views_ref["resolved_path"] == "src/views/index.ts"
    assert views_ref["resolved_language"] == "typescript"

    public_ref = result.data["export_index"]["publicHelper"][0]
    assert public_ref["module"] == "./helper"
    assert public_ref["resolved_path"] == "src/helper.ts"
    assert public_ref["resolved_language"] == "typescript"

    resolved_query_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "import_query": "src/helper.ts",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert resolved_query_result.success, resolved_query_result.error
    assert set(resolved_query_result.data["import_index"]) == {"./helper"}
    assert {
        ref["source"] for ref in resolved_query_result.data["import_index"]["./helper"]
    } == {"import", "export"}

    windows_resolved_query_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "import_query": "src\\helper.ts",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert windows_resolved_query_result.success, windows_resolved_query_result.error
    assert windows_resolved_query_result.data["import_query"] == "src/helper.ts"
    assert set(windows_resolved_query_result.data["import_index"]) == {"./helper"}

    export_resolved_query_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "export_query": "src/helper.ts",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert export_resolved_query_result.success, export_resolved_query_result.error
    assert set(export_resolved_query_result.data["export_index"]) == {"publicHelper"}
    assert (
        export_resolved_query_result.data["export_index"]["publicHelper"][0]["resolved_path"]
        == "src/helper.ts"
    )

    windows_export_resolved_query_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "export_query": "src\\helper.ts",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert windows_export_resolved_query_result.success
    assert windows_export_resolved_query_result.data["export_query"] == "src/helper.ts"
    assert set(windows_export_resolved_query_result.data["export_index"]) == {
        "publicHelper"
    }


@pytest.mark.asyncio
async def test_project_outline_resolves_python_src_layout_absolute_imports(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "mypkg").mkdir()
    (project / "src" / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
    (project / "src" / "mypkg" / "core.py").write_text(
        "def run():\n    return True\n",
        encoding="utf-8",
    )
    (project / "src" / "mypkg" / "util.py").write_text(
        "def normalize(value):\n    return value\n",
        encoding="utf-8",
    )
    (project / "tests" / "test_src_layout.py").write_text(
        "from app import greet\n"
        "from mypkg.core import run\n"
        "import mypkg.util as util\n"
        "from mypkg import core as core_mod\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "tests/test_src_layout.py", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["count"] == 1
    assert "files" not in result.data
    app_ref = result.data["import_index"]["app"][0]
    assert app_ref["resolved_path"] == "src/app.py"
    assert app_ref["resolved_language"] == "python"
    core_ref = result.data["import_index"]["mypkg.core"][0]
    assert core_ref["resolved_path"] == "src/mypkg/core.py"
    assert core_ref["resolved_language"] == "python"
    util_ref = result.data["import_index"]["mypkg.util"][0]
    assert util_ref["resolved_path"] == "src/mypkg/util.py"
    assert util_ref["resolved_language"] == "python"
    assert util_ref["aliases"] == [{"name": "mypkg.util", "asname": "util"}]
    package_ref = result.data["import_index"]["mypkg"][0]
    assert package_ref["resolved_path"] == "src/mypkg/__init__.py"
    assert package_ref["resolved_names"] == [
        {
            "name": "core",
            "module": "mypkg.core",
            "resolved_path": "src/mypkg/core.py",
            "resolved_language": "python",
            "asname": "core_mod",
        }
    ]

    query_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "tests/test_src_layout.py",
            "include_files": False,
            "import_query": "src/mypkg/core.py",
        },
        _ctx(project),
    )

    assert query_result.success, query_result.error
    assert set(query_result.data["import_index"]) == {"mypkg", "mypkg.core"}


@pytest.mark.asyncio
async def test_project_outline_resolves_python_pyproject_package_dir(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "lib" / "acme").mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[tool.setuptools.package-dir]\n"
        '"" = "lib"\n',
        encoding="utf-8",
    )
    (project / "lib" / "acme" / "core.py").write_text(
        "def run():\n    return True\n",
        encoding="utf-8",
    )
    (project / "lib" / "acme" / "util.py").write_text(
        "def normalize(value):\n    return value\n",
        encoding="utf-8",
    )
    (project / "tests" / "test_lib_layout.py").write_text(
        "from acme.core import run\n"
        "import acme.util as util\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "tests/test_lib_layout.py", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    core_ref = result.data["import_index"]["acme.core"][0]
    assert core_ref["resolved_path"] == "lib/acme/core.py"
    assert core_ref["resolved_language"] == "python"
    util_ref = result.data["import_index"]["acme.util"][0]
    assert util_ref["resolved_path"] == "lib/acme/util.py"
    assert util_ref["resolved_language"] == "python"


@pytest.mark.asyncio
async def test_project_outline_resolves_python_setup_cfg_package_dir(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "lib" / "acme").mkdir(parents=True)
    (project / "setup.cfg").write_text(
        "[options]\n"
        "package_dir =\n"
        "    = lib\n",
        encoding="utf-8",
    )
    (project / "lib" / "acme" / "core.py").write_text(
        "def run():\n    return True\n",
        encoding="utf-8",
    )
    (project / "tests" / "test_setup_cfg_layout.py").write_text(
        "from acme.core import run\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "tests/test_setup_cfg_layout.py", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    core_ref = result.data["import_index"]["acme.core"][0]
    assert core_ref["resolved_path"] == "lib/acme/core.py"
    assert core_ref["resolved_language"] == "python"


@pytest.mark.asyncio
async def test_project_outline_resolves_python_setup_cfg_find_where(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "python_src" / "acme").mkdir(parents=True)
    (project / "setup.cfg").write_text(
        "[options.packages.find]\n"
        "where = python_src\n",
        encoding="utf-8",
    )
    (project / "python_src" / "acme" / "core.py").write_text(
        "def run():\n    return True\n",
        encoding="utf-8",
    )
    (project / "tests" / "test_find_where_layout.py").write_text(
        "from acme.core import run\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "tests/test_find_where_layout.py", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    core_ref = result.data["import_index"]["acme.core"][0]
    assert core_ref["resolved_path"] == "python_src/acme/core.py"
    assert core_ref["resolved_language"] == "python"


@pytest.mark.asyncio
async def test_project_outline_resolves_tsconfig_paths_and_base_url(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "models").mkdir()
    (project / "src" / "components").mkdir()
    (project / "tsconfig.json").write_text(
        "{\n"
        "  // project aliases used by frontend code\n"
        '  "compilerOptions": {\n'
        '    "baseUrl": "src",\n'
        '    "paths": {\n'
        '      "@/*": ["*"],\n'
        '      "@models/*": ["models/*"],\n'
        "    },\n"
        "  },\n"
        "}\n",
        encoding="utf-8",
    )
    (project / "src" / "models" / "user.ts").write_text(
        "export interface User { id: string }\n",
        encoding="utf-8",
    )
    (project / "src" / "components" / "Button.tsx").write_text(
        "export default function Button() { return null; }\n",
        encoding="utf-8",
    )
    (project / "src" / "entry.ts").write_text(
        "import { User } from '@models/user';\n"
        "import Button from 'components/Button';\n"
        "export { User as PublicUser } from '@/models/user';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/entry.ts", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    model_ref = result.data["import_index"]["@models/user"][0]
    assert model_ref["resolved_path"] == "src/models/user.ts"
    assert model_ref["resolved_language"] == "typescript"
    button_ref = result.data["import_index"]["components/Button"][0]
    assert button_ref["resolved_path"] == "src/components/Button.tsx"
    assert button_ref["resolved_language"] == "typescript"
    public_ref = result.data["export_index"]["PublicUser"][0]
    assert public_ref["module"] == "@/models/user"
    assert public_ref["resolved_path"] == "src/models/user.ts"
    assert public_ref["resolved_language"] == "typescript"


@pytest.mark.asyncio
async def test_project_outline_resolves_local_tsconfig_extends_paths(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "shared").mkdir()
    (project / "tsconfig.base.json").write_text(
        "{\n"
        '  "compilerOptions": {\n'
        '    "baseUrl": "src",\n'
        '    "paths": {\n'
        '      "@shared/*": ["shared/*"]\n'
        "    }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (project / "tsconfig.json").write_text(
        "{\n"
        '  "extends": "./tsconfig.base",\n'
        '  "compilerOptions": {\n'
        '    "jsx": "react-jsx"\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (project / "src" / "shared" / "format.ts").write_text(
        "export function formatName(value: string) { return value.trim(); }\n",
        encoding="utf-8",
    )
    (project / "src" / "entry.ts").write_text(
        "import { formatName } from '@shared/format';\n"
        "export { formatName as publicFormatName } from '@shared/format';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/entry.ts", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    import_ref = result.data["import_index"]["@shared/format"][0]
    assert import_ref["resolved_path"] == "src/shared/format.ts"
    assert import_ref["resolved_language"] == "typescript"
    export_ref = result.data["export_index"]["publicFormatName"][0]
    assert export_ref["resolved_path"] == "src/shared/format.ts"
    assert export_ref["resolved_language"] == "typescript"


@pytest.mark.asyncio
async def test_project_outline_resolves_extends_paths_relative_to_parent_config_dir(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "config").mkdir()
    (project / "src" / "shared").mkdir()
    (project / "config" / "tsconfig.base.json").write_text(
        "{\n"
        '  "compilerOptions": {\n'
        '    "baseUrl": "../src",\n'
        '    "paths": {\n'
        '      "@shared/*": ["shared/*"]\n'
        "    }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (project / "tsconfig.json").write_text(
        "{\n"
        '  "extends": "./config/tsconfig.base"\n'
        "}\n",
        encoding="utf-8",
    )
    (project / "src" / "shared" / "format.ts").write_text(
        "export function formatName(value: string) { return value.trim(); }\n",
        encoding="utf-8",
    )
    (project / "src" / "entry.ts").write_text(
        "import { formatName } from '@shared/format';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/entry.ts", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    import_ref = result.data["import_index"]["@shared/format"][0]
    assert import_ref["resolved_path"] == "src/shared/format.ts"
    assert import_ref["resolved_language"] == "typescript"


@pytest.mark.asyncio
async def test_project_outline_resolves_jsconfig_base_url(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "utils").mkdir()
    (project / "jsconfig.json").write_text(
        "{\n"
        '  "compilerOptions": {\n'
        '    "baseUrl": "src"\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (project / "src" / "utils" / "helper.js").write_text(
        "export function helper() { return true; }\n",
        encoding="utf-8",
    )
    (project / "src" / "entry.js").write_text(
        "import { helper } from 'utils/helper';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/entry.js", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    helper_ref = result.data["import_index"]["utils/helper"][0]
    assert helper_ref["resolved_path"] == "src/utils/helper.js"
    assert helper_ref["resolved_language"] == "javascript"


@pytest.mark.asyncio
async def test_project_outline_resolves_package_json_imports(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "shared").mkdir()
    (project / "package.json").write_text(
        "{\n"
        '  "imports": {\n'
        '    "#shared/*": "./src/shared/*",\n'
        '    "#config": {\n'
        '      "types": "./src/config.d.ts",\n'
        '      "default": "./src/config.ts"\n'
        "    },\n"
        '    "ignored": "./src/ignored.ts",\n'
        '    "#external": "left-pad"\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (project / "src" / "shared" / "format.ts").write_text(
        "export function formatName(value: string) { return value.trim(); }\n",
        encoding="utf-8",
    )
    (project / "src" / "config.ts").write_text(
        "export const config = { enabled: true };\n",
        encoding="utf-8",
    )
    (project / "src" / "entry.ts").write_text(
        "import { formatName } from '#shared/format';\n"
        "export { config as publicConfig } from '#config';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/entry.ts", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    import_ref = result.data["import_index"]["#shared/format"][0]
    assert import_ref["resolved_path"] == "src/shared/format.ts"
    assert import_ref["resolved_language"] == "typescript"
    export_ref = result.data["export_index"]["publicConfig"][0]
    assert export_ref["module"] == "#config"
    assert export_ref["resolved_path"] == "src/config.ts"
    assert export_ref["resolved_language"] == "typescript"
    assert "#external" not in result.data["import_index"]
    assert "ignored" not in result.data["import_index"]


@pytest.mark.asyncio
async def test_project_outline_resolves_package_json_self_reference_exports(
    project: Path,
) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "features").mkdir()
    (project / "package.json").write_text(
        "{\n"
        '  "name": "@acme/ui",\n'
        '  "exports": {\n'
        '    ".": "./src/index.ts",\n'
        '    "./format": {\n'
        '      "types": "./src/format.d.ts",\n'
        '      "default": "./src/format.ts"\n'
        "    },\n"
        '    "./features/*": "./src/features/*",\n'
        '    "./external": "left-pad"\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (project / "src" / "index.ts").write_text(
        "export const root = true;\n",
        encoding="utf-8",
    )
    (project / "src" / "format.ts").write_text(
        "export function formatName(value: string) { return value.trim(); }\n",
        encoding="utf-8",
    )
    (project / "src" / "features" / "button.ts").write_text(
        "export const button = true;\n",
        encoding="utf-8",
    )
    (project / "src" / "entry.ts").write_text(
        "import { root } from '@acme/ui';\n"
        "import { button } from '@acme/ui/features/button';\n"
        "export { formatName as publicFormatName } from '@acme/ui/format';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/entry.ts", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    root_ref = result.data["import_index"]["@acme/ui"][0]
    assert root_ref["resolved_path"] == "src/index.ts"
    assert root_ref["resolved_language"] == "typescript"
    feature_ref = result.data["import_index"]["@acme/ui/features/button"][0]
    assert feature_ref["resolved_path"] == "src/features/button.ts"
    assert feature_ref["resolved_language"] == "typescript"
    export_ref = result.data["export_index"]["publicFormatName"][0]
    assert export_ref["module"] == "@acme/ui/format"
    assert export_ref["resolved_path"] == "src/format.ts"
    assert export_ref["resolved_language"] == "typescript"
    assert "@acme/ui/external" not in result.data["import_index"]


@pytest.mark.asyncio
async def test_project_outline_resolves_single_file_dependencies_from_filesystem(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "views").mkdir()
    (project / "src" / "helper.ts").write_text(
        "export const helper = () => true;\n",
        encoding="utf-8",
    )
    (project / "src" / "views" / "index.ts").write_text(
        "export const route = '/';\n",
        encoding="utf-8",
    )
    (project / "src" / "entry.ts").write_text(
        "import { helper } from './helper';\n"
        "export { route } from './views';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/entry.ts", "include_files": False},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["count"] == 1
    assert "files" not in result.data
    helper_ref = result.data["import_index"]["./helper"][0]
    assert helper_ref["resolved_path"] == "src/helper.ts"
    assert helper_ref["resolved_language"] == "typescript"
    route_ref = result.data["export_index"]["route"][0]
    assert route_ref["resolved_path"] == "src/views/index.ts"
    assert route_ref["resolved_language"] == "typescript"

    (project / "src" / "pkg").mkdir()
    (project / "src" / "pkg" / "consumer.py").write_text(
        "from ..app import greet\n",
        encoding="utf-8",
    )
    python_result = await _run(
        ProjectOutlineTool(),
        {"path": "src/pkg/consumer.py", "include_files": False},
        _ctx(project),
    )

    assert python_result.success, python_result.error
    python_ref = python_result.data["import_index"]["..app"][0]
    assert python_ref["resolved_path"] == "src/app.py"
    assert python_ref["resolved_language"] == "python"


@pytest.mark.asyncio
async def test_project_outline_can_omit_files_and_filter_export_index(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "barrel.ts").write_text(
        "export { buildWidget as WidgetFactory } from './widget';\n"
        "export * from './public';\n"
        "export = WidgetFactory;\n",
        encoding="utf-8",
    )
    (project / "src" / "api.js").write_text(
        "module.exports = { createApp, start: boot }\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "export_query": "widget",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["include_files"] is False
    assert result.data["files_omitted"] is True
    assert "files" not in result.data
    assert result.data["export_query"] == "widget"
    assert result.data["summary"]["export_query"] == "widget"
    assert set(result.data["export_index"]) == {"WidgetFactory"}
    refs = result.data["export_index"]["WidgetFactory"]
    assert {ref["kind"] for ref in refs} == {"export_equals", "named"}
    named_ref = next(ref for ref in refs if ref["kind"] == "named")
    assert named_ref["module"] == "./widget"
    assert named_ref["aliases"] == [{"name": "buildWidget", "asname": "WidgetFactory"}]

    alias_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "export_query": "buildWidget",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert alias_result.success, alias_result.error
    assert set(alias_result.data["export_index"]) == {"WidgetFactory"}
    assert alias_result.data["export_index"]["WidgetFactory"][0]["kind"] == "named"

    module_result = await _run(
        ProjectOutlineTool(),
        {
            "include_files": False,
            "export_query": "./public",
            "max_files": 20,
        },
        _ctx(project),
    )

    assert module_result.success, module_result.error
    assert set(module_result.data["export_index"]) == {"*"}
    assert module_result.data["export_index"]["*"][0]["module"] == "./public"


@pytest.mark.asyncio
async def test_project_outline_export_query_avoids_resolving_raw_misses(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leagent.project.tools import outline as outline_module
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "export_search.ts").write_text(
        "\n".join(
            f"export {{ value{index:02d} }} from './mod{index:02d}';"
            for index in range(6)
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    def count_resolution(**kwargs: Any) -> None:
        calls.append(str(kwargs.get("module")))

    monkeypatch.setattr(outline_module, "_resolve_dependency_path", count_resolution)

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/export_search.ts",
            "include_files": False,
            "include_import_index": False,
            "export_query": "value03",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert set(result.data["export_index"]) == {"value03"}
    assert calls == ["./mod03"]


@pytest.mark.asyncio
async def test_project_outline_export_index_includes_declare_and_abstract(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "client.d.ts").write_text(
        "export declare function createClient(options: Options): Client;\n"
        "export declare interface Options { baseUrl: string }\n"
        "export abstract class BaseClient {\n"
        "  abstract request(path: string): Promise<void>;\n"
        "}\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/client.d.ts",
            "include_files": False,
            "export_query": "client",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    assert set(result.data["export_index"]) == {"BaseClient", "createClient"}
    assert result.data["export_index"]["createClient"][0]["declaration_kind"] == "function"
    assert result.data["export_index"]["BaseClient"][0]["declaration_kind"] == "class"

    kind_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/client.d.ts",
            "include_files": False,
            "export_query": "function",
        },
        _ctx(project),
    )

    assert kind_result.success, kind_result.error
    assert set(kind_result.data["export_index"]) == {"createClient"}
    assert kind_result.data["export_index"]["createClient"][0]["declaration_kind"] == "function"


@pytest.mark.asyncio
async def test_project_outline_export_index_includes_namespace_and_module(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "sdk.d.ts").write_text(
        "export namespace SDK {\n"
        "  export interface Config { token: string }\n"
        "}\n"
        "export module Compat.Tools {\n"
        "  export function run(): void;\n"
        "}\n"
        "declare module 'legacy-plugin' {\n"
        "  export const plugin: unknown;\n"
        "}\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/sdk.d.ts",
            "include_files": False,
            "export_query": "compat",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    assert set(result.data["export_index"]) == {"Compat.Tools"}
    assert result.data["export_index"]["Compat.Tools"][0]["declaration_kind"] == "module"

    symbols = result.data["symbol_index"]
    assert "SDK" in symbols
    assert "legacy-plugin" in symbols


@pytest.mark.asyncio
async def test_project_outline_indexes_typescript_import_equals(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "legacy.d.ts").write_text(
        "import Express = require('express');\n"
        "export import LegacyClient = require('./legacy-client');\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/legacy.d.ts",
            "include_files": False,
            "import_query": "legacy",
            "export_query": "legacy",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    assert set(result.data["import_index"]) == {"./legacy-client"}
    import_ref = result.data["import_index"]["./legacy-client"][0]
    assert import_ref["kind"] == "import_equals"
    assert import_ref["names"] == ["LegacyClient"]
    assert set(result.data["export_index"]) == {"LegacyClient"}
    export_ref = result.data["export_index"]["LegacyClient"][0]
    assert export_ref["kind"] == "export_import_equals"
    assert export_ref["module"] == "./legacy-client"


@pytest.mark.asyncio
async def test_project_outline_import_index_includes_commonjs_requires(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "server.js").write_text(
        "const express = require('express')\n"
        "const { readFile: read } = require('node:fs/promises')\n"
        "require('./setup')\n"
        "const sample = \"require('fake')\";\n"
        "module.exports = { createApp }\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/server.js",
            "include_files": False,
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    assert set(result.data["import_index"]) == {
        "./setup",
        "express",
        "node:fs/promises",
    }
    assert "fake" not in result.data["import_index"]
    assert result.data["import_index"]["express"][0]["names"] == ["express"]
    assert result.data["import_index"]["node:fs/promises"][0]["aliases"] == [
        {"name": "readFile", "asname": "read"}
    ]


@pytest.mark.asyncio
async def test_project_outline_import_index_includes_dynamic_imports(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "lazy.tsx").write_text(
        "const LazyPanel = lazy(() => import('./views/LazyPanel'))\n"
        "await import('./setup')\n"
        "const sample = \"import('./fake')\";\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/lazy.tsx",
            "include_files": False,
            "import_query": "panel",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    assert set(result.data["import_index"]) == {"./views/LazyPanel"}
    assert result.data["import_index"]["./views/LazyPanel"][0]["kind"] == "dynamic_import"
    assert result.data["import_index"]["./views/LazyPanel"][0]["line"] == 1


@pytest.mark.asyncio
async def test_project_outline_import_index_includes_reexport_dependencies(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "barrel.ts").write_text(
        "export { buildWidget as WidgetFactory } from './widget';\n"
        "export * from './public';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/barrel.ts",
            "include_files": False,
            "import_query": "public",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    assert set(result.data["import_index"]) == {"./public"}
    ref = result.data["import_index"]["./public"][0]
    assert ref["source"] == "export"
    assert ref["kind"] == "all"
    assert ref["names"] == ["*"]


@pytest.mark.asyncio
async def test_project_outline_indexes_typescript_namespace_reexports(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "index.ts").write_text(
        "export type * from './types';\n"
        "export * as Components from './components';\n"
        "export type * as PublicTypes from './public-types';\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/index.ts",
            "include_files": False,
            "import_query": "types",
            "export_query": "public",
        },
        _ctx(project),
    )

    assert result.success, result.error
    assert "files" not in result.data
    assert set(result.data["import_index"]) == {"./public-types", "./types"}
    assert result.data["import_index"]["./types"][0]["kind"] == "all_type"
    assert result.data["import_index"]["./public-types"][0]["kind"] == "namespace_type"
    assert set(result.data["export_index"]) == {"PublicTypes"}
    assert result.data["export_index"]["PublicTypes"][0]["module"] == "./public-types"


@pytest.mark.asyncio
async def test_project_outline_import_index_includes_nested_python_imports(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "deps.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from src.types import Model\n"
        "try:\n"
        "    import orjson as json_impl\n"
        "except ImportError:\n"
        "    import json as json_impl\n"
        "def load_path():\n"
        "    import pathlib\n"
        "    return pathlib.Path('.')\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/deps.py"},
        _ctx(project),
    )

    assert result.success, result.error
    assert {"typing", "src.types", "orjson", "json", "pathlib"} <= set(
        result.data["import_index"]
    )
    assert result.data["import_index"]["pathlib"][0]["line"] == 9
    assert result.data["import_index"]["orjson"][0]["aliases"] == [
        {"name": "orjson", "asname": "json_impl"}
    ]


@pytest.mark.asyncio
async def test_project_outline_import_index_expands_relative_from_imports(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "relative.py").write_text(
        "from . import util\n"
        "from .. import shared\n"
        "from .app import greet\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/relative.py"},
        _ctx(project),
    )

    assert result.success, result.error
    assert ".util" in result.data["import_index"]
    assert "..shared" in result.data["import_index"]
    assert ".app" in result.data["import_index"]
    assert "." not in result.data["import_index"]
    assert ".." not in result.data["import_index"]


@pytest.mark.asyncio
async def test_project_outline_symbol_index_is_bounded(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "many.py").write_text(
        "\n".join(f"def f{i}():\n    return {i}\n" for i in range(5)),
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/many.py", "max_index_symbols": 2},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["symbol_index_count"] == 2
    assert result.data["symbol_index_total_matches"] == 5
    assert result.data["symbol_index_truncated"] is True
    assert result.data["summary"]["symbol_index_truncated"] is True
    assert result.data["summary"]["symbol_index_total_matches"] == 5
    indexed_refs = sum(len(refs) for refs in result.data["symbol_index"].values())
    assert indexed_refs == 2

    page_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/many.py",
            "max_index_symbols": 2,
            "symbol_index_offset": 2,
        },
        _ctx(project),
    )

    assert page_result.success, page_result.error
    assert page_result.data["symbol_index_offset"] == 2
    assert page_result.data["symbol_index_total_matches"] == 5
    assert page_result.data["symbol_index_count"] == 2
    assert page_result.data["symbol_index_truncated"] is True
    assert set(page_result.data["symbol_index"]) == {"f2", "f3"}
    assert page_result.data["summary"]["symbol_index_offset"] == 2


@pytest.mark.asyncio
async def test_project_outline_import_index_is_bounded(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "imports.py").write_text(
        "import os\n"
        "import sys\n"
        "import json\n"
        "from src.app import greet\n",
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/imports.py", "max_index_imports": 2},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["import_index_count"] == 2
    assert result.data["import_index_total_matches"] == 4
    assert result.data["import_index_truncated"] is True
    assert result.data["summary"]["import_index_truncated"] is True
    assert result.data["summary"]["import_index_total_matches"] == 4
    indexed_refs = sum(len(refs) for refs in result.data["import_index"].values())
    assert indexed_refs == 2

    page_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/imports.py",
            "max_index_imports": 2,
            "import_index_offset": 2,
        },
        _ctx(project),
    )

    assert page_result.success, page_result.error
    assert page_result.data["import_index_offset"] == 2
    assert page_result.data["import_index_total_matches"] == 4
    assert page_result.data["import_index_count"] == 2
    assert page_result.data["import_index_truncated"] is False
    assert set(page_result.data["import_index"]) == {"json", "src.app"}
    assert page_result.data["summary"]["import_index_offset"] == 2


@pytest.mark.asyncio
async def test_project_outline_export_index_is_bounded(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    (project / "src" / "exports.ts").write_text(
        "\n".join(f"export const value{i} = {i};" for i in range(5)),
        encoding="utf-8",
    )

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/exports.ts", "max_index_exports": 2},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["export_index_count"] == 2
    assert result.data["export_index_total_matches"] == 5
    assert result.data["export_index_truncated"] is True
    assert result.data["summary"]["export_index_truncated"] is True
    assert result.data["summary"]["export_index_total_matches"] == 5
    indexed_refs = sum(len(refs) for refs in result.data["export_index"].values())
    assert indexed_refs == 2

    page_result = await _run(
        ProjectOutlineTool(),
        {
            "path": "src/exports.ts",
            "max_index_exports": 2,
            "export_index_offset": 3,
        },
        _ctx(project),
    )

    assert page_result.success, page_result.error
    assert page_result.data["export_index_offset"] == 3
    assert page_result.data["export_index_total_matches"] == 5
    assert page_result.data["export_index_count"] == 2
    assert page_result.data["export_index_truncated"] is False
    assert set(page_result.data["export_index"]) == {"value3", "value4"}
    assert page_result.data["summary"]["export_index_offset"] == 3


@pytest.mark.asyncio
async def test_project_outline_skips_oversized_file(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    large = project / "src" / "large.py"
    large.write_text("VALUE = '" + ("x" * 2048) + "'\n", encoding="utf-8")

    result = await _run(
        ProjectOutlineTool(),
        {"path": "src/large.py", "max_file_bytes": 1024},
        _ctx(project),
    )

    assert result.success, result.error
    assert result.data["skipped"] == 1
    f0 = result.data["files"][0]
    assert f0["skipped"] is True
    assert f0["skip_reason"] == "file_too_large"
    assert f0["diagnostics"][0]["code"] == "file_too_large"
    assert f0["size_bytes"] > result.data["max_file_bytes"]
    assert result.data["summary"]["diagnostics"] == 1
    assert result.data["summary"]["problem_files"] == ["src/large.py"]
    assert result.data["summary"]["skipped"] == 1
    assert result.data["summary"]["size_bytes"] == f0["size_bytes"]
    assert result.data["summary"]["line_count"] == 0
    assert result.data["summary"]["char_count"] == 0
    assert result.data["summary"]["largest_files"] == [
        {
            "path": "src/large.py",
            "language": "python",
            "size_bytes": f0["size_bytes"],
            "line_count": 0,
            "char_count": 0,
        }
    ]


@pytest.mark.asyncio
async def test_project_outline_reuses_cache_for_unchanged_file(project: Path) -> None:
    from leagent.project.tools.outline import ProjectOutlineTool

    tool = ProjectOutlineTool()
    first = await _run(tool, {"path": "src/app.py"}, _ctx(project))
    second = await _run(tool, {"path": "src/app.py"}, _ctx(project))

    assert first.success, first.error
    assert second.success, second.error
    assert first.data["cache_hits"] == 0
    assert second.data["cache_hits"] == 1
    assert second.data["files"][0]["path"] == "src/app.py"


@pytest.mark.asyncio
async def test_project_outline_reuses_dependency_config_cache(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leagent.project.tools import outline as outline_module
    from leagent.project.tools.outline import ProjectOutlineTool

    outline_module._OUTLINE_CACHE.clear()
    outline_module._PYTHON_SOURCE_ROOT_CACHE.clear()
    outline_module._PACKAGE_JSON_MAPPING_CACHE.clear()
    outline_module._TS_JS_CONFIG_PAYLOAD_CACHE.clear()

    (project / "pyproject.toml").write_text(
        "[tool.setuptools.package-dir]\n"
        '"" = "src"\n',
        encoding="utf-8",
    )
    (project / "package.json").write_text(
        '{ "name": "@acme/app", "imports": { "#alias": "./src/helper.ts" } }\n',
        encoding="utf-8",
    )
    (project / "tsconfig.json").write_text(
        '{ "compilerOptions": { "baseUrl": ".", "paths": { "@app/*": ["src/*"] } } }\n',
        encoding="utf-8",
    )
    (project / "src" / "entry.ts").write_text(
        "import { helper } from '@app/helper';\n"
        "import alias from '#alias';\n"
        "export { helper } from '@app/helper';\n",
        encoding="utf-8",
    )
    (project / "src" / "helper.ts").write_text(
        "export const helper = 1;\n",
        encoding="utf-8",
    )

    original_read = outline_module.read_text_with_detection
    config_reads: dict[str, int] = {}

    def counting_read(path: Any, *args: Any, **kwargs: Any) -> Any:
        name = getattr(path, "name", "")
        if name in {"pyproject.toml", "package.json", "tsconfig.json"}:
            config_reads[name] = config_reads.get(name, 0) + 1
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(outline_module, "read_text_with_detection", counting_read)

    params = {
        "include_files": False,
        "max_files": 20,
    }
    first = await _run(ProjectOutlineTool(), params, _ctx(project))
    second = await _run(ProjectOutlineTool(), params, _ctx(project))

    assert first.success, first.error
    assert second.success, second.error
    assert config_reads == {
        "pyproject.toml": 1,
        "package.json": 1,
        "tsconfig.json": 1,
    }

    (project / "tsconfig.json").write_text(
        '{ "compilerOptions": { "baseUrl": ".", "paths": { "@app/*": ["src/*"], '
        '"@lib/*": ["src/*"] } } }\n',
        encoding="utf-8",
    )
    third = await _run(ProjectOutlineTool(), params, _ctx(project))

    assert third.success, third.error
    assert config_reads == {
        "pyproject.toml": 1,
        "package.json": 1,
        "tsconfig.json": 2,
    }


@pytest.mark.asyncio
async def test_project_write_content_blob_id(project: Path) -> None:
    from leagent.project.tools.write import ProjectWriteTool
    from leagent.tools.util.tool_argument_blob import ToolArgumentBlobTool

    ctx = _ctx(project)
    blob_tool = ToolArgumentBlobTool()
    created = await _run(blob_tool, {"action": "create"}, ctx)
    assert created.success, created.error
    bid = created.data["blob_id"]
    assert isinstance(bid, str) and bid
    await _run(
        blob_tool,
        {"action": "append", "blob_id": bid, "chunk": "alpha\nbeta\n"},
        ctx,
    )
    fin = await _run(blob_tool, {"action": "finalize", "blob_id": bid}, ctx)
    assert fin.data.get("ok") is True

    write = ProjectWriteTool()
    res = await _run(
        write,
        {"path": "staged.txt", "content_blob_id": bid},
        ctx,
    )
    assert res.success, res.error
    assert (project / "staged.txt").read_text(encoding="utf-8") == "alpha\nbeta\n"


@pytest.mark.asyncio
async def test_project_edit_old_string_blob_id(project: Path) -> None:
    from leagent.project.tools.edit import ProjectEditTool
    from leagent.tools.util.tool_argument_blob import ToolArgumentBlobTool

    ctx = _ctx(project)
    blob_tool = ToolArgumentBlobTool()
    bid = (await _run(blob_tool, {"action": "create"}, ctx)).data["blob_id"]
    await _run(
        blob_tool,
        {"action": "append", "blob_id": bid, "chunk": "def greet(name: str) -> str:"},
        ctx,
    )
    await _run(blob_tool, {"action": "finalize", "blob_id": bid}, ctx)

    edit = ProjectEditTool()
    res = await _run(
        edit,
        {
            "path": "src/app.py",
            "old_string_blob_id": bid,
            "new_string": "def greet(name: str) -> str:  #patched",
        },
        ctx,
    )
    assert res.success, res.error
    text = (project / "src" / "app.py").read_text(encoding="utf-8")
    assert "#patched" in text


@pytest.mark.asyncio
async def test_project_apply_patch_diff_blob_id(project: Path) -> None:
    from leagent.project.tools.patch import ProjectApplyPatchTool
    from leagent.tools.util.tool_argument_blob import ToolArgumentBlobTool

    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def greet(name: str) -> str:\n"
        "+def greet(name: str) -> str:  #via_blob_patch\n"
        "     return f\"hello {name}\"\n"
    )
    ctx = _ctx(project)
    blob_tool = ToolArgumentBlobTool()
    bid = (await _run(blob_tool, {"action": "create"}, ctx)).data["blob_id"]
    await _run(blob_tool, {"action": "append", "blob_id": bid, "chunk": diff}, ctx)
    await _run(blob_tool, {"action": "finalize", "blob_id": bid}, ctx)

    patch = ProjectApplyPatchTool()
    res = await _run(patch, {"diff_blob_id": bid}, ctx)
    assert res.success, res.error
    assert "#via_blob_patch" in (project / "src" / "app.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_tool_argument_blob_append_chunk_base64_html(project: Path) -> None:
    import base64

    from leagent.project.tools.write import ProjectWriteTool
    from leagent.tools.util.tool_argument_blob import ToolArgumentBlobTool

    ctx = _ctx(project)
    blob_tool = ToolArgumentBlobTool()
    bid = (await _run(blob_tool, {"action": "create"}, ctx)).data["blob_id"]
    html = '<!DOCTYPE html><html lang="zh-CN"><body id="q">a"b\'c</body></html>'
    b64 = base64.standard_b64encode(html.encode("utf-8")).decode("ascii")
    res = await _run(
        blob_tool,
        {"action": "append", "blob_id": bid, "chunk_base64": b64},
        ctx,
    )
    assert res.success and res.data.get("ok") is True
    await _run(blob_tool, {"action": "finalize", "blob_id": bid}, ctx)
    wr = await _run(
        ProjectWriteTool(),
        {"path": "page.html", "content_blob_id": bid},
        ctx,
    )
    assert wr.success, wr.error
    assert (project / "page.html").read_text(encoding="utf-8") == html


@pytest.mark.asyncio
async def test_tool_argument_blob_chunk_base64_wins_over_chunk(project: Path) -> None:
    import base64

    from leagent.project.tools.write import ProjectWriteTool
    from leagent.tools.util.tool_argument_blob import ToolArgumentBlobTool

    ctx = _ctx(project)
    blob_tool = ToolArgumentBlobTool()
    bid = (await _run(blob_tool, {"action": "create"}, ctx)).data["blob_id"]
    want = "from-base64-layer"
    b64 = base64.standard_b64encode(want.encode()).decode("ascii")
    res = await _run(
        blob_tool,
        {
            "action": "append",
            "blob_id": bid,
            "chunk": "plain-wrong",
            "chunk_base64": b64,
        },
        ctx,
    )
    assert res.success and res.data.get("ok") is True
    await _run(blob_tool, {"action": "finalize", "blob_id": bid}, ctx)
    wr = await _run(
        ProjectWriteTool(),
        {"path": "winner.txt", "content_blob_id": bid},
        ctx,
    )
    assert wr.success, wr.error
    assert (project / "winner.txt").read_text(encoding="utf-8") == want


@pytest.mark.asyncio
async def test_tool_argument_blob_append_invalid_base64(project: Path) -> None:
    from leagent.tools.util.tool_argument_blob import ToolArgumentBlobTool

    ctx = _ctx(project)
    blob_tool = ToolArgumentBlobTool()
    bid = (await _run(blob_tool, {"action": "create"}, ctx)).data["blob_id"]
    res = await _run(
        blob_tool,
        {"action": "append", "blob_id": bid, "chunk_base64": "not-valid-base64!!!"},
        ctx,
    )
    assert res.success
    assert res.data.get("ok") is False
    assert "base64" in (res.data.get("error") or "").lower()


# ---------------------------------------------------------------------------
# services/coding_projects/paths.validate_project_path
# ---------------------------------------------------------------------------


def test_validate_project_path_accepts_existing_dir(project: Path) -> None:
    """A real directory resolves cleanly when no allow-list is set."""
    from leagent.project.paths import validate_project_path

    resolved = validate_project_path(str(project))
    assert resolved == project.resolve()


def test_validate_project_path_rejects_missing(tmp_path: Path) -> None:
    """A path that doesn't exist must not pass."""
    from leagent.project.paths import (
        ProjectPathSafetyError,
        validate_project_path,
    )

    ghost = tmp_path / "does-not-exist"
    with pytest.raises(ProjectPathSafetyError):
        validate_project_path(str(ghost))


def test_validate_project_path_rejects_relative(tmp_path: Path) -> None:
    from leagent.project.paths import (
        ProjectPathSafetyError,
        validate_project_path,
    )

    with pytest.raises(ProjectPathSafetyError):
        validate_project_path("relative/path")


def test_validate_project_path_honours_allowed_roots(
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path outside ``FILES_PROJECTS_ALLOWED_ROOTS`` is rejected."""
    from leagent.project import paths as safety_mod
    from leagent.project.paths import (
        ProjectPathSafetyError,
        validate_project_path,
    )

    sentinel_root = tmp_path / "sentinel-root"
    sentinel_root.mkdir()
    inner = sentinel_root / "inner"
    inner.mkdir()

    # Stub the live setting so we don't depend on the test env.
    monkeypatch.setattr(
        safety_mod,
        "get_allowed_project_roots",
        lambda: (sentinel_root.resolve(),),
    )

    # Inside the allowed root → ok.
    assert validate_project_path(str(inner)) == inner.resolve()

    # Outside the allowed root → rejected.
    with pytest.raises(ProjectPathSafetyError):
        validate_project_path(str(project))


@pytest.mark.asyncio
async def test_tool_argument_blob_blob_disk_path_and_discard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    project: Path,
) -> None:
    """Regression: _blob_disk_path must resolve upload_dir; discard always checks disk."""
    from leagent.tools.util.tool_argument_blob import (
        ToolArgumentBlobTool,
        _blob_disk_path,
    )

    fake_files = SimpleNamespace(upload_dir=str(tmp_path))
    fake_settings = SimpleNamespace(files=fake_files)
    monkeypatch.setattr("leagent.config.settings.get_settings", lambda: fake_settings)

    sid = "s-blob-disk"
    bid = "a" * 32
    resolved = _blob_disk_path(sid, bid)
    assert resolved is not None
    assert resolved == (tmp_path / "tool-argument-blobs" / sid / f"{bid}.bin").resolve()

    ctx = _ctx(project)
    tool = ToolArgumentBlobTool()
    created = await _run(tool, {"action": "create"}, ctx)
    assert created.success, created.error
    blob_id = str(created.data["blob_id"])
    discarded = await _run(
        tool,
        {"action": "discard", "blob_id": blob_id},
        ctx,
    )
    assert discarded.success, discarded.error
    assert discarded.data.get("ok") is True


# ---------------------------------------------------------------------------
# Operation-model compatibility: tool returns validate as typed ops
# ---------------------------------------------------------------------------


def _ctx_with_journal(project: Path) -> ToolContext:
    """Build a tool context with an OperationJournal attached."""
    from leagent.code.operations import JOURNAL_CONTEXT_KEY, OperationJournal

    return ToolContext(
        user_id="u1",
        session_id="s1",
        extra={
            "project_roots": [str(project)],
            JOURNAL_CONTEXT_KEY: OperationJournal(),
        },
    )


@pytest.mark.asyncio
async def test_project_write_result_validates_as_file_write_op(project: Path) -> None:
    """project_write return dict must be a valid FileWriteOp."""
    from leagent.code.operations import FileWriteOp
    from leagent.project.tools.write import ProjectWriteTool

    ctx = _ctx_with_journal(project)
    res = await _run(ProjectWriteTool(), {
        "path": "src/new_mod.py",
        "content": "X = 1\n",
    }, ctx)
    assert res.success, res.error
    op = FileWriteOp.model_validate(res.data)
    assert op.path == "src/new_mod.py"
    assert op.created is True
    assert op.bytes_written > 0


@pytest.mark.asyncio
async def test_project_edit_result_validates_as_file_edit_op(project: Path) -> None:
    """project_edit return dict must be a valid FileEditOp."""
    from leagent.code.operations import FileEditOp
    from leagent.project.tools.edit import ProjectEditTool

    ctx = _ctx_with_journal(project)
    res = await _run(ProjectEditTool(), {
        "path": "src/app.py",
        "old_string": "def greet(name: str) -> str:",
        "new_string": "def greet(name: str, greeting: str = 'hello') -> str:",
    }, ctx)
    assert res.success, res.error
    op = FileEditOp.model_validate(res.data)
    assert op.path == "src/app.py"
    assert op.replacements == 1


@pytest.mark.asyncio
async def test_project_write_records_journal_entry(project: Path) -> None:
    """project_write must append a JournalEntry to the operation journal."""
    from leagent.code.operations import JOURNAL_CONTEXT_KEY, OperationJournal
    from leagent.project.tools.write import ProjectWriteTool

    ctx = _ctx_with_journal(project)
    journal: OperationJournal = ctx.extra[JOURNAL_CONTEXT_KEY]
    assert len(journal) == 0

    await _run(ProjectWriteTool(), {
        "path": "src/journaled.py",
        "content": "Y = 2\n",
    }, ctx)
    assert len(journal) == 1
    entry = journal.recent()[0]
    assert entry.tool == "project_write"
    assert entry.kind == "file_write"
    assert entry.path == "src/journaled.py"


@pytest.mark.asyncio
async def test_project_edit_records_journal_entry(project: Path) -> None:
    """project_edit must append a JournalEntry to the operation journal."""
    from leagent.code.operations import JOURNAL_CONTEXT_KEY, OperationJournal
    from leagent.project.tools.edit import ProjectEditTool

    ctx = _ctx_with_journal(project)
    journal: OperationJournal = ctx.extra[JOURNAL_CONTEXT_KEY]

    await _run(ProjectEditTool(), {
        "path": "src/app.py",
        "old_string": "def greet(name: str) -> str:",
        "new_string": "def greet(name: str) -> str:  # updated",
    }, ctx)
    assert len(journal) == 1
    entry = journal.recent()[0]
    assert entry.tool == "project_edit"
    assert entry.kind == "file_edit"
