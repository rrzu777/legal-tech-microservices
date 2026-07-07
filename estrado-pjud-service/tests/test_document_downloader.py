import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app import document_downloader
from app.document_downloader import download_documents, DownloadedDoc, MAX_DOC_SIZE


def _pdf_resp():
    resp = MagicMock()
    resp.content = b"%PDF-1.4 " + b"x" * 200
    resp.headers = {"content-type": "application/pdf"}
    return resp


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


@pytest.mark.asyncio
async def test_retries_transient_error_then_succeeds(monkeypatch):
    """Transient residential-proxy flakiness (RemoteProtocolError, ProxyError
    504, timeouts) should be retried, not dropped on the first failure."""
    monkeypatch.setattr(document_downloader.asyncio, "sleep", AsyncMock())
    session = AsyncMock()
    session.download_document.side_effect = [
        httpx.RemoteProtocolError("peer closed connection"),
        httpx.ProxyError("504 Gateway Timeout"),
        _pdf_resp(),
    ]

    movements = [{"documento_url": "doc.php", "documento_token": "tok"}]
    results = await download_documents(session, movements)

    assert len(results) == 1
    assert session.download_document.call_count == 3


@pytest.mark.asyncio
async def test_gives_up_after_exhausting_transient_retries(monkeypatch):
    """After DOC_RETRY_ATTEMPTS transient failures the doc is dropped (None),
    non-fatal — sync continues. Attempts are bounded."""
    monkeypatch.setattr(document_downloader.asyncio, "sleep", AsyncMock())
    session = AsyncMock()
    session.download_document.side_effect = httpx.ConnectError("no route to host")

    movements = [{"documento_url": "doc.php", "documento_token": "tok"}]
    results = await download_documents(session, movements)

    assert len(results) == 0
    assert session.download_document.call_count == document_downloader.DOC_RETRY_ATTEMPTS


@pytest.mark.asyncio
async def test_does_not_retry_non_transient_error(monkeypatch):
    """A non-transport error (e.g. bad token / programming error) is NOT a
    residential hiccup — fail fast, no retry."""
    monkeypatch.setattr(document_downloader.asyncio, "sleep", AsyncMock())
    session = AsyncMock()
    session.download_document.side_effect = ValueError("bad token")

    movements = [{"documento_url": "doc.php", "documento_token": "tok"}]
    results = await download_documents(session, movements)

    assert len(results) == 0
    assert session.download_document.call_count == 1
