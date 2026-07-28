"""Shared type aliases and constants for settings sub-modules."""

from __future__ import annotations

from typing import Literal

LogFormat = Literal["json", "console", "auto"]
RuntimeProfile = Literal["standard", "coding_long", "coding_extended"]

CANVAS_PREVIEW_CSP_LOCAL: str = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "img-src https: http: data: blob:; "
    "media-src mediastream: blob: 'self'; "
    "font-src https: http: data:; "
    "style-src https: http: 'unsafe-inline'; "
    "script-src https: http: 'unsafe-inline' 'unsafe-eval'; "
    "connect-src https: http: wss: ws:; "
    "frame-src https: http:; "
    "worker-src blob:; "
    "child-src blob: https: http:;"
)
