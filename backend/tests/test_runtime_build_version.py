"""Runtime desktop build-version override coverage."""

from __future__ import annotations

import runpy
from pathlib import Path

_PACKAGE_INIT = Path(__file__).resolve().parents[1] / "leagent" / "__init__.py"


def _load_package_version(monkeypatch, build_version: str | None) -> str:
    if build_version is None:
        monkeypatch.delenv("LEAGENT_BUILD_VERSION", raising=False)
    else:
        monkeypatch.setenv("LEAGENT_BUILD_VERSION", build_version)
    namespace = runpy.run_path(str(_PACKAGE_INIT))
    return str(namespace["__version__"])


def test_desktop_build_version_overrides_source_version(monkeypatch) -> None:
    assert _load_package_version(monkeypatch, "1.2.12") == "1.2.12"


def test_source_version_remains_the_default(monkeypatch) -> None:
    assert _load_package_version(monkeypatch, None) == "1.2.12"
