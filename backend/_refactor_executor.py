"""One-shot refactor: strip parsing funcs from executor.py, re-export them.

Deletes the JSON parsing/repair/recovery block (now in tool_args_parsing.py)
and the moved ``normalize_tool_parameters`` from executor.py, inserting a
re-export import so ``from leagent.tools.executor import X`` keeps working.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import leagent.tools.tool_args_parsing as tap

EXEC = Path("leagent/tools/executor.py")
lines = EXEC.read_text(encoding="utf-8").splitlines(keepends=True)

# Collect the public + private function names defined in the parsing module.
names = sorted(
    n
    for n, obj in inspect.getmembers(tap, inspect.isfunction)
    if getattr(obj, "__module__", None) == "leagent.tools.tool_args_parsing"
)
assert names, "no functions found in tool_args_parsing"

# Verify boundary lines (1-based) match expectations before slicing.
def at(lineno: int) -> str:
    return lines[lineno - 1]

assert at(60).startswith("_AUDIT_VALUE_LIMIT"), at(60)
assert at(63).startswith("def _strip_json_code_fence"), at(63)
assert at(1213).startswith("def _json_dumps_limited"), at(1213)
assert at(1218).startswith("def _uuid_or_none"), at(1218)
assert at(1227).startswith("def normalize_tool_parameters"), at(1227)
assert at(1257).startswith("# ----"), at(1257)

import_block = (
    "\n"
    "# JSON tool-argument parsing/repair/recovery lives in a dedicated module;\n"
    "# re-exported here so existing ``from leagent.tools.executor import ...`` and\n"
    "# tests that reach for these helpers keep working unchanged.\n"
    "from leagent.tools.tool_args_parsing import (  # noqa: E402\n"
    + "".join(f"    {n},\n" for n in names)
    + ")\n"
)

new_lines: list[str] = []
new_lines.extend(lines[0:60])          # 1..60  header/imports/SENSITIVE/_AUDIT
new_lines.append(import_block)
new_lines.append("\n")
new_lines.extend(lines[1212:1226])     # 1213..1226  _json_dumps_limited + _uuid_or_none
new_lines.extend(lines[1256:])         # 1257..end   dispatch comment + classes

EXEC.write_text("".join(new_lines), encoding="utf-8")
print("names re-exported:", len(names))
print("new line count:", len("".join(new_lines).splitlines()))
