"""Tests for POST /api/v1/files/bundle/download helpers (filename sanitization)."""

from __future__ import annotations

from uuid import UUID

from leagent.api.v1 import files as v1_files


_FILE_ID = UUID("00000000-0000-0000-0000-000000000123")


class TestSanitizeBundleFilename:
    def test_defaults_to_zip(self) -> None:
        assert v1_files._sanitize_bundle_download_filename(None) == "workspace-files.zip"
        assert v1_files._sanitize_bundle_download_filename("") == "workspace-files.zip"

    def test_strips_path_segments(self) -> None:
        assert v1_files._sanitize_bundle_download_filename("../../../etc/passwd") == "passwd.zip"

    def test_appends_zip_extension(self) -> None:
        assert v1_files._sanitize_bundle_download_filename("my-export").endswith(".zip")
        assert "my-export" in v1_files._sanitize_bundle_download_filename("my-export")


class TestSanitizeBundleArcname:
    def test_strips_path_segments(self) -> None:
        assert v1_files._sanitize_bundle_arcname("../../../etc/passwd", _FILE_ID) == "passwd"
        assert v1_files._sanitize_bundle_arcname("..\\..\\evil.txt", _FILE_ID) == "evil.txt"

    def test_removes_unsafe_characters(self) -> None:
        assert v1_files._sanitize_bundle_arcname("a/b report?.txt", _FILE_ID) == "b_report.txt"

    def test_falls_back_to_file_id(self) -> None:
        assert v1_files._sanitize_bundle_arcname("../", _FILE_ID) == f"file-{_FILE_ID}"
