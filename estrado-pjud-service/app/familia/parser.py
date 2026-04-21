"""Parse HTML from consultaMisCausasFamilia.php into structured dicts.

Column layout (colspan=7 confirmed from OJV HTML):
    0  — Detail link (onclick with detalleCausaFamilia or similar)
    1  — RIT  (e.g. C-123-2024)
    2  — Tribunal
    3  — Caratulado
    4  — Materia
    5  — Estado
    6  — Fecha ingreso (DD/MM/AAAA)

NOTE: Column positions have NOT been validated against a real authenticated
response. Adjust _COL_* constants below if the actual layout differs.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from app.familia.models import FamiliaCaso

logger = logging.getLogger(__name__)

# Adjust these if column order turns out to be different
_COL_RIT          = 1
_COL_TRIBUNAL     = 2
_COL_CARATULADO   = 3
_COL_MATERIA      = 4
_COL_ESTADO       = 5
_COL_FECHA        = 6
_MIN_COLS         = 6   # minimum tds to treat row as a data row

_NO_RESULTS_MSGS = [
    "no existen causas",
    "no se encontraron",
    "sin resultados",
    "sin causas",
]


def _clean(tag) -> str:
    return " ".join(tag.get_text().split()) if tag else ""


def _is_no_results(html: str) -> bool:
    lower = html.lower()
    return any(msg in lower for msg in _NO_RESULTS_MSGS)


def _is_auth_error(html: str) -> bool:
    lower = html.lower()
    return any(k in lower for k in [
        "no tiene permiso", "sesión expirada", "debe iniciar sesión",
        "acceso denegado", "session expired", "login",
    ])


def parse_familia_results(html: str) -> tuple[list[FamiliaCaso], str | None]:
    """Parse the HTML fragment returned by consultaMisCausasFamilia.php.

    Returns:
        (casos, error_code) — error_code is None on success,
        "no_cases" if zero results, "parse_error" on unexpected structure,
        "session_error" if OJV returned an auth error page.
    """
    if _is_auth_error(html):
        return [], "session_error"

    if _is_no_results(html):
        return [], "no_cases"

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")

    if not rows:
        # Might be an empty response or a format we don't recognize
        if html.strip():
            logger.warning("parse_familia: no <tr> rows found — unexpected format")
            return [], "parse_error"
        return [], "no_cases"

    casos: list[FamiliaCaso] = []
    skipped = 0

    for tr in rows:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < _MIN_COLS:
            skipped += 1
            continue

        # Skip header-like rows (th cells or "No existen causas" colspan)
        if tr.find("th"):
            continue
        first_td = tds[0]
        colspan = first_td.get("colspan")
        if colspan:
            text = _clean(first_td)
            if any(msg in text.lower() for msg in _NO_RESULTS_MSGS):
                return [], "no_cases"
            skipped += 1
            continue

        rit         = _clean(tds[_COL_RIT])         if len(tds) > _COL_RIT         else ""
        tribunal    = _clean(tds[_COL_TRIBUNAL])    if len(tds) > _COL_TRIBUNAL    else ""
        caratulado  = _clean(tds[_COL_CARATULADO])  if len(tds) > _COL_CARATULADO  else ""
        materia     = _clean(tds[_COL_MATERIA])     if len(tds) > _COL_MATERIA     else ""
        estado      = _clean(tds[_COL_ESTADO])      if len(tds) > _COL_ESTADO      else ""
        fecha       = _clean(tds[_COL_FECHA])       if len(tds) > _COL_FECHA       else None

        if not rit:
            skipped += 1
            continue

        casos.append(FamiliaCaso(
            rit=rit,
            tribunal=tribunal,
            caratulado=caratulado,
            materia=materia,
            estado=estado,
            fecha_ingreso=fecha or None,
        ))

    if skipped:
        logger.debug("parse_familia: skipped %d rows", skipped)

    if not casos:
        return [], "no_cases"

    return casos, None
