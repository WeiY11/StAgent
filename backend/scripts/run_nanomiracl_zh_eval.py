"""Run the prepared NanoMIRACL-zh corpus through an isolated lexical benchmark.

This command never imports public data into the active StAgent database and
never enables embeddings, remote reranking, OpenSearch, or query rewriting.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.prepare_retrieval_eval import (
    DEFAULT_OUTPUT_DIR,
    NANOMIRACL_DATASET_ID,
    NANOMIRACL_ZH_REVISION,
    verify_nanomiracl_zh,
)
from StAgent.knowledge.evaluation_dataset import parse_public_retrieval_dataset
from StAgent.knowledge.evaluation_runner import run_isolated_public_retrieval_evaluation

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RUNS_ROOT = _PROJECT_ROOT / "downloads" / "retrieval_eval" / "runs"
_PROVENANCE_FILES = (
    "backend/scripts/run_nanomiracl_zh_eval.py",
    "backend/StAgent/config/settings.py",
    "backend/StAgent/db/service.py",
    "backend/StAgent/db/repositories/document_chunks.py",
    "backend/StAgent/file/service.py",
    "backend/StAgent/file/storage/__init__.py",
    "backend/StAgent/file/storage/backend.py",
    "backend/StAgent/file/storage/local.py",
    "backend/StAgent/knowledge/acl.py",
    "backend/StAgent/knowledge/evaluation.py",
    "backend/StAgent/knowledge/evaluation_dataset.py",
    "backend/StAgent/knowledge/evaluation_import.py",
    "backend/StAgent/knowledge/evaluation_runner.py",
    "backend/StAgent/knowledge/rerank.py",
    "backend/StAgent/library/chunking.py",
    "backend/StAgent/library/fts.py",
    "backend/StAgent/library/retrieval.py",
    "backend/StAgent/alembic/versions/0025_fts_cjk_trigram_sync.py",
    "backend/pyproject.toml",
    "backend/uv.lock",
)


def _sha256(path: Path) -> str:
    """Return the SHA-256 for one checked local artifact."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_file_hashes() -> dict[str, str]:
    """Hash every local module that determines this isolated benchmark."""

    hashes: dict[str, str] = {}
    for relative_path in _PROVENANCE_FILES:
        path = _PROJECT_ROOT / relative_path
        if not path.is_file():
            raise FileNotFoundError(f"benchmark provenance source is missing: {path}")
        hashes[relative_path] = _sha256(path)
    return hashes


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load a local JSONL artifact after the preparer has verified its hash."""

    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path.name} line {line_number}") from exc
        if not isinstance(value, Mapping):
            raise ValueError(f"non-object row in {path.name} line {line_number}")
        rows.append(dict(value))
    return rows


def load_prepared_nanomiracl_zh(dataset_dir: Path) -> tuple[Any, dict[str, Any], str]:
    """Verify and parse the pinned prepared dataset without network access."""

    dataset_dir = dataset_dir.resolve()
    manifest = verify_nanomiracl_zh(dataset_dir)
    corpus_rows = _read_jsonl(dataset_dir / "corpus.jsonl")
    query_rows = _read_jsonl(dataset_dir / "queries.jsonl")
    qrel_rows = _read_jsonl(dataset_dir / "qrels.jsonl")
    dataset = parse_public_retrieval_dataset(
        dataset_id=str(manifest["dataset_id"]),
        revision=str(manifest["dataset_revision"]),
        corpus_rows=[{"_id": row["source_id"], "text": row["text"]} for row in corpus_rows],
        query_rows=[{"_id": row["source_id"], "text": row["text"]} for row in query_rows],
        qrel_rows=[
            {
                "query-id": row["query_source_id"],
                "corpus-id": row["corpus_source_id"],
            }
            for row in qrel_rows
        ],
    )
    if dataset.dataset_id != NANOMIRACL_DATASET_ID or dataset.revision != NANOMIRACL_ZH_REVISION:
        raise ValueError("prepared dataset identity does not match the pinned NanoMIRACL-zh source")
    return dataset, manifest, _sha256(dataset_dir / "manifest.json")


def _resolve_new_run_dir(run_dir: Path) -> Path:
    """Keep benchmark output inside the ignored retrieval-evaluation run tree."""

    resolved_run_dir = run_dir.resolve()
    resolved_root = _RUNS_ROOT.resolve()
    try:
        resolved_run_dir.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"run directory must be inside {resolved_root}") from exc
    if resolved_run_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing run directory: {resolved_run_dir}")
    return resolved_run_dir


def _default_run_dir() -> Path:
    """Build a unique, readable default run directory name in UTC."""

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return _RUNS_ROOT / f"nanomiracl-zh-{stamp}"


def _git_evidence() -> dict[str, Any]:
    """Record source-tree identity without changing the worktree."""

    def run_git(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=_PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip()

    status = run_git("status", "--porcelain")
    return {
        "commit": run_git("rev-parse", "HEAD"),
        "worktree_dirty": bool(status) if status is not None else None,
    }


def _write_run_metadata(run_dir: Path, *, dataset_dir: Path, manifest_sha256: str) -> None:
    """Write execution provenance beside immutable metric results."""

    metadata = {
        "script": str(Path(__file__).resolve()),
        "script_sha256": _sha256(Path(__file__).resolve()),
        "python": sys.version,
        "platform": platform.platform(),
        "dataset_dir": str(dataset_dir.resolve()),
        "dataset_manifest_sha256": manifest_sha256,
        "git": _git_evidence(),
        "source_file_sha256": _source_file_hashes(),
    }
    with (run_dir / "run_metadata.json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(metadata, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Load verified source artifacts, run the benchmark, then save provenance."""

    dataset_dir = args.dataset_dir.resolve()
    run_dir = _resolve_new_run_dir(args.run_dir or _default_run_dir())
    dataset, manifest, manifest_sha256 = load_prepared_nanomiracl_zh(dataset_dir)
    result = await run_isolated_public_retrieval_evaluation(
        dataset,
        run_dir,
        source_manifest=manifest,
        source_manifest_sha256=manifest_sha256,
    )
    _write_run_metadata(run_dir, dataset_dir=dataset_dir, manifest_sha256=manifest_sha256)
    return result


def main() -> None:
    """Parse CLI arguments and print one compact machine-readable summary."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="prepared, pinned NanoMIRACL-zh directory verified before running",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="new run directory under downloads/retrieval_eval/runs",
    )
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    print(
        json.dumps(
            {
                "run_dir": result["run_paths"]["run_dir"],
                "corpus_passages": result["mapping_validation"]["corpus_passages"],
                "queries": result["dataset"]["queries"],
                "baseline_recall_at_10": result["configurations"]["lexical_baseline"]["metrics"]["at_10"][
                    "recall_at_k"
                ],
                "reranked_recall_at_10": result["configurations"]["lexical_rerank"]["metrics"]["at_10"]["recall_at_k"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
