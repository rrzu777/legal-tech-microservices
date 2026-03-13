import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.document_downloader import download_documents, DownloadedDoc, MAX_DOC_SIZE


@pytest.mark.asyncio
async def test_downloads_document_with_token():
    mock_resp = MagicMock()
    mock_resp.content = b"%PDF-1.4 fake content here plus padding to exceed minimum size check of one hundred bytes total needed for the test to pass validation"
    mock_resp.headers = {"content-type": "application/pdf"}
    mock_resp.raise_for_status = MagicMock()

    session = AsyncMock()
    session.download_document.return_value = mock_resp

    movements = [
        {"documento_url": "ADIR/civil/docuS.php", "documento_token": "eyJhbGciOiJ..."},
        {"documento_url": None, "documento_token": None},
    ]

    results = await download_documents(session, movements)

    assert len(results) == 1
    assert results[0].index == 0
    assert results[0].content_type == "application/pdf"
    assert results[0].extension == "pdf"
    session.download_document.assert_called_once_with(
        "ADIR/civil/docuS.php", "eyJhbGciOiJ...", "dtaDoc",
    )


@pytest.mark.asyncio
async def test_skips_oversized_documents():
    mock_resp = MagicMock()
    mock_resp.content = b"x" * (MAX_DOC_SIZE + 1)
    mock_resp.headers = {"content-type": "application/pdf"}
    mock_resp.raise_for_status = MagicMock()

    session = AsyncMock()
    session.download_document.return_value = mock_resp

    movements = [{"documento_url": "doc.php", "documento_token": "tok"}]
    results = await download_documents(session, movements)

    assert len(results) == 0


@pytest.mark.asyncio
async def test_skips_suspiciously_small_documents():
    mock_resp = MagicMock()
    mock_resp.content = b"tiny"
    mock_resp.headers = {"content-type": "application/pdf"}
    mock_resp.raise_for_status = MagicMock()

    session = AsyncMock()
    session.download_document.return_value = mock_resp

    movements = [{"documento_url": "doc.php", "documento_token": "tok"}]
    results = await download_documents(session, movements)

    assert len(results) == 0


@pytest.mark.asyncio
async def test_handles_download_failure_gracefully():
    session = AsyncMock()
    session.download_document.side_effect = Exception("Connection refused")

    movements = [{"documento_url": "doc.php", "documento_token": "tok"}]
    results = await download_documents(session, movements)

    assert len(results) == 0
