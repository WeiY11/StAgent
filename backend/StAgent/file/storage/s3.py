"""S3-compatible storage backend for managed file blobs.

The adapter accepts both AWS S3 and MinIO-compatible clients.  It deliberately
uses a small boto3-style surface and lazy imports so local development does not
need cloud SDK dependencies.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any, BinaryIO

if TYPE_CHECKING:
    from StAgent.config.settings import ObjectStorageSettings


def _normalize_relative_key(value: str, *, allow_empty: bool = False) -> str:
    """Normalize a key while refusing path-like prefix escapes.

    Object stores do not interpret ``..`` specially, but accepting it here
    would make the security boundary differ from the local backend and could
    let a caller write outside the configured logical prefix.
    """
    raw = str(value or "").replace("\\", "/")
    if raw.startswith("/"):
        raise ValueError("S3 storage key must be a relative path")
    parts = raw.split("/")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("S3 storage key must be a relative path without '.' or '..' segments")
    normalized = "/".join(part for part in parts if part)
    if not normalized and not allow_empty:
        raise ValueError("S3 storage key must be a non-empty relative path")
    return normalized


def _read_binary(data: BinaryIO) -> bytes:
    data.seek(0)
    raw = data.read()
    if not isinstance(raw, bytes):
        raise TypeError("S3 storage input must produce bytes")
    return raw


def _is_not_found(error: Exception) -> bool:
    """Recognize boto3-compatible missing-object errors without importing botocore."""
    if isinstance(error, (FileNotFoundError, KeyError)):
        return True
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    detail = response.get("Error")
    code = str(detail.get("Code", "")) if isinstance(detail, dict) else ""
    return code in {"404", "NoSuchKey", "NoSuchObject", "NotFound"}


class S3StorageBackend:
    """StorageBackend implementation over a boto3-compatible S3 client."""

    def __init__(self, client: Any, *, bucket: str, prefix: str = "") -> None:
        self._client = client
        self._bucket = str(bucket or "").strip()
        if not self._bucket or any(char in self._bucket for char in "/\\"):
            raise ValueError("S3 bucket must be a non-empty bucket name without a path")
        self._prefix = _normalize_relative_key(prefix, allow_empty=True)

    @property
    def client(self) -> Any:
        """Underlying client for controlled health checks and diagnostics."""
        return self._client

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def prefix(self) -> str:
        return self._prefix

    def key_from_storage_path(self, storage_path: str) -> str | None:
        """Recover a logical backend key from this backend's persisted URI."""
        marker = f"s3://{self._bucket}/"
        raw = str(storage_path or "")
        if not raw.startswith(marker):
            return None
        object_key = raw[len(marker) :]
        prefix = f"{self._prefix}/" if self._prefix else ""
        if prefix:
            if not object_key.startswith(prefix):
                return None
            object_key = object_key[len(prefix) :]
        try:
            return _normalize_relative_key(object_key)
        except ValueError:
            return None

    def _object_key(self, key: str) -> str:
        normalized = _normalize_relative_key(key)
        return f"{self._prefix}/{normalized}" if self._prefix else normalized

    async def put(
        self,
        data: bytes | BinaryIO,
        key: str,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raw = data if isinstance(data, bytes) else await asyncio.to_thread(_read_binary, data)
        object_key = self._object_key(key)
        normalized_metadata = {
            str(name): str(value)
            for name, value in (metadata or {}).items()
            if str(name).strip()
        }
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=object_key,
            Body=raw,
            ContentType=content_type or "application/octet-stream",
            Metadata=normalized_metadata,
        )
        return {
            "size": len(raw),
            "checksum": hashlib.sha256(raw).hexdigest(),
            "storage_path": f"s3://{self._bucket}/{object_key}",
            "content_type": content_type or "application/octet-stream",
        }

    async def get(self, key: str) -> tuple[bytes, str]:
        object_key = self._object_key(key)
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=object_key,
            )
        except Exception as exc:
            if _is_not_found(exc):
                raise FileNotFoundError(f"Not found: {key}") from exc
            raise
        body = response.get("Body") if isinstance(response, dict) else None
        if body is None or not hasattr(body, "read"):
            raise RuntimeError("S3 get_object returned no readable body")
        try:
            raw = await asyncio.to_thread(body.read)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                await asyncio.to_thread(close)
        if not isinstance(raw, bytes):
            raise TypeError("S3 get_object body must contain bytes")
        content_type = (
            str(response.get("ContentType") or "application/octet-stream")
            if isinstance(response, dict)
            else "application/octet-stream"
        )
        return raw, content_type

    async def delete(self, key: str) -> bool:
        object_key = self._object_key(key)
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=object_key,
        )
        # S3 delete is intentionally idempotent: a successful response means
        # the desired postcondition (object absent) now holds.
        return True

    async def exists(self, key: str) -> bool:
        object_key = self._object_key(key)
        try:
            await asyncio.to_thread(
                self._client.head_object,
                Bucket=self._bucket,
                Key=object_key,
            )
        except Exception as exc:
            if _is_not_found(exc):
                return False
            raise
        return True

    async def presign(self, key: str, *, expires_in: int = 3600) -> str | None:
        if expires_in < 1:
            raise ValueError("expires_in must be positive")
        object_key = self._object_key(key)
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": object_key},
            ExpiresIn=int(expires_in),
        )


def build_s3_storage_backend(
    settings: ObjectStorageSettings,
    *,
    client: Any | None = None,
) -> tuple[S3StorageBackend, Any]:
    """Build an S3 backend, importing boto3 only for real runtime clients."""
    if not settings.enabled:
        raise ValueError("Object storage is disabled")
    if client is None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError(
                "S3-compatible storage requires the production dependency extra "
                "(uv sync --extra production)"
            ) from exc
        client_kwargs: dict[str, Any] = {
            "service_name": "s3",
            "region_name": settings.region_name,
            "config": Config(
                connect_timeout=settings.connect_timeout_seconds,
                read_timeout=settings.connect_timeout_seconds,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        }
        if settings.endpoint_url:
            client_kwargs["endpoint_url"] = settings.endpoint_url
        if settings.access_key_id:
            client_kwargs["aws_access_key_id"] = settings.access_key_id
        if settings.secret_access_key:
            client_kwargs["aws_secret_access_key"] = settings.secret_access_key
        if settings.session_token:
            client_kwargs["aws_session_token"] = settings.session_token
        client = boto3.client(**client_kwargs)
    return S3StorageBackend(client, bucket=settings.bucket, prefix=settings.prefix), client


__all__ = ["S3StorageBackend", "build_s3_storage_backend"]
