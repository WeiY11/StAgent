"""Tests for the release version consistency check.

Loaded by file path because ``backend/scripts`` is not an installed package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release_versions.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_release_versions", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_release_versions"] = module
    spec.loader.exec_module(module)
    return module


crv = _load()


def _make_repo(tmp_path: Path, version: str) -> Path:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "StAgent").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "desktop" / "electron").mkdir(parents=True)
    (tmp_path / "backend" / "pyproject.toml").write_text(
        f'[project]\nname = "StAgent"\nversion = "{version}"\n', encoding="utf-8"
    )
    (tmp_path / "backend" / "StAgent" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (tmp_path / "frontend" / "package.json").write_text(
        json.dumps({"name": "frontend", "version": version}), encoding="utf-8"
    )
    (tmp_path / "desktop" / "electron" / "package.json").write_text(
        json.dumps({"name": "desktop", "version": version}), encoding="utf-8"
    )
    return tmp_path


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("v1.2.7", "1.2.7"), ("desktop-v1.2.7", "1.2.7"), ("1.2.7", "1.2.7")],
)
def test_normalize_tag_strips_prefixes(tag: str, expected: str) -> None:
    assert crv.normalize_tag(tag) == expected


def test_matching_versions_pass(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, "1.2.7")
    assert crv.main(["--tag", "v1.2.7", "--root", str(root)]) == 0


def test_mismatched_version_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _make_repo(tmp_path, "1.2.7")
    (root / "frontend" / "package.json").write_text(
        json.dumps({"name": "frontend", "version": "1.2.6"}), encoding="utf-8"
    )
    assert crv.main(["--tag", "v1.2.7", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "frontend/package.json" in out
    assert "1.2.6" in out


def test_sdk_version_is_not_checked(tmp_path: Path) -> None:
    # An SDK version divergent from the product version must not fail the gate.
    root = _make_repo(tmp_path, "1.2.7")
    sdk = root / "backend" / "StAgent" / "sdk"
    sdk.mkdir()
    (sdk / "_version.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    assert crv.main(["--tag", "v1.2.7", "--root", str(root)]) == 0
    assert "backend/StAgent/sdk" not in crv.read_versions(root)
