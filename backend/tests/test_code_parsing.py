"""Tests for lightweight code parsing helpers."""

from __future__ import annotations

from StAgent.code.parsing import detect_code_language, parse_code_source


def test_detect_code_language_from_suffix() -> None:
    assert detect_code_language("app.py") == "python"
    assert detect_code_language("component.tsx") == "typescript"
    assert detect_code_language("module.mts") == "typescript"
    assert detect_code_language("module.cts") == "typescript"
    assert detect_code_language("view.jsx") == "javascript"
    assert detect_code_language("entry.mjs") == "javascript"
    assert detect_code_language("config.cjs") == "javascript"
    assert detect_code_language("README.md") == "unknown"


def test_parse_python_symbols_imports_methods_and_exports() -> None:
    source = "\n".join(
        [
            "import os",
            "from pathlib import Path as P",
            "",
            "__all__ = ['Worker', 'build']",
            "",
            "class Worker(BaseWorker, mixins.Runner):",
            "    def run(self) -> None:",
            "        pass",
            "",
            "async def build() -> None:",
            "    pass",
        ]
    )

    result = parse_code_source(source, path="src/app.py")

    assert result["language"] == "python"
    assert result["diagnostics"] == []
    assert result["imports"][0] == {
        "kind": "import",
        "module": None,
        "names": ["os"],
        "line": 1,
        "end_line": 1,
    }
    assert result["imports"][1]["module"] == "pathlib"
    symbols = {(item["kind"], item["name"]) for item in result["symbols"]}
    assert ("class", "Worker") in symbols
    assert ("method", "Worker.run") in symbols
    assert ("async_function", "build") in symbols
    symbols_by_name = {item["name"]: item for item in result["symbols"]}
    assert symbols_by_name["Worker"]["exported"] is True
    assert symbols_by_name["Worker"]["bases"] == ["BaseWorker", "mixins.Runner"]
    assert symbols_by_name["build"]["exported"] is True
    assert "exported" not in symbols_by_name["Worker.run"]
    assert result["exports"] == [
        {
            "kind": "__all__",
            "names": ["Worker", "build"],
            "line": 4,
            "end_line": 4,
        }
    ]
    assert result["line_count"] == 11
    assert result["char_count"] == len(source)


def test_parse_python_multiline_import_export_spans() -> None:
    source = "\n".join(
        [
            "from pathlib import (",
            "    Path,",
            "    PurePath,",
            ")",
            "__all__ = [",
            "    'Path',",
            "    'PurePath',",
            "]",
        ]
    )

    result = parse_code_source(source, path="src/paths.py")

    assert result["imports"][0]["line"] == 1
    assert result["imports"][0]["end_line"] == 4
    assert result["exports"][0]["line"] == 5
    assert result["exports"][0]["end_line"] == 8


def test_parse_python_decorated_methods_and_properties() -> None:
    source = "\n".join(
        [
            "from functools import cached_property",
            "",
            "class Settings:",
            "    @property",
            "    def value(self) -> str:",
            "        return 'ok'",
            "",
            "    @value.setter",
            "    def value(self, next_value: str) -> None:",
            "        self._value = next_value",
            "",
            "    @classmethod",
            "    def create(cls):",
            "        return cls()",
            "",
            "    @staticmethod",
            "    def normalize(value: str) -> str:",
            "        return value.strip()",
            "",
            "    @cached_property",
            "    def token(self) -> str:",
            "        return 'token'",
        ]
    )

    result = parse_code_source(source, path="src/settings.py")

    value_symbols = [
        item for item in result["symbols"] if item["name"] == "Settings.value"
    ]
    assert {(item["kind"], tuple(item["decorators"])) for item in value_symbols} == {
        ("getter", ("property",)),
        ("setter", ("value.setter",)),
    }
    symbols_by_name = {item["name"]: item for item in result["symbols"]}
    assert symbols_by_name["Settings.create"]["kind"] == "method"
    assert symbols_by_name["Settings.create"]["decorators"] == ["classmethod"]
    assert symbols_by_name["Settings.normalize"]["kind"] == "method"
    assert symbols_by_name["Settings.normalize"]["decorators"] == ["staticmethod"]
    assert symbols_by_name["Settings.token"]["kind"] == "getter"
    assert symbols_by_name["Settings.token"]["decorators"] == ["cached_property"]


def test_parse_python_all_exports_assigned_values_as_symbols() -> None:
    source = "\n".join(
        [
            "VERSION = '1.0'",
            "runtime_config: dict[str, str] = {}",
            "PUBLIC_A, public_b = (1, 2)",
            "hidden = object()",
            "__all__ = ['VERSION', 'runtime_config', 'PUBLIC_A', 'public_b']",
        ]
    )

    result = parse_code_source(source, path="src/public_values.py")

    symbols_by_name = {item["name"]: item for item in result["symbols"]}
    assert symbols_by_name["VERSION"]["kind"] == "constant"
    assert symbols_by_name["VERSION"]["line"] == 1
    assert symbols_by_name["VERSION"]["exported"] is True
    assert symbols_by_name["runtime_config"]["kind"] == "variable"
    assert symbols_by_name["runtime_config"]["exported"] is True
    assert symbols_by_name["PUBLIC_A"]["kind"] == "constant"
    assert symbols_by_name["public_b"]["kind"] == "variable"
    assert "hidden" not in symbols_by_name


def test_parse_python_nested_imports_for_dependency_navigation() -> None:
    source = "\n".join(
        [
            "from typing import TYPE_CHECKING",
            "",
            "if TYPE_CHECKING:",
            "    from src.types import Model",
            "",
            "try:",
            "    import orjson as json_impl",
            "except ImportError:",
            "    import json as json_impl",
            "",
            "def load_path():",
            "    import pathlib",
            "    return pathlib.Path('.')",
        ]
    )

    result = parse_code_source(source, path="src/deps.py")

    modules = [
        item["module"] if item["module"] is not None else item["names"][0]
        for item in result["imports"]
    ]
    assert modules == ["typing", "src.types", "orjson", "json", "pathlib"]
    assert result["imports"][2]["aliases"] == [
        {"name": "orjson", "asname": "json_impl"}
    ]
    symbols = {(item["kind"], item["name"]) for item in result["symbols"]}
    assert symbols == {("function", "load_path")}


def test_parse_python_syntax_error_keeps_compat_fields() -> None:
    result = parse_code_source("def broken(\n", path="broken.py")

    assert result["language"] == "python"
    assert result["parse_error"]
    assert result["lineno"] == 1
    assert result["offset"]
    assert result["symbols"] == []
    assert result["imports"] == []
    assert result["diagnostics"][0]["code"] == "python_syntax_error"


def test_parse_typescript_symbols_imports_exports_and_ignores_comments() -> None:
    source = "\n".join(
        [
            "import React, { useMemo as memo } from 'react';",
            "import type { User } from './types';",
            "// export function fake() {}",
            "/* class Hidden {} */",
            "export interface Props extends BaseProps, WithTheme { name: string }",
            "export type Mode = 'a' | 'b';",
            "export enum Status { Ready }",
            "export class Widget {",
            "  render(): string { return 'ok'; }",
            "}",
            "export const makeWidget = (name: string) => new Widget();",
            "export function hydrate(): void {}",
            "export { Widget as PublicWidget };",
        ]
    )

    result = parse_code_source(source, path="src/widget.ts")

    assert result["language"] == "typescript"
    assert result["diagnostics"] == []
    imports = result["imports"]
    assert imports[0]["module"] == "react"
    assert imports[0]["names"] == ["memo", "React"]
    assert imports[0]["aliases"] == [{"name": "useMemo", "asname": "memo"}]
    assert imports[1]["kind"] == "import_type"
    symbols = {(item["kind"], item["name"]) for item in result["symbols"]}
    assert ("function", "fake") not in symbols
    assert ("class", "Hidden") not in symbols
    assert ("interface", "Props") in symbols
    assert ("type", "Mode") in symbols
    assert ("enum", "Status") in symbols
    assert ("class", "Widget") in symbols
    assert ("method", "Widget.render") in symbols
    assert ("function", "makeWidget") in symbols
    assert ("function", "hydrate") in symbols
    symbols_by_name = {item["name"]: item for item in result["symbols"]}
    assert symbols_by_name["Props"]["bases"] == ["BaseProps", "WithTheme"]
    assert any(item["kind"] == "named" and item["names"] == ["PublicWidget"] for item in result["exports"])


def test_parse_typescript_type_specifier_names_are_normalized() -> None:
    source = "\n".join(
        [
            "import { type User, type Role as PublicRole } from './types';",
            "export { type User as PublicUser, type Role };",
        ]
    )

    result = parse_code_source(source, path="src/types.ts")

    assert result["imports"][0]["names"] == ["User", "PublicRole"]
    assert result["imports"][0]["aliases"] == [{"name": "Role", "asname": "PublicRole"}]
    assert result["exports"][0]["names"] == ["PublicUser", "Role"]
    assert result["exports"][0]["aliases"] == [
        {"name": "User", "asname": "PublicUser"}
    ]


def test_parse_typescript_multiline_import_export_spans() -> None:
    source = "\n".join(
        [
            "import {",
            "  User,",
            "  Role as PublicRole,",
            "} from './types';",
            "export {",
            "  User as PublicUser,",
            "  PublicRole,",
            "} from './types';",
        ]
    )

    result = parse_code_source(source, path="src/types.ts")

    assert result["imports"][0]["line"] == 1
    assert result["imports"][0]["end_line"] == 4
    assert result["imports"][0]["names"] == ["User", "PublicRole"]
    assert result["exports"][0]["line"] == 5
    assert result["exports"][0]["end_line"] == 8
    assert result["exports"][0]["names"] == ["PublicUser", "PublicRole"]


def test_parse_typescript_declare_and_abstract_declarations() -> None:
    source = "\n".join(
        [
            "export declare function createClient(options: Options): Client;",
            "export declare interface Options { baseUrl: string }",
            "export abstract class BaseClient {",
            "  abstract request(path: string): Promise<void>;",
            "  protected get token(): string { return ''; }",
            "}",
            "declare class LocalAmbient {}",
        ]
    )

    result = parse_code_source(source, path="src/client.d.ts")

    symbols = {(item["kind"], item["name"], item["line"]) for item in result["symbols"]}
    assert ("function", "createClient", 1) in symbols
    assert ("interface", "Options", 2) in symbols
    assert ("class", "BaseClient", 3) in symbols
    assert ("method", "BaseClient.request", 4) in symbols
    assert ("getter", "BaseClient.token", 5) in symbols
    assert ("class", "LocalAmbient", 7) in symbols
    exports = {(item["kind"], item["declaration_kind"], item["names"][0]) for item in result["exports"]}
    assert ("declaration", "function", "createClient") in exports
    assert ("declaration", "interface", "Options") in exports
    assert ("declaration", "class", "BaseClient") in exports


def test_parse_typescript_namespace_and_module_declarations() -> None:
    source = "\n".join(
        [
            "export namespace SDK {",
            "  export interface Config { token: string }",
            "}",
            "export module Compat.Tools {",
            "  export function run(): void;",
            "}",
            "declare module 'legacy-plugin' {",
            "  export const plugin: unknown;",
            "}",
        ]
    )

    result = parse_code_source(source, path="src/sdk.d.ts")

    symbols = {(item["kind"], item["name"], item["line"]) for item in result["symbols"]}
    assert ("namespace", "SDK", 1) in symbols
    assert ("module", "Compat.Tools", 4) in symbols
    assert ("module", "legacy-plugin", 7) in symbols
    exports = {(item["kind"], item["declaration_kind"], item["names"][0]) for item in result["exports"]}
    assert ("declaration", "namespace", "SDK") in exports
    assert ("declaration", "module", "Compat.Tools") in exports


def test_parse_javascript_symbols_imports_exports_and_arrow_functions() -> None:
    source = "\n".join(
        [
            "import './setup';",
            "import helper from './helper.js';",
            "export default function App() { return null; }",
            "export const mount = async () => helper();",
            "class LocalStore {",
            "  save(value) { return value; }",
            "}",
            "export * from './public.js';",
        ]
    )

    result = parse_code_source(source, path="src/app.jsx")

    assert result["language"] == "javascript"
    assert result["imports"][0]["kind"] == "side_effect"
    assert result["imports"][1]["names"] == ["helper"]
    symbols = {(item["kind"], item["name"]) for item in result["symbols"]}
    assert ("function", "App") in symbols
    assert ("function", "mount") in symbols
    assert ("class", "LocalStore") in symbols
    assert ("method", "LocalStore.save") in symbols
    assert any(item["kind"] == "all" and item["module"] == "./public.js" for item in result["exports"])


def test_parse_typescript_wrapped_function_variables() -> None:
    source = "\n".join(
        [
            "import React, { lazy, memo } from 'react';",
            "export const MemoPanel = memo(() => null);",
            "export const RefButton = React.forwardRef<HTMLButtonElement, Props>(",
            "  (props, ref) => null,",
            ");",
            "const LazyPanel = lazy(() => import('./LazyPanel'));",
            "const ids = items.map((item) => item.id);",
        ]
    )

    result = parse_code_source(source, path="src/components.tsx")

    symbols_by_name = {item["name"]: item for item in result["symbols"]}
    assert symbols_by_name["MemoPanel"]["kind"] == "function"
    assert symbols_by_name["MemoPanel"]["exported"] is True
    assert symbols_by_name["RefButton"]["kind"] == "function"
    assert symbols_by_name["RefButton"]["exported"] is True
    assert symbols_by_name["LazyPanel"]["kind"] == "function"
    assert "ids" not in symbols_by_name
    assert any(
        item["kind"] == "dynamic_import" and item["module"] == "./LazyPanel"
        for item in result["imports"]
    )


def test_parse_typescript_exported_values_as_symbols() -> None:
    source = "\n".join(
        [
            "export const appConfig = defineConfig({ mode: 'spa' });",
            "export let currentMode = 'dev';",
            "export var legacyFlag = true;",
            "const internal = 1;",
            "export const makeValue = () => 1;",
        ]
    )

    result = parse_code_source(source, path="src/config.ts")

    symbols_by_name = {item["name"]: item for item in result["symbols"]}
    assert symbols_by_name["appConfig"]["kind"] == "constant"
    assert symbols_by_name["appConfig"]["exported"] is True
    assert symbols_by_name["currentMode"]["kind"] == "variable"
    assert symbols_by_name["legacyFlag"]["kind"] == "variable"
    assert symbols_by_name["makeValue"]["kind"] == "function"
    assert "internal" not in symbols_by_name


def test_parse_javascript_anonymous_default_exports_are_navigation_symbols() -> None:
    function_source = "\n".join(
        [
            "export default function () {",
            "  const localArrow = () => true;",
            "  return localArrow();",
            "}",
            "export default async () => import('./lazy');",
        ]
    )

    function_result = parse_code_source(function_source, path="src/defaults.jsx")

    function_symbols = {
        (item["kind"], item["name"], item["line"]) for item in function_result["symbols"]
    }
    assert ("function", "default", 1) in function_symbols
    assert ("function", "default", 5) in function_symbols
    assert ("function", "localArrow", 2) not in function_symbols
    assert all(item["exported"] is True for item in function_result["symbols"])
    assert function_result["exports"] == [
        {"kind": "default", "names": ["default"], "line": 1, "end_line": 1},
        {"kind": "default", "names": ["default"], "line": 5, "end_line": 5},
    ]

    class_source = "\n".join(
        [
            "export default class extends Base {",
            "  render() { return null; }",
            "}",
        ]
    )

    class_result = parse_code_source(class_source, path="src/default-class.jsx")

    class_symbols = {
        (item["kind"], item["name"], item["line"]) for item in class_result["symbols"]
    }
    assert ("class", "default", 1) in class_symbols
    assert ("method", "default.render", 2) in class_symbols


def test_parse_typescript_star_and_namespace_reexports() -> None:
    source = "\n".join(
        [
            "export type * from './types';",
            "export * as Components from './components';",
            "export type * as PublicTypes from './public-types';",
        ]
    )

    result = parse_code_source(source, path="src/index.ts")

    assert result["exports"] == [
        {
            "kind": "all_type",
            "module": "./types",
            "names": ["*"],
            "line": 1,
            "end_line": 1,
        },
        {
            "kind": "namespace",
            "module": "./components",
            "names": ["Components"],
            "line": 2,
            "end_line": 2,
        },
        {
            "kind": "namespace_type",
            "module": "./public-types",
            "names": ["PublicTypes"],
            "line": 3,
            "end_line": 3,
        },
    ]


def test_parse_javascript_ignores_local_symbols_inside_function_blocks() -> None:
    source = "\n".join(
        [
            "export function outer() {",
            "  const localArrow = () => true;",
            "  function nested() { return false; }",
            "  class LocalClass {}",
            "  return localArrow();",
            "}",
            "export const topLevel = () => true;",
            "class Store {",
            "  run() {",
            "    const localMethodHelper = () => true;",
            "    function nestedInMethod() { return false; }",
            "    return localMethodHelper();",
            "  }",
            "}",
        ]
    )

    result = parse_code_source(source, path="src/local.js")

    symbols = {(item["kind"], item["name"]) for item in result["symbols"]}
    assert ("function", "outer") in symbols
    assert ("function", "topLevel") in symbols
    assert ("class", "Store") in symbols
    assert ("method", "Store.run") in symbols
    assert ("function", "localArrow") not in symbols
    assert ("function", "nested") not in symbols
    assert ("class", "LocalClass") not in symbols
    assert ("function", "localMethodHelper") not in symbols
    assert ("function", "nestedInMethod") not in symbols


def test_parse_javascript_commonjs_requires_exports_and_ignores_strings() -> None:
    source = "\n".join(
        [
            "const express = require('express')",
            "const { readFile: read, constants } = require('node:fs/promises')",
            "require('./setup')",
            "const sample = \"require('fake')\";",
            "loader.require('method-fake');",
            "// require('comment')",
            "module.exports = { createApp, start: boot }",
            "exports.health = health",
            "module.exports.extra = extra",
            "module.exports = require('./public')",
        ]
    )

    result = parse_code_source(source, path="src/server.js")

    imports = result["imports"]
    assert [(item["kind"], item["module"], item["names"]) for item in imports] == [
        ("require", "express", ["express"]),
        ("require", "node:fs/promises", ["read", "constants"]),
        ("require", "./setup", []),
        ("require", "./public", []),
    ]
    assert imports[1]["aliases"] == [{"name": "readFile", "asname": "read"}]
    assert all(item["module"] != "fake" for item in imports)
    assert all(item["module"] != "method-fake" for item in imports)
    assert all(item["module"] != "comment" for item in imports)
    exports = {(item["kind"], tuple(item["names"]), item.get("module")) for item in result["exports"]}
    assert ("commonjs", ("createApp", "start"), None) in exports
    assert ("commonjs", ("health",), None) in exports
    assert ("commonjs", ("extra",), None) in exports
    assert ("commonjs_reexport", ("*",), "./public") in exports


def test_parse_typescript_dynamic_imports_and_export_equals() -> None:
    source = "\n".join(
        [
            "const LazyView = lazy(() => import('./views/LazyView'))",
            "await import(\"./setup\")",
            "const sample = \"import('./fake')\";",
            "loader.import('./method-fake');",
            "export = LazyView",
        ]
    )

    result = parse_code_source(source, path="src/lazy.tsx")

    imports = result["imports"]
    assert [(item["kind"], item["module"], item["names"]) for item in imports] == [
        ("dynamic_import", "./views/LazyView", []),
        ("dynamic_import", "./setup", []),
    ]
    assert all(item["module"] != "./fake" for item in imports)
    assert all(item["module"] != "./method-fake" for item in imports)
    assert result["exports"] == [
        {"kind": "export_equals", "names": ["LazyView"], "line": 5, "end_line": 5}
    ]


def test_parse_typescript_import_equals_and_export_import_equals() -> None:
    source = "\n".join(
        [
            "import Express = require('express');",
            "export import LegacyClient = require('./legacy-client');",
        ]
    )

    result = parse_code_source(source, path="src/legacy.d.ts")

    assert result["imports"] == [
        {
            "kind": "import_equals",
            "module": "express",
            "names": ["Express"],
            "line": 1,
            "end_line": 1,
        },
        {
            "kind": "import_equals",
            "module": "./legacy-client",
            "names": ["LegacyClient"],
            "line": 2,
            "end_line": 2,
        },
    ]
    assert result["exports"] == [
        {
            "kind": "export_import_equals",
            "module": "./legacy-client",
            "names": ["LegacyClient"],
            "line": 2,
            "end_line": 2,
        }
    ]


def test_parse_typescript_multiline_statements_and_class_field_methods() -> None:
    source = "\n".join(
        [
            "import {",
            "  useMemo as memo,",
            "  useState,",
            "} from 'react';",
            "export type {",
            "  User as PublicUser,",
            "} from './types';",
            "@sealed()",
            "export class Dashboard extends BaseDashboard implements OnInit, AfterViewInit {",
            "  private handleClick = (",
            "    event: MouseEvent,",
            "  ) => event.type;",
            "  render(",
            "    label: string,",
            "  ): string {",
            "    return label;",
            "  }",
            "}",
            "export function hydrate(",
            "  id: string,",
            "): void {",
            "  console.log(id);",
            "}",
            "export const mount = async (",
            "  node: HTMLElement,",
            ") => hydrate(node.id);",
        ]
    )

    result = parse_code_source(source, path="src/dashboard.tsx")

    assert result["language"] == "typescript"
    assert result["imports"][0]["names"] == ["memo", "useState"]
    assert any(item["kind"] == "named_type" and item["names"] == ["PublicUser"] for item in result["exports"])
    symbols = {(item["kind"], item["name"], item["line"]) for item in result["symbols"]}
    assert ("class", "Dashboard", 9) in symbols
    assert ("method", "Dashboard.handleClick", 10) in symbols
    assert ("method", "Dashboard.render", 13) in symbols
    assert ("function", "hydrate", 19) in symbols
    assert ("function", "mount", 24) in symbols
    symbols_by_name = {item["name"]: item for item in result["symbols"]}
    assert symbols_by_name["Dashboard"]["decorators"] == ["sealed"]


def test_parse_typescript_multiline_and_method_decorators() -> None:
    source = "\n".join(
        [
            "@Component({",
            "  selector: 'app-dashboard',",
            "  providers: [DashboardService],",
            "})",
            "export class Dashboard extends BaseDashboard implements OnInit, AfterViewInit {",
            "  @Trace()",
            "  load(): void {}",
            "",
            "  @Input()",
            "  set value(next: string) {}",
            "}",
        ]
    )

    result = parse_code_source(source, path="src/dashboard.ts")

    symbols_by_name = {item["name"]: item for item in result["symbols"]}
    assert symbols_by_name["Dashboard"]["line"] == 5
    assert symbols_by_name["Dashboard"]["bases"] == [
        "BaseDashboard",
        "OnInit",
        "AfterViewInit",
    ]
    assert symbols_by_name["Dashboard"]["decorators"] == ["Component"]
    assert symbols_by_name["Dashboard.load"]["kind"] == "method"
    assert symbols_by_name["Dashboard.load"]["decorators"] == ["Trace"]
    assert symbols_by_name["Dashboard.value"]["kind"] == "setter"
    assert symbols_by_name["Dashboard.value"]["decorators"] == ["Input"]


def test_parse_typescript_class_accessors() -> None:
    source = "\n".join(
        [
            "class Settings {",
            "  get value(): string {",
            "    return 'ok';",
            "  }",
            "  set value(next: string) {",
            "    void next;",
            "  }",
            "  get #secret(): string {",
            "    return 'private';",
            "  }",
            "  #compute(): string {",
            "    return this.#secret;",
            "  }",
            "  #hydrate = () => this.#compute();",
            "}",
        ]
    )

    result = parse_code_source(source, path="src/settings.ts")

    symbols = {(item["kind"], item["name"], item["line"]) for item in result["symbols"]}
    assert ("class", "Settings", 1) in symbols
    assert ("getter", "Settings.value", 2) in symbols
    assert ("setter", "Settings.value", 5) in symbols
    assert ("getter", "Settings.#secret", 8) in symbols
    assert ("method", "Settings.#compute", 11) in symbols
    assert ("method", "Settings.#hydrate", 14) in symbols


def test_parse_typescript_deduplicates_class_field_methods() -> None:
    result = parse_code_source(
        "\n".join(
            [
                "class Store {",
                "  save = (value: string) => value;",
                "}",
            ]
        ),
        path="src/store.ts",
    )

    names = [item["name"] for item in result["symbols"] if item["kind"] == "method"]
    assert names == ["Store.save"]
