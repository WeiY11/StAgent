#!/usr/bin/env python
"""Fail-closed release version consistency check.

Compares the product version declared across the repository against the release
tag. It prints only mismatched files and exits non-zero on any mismatch; it
never rewrites files.

``leagent.sdk.__version__`` is intentionally *excluded*: the Agent SDK is a
separately versioned public surface (e.g. ``0.1.0``) and must not be forced to
match the product version (e.g. ``1.2.7``).
"""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

# backend/scripts/check_release_versions.py -> repo root is two levels up.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Files that must always agree with the release tag. The SDK is deliberately
# absent (see module docstring).
_PRODUCT_FILES = (
    "backend/pyproject.toml",
    "backend/leagent/__init__.py",
    "frontend/package.json",
    "desktop/electron/package.json",
)


def normalize_tag(tag: str) -> str:
    """Strip a ``desktop-v`` or ``v`` prefix from a git tag."""
    for prefix in ("desktop-v", "v"):
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return tag


def _toml_version(path: Path) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data.get("project", {}).get("version", ""))


def _init_version(path: Path) -> str:
    match = re.search(
        r"""__version__\s*=\s*["']([^"']+)["']""",
        path.read_text(encoding="utf-8"),
    )
    return match.group(1) if match else ""


def _json_version(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data.get("version", ""))


def read_versions(root: Path) -> dict[str, str]:
    """Return the declared version for each product file, keyed by rel path."""
    return {
        "backend/pyproject.toml": _toml_version(root / "backend/pyproject.toml"),
        "backend/leagent/__init__.py": _init_version(root / "backend/leagent/__init__.py"),
        "frontend/package.json": _json_version(root / "frontend/package.json"),
        "desktop/electron/package.json": _json_version(root / "desktop/electron/package.json"),
    }


def find_mismatches(versions: dict[str, str], expected: str) -> dict[str, str]:
    return {name: ver for name, ver in versions.items() if ver != expected}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Release tag, e.g. v1.2.7 or desktop-v1.2.7.")
    parser.add_argument(
        "--root",
        default=str(_REPO_ROOT),
        help="Repository root (defaults to the checkout containing this script).",
    )
    args = parser.parse_args(argv)

    expected = normalize_tag(args.tag)
    versions = read_versions(Path(args.root))
    mismatches = find_mismatches(versions, expected)

    if mismatches:
        print(f"release version mismatch (expected {expected!r} from tag {args.tag!r}):")
        for name in _PRODUCT_FILES:
            if name in mismatches:
                print(f"  {name}: {mismatches[name]!r}")
        return 1

    print(f"all product versions match {expected!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
