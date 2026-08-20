"""S3-compatible object storage (MinIO locally).

boto3 is synchronous, so calls are dispatched to a thread to keep the event loop
free. The client is created lazily and reused — boto3 clients are thread-safe.
"""

from __future__ import annotations

import asyncio
from typing import Any, BinaryIO

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import S3Settings, settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger

logger = get_logger(__name__)


#: The process-wide boto3 client. One per process so the connection pool and its
#: keep-alives are shared, which is the same reasoning `get_document_store`,
#: `get_mail_client` and `get_oidc_client` all give for the same shape.
_client: Any | None = None


def get_s3_client(config: S3Settings | None = None) -> Any:
    """The bucket client, built once per process.

    A module-level singleton rather than `@lru_cache`, and that is a fix rather than a
    preference. `lru_cache` hashes its arguments, and `S3Settings` is a pydantic model —
    which defines `__eq__` and is therefore unhashable. So the only caller that passes a
    config, `ObjectStorage.__init__`, raised `TypeError: unhashable type: 'S3Settings'`
    and **the documented default document store could not be constructed at all**.

    It survived because a developer `.env` setting `CWB_FNOL_DOCUMENT_STORE=filesystem`
    never reaches this line. CI, which has no `.env` and so takes the `s3` default, found
    it — as would any deployment using object storage, which `.env.example` calls the
    deployed answer.

    `config` is honoured on the first call and ignored afterwards, exactly as the other
    factories here behave. Tests that need a different client call `set_s3_client`.
    """
    global _client
    if _client is None:
        config = config or settings.s3
        _client = _build(config)
    return _client


def set_s3_client(client: Any | None) -> None:
    """Override the process client. For tests."""
    global _client
    _client = client


def _build(config: S3Settings) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        region_name=config.region,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if config.use_path_style else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


class ObjectStorage:
    """Thin async wrapper over the bucket this service owns."""

    def __init__(self, config: S3Settings | None = None) -> None:
        self._config = config or settings.s3
        self._client = get_s3_client(self._config)

    @property
    def bucket(self) -> str:
        return self._config.bucket

    async def _call(self, method: str, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(getattr(self._client, method), **kwargs)
        except (BotoCoreError, ClientError) as exc:
            logger.error("s3_call_failed", method=method, error=str(exc))
            raise ExternalServiceError(f"Object storage call `{method}` failed.") from exc

    async def put_object(
        self,
        key: str,
        body: bytes | BinaryIO,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        await self._call(
            "put_object",
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            Metadata=metadata or {},
        )

    async def get_object(self, key: str) -> bytes:
        response = await self._call("get_object", Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(response["Body"].read)

    async def delete_object(self, key: str) -> None:
        await self._call("delete_object", Bucket=self.bucket, Key=key)

    async def object_exists(self, key: str) -> bool:
        try:
            await self._call("head_object", Bucket=self.bucket, Key=key)
            return True
        except ExternalServiceError:
            return False

    async def presigned_get_url(self, key: str, *, expires_in: int | None = None) -> str:
        return await self._call(
            "generate_presigned_url",
            ClientMethod="get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in or self._config.presign_expiry_seconds,
        )

    async def presigned_put_url(self, key: str, *, expires_in: int | None = None) -> str:
        return await self._call(
            "generate_presigned_url",
            ClientMethod="put_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in or self._config.presign_expiry_seconds,
        )

    async def ensure_bucket(self) -> None:
        """Create the bucket when absent. Local/dev convenience."""
        try:
            await self._call("head_bucket", Bucket=self.bucket)
        except ExternalServiceError:
            logger.info("s3_creating_bucket", bucket=self.bucket)
            await self._call("create_bucket", Bucket=self.bucket)


def get_storage() -> ObjectStorage:
    return ObjectStorage()
