"""Local filesystem directory browsing for the folder-grant modal.

Exposes ``GET /browse-directories`` so the frontend can navigate the local
filesystem visually instead of typing absolute paths.  Only names and
``is_dir`` are returned — no file contents are ever exposed.

Composed onto the chat router via ``include_router`` in
:mod:`StAgent.api.v1.chat`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status

from StAgent.api.schemas.chat import (
    BrowseResponse as _BrowseResponse,
)
from StAgent.api.schemas.chat import (
    DirEntry as _DirEntry,
)
from StAgent.services.auth import CurrentUserId  # noqa: TC001

router = APIRouter()


def _quick_access_dirs() -> list[_DirEntry]:
    """Well-known directories for the current OS user."""
    home = Path.home()
    candidates = [
        ("Home", home),
        ("Desktop", home / "Desktop"),
        ("Documents", home / "Documents"),
        ("Downloads", home / "Downloads"),
    ]
    out: list[_DirEntry] = []
    for label, p in candidates:
        try:
            resolved = p.resolve(strict=True)
            if resolved.is_dir():
                out.append(_DirEntry(name=label, path=str(resolved), is_dir=True))
        except (OSError, RuntimeError):
            continue
    return out


@router.get("/browse-directories", response_model=_BrowseResponse)
async def browse_directories(
    user_id: CurrentUserId,
    path: str | None = Query(None, max_length=4096),
) -> _BrowseResponse:
    """List subdirectories and files at *path* on the local machine.

    Used by the folder-grant modal to let users navigate the filesystem
    visually instead of typing absolute paths by hand.  Returns only
    names and ``is_dir`` — no file contents are exposed.
    """
    quick = _quick_access_dirs()

    if not path:
        root = Path.home()
    else:
        root = Path(path).expanduser()

    try:
        root = root.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Directory not found: {path}",
        )

    if not root.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Not a directory: {path}",
        )

    entries: list[_DirEntry] = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            entries.append(
                _DirEntry(name=child.name, path=str(child), is_dir=child.is_dir())
            )
    except PermissionError:
        pass

    parent_str: str | None = None
    if root.parent != root:
        parent_str = str(root.parent)

    return _BrowseResponse(
        path=str(root),
        parent=parent_str,
        entries=entries,
        quick_access=quick,
    )
