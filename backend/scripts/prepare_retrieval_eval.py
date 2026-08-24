"""Explicitly download and validate a pinned public retrieval-evaluation set.

This utility only materializes source corpus, queries, qrels, and a manifest.
It does not import content into a LeAgent knowledge base, download embedding
models, or reuse the upstream BM25 candidate list as a retrieval result.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from StAgent.knowledge.evaluation_dataset import parse_public_retrieval_dataset

NANOMIRACL_DATASET_ID = "hotchpotch/NanoMIRACL"
NANOMIRACL_ZH_REVISION = "6e98c995a5be54ef41d3a016a2f30314dd625619"
NANOMIRACL_ZH_SPLIT = "zh"
DATASET_VIEWER_ROWS_URL = "https://datasets-server.huggingface.co/rows"
NANOMIRACL_DATA_CARD_URL = (
    f"https://huggingface.co/datasets/hotchpotch/NanoMIRACL/blob/{NANOMIRACL_ZH_REVISION}/README.md"
)
PAGE_LENGTH = 100
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = _PROJECT_ROOT / "downloads" / "retrieval_eval" / "nanomiracl-zh" / NANOMIRACL_ZH_REVISION
DEFAULT_DESTINATION_ROOT = _PROJECT_ROOT / "downloads" / "retrieval_eval"

PageFetcher = Callable[[str, int, int], Awaitable[Mapping[str, Any]]]


async def _fetch_rows_page(
    client: httpx.AsyncClient,
    config: str,
    offset: int,
    length: int,
) -> Mapping[str, Any]:
    """Fetch one pinned page from the Hugging Face Dataset Viewer API."""
    response = await client.get(
        DATASET_VIEWER_ROWS_URL,
        params={
            "dataset": NANOMIRACL_DATASET_ID,
            "config": config,
            "split": NANOMIRACL_ZH_SPLIT,
            "revision": NANOMIRACL_ZH_REVISION,
            "offset": offset,
            "length": length,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"Dataset Viewer returned a non-object page for {config} offset {offset}")
    return payload


async def _fetch_config_rows(
    config: str,
    *,
    fetch_page: PageFetcher,
) -> list[dict[str, Any]]:
    """Read a complete bounded Dataset Viewer config without trusting a partial page."""
    rows: list[dict[str, Any]] = []
    total_rows: int | None = None
    offset = 0
    while total_rows is None or offset < total_rows:
        payload = await fetch_page(config, offset, PAGE_LENGTH)
        raw_total = payload.get("num_rows_total")
        if isinstance(raw_total, bool) or not isinstance(raw_total, int) or raw_total <= 0:
            raise RuntimeError(f"Dataset Viewer returned invalid num_rows_total for {config}")
        if total_rows is None:
            total_rows = raw_total
        elif raw_total != total_rows:
            raise RuntimeError(f"Dataset Viewer changed num_rows_total while reading {config}")

        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list) or not raw_rows:
            raise RuntimeError(f"Dataset Viewer returned an empty page before {config} was complete")
        page_rows: list[dict[str, Any]] = []
        for index, item in enumerate(raw_rows):
            if not isinstance(item, Mapping) or not isinstance(item.get("row"), Mapping):
                raise RuntimeError(f"Dataset Viewer returned malformed {config} row at offset {offset + index}")
            page_rows.append(dict(item["row"]))
        if offset + len(page_rows) > total_rows:
            raise RuntimeError(f"Dataset Viewer returned too many {config} rows")
        rows.extend(page_rows)
        offset += len(page_rows)

    if len(rows) != total_rows:
        raise RuntimeError(f"Dataset Viewer returned {len(rows)} of {total_rows} {config} rows")
    return rows


async def fetch_nanomiracl_zh_rows(
    *,
    fetch_page: PageFetcher | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch NanoMIRACL Chinese corpus, queries, and qrels at one fixed revision."""
    if fetch_page is None:
        timeout = httpx.Timeout(30.0, connect=15.0)
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": "StAgent-retrieval-eval-preparer/1.0"},
            timeout=timeout,
        ) as client:

            async def fetch_page(config: str, offset: int, length: int) -> Mapping[str, Any]:
                return await _fetch_rows_page(client, config, offset, length)

            return (
                await _fetch_config_rows("corpus", fetch_page=fetch_page),
                await _fetch_config_rows("queries", fetch_page=fetch_page),
                await _fetch_config_rows("qrels", fetch_page=fetch_page),
            )
    return (
        await _fetch_config_rows("corpus", fetch_page=fetch_page),
        await _fetch_config_rows("queries", fetch_page=fetch_page),
        await _fetch_config_rows("qrels", fetch_page=fetch_page),
    )


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Write deterministic UTF-8 JSONL rows to a fresh staging path."""
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write one deterministic UTF-8 JSON object to a fresh staging path."""
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
        handle.write("\n")


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one fully written output file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_new_output_dir(output_dir: Path, *, destination_root: Path) -> Path:
    """Resolve a requested destination and keep materialization inside its data tree."""
    resolved_output_dir = output_dir.resolve()
    resolved_destination_root = destination_root.resolve()
    try:
        resolved_output_dir.relative_to(resolved_destination_root)
    except ValueError as exc:
        raise ValueError(
            f"output directory must be inside the retrieval evaluation download tree: {resolved_destination_root}"
        ) from exc
    return resolved_output_dir


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read one prepared JSONL file and reject malformed rows deterministically."""
    if not path.is_file():
        raise ValueError(f"prepared file is missing: {path.name}")
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path.name} row {index}") from exc
        if not isinstance(value, Mapping):
            raise ValueError(f"non-object row in {path.name} at {index}")
        rows.append(dict(value))
    return rows


def _load_manifest(path: Path) -> dict[str, Any]:
    """Load a prepared manifest as a JSON object."""
    if not path.is_file():
        raise ValueError("prepared manifest.json is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("prepared manifest.json is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("prepared manifest.json must be an object")
    return dict(value)


def _expected_file_metadata(manifest: Mapping[str, Any], filename: str) -> tuple[int, str]:
    """Read the declared count and SHA-256 for one prepared JSONL file."""
    files = manifest.get("files")
    if not isinstance(files, Mapping) or not isinstance(files.get(filename), Mapping):
        raise ValueError(f"manifest is missing metadata for {filename}")
    metadata = files[filename]
    rows = metadata.get("rows")
    digest = metadata.get("sha256")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise ValueError(f"manifest has invalid row count for {filename}")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError(f"manifest has invalid SHA-256 for {filename}")
    return rows, digest


def verify_nanomiracl_zh(output_dir: Path) -> dict[str, Any]:
    """Verify a prepared NanoMIRACL-zh copy without accessing the network."""
    output_dir = output_dir.resolve()
    manifest = _load_manifest(output_dir / "manifest.json")
    if manifest.get("dataset_id") != NANOMIRACL_DATASET_ID:
        raise ValueError("manifest dataset_id does not identify NanoMIRACL")
    if manifest.get("dataset_revision") != NANOMIRACL_ZH_REVISION:
        raise ValueError("manifest dataset_revision does not match the pinned revision")
    if manifest.get("split") != NANOMIRACL_ZH_SPLIT:
        raise ValueError("manifest split does not match zh")
    if manifest.get("qrel_level") != "passage":
        raise ValueError("manifest qrel_level must be passage")

    prepared_rows: dict[str, list[dict[str, Any]]] = {}
    for filename in ("corpus.jsonl", "queries.jsonl", "qrels.jsonl"):
        expected_rows, expected_digest = _expected_file_metadata(manifest, filename)
        path = output_dir / filename
        if not path.is_file():
            raise ValueError(f"prepared file is missing: {filename}")
        actual_digest = _sha256(path)
        if actual_digest != expected_digest:
            raise ValueError(f"SHA-256 mismatch for {filename}")
        rows = _read_jsonl(path)
        if len(rows) != expected_rows:
            raise ValueError(f"row count mismatch for {filename}")
        prepared_rows[filename] = rows

    dataset = parse_public_retrieval_dataset(
        dataset_id=manifest["dataset_id"],
        revision=manifest["dataset_revision"],
        corpus_rows=[{"_id": row.get("source_id"), "text": row.get("text")} for row in prepared_rows["corpus.jsonl"]],
        query_rows=[{"_id": row.get("source_id"), "text": row.get("text")} for row in prepared_rows["queries.jsonl"]],
        qrel_rows=[
            {
                "query-id": row.get("query_source_id"),
                "corpus-id": row.get("corpus_source_id"),
            }
            for row in prepared_rows["qrels.jsonl"]
        ],
    )
    expected_counts = {"corpus": len(dataset.corpus), "queries": len(dataset.queries), "qrels": len(dataset.qrels)}
    if manifest.get("counts") != expected_counts:
        raise ValueError("manifest counts do not match prepared data")
    return manifest


async def prepare_nanomiracl_zh(
    output_dir: Path,
    *,
    fetch_page: PageFetcher | None = None,
    destination_root: Path = DEFAULT_DESTINATION_ROOT,
) -> dict[str, Any]:
    """Materialize a pinned NanoMIRACL-zh copy without overwriting existing data."""
    output_dir = _resolve_new_output_dir(output_dir, destination_root=destination_root)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing dataset directory: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    corpus_rows, query_rows, qrel_rows = await fetch_nanomiracl_zh_rows(fetch_page=fetch_page)
    dataset = parse_public_retrieval_dataset(
        dataset_id=NANOMIRACL_DATASET_ID,
        revision=NANOMIRACL_ZH_REVISION,
        corpus_rows=corpus_rows,
        query_rows=query_rows,
        qrel_rows=qrel_rows,
    )

    with tempfile.TemporaryDirectory(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent) as staging_name:
        staging_dir = Path(staging_name)
        corpus_path = staging_dir / "corpus.jsonl"
        queries_path = staging_dir / "queries.jsonl"
        qrels_path = staging_dir / "qrels.jsonl"
        _write_jsonl(
            corpus_path,
            [{"source_id": record.source_id, "text": record.text} for record in dataset.corpus],
        )
        _write_jsonl(
            queries_path,
            [{"source_id": record.source_id, "text": record.text} for record in dataset.queries],
        )
        _write_jsonl(
            qrels_path,
            [
                {
                    "corpus_source_id": qrel.corpus_source_id,
                    "query_source_id": qrel.query_source_id,
                }
                for qrel in dataset.qrels
            ],
        )
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "dataset_id": dataset.dataset_id,
            "dataset_revision": dataset.revision,
            "split": NANOMIRACL_ZH_SPLIT,
            "qrel_level": "passage",
            "counts": {
                "corpus": len(dataset.corpus),
                "queries": len(dataset.queries),
                "qrels": len(dataset.qrels),
            },
            "files": {
                "corpus.jsonl": {"rows": len(dataset.corpus), "sha256": _sha256(corpus_path)},
                "queries.jsonl": {"rows": len(dataset.queries), "sha256": _sha256(queries_path)},
                "qrels.jsonl": {"rows": len(dataset.qrels), "sha256": _sha256(qrels_path)},
            },
            "source": {
                "data_card": NANOMIRACL_DATA_CARD_URL,
                "dataset_viewer": DATASET_VIEWER_ROWS_URL,
                "license": "other",
                "upstream_attribution_required": True,
            },
            "evaluation_guard": {
                "requires_complete_one_chunk_per_passage_mapping": True,
                "excluded_upstream_config": "bm25",
                "excluded_reason": "precomputed candidates include forced positives and are not independent retrieval input",
            },
        }
        _write_json(staging_dir / "manifest.json", manifest)
        staging_dir.replace(output_dir)
    return manifest


def main() -> None:
    """Parse download arguments and print a concise machine-readable result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="new destination directory; defaults to the ignored project downloads tree",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify an existing prepared copy locally instead of downloading it",
    )
    args = parser.parse_args()
    manifest = (
        verify_nanomiracl_zh(args.output_dir) if args.verify else asyncio.run(prepare_nanomiracl_zh(args.output_dir))
    )
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir.resolve()),
                "dataset_id": manifest["dataset_id"],
                "dataset_revision": manifest["dataset_revision"],
                "counts": manifest["counts"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
