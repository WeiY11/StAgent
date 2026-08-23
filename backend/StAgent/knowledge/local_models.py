"""Safe, explicit management for local SentenceTransformers model directories.

The registry intentionally has no discovery behaviour.  A model appears only
after an administrator imports a local directory or explicitly requests a Hub
download, and all model loading is subsequently offline.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from StAgent.file.primitives import is_path_inside

if TYPE_CHECKING:
    from collections.abc import Callable


class ModelValidationError(ValueError):
    """Raised when a model source cannot be safely managed."""


_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_UNSAFE_SUFFIXES = {".bin", ".pt", ".pth", ".pickle", ".pkl", ".exe", ".dll", ".so", ".dylib"}
_MAX_MODEL_BYTES = 10 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ManagedLocalModel:
    """Secret-free catalog record for one validated local model."""

    model_id: str
    display_name: str
    source_type: str
    source_reference: str
    resolved_revision: str
    manifest_hash: str
    dimension: int | None
    query_prompt: str | None
    document_prompt: str | None
    normalize: bool
    file_size: int
    status: str
    fingerprint: str


class LocalModelRegistry:
    """Persists validated model directories below ``knowledge/models`` only."""

    def __init__(
        self,
        StAgent_home: str | Path,
        *,
        max_model_bytes: int = _MAX_MODEL_BYTES,
        probe_vector: Callable[[Path], tuple[float, ...]] | None = None,
    ) -> None:
        if max_model_bytes <= 0:
            raise ValueError("max_model_bytes must be positive")
        home = Path(StAgent_home).expanduser().resolve()
        knowledge_root = home / "knowledge"
        knowledge_root.mkdir(parents=True, exist_ok=True)
        models_root = knowledge_root / "models"
        if models_root.is_symlink():
            raise ModelValidationError("managed models root must not be a symlink")
        models_root.mkdir(exist_ok=True)
        resolved_knowledge = knowledge_root.resolve()
        resolved_models = models_root.resolve()
        if (
            not is_path_inside(resolved_knowledge, home)
            or not is_path_inside(resolved_models, resolved_knowledge)
            or resolved_models.parent != resolved_knowledge
        ):
            raise ModelValidationError("managed models root escapes StAgent_HOME/knowledge")
        self.models_dir = resolved_models
        self.staging_dir = self.models_dir / ".staging"
        self.catalog_path = self.models_dir / "catalog.json"
        if self.catalog_path.is_symlink():
            raise ModelValidationError("managed model catalog must not be a symlink")
        self.max_model_bytes = max_model_bytes
        self._probe_vector = probe_vector or self._probe_sentence_transformer
        self.models_dir.mkdir(parents=True, exist_ok=True)
        if self.staging_dir.is_symlink():
            raise ModelValidationError("managed model staging root must not be a symlink")
        self.staging_dir.mkdir(exist_ok=True)

    @staticmethod
    def validate_huggingface_repo_id(repo_id: str) -> str:
        """Accept exactly a conservative ``owner/repository`` Hub identifier."""
        value = repo_id.strip()
        if not _REPO_ID.fullmatch(value) or value.endswith(".git") or ".." in value:
            raise ModelValidationError("invalid Hugging Face repository id")
        return value

    def list(self) -> list[ManagedLocalModel]:
        return list(self._read_catalog().values())

    def get(self, model_id: str) -> ManagedLocalModel | None:
        return self._read_catalog().get(model_id)

    def import_local(
        self,
        source_directory: str | Path,
        *,
        display_name: str,
        query_prompt: str | None = None,
        document_prompt: str | None = None,
        normalize: bool = True,
        dimension: int | None = None,
    ) -> ManagedLocalModel:
        """Copy a local model to staging, validate it, then publish atomically."""
        source = Path(source_directory).expanduser()
        if not source.is_dir() or source.is_symlink():
            raise ModelValidationError("model source must be a non-symlink directory")
        return self._stage_and_publish(
            source,
            display_name=display_name,
            source_type="local",
            source_reference=str(source.resolve()),
            resolved_revision="",
            query_prompt=query_prompt,
            document_prompt=document_prompt,
            normalize=normalize,
            dimension=dimension,
        )

    def download_huggingface(
        self,
        repo_id: str,
        *,
        revision: str | None = None,
        display_name: str | None = None,
        query_prompt: str | None = None,
        document_prompt: str | None = None,
        normalize: bool = True,
        dimension: int | None = None,
    ) -> ManagedLocalModel:
        """Explicitly download a Hub model into staging; never called implicitly."""
        safe_repo = self.validate_huggingface_repo_id(repo_id)
        stage = self._new_stage()
        try:
            try:
                hub = importlib.import_module("huggingface_hub")
            except ModuleNotFoundError as exc:
                raise RuntimeError("huggingface_hub is required for explicit model downloads") from exc
            info = hub.HfApi().model_info(safe_repo, revision=revision)
            resolved_revision = str(info.sha or "")
            if not re.fullmatch(r"[0-9a-f]{7,64}", resolved_revision):
                raise ModelValidationError("Hugging Face did not return an immutable commit SHA")
            snapshot_download: Callable[..., str] = hub.snapshot_download
            download_kwargs: dict[str, Any] = {
                "repo_id": safe_repo,
                "revision": resolved_revision,
                "local_dir": str(stage),
            }
            try:
                if "local_dir_use_symlinks" in inspect.signature(snapshot_download).parameters:
                    download_kwargs["local_dir_use_symlinks"] = False
            except (TypeError, ValueError):
                # The post-download validation still rejects any symlinked
                # files when a Hub client does not expose a stable signature.
                pass
            snapshot_download(**download_kwargs)
            return self._publish_stage(
                stage,
                display_name=display_name or safe_repo,
                source_type="huggingface",
                source_reference=safe_repo,
                resolved_revision=resolved_revision,
                query_prompt=query_prompt,
                document_prompt=document_prompt,
                normalize=normalize,
                dimension=dimension,
            )
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _stage_and_publish(self, source: Path, **kwargs: Any) -> ManagedLocalModel:
        stage = self._new_stage()
        try:
            self._copy_source(source, stage)
            return self._publish_stage(stage, **kwargs)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _new_stage(self) -> Path:
        stage = self.staging_dir / uuid.uuid4().hex
        stage.mkdir(parents=True)
        return stage

    def _copy_source(self, source: Path, stage: Path) -> None:
        source_root = source.resolve()
        total_size = 0
        for item in source.rglob("*"):
            if item.is_symlink() or not is_path_inside(item.resolve(), source_root):
                raise ModelValidationError("symlinked model files are not allowed")
            relative = item.relative_to(source)
            target = stage / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                total_size += item.stat().st_size
                if total_size > self.max_model_bytes:
                    raise ModelValidationError("model exceeds configured safe size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)

    def _publish_stage(self, stage: Path, **kwargs: Any) -> ManagedLocalModel:
        manifest_hash, total_size = self._validate_stage(stage)
        probed_dimension = self._validate_probe(stage)
        requested_dimension = kwargs["dimension"]
        if requested_dimension is not None and requested_dimension != probed_dimension:
            raise ModelValidationError("model probe dimension differs from configured dimension")
        model_id = f"local-{uuid.uuid4().hex}"
        destination = self.models_dir / model_id
        if destination.parent.resolve() != self.models_dir.resolve():
            raise ModelValidationError("model destination escapes managed directory")
        fingerprint = self._fingerprint(
            manifest_hash=manifest_hash,
            **{**kwargs, "dimension": probed_dimension},
        )
        record = ManagedLocalModel(
            model_id=model_id,
            display_name=str(kwargs["display_name"]).strip() or model_id,
            source_type=str(kwargs["source_type"]),
            source_reference=str(kwargs["source_reference"]),
            resolved_revision=str(kwargs["resolved_revision"]),
            manifest_hash=manifest_hash,
            dimension=probed_dimension,
            query_prompt=kwargs["query_prompt"],
            document_prompt=kwargs["document_prompt"],
            normalize=bool(kwargs["normalize"]),
            file_size=total_size,
            status="ready",
            fingerprint=fingerprint,
        )
        os.replace(stage, destination)
        try:
            catalog = self._read_catalog()
            catalog[record.model_id] = record
            self._write_catalog(catalog)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        return record

    def _validate_probe(self, stage: Path) -> int:
        try:
            vector = [float(value) for value in self._probe_vector(stage)]
        except ModelValidationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ModelValidationError("local model probe failed") from exc
        if not vector or not all(value == value and abs(value) != float("inf") for value in vector):
            raise ModelValidationError("local model probe returned invalid vector")
        return len(vector)

    @staticmethod
    def _probe_sentence_transformer(stage: Path) -> tuple[float, ...]:
        try:
            module = importlib.import_module("sentence_transformers")
        except ModuleNotFoundError as exc:
            raise ModelValidationError("sentence-transformers RAG extra is required to validate a model") from exc
        model = module.SentenceTransformer(
            str(stage),
            local_files_only=True,
            trust_remote_code=False,
        )
        raw = model.encode(["StAgent local embedding probe"], normalize_embeddings=True)
        values = raw.tolist() if hasattr(raw, "tolist") else raw
        if len(values) != 1:
            raise ModelValidationError("local model probe returned an unexpected vector count")
        return tuple(values[0])

    def _validate_stage(self, stage: Path) -> tuple[str, int]:
        total_size = 0
        manifest: list[tuple[str, int, str]] = []
        has_safetensors = False
        stage_root = stage.resolve()
        for item in sorted(stage.rglob("*")):
            if item.is_symlink() or not is_path_inside(item.resolve(), stage_root):
                raise ModelValidationError("symlinked model files are not allowed")
            if not item.is_file():
                continue
            if item.suffix.lower() in _UNSAFE_SUFFIXES:
                raise ModelValidationError("unsafe serialized or executable model file")
            if item.suffix.lower() == ".py":
                raise ModelValidationError("remote code files are not allowed")
            size = item.stat().st_size
            total_size += size
            if total_size > self.max_model_bytes:
                raise ModelValidationError("model exceeds configured safe size limit")
            relative = item.relative_to(stage).as_posix()
            digest = self._hash_file(item)
            manifest.append((relative, size, digest))
            has_safetensors = has_safetensors or item.suffix.lower() == ".safetensors"
            if item.suffix.lower() == ".json":
                self._reject_remote_code(item)
        if not has_safetensors:
            raise ModelValidationError("model must contain safetensors weights")
        blob = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest(), total_size

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _reject_remote_code(item: Path) -> None:
        try:
            value = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(value, dict) and ("auto_map" in value or value.get("trust_remote_code") is True):
            raise ModelValidationError("remote code configuration is not allowed")

    @staticmethod
    def _fingerprint(*, manifest_hash: str, **kwargs: Any) -> str:
        source_type = str(kwargs["source_type"])
        identity = {
            "source_type": source_type,
            # A local absolute path is not model identity: importing the same
            # validated files from another server directory must be stable.
            # Hub identity is the immutable commit SHA returned by model_info.
            "resolved_revision": kwargs["resolved_revision"] if source_type == "huggingface" else "",
            "manifest_hash": manifest_hash,
            "dimension": kwargs["dimension"],
            "query_prompt": kwargs["query_prompt"],
            "document_prompt": kwargs["document_prompt"],
            "normalize": bool(kwargs["normalize"]),
        }
        payload = json.dumps(identity, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _read_catalog(self) -> dict[str, ManagedLocalModel]:
        if self.catalog_path.is_symlink():
            raise ModelValidationError("managed model catalog must not be a symlink")
        if not self.catalog_path.exists():
            return {}
        raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        return {key: ManagedLocalModel(**value) for key, value in raw.items()}

    def _write_catalog(self, catalog: dict[str, ManagedLocalModel]) -> None:
        temporary = self.catalog_path.with_suffix(".tmp")
        if temporary.is_symlink():
            raise ModelValidationError("managed model catalog staging file must not be a symlink")
        payload = {key: asdict(value) for key, value in catalog.items()}
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.catalog_path)


__all__ = ["LocalModelRegistry", "ManagedLocalModel", "ModelValidationError"]
