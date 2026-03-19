"""Parse the HTML returned by PJUD anexo modal endpoints."""

import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def parse_anexo_list(html: str) -> list[dict]:
    """Parse an anexo modal HTML fragment.

    Returns list of dicts with:
      - download_url: form action path
      - download_token: JWT for dtaDoc param
      - download_param: always "dtaDoc"
      - label: document type/reference text
      - codigo: code string if present
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    table = soup.find("table")
    if not table:
        logger.warning("No table found in anexo HTML (%d chars)", len(html))
        return results

    tbody = table.find("tbody") or table
    rows = tbody.find_all("tr")

    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 2:
            continue

        # First column: document download form
        form = tds[0].find("form")
        if not form:
            continue

        action = form.get("action", "")
        token_input = form.find("input", {"name": "dtaDoc"})
        if not token_input:
            continue

        token = token_input.get("value", "")
        if not token:
            continue

        # Remaining columns: label/reference info (flexible across competencias)
        label_parts = []
        for td in tds[1:]:
            text = td.get_text(strip=True)
            if text and text != "0" and len(text) < 200:
                label_parts.append(text)

        label = " — ".join(label_parts) if label_parts else "Anexo"
        codigo = tds[1].get_text(strip=True) if len(tds) > 1 else None

        results.append({
            "download_url": action,
            "download_token": token,
            "download_param": "dtaDoc",
            "label": label,
            "codigo": codigo,
        })

    logger.info("Parsed %d anexo documents from HTML", len(results))
    return results
