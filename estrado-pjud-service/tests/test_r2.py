from unittest.mock import MagicMock, patch

from app.r2 import R2Client, MAX_DOC_SIZE

import pytest


def test_upload_stores_document():
    with patch("app.r2.boto3") as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.client.return_value = mock_s3

        client = R2Client("key", "secret", "https://r2.example.com", "test-bucket")
        result = client.upload("docs/test.pdf", b"%PDF-content", "application/pdf")

        mock_s3.put_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="docs/test.pdf",
            Body=b"%PDF-content",
            ContentType="application/pdf",
        )
        assert result.key == "docs/test.pdf"
        assert result.content_type == "application/pdf"


def test_upload_rejects_oversized_document():
    with patch("app.r2.boto3") as mock_boto:
        mock_boto.client.return_value = MagicMock()
        client = R2Client("key", "secret", "https://r2.example.com", "test-bucket")

        with pytest.raises(ValueError, match="too large"):
            client.upload("big.pdf", b"x" * (MAX_DOC_SIZE + 1), "application/pdf")


def test_exists_returns_true_when_found():
    with patch("app.r2.boto3") as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.client.return_value = mock_s3
        client = R2Client("key", "secret", "https://r2.example.com", "test-bucket")

        assert client.exists("docs/test.pdf") is True
        mock_s3.head_object.assert_called_once()
