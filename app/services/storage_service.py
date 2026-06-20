"""
S3-compatible presigned URL generation using boto3 with run_in_executor.

Design:
- Never receives binary data — presigned URL pattern only.
- storage_key format: uploads/quarantine/{tenant_id}/{uuid7}.{ext}
  The 'quarantine' prefix allows a virus-scan step to move clean files to
  uploads/files/ before serving.
- MIME allowlist and extension blocklist enforced before generating any URL.
"""

import asyncio
import logging
import os
from functools import partial
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from uuid_extensions import uuid7

from app.config import get_settings
from app.exceptions import ExternalServiceError, ValidationError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowlist / blocklist
# ---------------------------------------------------------------------------

ALLOWED_MIMES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
        "text/plain",
    }
)

BLOCKED_EXTENSIONS: frozenset[str] = frozenset(
    {".exe", ".sh", ".bat", ".ps1", ".py", ".php", ".js", ".jar"}
)

_MAX_FILENAME_LEN = 255


# ---------------------------------------------------------------------------
# StorageService
# ---------------------------------------------------------------------------


class StorageService:
    """Thin async wrapper around boto3 presigned URL generation.

    boto3 is synchronous; each call is offloaded to a thread via
    asyncio.get_event_loop().run_in_executor so it never blocks the event loop.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._bucket = settings.STORAGE_BUCKET
        self._expiry = settings.STORAGE_PRESIGN_EXPIRY_SECONDS

        _common = {
            "service_name": "s3",
            "aws_access_key_id": settings.STORAGE_ACCESS_KEY,
            "aws_secret_access_key": settings.STORAGE_SECRET_KEY,
            # Path-style addressing required for MinIO and most self-hosted S3
            "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        }
        # Real server-side ops (bucket/object I/O) use the INTERNAL endpoint
        # (e.g. docker "minio:9000"), reachable from this process.
        self._client_kwargs: dict[str, Any] = {**_common, "endpoint_url": settings.STORAGE_ENDPOINT}
        # Presigned URLs are consumed by the end user's BROWSER, so they're signed
        # against a browser-reachable host (STORAGE_PUBLIC_ENDPOINT, e.g.
        # "localhost:9000"), falling back to the internal endpoint if unset.
        self._presign_client_kwargs: dict[str, Any] = {
            **_common,
            "endpoint_url": settings.STORAGE_PUBLIC_ENDPOINT or settings.STORAGE_ENDPOINT,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self):
        """boto3 client for REAL operations (connects to the internal endpoint)."""
        return boto3.client(**self._client_kwargs)

    def _get_presign_client(self):
        """boto3 client used ONLY to SIGN URLs (browser-reachable endpoint)."""
        return boto3.client(**self._presign_client_kwargs)

    def _sync_ensure_bucket(self) -> None:
        client = self._get_client()
        try:
            client.head_bucket(Bucket=self._bucket)
        except Exception:
            try:
                client.create_bucket(Bucket=self._bucket)
            except Exception:
                pass  # already exists / race — ignore

    async def ensure_bucket(self) -> None:
        """Create the storage bucket if missing — call once at startup."""
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, self._sync_ensure_bucket)

    @staticmethod
    def _validate_upload(filename: str, mime_type: str) -> str:
        """Validate filename and MIME type; return the file extension.

        Raises ValidationError for any blocked or unsupported input.
        """
        if len(filename) > _MAX_FILENAME_LEN:
            raise ValidationError(
                f"Filename exceeds maximum length of {_MAX_FILENAME_LEN} characters"
            )

        ext = PurePosixPath(filename).suffix.lower()

        if ext in BLOCKED_EXTENSIONS:
            raise ValidationError(
                f"File extension '{ext}' is not permitted for upload"
            )

        if mime_type not in ALLOWED_MIMES:
            raise ValidationError(
                f"MIME type '{mime_type}' is not in the allowed list. "
                f"Supported: {', '.join(sorted(ALLOWED_MIMES))}"
            )

        return ext

    def _build_quarantine_key(self, tenant_id: str, ext: str) -> str:
        """Return a storage key under the quarantine prefix."""
        unique = str(uuid7())
        # ext is already lower-cased (e.g. ".pdf"); strip leading dot for clarity
        return f"uploads/quarantine/{tenant_id}/{unique}{ext}"

    # ------------------------------------------------------------------
    # Sync operations (run inside executor)
    # ------------------------------------------------------------------

    def _sync_presign_upload(
        self,
        storage_key: str,
        mime_type: str,
        max_bytes: int,
    ) -> str:
        """Generate a presigned PUT URL with a content-length restriction.

        Raises ClientError / BotoCoreError on S3 connectivity failure
        (caller converts to ExternalServiceError).
        """
        client = self._get_presign_client()
        url: str = client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": self._bucket,
                "Key": storage_key,
                "ContentType": mime_type,
                "ContentLength": max_bytes,
            },
            ExpiresIn=self._expiry,
            HttpMethod="PUT",
        )
        return url

    def _sync_presign_download(self, storage_key: str) -> str:
        """Generate a short-lived presigned GET URL."""
        client = self._get_presign_client()
        url: str = client.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": self._bucket,
                "Key": storage_key,
            },
            ExpiresIn=self._expiry,
            HttpMethod="GET",
        )
        return url

    def _sync_delete_object(self, storage_key: str) -> None:
        """Hard-delete a stored object — best-effort (errors are swallowed by caller)."""
        client = self._get_client()
        client.delete_object(Bucket=self._bucket, Key=storage_key)

    # ------------------------------------------------------------------
    # Public async interface
    # ------------------------------------------------------------------

    async def presigned_upload_url(
        self,
        tenant_id: str,
        filename: str,
        mime_type: str,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> tuple[str, str]:
        """Return (presigned_upload_url, storage_key).

        Storage key format: uploads/quarantine/{tenant_id}/{uuid7}.{ext}

        The quarantine prefix is intentional — a virus-scan pipeline is
        expected to move clean files to uploads/files/ before they are served.

        Raises:
            ValidationError: MIME type not in allowlist, extension in blocklist,
                             or filename too long.
            ExternalServiceError: S3 connectivity or signing failure.
        """
        ext = self._validate_upload(filename, mime_type)
        storage_key = self._build_quarantine_key(tenant_id, ext)

        loop = asyncio.get_event_loop()
        try:
            url: str = await loop.run_in_executor(
                None,
                partial(self._sync_presign_upload, storage_key, mime_type, max_bytes),
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error(
                "s3_presign_upload_failed",
                extra={"storage_key": storage_key, "error": str(exc)},
            )
            raise ExternalServiceError("s3") from exc

        return url, storage_key

    async def presigned_download_url(self, storage_key: str) -> str:
        """Return a short-lived presigned GET URL for reading a stored file.

        Raises:
            ExternalServiceError: S3 connectivity or signing failure.
        """
        loop = asyncio.get_event_loop()
        try:
            url: str = await loop.run_in_executor(
                None,
                partial(self._sync_presign_download, storage_key),
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error(
                "s3_presign_download_failed",
                extra={"storage_key": storage_key, "error": str(exc)},
            )
            raise ExternalServiceError("s3") from exc

        return url

    async def delete_object(self, storage_key: str) -> None:
        """Best-effort deletion of a stored S3 object.

        Errors are logged and swallowed — the caller records the attachment
        deletion regardless (avoids orphan DB rows from a temporary S3 outage).
        """
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                partial(self._sync_delete_object, storage_key),
            )
        except Exception as exc:
            logger.warning(
                "s3_delete_object_failed",
                extra={"storage_key": storage_key, "error": str(exc)},
            )


# Module-level singleton — instantiated once at import time so that the
# Settings lru_cache is only touched once per process.
_storage_service: StorageService | None = None


def get_storage_service() -> StorageService:
    """FastAPI dependency / convenience accessor."""
    global _storage_service
    if _storage_service is None:
        _storage_service = StorageService()
    return _storage_service
