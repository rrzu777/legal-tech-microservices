"""Cloudflare R2 document storage (S3-compatible)."""

import asyncio
import logging
from contextvars import copy_context
from functools import partial
from typing import NamedTuple

import boto3
from botocore.config import Config as BotoConfig
from worker.maintenance import has_active_operation, track_auxiliary

logger = logging.getLogger(__name__)

MAX_DOC_SIZE = 10 * 1024 * 1024  # 10 MB


async def _run_s3(call, **kwargs):
    if has_active_operation():
        future = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, **kwargs),
        )
        return await track_auxiliary(future)
    return await asyncio.to_thread(call, **kwargs)


class UploadResult(NamedTuple):
    key: str
    content_type: str


class R2Client:
    """Thin wrapper around boto3 S3 client for Cloudflare R2."""

    def __init__(self, access_key_id: str, secret_access_key: str, endpoint: str, bucket: str):
        self._bucket = bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=BotoConfig(
                retries={"max_attempts": 2, "mode": "standard"},
                signature_version="s3v4",
                region_name="auto",
            ),
        )

    async def upload(self, key: str, data: bytes, content_type: str) -> UploadResult:
        """Upload document bytes to R2. Returns the storage key.

        Runs boto3 sync call in a thread to avoid blocking the async event loop.
        """
        if len(data) > MAX_DOC_SIZE:
            raise ValueError(f"Document too large: {len(data)} bytes (max {MAX_DOC_SIZE})")

        await _run_s3(
            self._s3.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.info("Uploaded %s (%s, %d bytes)", key, content_type, len(data))
        return UploadResult(key=key, content_type=content_type)

    async def exists(self, key: str) -> bool:
        """Check if a document already exists in R2.

        Runs boto3 sync call in a thread to avoid blocking the async event loop.
        """
        def head_exists():
            try:
                self._s3.head_object(Bucket=self._bucket, Key=key)
            except self._s3.exceptions.ClientError as exc:
                # A confirmed missing object is an ordinary outcome, not an
                # uncertain auxiliary. Other errors retain their real failed
                # future even though the legacy public API returns False.
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if code in {"404", "NoSuchKey", "NotFound"}:
                    return False
                raise
            return True
        try:
            return await _run_s3(head_exists)
        except self._s3.exceptions.ClientError:
            return False
