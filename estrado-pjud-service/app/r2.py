"""Cloudflare R2 document storage (S3-compatible)."""

import logging
from typing import NamedTuple

import boto3
from botocore.config import Config as BotoConfig

logger = logging.getLogger(__name__)

MAX_DOC_SIZE = 10 * 1024 * 1024  # 10 MB


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

    def upload(self, key: str, data: bytes, content_type: str) -> UploadResult:
        """Upload document bytes to R2. Returns the storage key."""
        if len(data) > MAX_DOC_SIZE:
            raise ValueError(f"Document too large: {len(data)} bytes (max {MAX_DOC_SIZE})")

        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.info("Uploaded %s (%s, %d bytes)", key, content_type, len(data))
        return UploadResult(key=key, content_type=content_type)

    def exists(self, key: str) -> bool:
        """Check if a document already exists in R2."""
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except self._s3.exceptions.ClientError:
            return False
