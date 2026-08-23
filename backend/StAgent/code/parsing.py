"""Lightweight code parsing for project navigation.

The parser is intentionally dependency-free. Python uses ``ast`` for
syntax-aware extraction; TypeScript and JavaScript use a conservative
comment-aware scanner that finds common declarations, imports, and exports.
It is a navigation aid, not a language server or type checker.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from collections.abc import Iterable

CodeLanguage = Literal["python", "typescript", "javascript", "unknown"]


class CodeDiagnostic(TypedDict, total=False):
    message: str
    severity: str
    line: int
    column: int
    end_line: int | None
    end_column: int | None
    offset: int | None
    code: str


class CodeSymbol(TypedDict, total=False):
    kind: str
    name: str
    line: int
    end_line: int | None
    container: str
    decorators: list[str]
    bases: list[str]
    exported: bool


class CodeImport(TypedDict, total=False):
    kind: str
    module: str | None
    names: list[str]
    line: int
    end_line: int
    aliases: list[dict[str, str | None]]
    declaration_kind: str


class CodeParseResult(TypedDict, total=False):
    path: str
    language: CodeLanguage
    symbols: list[CodeSymbol]
    imports: list[CodeImport]
    exports: list[CodeImport]
    diagnostics: list[CodeDiagnostic]
    line_count: int
    char_count: int
    parse_error: str
    lineno: int | None
    offset: int | None


class _ClassFrame(TypedDict):
    name: str
    start_depth: int


class _FunctionFrame(TypedDict):
    start_depth: int


@dataclass(frozen=True)
class _TsJsStatement:
    line: int
    end_line: int
    text: str

SUPPORTED_CODE_SUFFIXES: frozenset[str] = frozenset({
    ".py",
    ".pyw",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
})

_TS_JS_KEYWORDS = frozenset({
    "catch",
    "do",
    "for",
    "function",
    "if",
    "return",
    "switch",
    "while",
})

_IDENT = r"[A-Za-z_$][\w$]*"
_MEMBER_IDENT = rf"#?{_IDENT}"
_TS_JS_DECL_MODIFIERS = r"(?:(?:declare|abstract|async)\s+)*"
_TS_JS_CLASS_MODIFIERS = r"(?:(?:declare|abstract)\s+)*"
_TS_JS_MEMBER_MODIFIERS = r"(?:(?:public|private|protected|static|async|abstract|override|readonly)\s+)*"
_TS_JS_ACCESSOR_MODIFIERS = r"(?:(?:public|private|protected|static|abstract|override)\s+)*"
_TS_NAMESPACE_NAME = rf"{_IDENT}(?:\.{_IDENT})*"
_TS_MODULE_NAME = rf"(?:{_TS_NAMESPACE_NAME}|['\"][^'\"]+['\"])"
_TS_JS_WRAPPED_FUNCTION_CALLEES = frozenset({
    "forwardRef",
    "lazy",
    "memo",
    "observer",
})
_PYTHON_SUFFIXES = frozenset({".py", ".pyw"})
_TYPESCRIPT_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".cts"})
_JAVASCRIPT_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs"})


def detect_code_language(path: str | None = None, content: str | None = None) -> CodeLanguage:
    """Detect the code language from path suffix and light content hints."""

    suffix = _suffix(path)
    if suffix in _PYTHON_SUFFIXES:
        return "python"
    if suffix in _TYPESCRIPT_SUFFIXES:
        return "typescript"
    if suffix in _JAVASCRIPT_SUFFIXES:
        return "javascript"

    head = (content or "").lstrip("\ufeff\u200b\u200c\u200d\u2060")[:256]
    if head.startswith("#!") and "python" in head.splitlines()[0].lower():
        return "python"
    if re.search(r"\b(import|export)\s+[\w{*]", head):
        return "javascript"
    return "unknown"


def parse_code_source(
    source: str,
    *,
    path: str = "<string>",
    language: CodeLanguage | Literal["auto"] = "auto",
) -> CodeParseResult:
    """Parse source into a compact symbol/import/export outline."""

    selected = detect_code_language(path, source) if language == "auto" else language
    if selected == "python":
        return _parse_python_source(source, path=path)
    if selected == "typescript" or selected == "javascript":
        return _parse_ts_js_source(source, path=path, language=selected)
    return _base_result(
        path=path,
        language="unknown",
        source=source,
        diagnostics=[
            _diagnostic(
                message="Unsupported code language.",
                line=1,
                column=1,
                code="unsupported_language",
            )
        ],
    )


def _parse_python_source(source: str, *, path: str) -> CodeParseResult:
    symbols: list[CodeSymbol] = []
    value_symbols: list[CodeSymbol] = []
    imports: list[CodeImport] = []
    exports: list[CodeImport] = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        line = int(getattr(exc, "lineno", None) or 1)
        column = int(getattr(exc, "offset", None) or 1)
        message = str(exc)
        result = _base_result(
            path=path,
            language="python",
            source=source,
            diagnostics=[
                _diagnostic(
                    message=message,
                    line=line,
                    column=column,
                    code="python_syntax_error",
                )
            ],
        )
        result.update(
            {
                "parse_error": message,
                "lineno": getattr(exc, "lineno", None),
                "offset": getattr(exc, "offset", None),
            }
        )
        return result

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(_python_imports(node))
        elif isinstance(node, ast.ImportFrom):
            imports.append(_python_import_from(node))

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.FunctionDef):
            symbols.append(_python_symbol("function", node.name, node))
        elif isinstance(node, ast.AsyncFunctionDef):
            symbols.append(_python_symbol("async_function", node.name, node))
        elif isinstance(node, ast.ClassDef):
            symbols.append(
                _python_symbol(
                    "class",
                    node.name,
                    node,
                    bases=_python_base_names(node.bases),
                )
            )
            symbols.extend(_python_class_methods(node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            all_names = _python_all_names(node)
            if all_names:
                exports.append(
                    {
                        "kind": "__all__",
                        "names": all_names,
                        "line": int(getattr(node, "lineno", 1)),
                        "end_line": _node_end_line(node) or int(getattr(node, "lineno", 1)),
                    }
                )
            else:
                value_symbols.extend(_python_assignment_symbols(node))

    exported_names = {
        name
        for item in exports
        if item.get("kind") == "__all__"
        for name in item.get("names", [])
        if isinstance(name, str)
    }
    if exported_names:
        existing_names = {
            name
            for symbol in symbols
            if isinstance((name := symbol.get("name")), str) and "." not in name
        }
        symbols.extend(
            _python_exported_value_symbols(
                value_symbols,
                exported_names,
                existing_names=existing_names,
            )
        )
        symbols = _mark_python_exported_symbols(symbols, exported_names)

    result = _base_result(path=path, language="python", source=source)
    result.update(
        {
            "symbols": _stable_symbols(symbols),
            "imports": _stable_imports(imports),
            "exports": _stable_imports(exports),
        }
    )
    return result


def _parse_ts_js_source(
    source: str,
    *,
    path: str,
    language: Literal["typescript", "javascript"],
) -> CodeParseResult:
    symbols: list[CodeSymbol] = []
    imports: list[CodeImport] = []
    exports: list[CodeImport] = []
    cleaned = _strip_comments_preserve_lines(source)
    class_stack: list[_ClassFrame] = []
    function_stack: list[_FunctionFrame] = []
    pending_decorators: list[str] = []
    brace_depth = 0

    for statement in _ts_js_statements(cleaned):
        class_stack = _active_classes(class_stack, brace_depth)
        function_stack = _active_function_blocks(function_stack, brace_depth)
        inside_function_block = bool(function_stack)

        if not inside_function_block and _is_ts_js_decorator_statement(statement.text):
            pending_decorators.extend(_ts_js_decorator_names(statement.text))
            brace_depth += _brace_delta(statement.text)
            continue

        imports.extend(_ts_js_imports(statement.text, statement.line, statement.end_line))
        line_exports = (
            []
            if inside_function_block
            else _ts_js_exports(statement.text, statement.line, statement.end_line)
        )
        exports.extend(line_exports)
        exported_names = {
            name
            for item in line_exports
            if item.get("kind") in {"declaration", "default"}
            for name in item.get("names", [])
        }

        if not inside_function_block:
            symbol_count_before = len(symbols)
            decorators = pending_decorators.copy()
            symbols.extend(_ts_js_default_symbols(statement.text, statement.line, decorators))
            class_match = re.match(
                rf"^(?:export\s+default\s+|export\s+)?"
                rf"{_TS_JS_CLASS_MODIFIERS}class\s+(?P<name>{_IDENT})\b",
                statement.text,
            )
            if class_match and class_match.group("name") not in {"extends", "implements"}:
                name = class_match.group("name")
                symbols.append(
                    _symbol(
                        kind="class",
                        name=name,
                        line=statement.line,
                        bases=_ts_js_class_bases(statement.text),
                        decorators=decorators,
                        exported=name in exported_names or statement.text.startswith("export "),
                    )
                )
                if "{" in statement.text:
                    class_stack.append({"name": name, "start_depth": brace_depth})
            elif _is_anonymous_default_class(statement.text) and "{" in statement.text:
                class_stack.append({"name": "default", "start_depth": brace_depth})

            if language == "typescript":
                symbols.extend(_ts_symbols(statement.text, statement.line, exported_names))

            symbols.extend(
                _ts_js_functions(
                    statement.text,
                    statement.line,
                    exported_names,
                    class_stack,
                    decorators,
                )
            )
            if not class_stack:
                symbols.extend(
                    _ts_js_exported_value_symbols(
                        statement.text,
                        statement.line,
                        line_exports,
                        decorators,
                    )
                )
            if len(symbols) > symbol_count_before or pending_decorators:
                pending_decorators = []
            if _opens_function_block(statement.text, class_stack) and _brace_delta(statement.text) > 0:
                function_stack.append({"start_depth": brace_depth})

        brace_depth += _brace_delta(statement.text)
        class_stack = _active_classes(class_stack, brace_depth)
        function_stack = _active_function_blocks(function_stack, brace_depth)

    result = _base_result(path=path, language=language, source=source)
    result.update(
        {
            "symbols": _stable_symbols(symbols),
            "imports": _stable_imports(imports),
            "exports": _stable_imports(exports),
        }
    )
    return result


def _base_result(
    *,
    path: str,
    language: CodeLanguage,
    source: str,
    diagnostics: list[CodeDiagnostic] | None = None,
) -> CodeParseResult:
    return {
        "path": path,
        "language": language,
        "symbols": [],
        "imports": [],
        "exports": [],
        "diagnostics": diagnostics or [],
        "line_count": len(source.splitlines()) if source else 0,
        "char_count": len(source),
    }


def _diagnostic(
    *,
    message: str,
    line: int,
    column: int,
    code: str,
    severity: str = "error",
) -> CodeDiagnostic:
    return {
        "message": message,
        "severity": severity,
        "line": line,
        "column": column,
        "end_line": None,
        "end_column": None,
        "offset": None,
        "code": code,
    }


def _symbol(
    *,
    kind: str,
    name: str,
    line: int,
    end_line: int | None = None,
    container: str | None = None,
    decorators: list[str] | None = None,
    bases: list[str] | None = None,
    exported: bool = False,
) -> CodeSymbol:
    payload: CodeSymbol = {
        "kind": kind,
        "name": name,
        "line": line,
        "end_line": end_line,
    }
    if container:
        payload["container"] = container
    if decorators:
        payload["decorators"] = decorators
    if bases:
        payload["bases"] = bases
    if exported:
        payload["exported"] = True
    return payload


def _python_symbol(
    kind: str,
    name: str,
    node: ast.AST,
    *,
    container: str | None = None,
    bases: list[str] | None = None,
) -> CodeSymbol:
    return _symbol(
        kind=kind,
        name=f"{container}.{name}" if container else name,
        line=int(getattr(node, "lineno", 1)),
        end_line=_node_end_line(node),
        container=container,
        decorators=_python_decorator_names(node),
        bases=bases,
    )


def _python_class_methods(node: ast.ClassDef) -> list[CodeSymbol]:
    out: list[CodeSymbol] = []
    for child in node.body:
        if isinstance(child, ast.FunctionDef):
            decorators = _python_decorator_names(child)
            out.append(
                _python_symbol(
                    _python_method_kind(child.name, decorators, is_async=False),
                    child.name,
                    child,
                    container=node.name,
                )
            )
        elif isinstance(child, ast.AsyncFunctionDef):
            decorators = _python_decorator_names(child)
            out.append(
                _python_symbol(
                    _python_method_kind(child.name, decorators, is_async=True),
                    child.name,
                    child,
                    container=node.name,
                )
            )
    return out


def _python_assignment_symbols(node: ast.Assign | ast.AnnAssign) -> list[CodeSymbol]:
    names = _python_assignment_target_names(node)
    return [
        _symbol(
            kind=_python_value_symbol_kind(name),
            name=name,
            line=int(getattr(node, "lineno", 1)),
            end_line=_node_end_line(node),
        )
        for name in names
        if name != "__all__"
    ]


def _python_assignment_target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return _unique_nonempty(
        name
        for target in targets
        for name in _python_target_names(target)
    )


def _python_target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [
            name
            for child in node.elts
            for name in _python_target_names(child)
        ]
    return []


def _python_value_symbol_kind(name: str) -> str:
    if any(char.isalpha() for char in name) and name.upper() == name:
        return "constant"
    return "variable"


def _python_method_kind(name: str, decorators: list[str], *, is_async: bool) -> str:
    if any(_is_python_property_setter(name, decorator) for decorator in decorators):
        return "setter"
    if any(_is_python_property_getter(decorator) for decorator in decorators):
        return "getter"
    return "async_method" if is_async else "method"


def _python_decorator_names(node: ast.AST) -> list[str]:
    decorators = getattr(node, "decorator_list", None)
    if not isinstance(decorators, list):
        return []
    return [
        name
        for decorator in decorators
        if (name := _python_decorator_name(decorator))
    ]


def _python_decorator_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _python_decorator_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _python_decorator_name(node.func)
    if isinstance(node, ast.Subscript):
        return _python_decorator_name(node.value)
    return None


def _python_base_names(nodes: list[ast.expr]) -> list[str]:
    return [
        name
        for node in nodes
        if (name := _python_base_name(node))
    ]


def _python_base_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _python_base_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Subscript):
        return _python_base_name(node.value)
    if isinstance(node, ast.Call):
        return _python_base_name(node.func)
    return None


def _is_python_property_getter(decorator: str) -> bool:
    return decorator in {"property", "cached_property"} or decorator.endswith(
        (".property", ".cached_property")
    )


def _is_python_property_setter(name: str, decorator: str) -> bool:
    return decorator == f"{name}.setter"


def _python_exported_value_symbols(
    symbols: list[CodeSymbol],
    exported_names: set[str],
    *,
    existing_names: set[str],
) -> list[CodeSymbol]:
    out: list[CodeSymbol] = []
    for symbol in symbols:
        name = symbol.get("name")
        if (
            isinstance(name, str)
            and name in exported_names
            and name not in existing_names
        ):
            exported_symbol = symbol.copy()
            exported_symbol["exported"] = True
            out.append(exported_symbol)
    return out


def _mark_python_exported_symbols(
    symbols: list[CodeSymbol],
    exported_names: set[str],
) -> list[CodeSymbol]:
    marked: list[CodeSymbol] = []
    for symbol in symbols:
        name = symbol.get("name")
        if isinstance(name, str) and "." not in name and name in exported_names:
            exported_symbol = symbol.copy()
            exported_symbol["exported"] = True
            marked.append(exported_symbol)
            continue
        marked.append(symbol)
    return marked


def _node_end_line(node: ast.AST) -> int | None:
    end = getattr(node, "end_lineno", None)
    return int(end) if isinstance(end, int) else None


def _python_imports(node: ast.Import) -> list[CodeImport]:
    out: list[CodeImport] = []
    for alias in node.names:
        entry: CodeImport = {
            "kind": "import",
            "module": None,
            "names": [alias.name],
            "line": int(node.lineno),
            "end_line": _node_end_line(node) or int(node.lineno),
        }
        if alias.asname:
            entry["aliases"] = [{"name": alias.name, "asname": alias.asname}]
        out.append(entry)
    return out


def _python_import_from(node: ast.ImportFrom) -> CodeImport:
    names = [a.name for a in node.names]
    module = f"{'.' * int(node.level or 0)}{node.module or ''}" or None
    entry: CodeImport = {
        "kind": "from",
        "module": module,
        "names": names,
        "line": int(node.lineno),
        "end_line": _node_end_line(node) or int(node.lineno),
    }
    aliases: list[dict[str, str | None]] = [
        {"name": alias.name, "asname": alias.asname}
        for alias in node.names
        if alias.asname
    ]
    if aliases:
        entry["aliases"] = aliases
    return entry


def _python_all_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    target: ast.expr | None = None
    value: ast.expr | None
    if isinstance(node, ast.Assign):
        for candidate in node.targets:
            if isinstance(candidate, ast.Name) and candidate.id == "__all__":
                target = candidate
                break
        value = node.value
    else:
        if isinstance(node.target, ast.Name) and node.target.id == "__all__":
            target = node.target
        value = node.value

    if target is None or not isinstance(value, (ast.List, ast.Tuple)):
        return []
    return [
        item.value
        for item in value.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def _ts_js_statements(source: str) -> list[_TsJsStatement]:
    statements: list[_TsJsStatement] = []
    parts: list[str] = []
    start_line = 1
    paren_depth = 0
    bracket_depth = 0

    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if not parts:
            start_line = lineno

        parts.append(stripped)
        text = _normalize_statement(" ".join(parts))
        paren_depth += _nesting_delta(stripped, "(", ")")
        bracket_depth += _nesting_delta(stripped, "[", "]")

        if _statement_complete(text, stripped, paren_depth, bracket_depth):
            statements.append(_TsJsStatement(start_line, lineno, text))
            parts = []
            paren_depth = 0
            bracket_depth = 0

    if parts:
        statements.append(
            _TsJsStatement(
                start_line,
                len(source.splitlines()) or start_line,
                _normalize_statement(" ".join(parts)),
            )
        )
    return statements


def _normalize_statement(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _statement_complete(
    text: str,
    last_line: str,
    paren_depth: int,
    bracket_depth: int,
) -> bool:
    if paren_depth > 0 or bracket_depth > 0:
        return False
    if text.startswith("@"):
        return True
    if last_line.startswith("}") or last_line.endswith(";"):
        return True
    if re.match(r"^import\s+['\"]", text):
        return True
    if re.match(r"^(?:export\s+)?import\s+[^=]+=\s*require\s*\(", text):
        return True
    if re.match(r"^import\b", text) and re.search(r"\sfrom\s+['\"][^'\"]+['\"]\s*;?$", text):
        return True
    if re.match(r"^export\s+(?:type\s+)?\{", text) and "}" in text:
        return True
    if _declaration_opens_block(text):
        return True
    return _looks_complete_without_semicolon(text, last_line)


def _declaration_opens_block(text: str) -> bool:
    if "{" not in text:
        return False
    return bool(
        re.match(
            rf"^(?:export\s+default\s+|export\s+)?(?:async\s+)?"
            rf"{_TS_JS_DECL_MODIFIERS}(?:class|function|interface|enum)\s+{_IDENT}\b",
            text,
        )
        or re.match(
            rf"^export\s+default\s+(?:{_TS_JS_DECL_MODIFIERS}function|"
            rf"{_TS_JS_CLASS_MODIFIERS}class)\b",
            text,
        )
        or re.match(
            rf"^export\s+default\s+(?:async\s*)?(?:\([^)]*\)|{_IDENT})\s*=>\s*\{{",
            text,
        )
        or re.match(
            rf"^(?:export\s+)?{_TS_JS_CLASS_MODIFIERS}"
            rf"(?:namespace|module)\s+{_TS_MODULE_NAME}",
            text,
        )
        or re.match(
            rf"^{_TS_JS_MEMBER_MODIFIERS}{_MEMBER_IDENT}\s*(?:<[^>]+>)?\s*\(",
            text,
        )
        or re.match(
            rf"^{_TS_JS_ACCESSOR_MODIFIERS}(?:get|set)\s+{_MEMBER_IDENT}\s*\(",
            text,
        )
        or re.match(
            rf"^(?:export\s+)?(?:const|let|var)\s+{_IDENT}\b.*=>\s*\{{",
            text,
        )
        or re.match(
            rf"^{_TS_JS_MEMBER_MODIFIERS}"
            rf"{_MEMBER_IDENT}\s*(?::[^=]+)?=\s*(?:async\s*)?"
            rf"(?:function\b|(?:\([^)]*\)|{_IDENT})\s*=>\s*\{{)",
            text,
        )
    )


def _looks_complete_without_semicolon(text: str, last_line: str) -> bool:
    if last_line.endswith((",", "=", "=>", "(", "{", "[", "|", "&")):
        return False
    return bool(
        re.match(r"^export\s+(?:default\s+)?(?:async\s+)?function\b", text)
        or re.match(
            rf"^export\s+default\s+(?:async\s*)?(?:\([^)]*\)|{_IDENT})\s*=>",
            text,
        )
        or re.match(rf"^(?:export\s+)?(?:const|let|var)\s+{_IDENT}\b.*(?:=>|=\s*function\b)", text)
        or _is_ts_js_wrapped_function_variable(text)
        or re.match(r"^(?:const|let|var)\s+.+?=\s*require\s*\(", text)
        or re.match(rf"^(?:const|let|var)\s+{_IDENT}\b.*\bimport\s*\(", text)
        or re.match(r"^(?:await\s+)?import\s*\(", text)
        or re.match(r"^require\s*\(", text)
        or re.match(r"^(?:module\.)?exports(?:\.[A-Za-z_$][\w$]*)?\s*=", text)
        or re.match(r"^export\s*=", text)
        or re.match(rf"^(?:export\s+)?type\s+{_IDENT}\b.*=", text)
        or re.match(rf"^(?:export\s+)?(?:interface|enum)\s+{_IDENT}\b", text)
        or re.match(
            rf"^(?:export\s+)?{_TS_JS_CLASS_MODIFIERS}"
            rf"(?:namespace|module)\s+{_TS_MODULE_NAME}",
            text,
        )
        or re.match(rf"^export\s+default\s+{_TS_JS_CLASS_MODIFIERS}class\b", text)
        or re.match(rf"^(?:export\s+default\s+|export\s+)?class\s+{_IDENT}\b", text)
    )


def _ts_js_imports(line: str, lineno: int, end_line: int | None = None) -> list[CodeImport]:
    resolved_end_line = end_line or lineno
    import_equals = re.match(
        rf"^(?:export\s+)?import\s+(?P<name>{_IDENT})\s*=\s*"
        r"require\s*\(\s*['\"](?P<module>[^'\"]+)['\"]\s*\)",
        line,
    )
    if import_equals:
        return [
            {
                "kind": "import_equals",
                "module": import_equals.group("module"),
                "names": [import_equals.group("name")],
                "line": lineno,
                "end_line": resolved_end_line,
            }
        ]

    side_effect = re.match(r"^import\s+['\"](?P<module>[^'\"]+)['\"]", line)
    if side_effect:
        return [
            {
                "kind": "side_effect",
                "module": side_effect.group("module"),
                "names": [],
                "line": lineno,
                "end_line": resolved_end_line,
            }
        ]

    match = re.match(
        r"^import\s+(?P<type>type\s+)?(?P<body>.+?)\s+from\s+['\"](?P<module>[^'\"]+)['\"]",
        line,
    )
    if match:
        body = match.group("body").strip()
        names, aliases = _split_ts_js_names_with_aliases(body)
        entry: CodeImport = {
            "kind": "import_type" if match.group("type") else "import",
            "module": match.group("module"),
            "names": names,
            "line": lineno,
            "end_line": resolved_end_line,
        }
        if aliases:
            entry["aliases"] = aliases
        return [entry]

    require_calls = _find_module_calls(line, callee="require")
    dynamic_imports = _find_module_calls(line, callee="import")
    out: list[CodeImport] = []
    if require_calls:
        out.extend(_ts_js_require_imports(line, lineno, require_calls, end_line=resolved_end_line))
    for module, _start in dynamic_imports:
        out.append(
            {
                "kind": "dynamic_import",
                "module": module,
                "names": [],
                "line": lineno,
                "end_line": resolved_end_line,
            }
        )
    return out


def _ts_js_require_imports(
    line: str,
    lineno: int,
    require_calls: list[tuple[str, int]],
    *,
    end_line: int | None = None,
) -> list[CodeImport]:
    resolved_end_line = end_line or lineno
    module, start = require_calls[0]
    declaration = re.match(
        r"^(?:const|let|var)\s+(?P<body>.+?)\s*=\s*$",
        line[:start],
    )
    if declaration:
        names, aliases = _split_cjs_require_bindings(declaration.group("body"))
        entry: CodeImport = {
            "kind": "require",
            "module": module,
            "names": names,
            "line": lineno,
            "end_line": resolved_end_line,
        }
        if aliases:
            entry["aliases"] = aliases
        return [entry]

    return [
        {
            "kind": "require",
            "module": module,
            "names": [],
            "line": lineno,
            "end_line": resolved_end_line,
        }
        for module, _start in require_calls
    ]


def _ts_js_exports(line: str, lineno: int, end_line: int | None = None) -> list[CodeImport]:
    resolved_end_line = end_line or lineno
    commonjs = _ts_js_commonjs_exports(line, lineno, end_line=resolved_end_line)
    if commonjs:
        return commonjs

    if not line.startswith("export"):
        return []

    import_equals = re.match(
        rf"^export\s+import\s+(?P<name>{_IDENT})\s*=\s*"
        r"require\s*\(\s*['\"](?P<module>[^'\"]+)['\"]\s*\)",
        line,
    )
    if import_equals:
        return [
            {
                "kind": "export_import_equals",
                "module": import_equals.group("module"),
                "names": [import_equals.group("name")],
                "line": lineno,
                "end_line": resolved_end_line,
            }
        ]

    equals_match = re.match(rf"^export\s*=\s*(?P<name>{_IDENT})\s*;?$", line)
    if equals_match:
        return [
            {
                "kind": "export_equals",
                "names": [equals_match.group("name")],
                "line": lineno,
                "end_line": resolved_end_line,
            }
        ]

    all_match = re.match(
        rf"^export\s+(?P<type>type\s+)?\*\s+"
        rf"(?:(?:as\s+(?P<name>{_IDENT})\s+))?"
        r"from\s+['\"](?P<module>[^'\"]+)['\"]",
        line,
    )
    if all_match:
        name = all_match.group("name")
        type_prefix = bool(all_match.group("type"))
        return [
            {
                "kind": (
                    "namespace_type"
                    if name and type_prefix
                    else "namespace"
                    if name
                    else "all_type"
                    if type_prefix
                    else "all"
                ),
                "module": all_match.group("module"),
                "names": [name or "*"],
                "line": lineno,
                "end_line": resolved_end_line,
            }
        ]

    named_match = re.match(
        r"^export\s+(?P<type>type\s+)?\{(?P<body>[^}]+)\}(?:\s+from\s+['\"](?P<module>[^'\"]+)['\"])?",
        line,
    )
    if named_match:
        names, aliases = _split_export_names_with_aliases(named_match.group("body"))
        entry: CodeImport = {
            "kind": "named_type" if named_match.group("type") else "named",
            "module": named_match.group("module"),
            "names": names,
            "line": lineno,
            "end_line": resolved_end_line,
        }
        if aliases:
            entry["aliases"] = aliases
        return [
            entry
        ]

    decl_match = re.match(
        rf"^export\s+(?:default\s+)?{_TS_JS_DECL_MODIFIERS}"
        rf"(?P<kind>class|function|interface|type|enum|const|let|var|namespace|module)\s+"
        rf"(?P<name>{_TS_MODULE_NAME})",
        line,
    )
    if decl_match:
        return [
            {
                "kind": "default" if line.startswith("export default") else "declaration",
                "declaration_kind": decl_match.group("kind"),
                "names": [_strip_ts_module_name(decl_match.group("name"))],
                "line": lineno,
                "end_line": resolved_end_line,
            }
        ]

    if line.startswith("export default"):
        return [
            {
                "kind": "default",
                "names": ["default"],
                "line": lineno,
                "end_line": resolved_end_line,
            }
        ]
    return []


def _ts_js_commonjs_exports(
    line: str,
    lineno: int,
    *,
    end_line: int | None = None,
) -> list[CodeImport]:
    resolved_end_line = end_line or lineno
    member_match = re.match(
        rf"^(?:module\.)?exports\.(?P<name>{_IDENT})\s*=",
        line,
    )
    if member_match:
        return [
            {
                "kind": "commonjs",
                "names": [member_match.group("name")],
                "line": lineno,
                "end_line": resolved_end_line,
            }
        ]

    module_match = re.match(r"^module\.exports\s*=\s*(?P<body>.+?)\s*;?$", line)
    if not module_match:
        return []

    body = module_match.group("body").strip()
    require_calls = _find_module_calls(body, callee="require")
    if require_calls and body.startswith("require"):
        return [
            {
                "kind": "commonjs_reexport",
                "module": require_calls[0][0],
                "names": ["*"],
                "line": lineno,
                "end_line": resolved_end_line,
            }
        ]

    return [
        {
            "kind": "commonjs",
            "names": _commonjs_export_names(body),
            "line": lineno,
            "end_line": resolved_end_line,
        }
    ]


def _ts_symbols(line: str, lineno: int, exported_names: set[str]) -> list[CodeSymbol]:
    out: list[CodeSymbol] = []
    for kind in ("interface", "type", "enum"):
        match = re.match(
            rf"^(?:export\s+)?{_TS_JS_DECL_MODIFIERS}{kind}\s+(?P<name>{_IDENT})\b",
            line,
        )
        if match:
            name = match.group("name")
            out.append(
                _symbol(
                    kind=kind,
                    name=name,
                    line=lineno,
                    bases=_ts_js_interface_bases(line) if kind == "interface" else None,
                    exported=name in exported_names or line.startswith("export "),
                )
            )
    ns_match = re.match(
        rf"^(?:export\s+)?{_TS_JS_CLASS_MODIFIERS}"
        rf"(?P<kind>namespace|module)\s+(?P<name>{_TS_MODULE_NAME})",
        line,
    )
    if ns_match:
        kind = ns_match.group("kind")
        name = _strip_ts_module_name(ns_match.group("name"))
        out.append(
            _symbol(
                kind=kind,
                name=name,
                line=lineno,
                exported=name in exported_names or line.startswith("export "),
            )
        )
    return out


def _ts_js_functions(
    line: str,
    lineno: int,
    exported_names: set[str],
    class_stack: list[_ClassFrame],
    decorators: list[str] | None = None,
) -> list[CodeSymbol]:
    out: list[CodeSymbol] = []
    function_match = re.match(
        rf"^(?:export\s+default\s+|export\s+)?"
        rf"{_TS_JS_DECL_MODIFIERS}function\s+(?P<name>{_IDENT})\b",
        line,
    )
    if function_match:
        name = function_match.group("name")
        out.append(
            _symbol(
                kind="function",
                name=name,
                line=lineno,
                decorators=decorators,
                exported=name in exported_names or line.startswith("export "),
            )
        )

    variable_function_name = _ts_js_variable_function_name(line)
    if variable_function_name:
        name = variable_function_name
        out.append(
            _symbol(
                kind="function",
                name=name,
                line=lineno,
                decorators=decorators,
                exported=name in exported_names or line.startswith("export "),
            )
        )
    else:
        wrapped_function_name = _ts_js_wrapped_function_variable_name(line)
        if wrapped_function_name:
            out.append(
                _symbol(
                    kind="function",
                    name=wrapped_function_name,
                    line=lineno,
                    decorators=decorators,
                    exported=wrapped_function_name in exported_names or line.startswith("export "),
                )
            )

    if class_stack:
        accessor_match = re.match(
            rf"^{_TS_JS_ACCESSOR_MODIFIERS}(?P<accessor>get|set)\s+"
            rf"(?P<name>{_MEMBER_IDENT})\s*\(",
            line,
        )
        if accessor_match:
            container = str(class_stack[-1]["name"])
            out.append(
                _symbol(
                    kind="getter" if accessor_match.group("accessor") == "get" else "setter",
                    name=f"{container}.{accessor_match.group('name')}",
                    line=lineno,
                    container=container,
                    decorators=decorators,
                )
            )

        method_match = re.match(
            rf"^{_TS_JS_MEMBER_MODIFIERS}(?P<name>{_MEMBER_IDENT})"
            rf"\s*(?:<[^>]+>)?\s*\(",
            line,
        )
        if method_match:
            name = method_match.group("name")
            if name not in _TS_JS_KEYWORDS:
                container = str(class_stack[-1]["name"])
                out.append(
                    _symbol(
                        kind="method",
                        name=f"{container}.{name}",
                        line=lineno,
                        container=container,
                        decorators=decorators,
                    )
                )
        field_function_match = re.match(
            rf"^{_TS_JS_MEMBER_MODIFIERS}"
            rf"(?P<name>{_MEMBER_IDENT})\s*(?::[^=]+)?=\s*(?:async\s*)?"
            rf"(?:function\b|(?:\([^)]*\)|{_IDENT})\s*=>)",
            line,
        )
        if field_function_match:
            name = field_function_match.group("name")
            if name not in _TS_JS_KEYWORDS:
                container = str(class_stack[-1]["name"])
                out.append(
                    _symbol(
                        kind="method",
                        name=f"{container}.{name}",
                        line=lineno,
                        container=container,
                        decorators=decorators,
                    )
                )
    return out


def _ts_js_variable_function_name(line: str) -> str | None:
    match = re.match(
        rf"^(?:export\s+)?(?:const|let|var)\s+(?P<name>{_IDENT})\s*(?::[^=]+)?="
        rf"\s*(?:async\s*)?(?:function\b|(?:\([^)]*\)|{_IDENT})\s*=>)",
        line,
    )
    return match.group("name") if match else None


def _is_ts_js_wrapped_function_variable(line: str) -> bool:
    return _ts_js_wrapped_function_variable_name(line) is not None


def _ts_js_wrapped_function_variable_name(line: str) -> str | None:
    match = re.match(
        rf"^(?:export\s+)?(?:const|let|var)\s+(?P<name>{_IDENT})"
        rf"\s*(?::[^=]+)?=\s*(?P<rhs>.+)$",
        line,
    )
    if not match:
        return None

    rhs = match.group("rhs").strip()
    callee_match = re.match(
        rf"(?P<callee>{_IDENT}(?:\.{_IDENT})*)\s*(?:<[^()]+>)?\s*\(",
        rhs,
    )
    if not callee_match:
        return None

    callee = callee_match.group("callee")
    if callee.rsplit(".", 1)[-1] not in _TS_JS_WRAPPED_FUNCTION_CALLEES:
        return None
    if "=>" not in rhs and not re.search(r"\bfunction\b", rhs):
        return None
    return match.group("name")


def _ts_js_exported_value_symbols(
    line: str,
    lineno: int,
    line_exports: list[CodeImport],
    decorators: list[str] | None = None,
) -> list[CodeSymbol]:
    if _ts_js_variable_function_name(line) or _ts_js_wrapped_function_variable_name(line):
        return []

    out: list[CodeSymbol] = []
    for item in line_exports:
        if item.get("kind") != "declaration":
            continue
        declaration_kind = item.get("declaration_kind")
        if declaration_kind not in {"const", "let", "var"}:
            continue
        symbol_kind = "constant" if declaration_kind == "const" else "variable"
        out.extend(
            _symbol(
                kind=symbol_kind,
                name=name,
                line=lineno,
                decorators=decorators,
                exported=True,
            )
            for name in item.get("names", [])
            if isinstance(name, str)
        )
    return out


def _ts_js_default_symbols(
    line: str,
    lineno: int,
    decorators: list[str] | None = None,
) -> list[CodeSymbol]:
    if not line.startswith("export default"):
        return []

    body = line.removeprefix("export default").strip()
    kind = "value"
    if re.match(rf"^{_TS_JS_DECL_MODIFIERS}function\b", body) or re.match(
        rf"^(?:async\s*)?(?:\([^)]*\)|{_IDENT})\s*=>",
        body,
    ):
        kind = "function"
    elif re.match(rf"^{_TS_JS_CLASS_MODIFIERS}class\b", body):
        kind = "class"

    return [
        _symbol(
            kind=kind,
            name="default",
            line=lineno,
            bases=_ts_js_class_bases(line) if kind == "class" else None,
            decorators=decorators,
            exported=True,
        )
    ]


def _is_ts_js_decorator_statement(line: str) -> bool:
    return line.startswith("@")


def _ts_js_decorator_names(line: str) -> list[str]:
    return _unique_nonempty(
        match.group("name")
        for match in re.finditer(rf"@(?P<name>{_IDENT}(?:\.{_IDENT})*)", line)
    )


def _ts_js_class_bases(line: str) -> list[str]:
    bases: list[str] = []
    extends_match = re.search(
        r"\bextends\s+(?P<body>.+?)(?:\s+implements\b|[\{;]|$)",
        line,
    )
    if extends_match:
        bases.extend(_split_ts_js_type_references(extends_match.group("body")))

    implements_match = re.search(
        r"\bimplements\s+(?P<body>.+?)(?:[\{;]|$)",
        line,
    )
    if implements_match:
        bases.extend(_split_ts_js_type_references(implements_match.group("body")))
    return _unique_nonempty(bases)


def _ts_js_interface_bases(line: str) -> list[str]:
    extends_match = re.search(r"\bextends\s+(?P<body>.+?)(?:[\{;]|$)", line)
    if not extends_match:
        return []
    return _split_ts_js_type_references(extends_match.group("body"))


def _split_ts_js_type_references(body: str) -> list[str]:
    out: list[str] = []
    for item in _split_top_level_commas(body):
        cleaned = _clean_ts_js_type_reference(item)
        if cleaned:
            out.append(cleaned)
    return _unique_nonempty(out)


def _split_top_level_commas(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    angle = 0
    paren = 0
    bracket = 0
    for index, char in enumerate(value):
        if char == "<":
            angle += 1
        elif char == ">" and angle > 0:
            angle -= 1
        elif char == "(":
            paren += 1
        elif char == ")" and paren > 0:
            paren -= 1
        elif char == "[":
            bracket += 1
        elif char == "]" and bracket > 0:
            bracket -= 1
        elif char == "," and angle == 0 and paren == 0 and bracket == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _clean_ts_js_type_reference(value: str) -> str:
    cleaned = value.strip().strip("{};")
    return re.sub(r"\s+", " ", cleaned)


def _split_ts_js_names(body: str) -> list[str]:
    return _split_ts_js_names_with_aliases(body)[0]


def _split_ts_js_names_with_aliases(
    body: str,
) -> tuple[list[str], list[dict[str, str | None]]]:
    body = body.strip()
    if not body:
        return [], []
    names: list[str] = []
    aliases: list[dict[str, str | None]] = []

    default_part = body
    if "{" in body:
        default_part = body.split("{", 1)[0].strip().strip(",")
        named = body.split("{", 1)[1].split("}", 1)[0]
        named_names, named_aliases = _split_export_names_with_aliases(named)
        names.extend(named_names)
        aliases.extend(named_aliases)
    if default_part and not default_part.startswith("*"):
        for part in default_part.split(","):
            cleaned = part.strip()
            if cleaned:
                names.append(_name_without_alias(cleaned))
    star_match = re.search(r"\*\s+as\s+(?P<name>[A-Za-z_$][\w$]*)", body)
    if star_match:
        names.append(star_match.group("name"))

    return _unique_nonempty(names), aliases


def _split_cjs_require_bindings(
    body: str,
) -> tuple[list[str], list[dict[str, str | None]]]:
    body = body.strip()
    if not body:
        return [], []
    if body.startswith("{") and "}" in body:
        names: list[str] = []
        aliases: list[dict[str, str | None]] = []
        for part in body[1:body.rfind("}")].split(","):
            cleaned = part.strip()
            if not cleaned or cleaned.startswith("..."):
                continue
            if ":" in cleaned:
                source, local = [item.strip() for item in cleaned.split(":", 1)]
                local_name = _strip_js_binding_default(local)
                source_name = _strip_js_property_key(source)
                if local_name:
                    names.append(local_name)
                    aliases.append({"name": source_name, "asname": local_name})
                continue
            name = _strip_js_property_key(cleaned)
            if name:
                names.append(name)
        return _unique_nonempty(names), aliases

    match = re.match(rf"^(?P<name>{_IDENT})$", body)
    return ([match.group("name")] if match else []), []


def _split_export_names(body: str) -> list[str]:
    return _split_export_names_with_aliases(body)[0]


def _split_export_names_with_aliases(
    body: str,
) -> tuple[list[str], list[dict[str, str | None]]]:
    names: list[str] = []
    aliases: list[dict[str, str | None]] = []
    for part in body.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        parts = re.split(r"\s+as\s+", cleaned)
        if len(parts) > 1:
            source_name = _strip_ts_type_specifier(parts[0])
            exported_name = _strip_ts_type_specifier(parts[-1])
            if exported_name:
                names.append(exported_name)
                if source_name and source_name != exported_name:
                    aliases.append({"name": source_name, "asname": exported_name})
            continue
        name = _strip_ts_type_specifier(cleaned)
        if name:
            names.append(name)
    return _unique_nonempty(names), aliases


def _commonjs_export_names(body: str) -> list[str]:
    body = body.strip()
    if body.startswith("{") and "}" in body:
        names: list[str] = []
        for part in body[1:body.rfind("}")].split(","):
            cleaned = part.strip()
            if not cleaned or cleaned.startswith("..."):
                continue
            source = cleaned.split(":", 1)[0].strip()
            name = _strip_js_property_key(source)
            if name:
                names.append(name)
        return _unique_nonempty(names)

    named_decl = re.match(
        rf"^(?:async\s+)?(?:function|class)\s+(?P<name>{_IDENT})\b",
        body,
    )
    if named_decl:
        return [named_decl.group("name")]

    identifier = re.match(rf"^(?P<name>{_IDENT})$", body)
    if identifier:
        return [identifier.group("name")]
    return ["default"]


def _name_without_alias(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parts = re.split(r"\s+as\s+", value)
    return _strip_ts_type_specifier(parts[-1].strip())


def _strip_ts_type_specifier(value: str) -> str:
    return re.sub(r"^type\s+", "", value.strip())


def _strip_ts_module_name(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _strip_js_binding_default(value: str) -> str:
    return value.split("=", 1)[0].strip()


def _strip_js_property_key(value: str) -> str:
    value = _strip_js_binding_default(value).strip()
    if (value.startswith("'") and "'" in value[1:]) or (
        value.startswith('"') and '"' in value[1:]
    ):
        return value[1:].split(value[0], 1)[0]
    return value


def _find_module_calls(line: str, *, callee: str) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    state = "normal"
    quote = ""
    escaped = False
    i = 0
    while i < len(line):
        ch = line[i]

        if state == "normal":
            if ch in {"'", '"', "`"}:
                quote = ch
                escaped = False
                state = "string"
                i += 1
                continue
            if _is_word_at(line, i, callee):
                parsed = _parse_literal_module_call(line, i + len(callee))
                if parsed is not None:
                    module, end = parsed
                    calls.append((module, i))
                    i = end
                    continue
            i += 1
            continue

        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == quote:
            state = "normal"
        i += 1
    return calls


def _is_word_at(line: str, start: int, word: str) -> bool:
    end = start + len(word)
    if line[start:end] != word:
        return False
    before = line[start - 1] if start > 0 else ""
    after = line[end] if end < len(line) else ""
    return not (before.isalnum() or before in {"_", "$", "."}) and not (
        after.isalnum() or after in {"_", "$"}
    )


def _parse_literal_module_call(line: str, start: int) -> tuple[str, int] | None:
    i = _skip_spaces(line, start)
    if i >= len(line) or line[i] != "(":
        return None
    i = _skip_spaces(line, i + 1)
    if i >= len(line) or line[i] not in {"'", '"', "`"}:
        return None
    quote = line[i]
    i += 1
    chars: list[str] = []
    escaped = False
    while i < len(line):
        ch = line[i]
        if escaped:
            chars.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == quote:
            i = _skip_spaces(line, i + 1)
            if i < len(line) and line[i] == ")":
                return "".join(chars), i + 1
            return None
        else:
            chars.append(ch)
        i += 1
    return None


def _skip_spaces(text: str, start: int) -> int:
    i = start
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _unique_nonempty(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _stable_symbols(symbols: list[CodeSymbol]) -> list[CodeSymbol]:
    seen: set[tuple[str, str, int, str]] = set()
    out: list[CodeSymbol] = []
    for item in sorted(
        symbols,
        key=lambda row: (
            int(row.get("line") or 0),
            str(row.get("kind") or ""),
            str(row.get("name") or ""),
            str(row.get("container") or ""),
        ),
    ):
        key = (
            str(item.get("kind") or ""),
            str(item.get("name") or ""),
            int(item.get("line") or 0),
            str(item.get("container") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _stable_imports(entries: list[CodeImport]) -> list[CodeImport]:
    seen: set[tuple[str, str, tuple[str, ...], int]] = set()
    out: list[CodeImport] = []
    for item in sorted(
        entries,
        key=lambda row: (
            int(row.get("line") or 0),
            str(row.get("kind") or ""),
            str(row.get("module") or ""),
            tuple(row.get("names") or []),
        ),
    ):
        key = (
            str(item.get("kind") or ""),
            str(item.get("module") or ""),
            tuple(item.get("names") or []),
            int(item.get("line") or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _strip_comments_preserve_lines(source: str) -> str:
    out: list[str] = []
    state = "normal"
    quote = ""
    escaped = False
    i = 0
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""

        if state == "normal":
            if ch == "/" and nxt == "/":
                out.extend((" ", " "))
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                out.extend((" ", " "))
                state = "block_comment"
                i += 2
                continue
            if ch in {"'", '"', "`"}:
                quote = ch
                escaped = False
                state = "string"
            out.append(ch)
            i += 1
            continue

        if state == "string":
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                state = "normal"
            i += 1
            continue

        if state == "line_comment":
            if ch == "\n":
                out.append(ch)
                state = "normal"
            else:
                out.append(" ")
            i += 1
            continue

        if state == "block_comment":
            if ch == "*" and nxt == "/":
                out.extend((" ", " "))
                state = "normal"
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
            continue

    return "".join(out)


def _brace_delta(line: str) -> int:
    delta = 0
    state = "normal"
    quote = ""
    escaped = False
    for ch in line:
        if state == "normal":
            if ch in {"'", '"', "`"}:
                state = "string"
                quote = ch
            elif ch == "{":
                delta += 1
            elif ch == "}":
                delta -= 1
            continue

        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == quote:
            state = "normal"
    return delta


def _nesting_delta(line: str, open_ch: str, close_ch: str) -> int:
    delta = 0
    state = "normal"
    quote = ""
    escaped = False
    for ch in line:
        if state == "normal":
            if ch in {"'", '"', "`"}:
                state = "string"
                quote = ch
            elif ch == open_ch:
                delta += 1
            elif ch == close_ch:
                delta -= 1
            continue

        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == quote:
            state = "normal"
    return delta


def _active_classes(class_stack: list[_ClassFrame], brace_depth: int) -> list[_ClassFrame]:
    return [
        item
        for item in class_stack
        if brace_depth > item["start_depth"]
    ]


def _active_function_blocks(
    function_stack: list[_FunctionFrame],
    brace_depth: int,
) -> list[_FunctionFrame]:
    return [
        item
        for item in function_stack
        if brace_depth > item["start_depth"]
    ]


def _opens_function_block(line: str, class_stack: list[_ClassFrame]) -> bool:
    if re.match(
        rf"^(?:export\s+default\s+|export\s+)?"
        rf"{_TS_JS_DECL_MODIFIERS}function\b",
        line,
    ):
        return True
    if re.match(
        rf"^export\s+default\s+(?:async\s*)?(?:\([^)]*\)|{_IDENT})\s*=>",
        line,
    ):
        return True
    if re.match(
        rf"^(?:export\s+)?(?:const|let|var)\s+{_IDENT}\s*(?::[^=]+)?="
        rf"\s*(?:async\s*)?(?:function\b|(?:\([^)]*\)|{_IDENT})\s*=>)",
        line,
    ):
        return True
    if not class_stack:
        return False
    return bool(
        re.match(
            rf"^{_TS_JS_ACCESSOR_MODIFIERS}(?:get|set)\s+{_MEMBER_IDENT}\s*\(",
            line,
        )
        or re.match(
            rf"^{_TS_JS_MEMBER_MODIFIERS}{_MEMBER_IDENT}\s*(?:<[^>]+>)?\s*\(",
            line,
        )
        or re.match(
            rf"^{_TS_JS_MEMBER_MODIFIERS}{_MEMBER_IDENT}\s*(?::[^=]+)?="
            rf"\s*(?:async\s*)?(?:function\b|(?:\([^)]*\)|{_IDENT})\s*=>)",
            line,
        )
    )


def _is_anonymous_default_class(line: str) -> bool:
    match = re.match(
        rf"^export\s+default\s+{_TS_JS_CLASS_MODIFIERS}class(?:\s+(?P<next>[^\s{{]+))?",
        line,
    )
    if not match:
        return False
    next_token = match.group("next")
    return next_token is None or next_token in {"extends", "implements"}


def _suffix(path: str | None) -> str:
    if not path:
        return ""
    dot = path.rfind(".")
    return path[dot:].lower() if dot >= 0 else ""
