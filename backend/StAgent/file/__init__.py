"""Unified file management layer for StAgent.

This package is the single authoritative home for managed-blob primitives,
storage backends, and the file lifecycle service.  All subsystems that
touch persistent files — chat uploads, tool outputs, knowledge documents,
code-execution artifacts — depend downward into this package.

Coding-project source trees (``StAgent.project``) are *not* managed
blobs and never flow through the file service.
"""

from StAgent.file.primitives import (
    DEFAULT_CHECKSUM_ALGO,
    MAX_FILENAME_LENGTH,
    FileKind,
    FileScope,
    classify_file_kind,
    detect_mime,
    is_path_inside,
    sanitize_filename,
)
from StAgent.file.service import FileAccessLevel, FileAccessPolicy, FileRef, FileService

__all__ = [
    "DEFAULT_CHECKSUM_ALGO",
    "MAX_FILENAME_LENGTH",
    "FileAccessLevel",
    "FileAccessPolicy",
    "FileKind",
    "FileRef",
    "FileScope",
    "FileService",
    "classify_file_kind",
    "detect_mime",
    "is_path_inside",
    "sanitize_filename",
]
