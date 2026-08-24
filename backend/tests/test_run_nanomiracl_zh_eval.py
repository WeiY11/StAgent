"""Provenance checks for the isolated NanoMIRACL benchmark CLI."""

from __future__ import annotations

import json

from scripts.run_nanomiracl_zh_eval import _sha256, _write_run_metadata


def test_run_metadata_hashes_the_retrieval_implementation(tmp_path) -> None:
    """A dirty worktree run still records the exact retrieval source inputs."""

    _write_run_metadata(
        tmp_path,
        dataset_dir=tmp_path,
        manifest_sha256="dataset-manifest-hash",
    )

    metadata = json.loads((tmp_path / "run_metadata.json").read_text(encoding="utf-8"))
    sources = metadata["source_file_sha256"]
    assert sources["backend/StAgent/knowledge/rerank.py"] == _sha256(
        _write_run_metadata.__globals__["_PROJECT_ROOT"] / "backend/StAgent/knowledge/rerank.py"
    )
    assert sources["backend/StAgent/db/repositories/document_chunks.py"] == _sha256(
        _write_run_metadata.__globals__["_PROJECT_ROOT"] / "backend/StAgent/db/repositories/document_chunks.py"
    )
    assert sources["backend/uv.lock"] == _sha256(_write_run_metadata.__globals__["_PROJECT_ROOT"] / "backend/uv.lock")
