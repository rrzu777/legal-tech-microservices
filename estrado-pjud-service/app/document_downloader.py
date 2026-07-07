"""Download PJUD documents using form action + dtaDoc JWT."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NamedTuple

import httpx

if TYPE_CHECKING:
    from app.session import OJVSession

logger = logging.getLogger(__name__)

MAX_DOC_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_CONCURRENT = 3
DOWNLOAD_DELAY_S = 0.5

# Retry acotado para flakiness transitoria de la IP residencial (peer closed /
# RemoteProtocolError, ProxyError 504, timeouts de lectura). Solo reintenta
# errores de transporte httpx — un fallo no-transitorio (token malo, bug) NO se
# reintenta. Al agotar los intentos el doc se descarta (None): no-fatal, el sync
# sigue. httpx.TransportError cubre RemoteProtocolError, ProxyError,
# TimeoutException, ConnectError y ReadError.
DOC_RETRY_ATTEMPTS = 3
DOC_RETRY_BACKOFF_S = 1.0

_CONTENT_TYPE_EXT = {
    "application/pdf": "pdf",
    "text/html": "html",
    "image/jpeg": "jpg",
    "image/png": "png",
}


class DownloadedDoc(NamedTuple):
    index: int
    data: bytes
    content_type: str
    extension: str


async def _fetch_with_retry(
    session: OJVSession, url: str, token: str, param: str
) -> httpx.Response:
    """Descarga un documento reintentando solo ante errores de transporte.

    Reintenta hasta DOC_RETRY_ATTEMPTS con backoff lineal. Re-lanza la última
    excepción transitoria si se agotan los intentos (el caller la captura y
    descarta el doc). Errores no-transitorios se propagan de inmediato.
    """
    for attempt in range(DOC_RETRY_ATTEMPTS):
        try:
            return await session.download_document(url, token, param)
        except httpx.TransportError as e:
            if attempt == DOC_RETRY_ATTEMPTS - 1:
                raise
            logger.info(
                "Descarga transitoria falló (intento %d/%d): %s; reintentando",
                attempt + 1, DOC_RETRY_ATTEMPTS, type(e).__name__,
            )
            await asyncio.sleep(DOC_RETRY_BACKOFF_S * (attempt + 1))
    raise RuntimeError("unreachable")  # el loop retorna o re-lanza; satisface el type checker


async def download_documents(
    session: OJVSession,
    movements: list[dict],
) -> list[DownloadedDoc]:
    """Download documents for movements that have documento_url + documento_token.

    Uses OJVSession.download_document() which preserves PJUD cookies and
    respects the adapter's built-in rate limiting.

    Additional limits: max 3 concurrent, 500ms delay between starts.
    Skips documents that are too large or fail to download.
    """
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    results: list[DownloadedDoc] = []

    async def _download_one(idx: int, mov: dict) -> DownloadedDoc | None:
        url = mov.get("documento_url")
        token = mov.get("documento_token")
        param_name = mov.get("documento_param", "dtaDoc")
        if not url or not token:
            return None

        async with sem:
            await asyncio.sleep(DOWNLOAD_DELAY_S)
            try:
                resp = await _fetch_with_retry(session, url, token, param_name)

                content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
                ext = _CONTENT_TYPE_EXT.get(content_type, "bin")

                if len(resp.content) > MAX_DOC_SIZE:
                    logger.warning("Document %d too large (%d bytes), skipping", idx, len(resp.content))
                    return None

                if len(resp.content) < 100:
                    logger.warning("Document %d suspiciously small (%d bytes), skipping", idx, len(resp.content))
                    return None

                return DownloadedDoc(index=idx, data=resp.content, content_type=content_type, extension=ext)

            except Exception:
                logger.warning("Failed to download document %d from %s", idx, url, exc_info=True)
                return None

    tasks = [_download_one(i, m) for i, m in enumerate(movements)]
    for result in await asyncio.gather(*tasks):
        if result is not None:
            results.append(result)

    logger.info("Downloaded %d/%d documents", len(results), sum(1 for m in movements if m.get("documento_url")))
    return results


async def download_single_document(
    session: "OJVSession",
    url: str,
    token: str,
    param: str = "dtaDoc",
) -> DownloadedDoc | None:
    """Download a single document by URL + token. Returns None on failure."""
    try:
        resp = await _fetch_with_retry(session, url, token, param)
        content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        ext = _CONTENT_TYPE_EXT.get(content_type, "bin")
        if len(resp.content) > MAX_DOC_SIZE:
            logger.warning("Document too large (%d bytes), skipping", len(resp.content))
            return None
        if len(resp.content) < 100:
            logger.warning("Document suspiciously small (%d bytes), skipping", len(resp.content))
            return None
        return DownloadedDoc(index=0, data=resp.content, content_type=content_type, extension=ext)
    except Exception:
        logger.warning("Failed to download document from %s", url, exc_info=True)
        return None
