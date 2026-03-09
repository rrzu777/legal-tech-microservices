"""Parse PJUD search-results HTML into structured dicts."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.parsers.normalizer import normalize_date


class AuthenticationRequired(Exception):
    """Raised when OJV requires authentication for a competencia."""
    pass


class NoResultsFound(Exception):
    """Raised when OJV returns no results for a search."""
    pass


def detect_auth_redirect(html: str) -> bool:
    """Detect if OJV redirected to index (auth required or ROL not found)."""
    return 'parent.window.open' in html or 'index.php' in html


# Matches detalleCausaCivil('eyJ...'), detalleCausaLaboral('eyJ...'), etc.
_JWT_RE = re.compile(
    r"detalleCausa\w+\('(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)'\)"
)


def _clean(text: str) -> str:
    """Strip and collapse whitespace in extracted cell text."""
    return " ".join(text.split())


def _parse_civil_row(tr) -> dict | None:
    """Parse a Civil search-result row.

    Column layout (5 <td>):
        0: search icon with onClick="detalleCausaCivil('JWT')"
        1: ROL  (e.g. C-1234-2024)
        2: fecha_ingreso  (DD/MM/YYYY)
        3: caratulado
        4: tribunal
    """
    tds = tr.find_all("td", recursive=False)
    if len(tds) < 5:
        return None

    a_tag = tds[0].find("a", onclick=True)
    if not a_tag:
        return None

    m = _JWT_RE.search(a_tag["onclick"])
    if not m:
        return None

    return {
        "key": m.group(1),
        "rol": _clean(tds[1].get_text()),
        "fecha_ingreso": normalize_date(_clean(tds[2].get_text())),
        "caratulado": _clean(tds[3].get_text()),
        "tribunal": _clean(tds[4].get_text()),
    }


def _parse_laboral_row(tr) -> dict | None:
    """Parse a Laboral search-result row.

    Column layout (6 <td>):
        0: search icon with onClick="detalleCausaLaboral('JWT')"
        1: ROL  (e.g. T-500-2024)
        2: tribunal
        3: caratulado
        4: fecha_ingreso  (DD/MM/YYYY)
        5: estado
    """
    tds = tr.find_all("td", recursive=False)
    if len(tds) < 5:
        return None

    a_tag = tds[0].find("a", onclick=True)
    if not a_tag:
        return None

    m = _JWT_RE.search(a_tag["onclick"])
    if not m:
        return None

    return {
        "key": m.group(1),
        "rol": _clean(tds[1].get_text()),
        "tribunal": _clean(tds[2].get_text()),
        "caratulado": _clean(tds[3].get_text()),
        "fecha_ingreso": normalize_date(_clean(tds[4].get_text())),
    }


def _parse_cobranza_row(tr) -> dict | None:
    """Parse a Cobranza search-result row.

    Column layout (7 <td>):
        0: search icon with onClick="detalleCausaCobranza('JWT')"
        1: ROL  (e.g. C-1000-2024)
        2: RIT number
        3: tribunal
        4: caratulado
        5: fecha_ingreso  (DD/MM/YYYY)
        6: estado
    """
    tds = tr.find_all("td", recursive=False)
    if len(tds) < 6:
        return None

    a_tag = tds[0].find("a", onclick=True)
    if not a_tag:
        return None

    m = _JWT_RE.search(a_tag["onclick"])
    if not m:
        return None

    return {
        "key": m.group(1),
        "rol": _clean(tds[1].get_text()),
        "tribunal": _clean(tds[3].get_text()),
        "caratulado": _clean(tds[4].get_text()),
        "fecha_ingreso": normalize_date(_clean(tds[5].get_text())),
    }


_ROW_PARSERS = {
    "civil": _parse_civil_row,
    "laboral": _parse_laboral_row,
    "cobranza": _parse_cobranza_row,
}


def parse_search_results(html: str, competencia: str) -> list[dict]:
    """Extract case search results from PJUD HTML.

    Parameters
    ----------
    html:
        Raw HTML string returned by the PJUD search endpoint.
    competencia:
        One of ``"civil"``, ``"laboral"``, ``"cobranza"`` (case-insensitive).

    Returns
    -------
    list[dict]
        Each dict contains keys: ``key``, ``rol``, ``tribunal``,
        ``caratulado``, ``fecha_ingreso``.
    """
    comp = competencia.lower()

    if detect_auth_redirect(html):
        raise AuthenticationRequired(
            f"OJV requires authentication for competencia={competencia!r}"
        )

    row_parser = _ROW_PARSERS.get(comp)
    if row_parser is None:
        raise ValueError(f"Unknown competencia: {competencia!r}")

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    for tr in soup.find_all("tr"):
        parsed = row_parser(tr)
        if parsed is not None:
            results.append(parsed)

    return results


def detect_blocked(html: str) -> bool:
    """Detect whether the response indicates blocking (captcha / empty).

    Returns ``True`` when the HTML is empty or contains a reCAPTCHA widget.
    """
    if not html or not html.strip():
        return True

    soup = BeautifulSoup(html, "html.parser")
    if soup.find(class_="g-recaptcha"):
        return True
    if soup.find(attrs={"data-sitekey": True}):
        return True

    return False
