"""Parse HTML from consultaMisCausasFamilia.php into structured dicts.

Column layout (colspan=7 confirmed from OJV HTML):
    0  — Detail link
    1  — RIT  (e.g. C-123-2024)
    2  — Tribunal
    3  — Caratulado
    4  — Materia
    5  — Estado
    6  — Fecha ingreso (DD/MM/AAAA)

NOTE: Columns unvalidated against a real authenticated response.
Adjust _COL_* constants if the actual layout differs.
"""

from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup

from app.familia.models import FamiliaCaso

logger = logging.getLogger(__name__)

_COL_RIT        = 1
_COL_TRIBUNAL   = 2
_COL_CARATULADO = 3
_COL_MATERIA    = 4
_COL_ESTADO     = 5
_COL_FECHA      = 6
_MIN_COLS       = 6

_NO_RESULTS_MSGS = [
    "no existen causas",
    "no se encontraron",
    "sin resultados",
    "sin causas",
]

_AUTH_ERROR_MSGS = [
    "no tiene permiso", "sesión expirada", "debe iniciar sesión",
    "acceso denegado", "session expired", "login",
]


def _clean(tag: Any) -> str:
    return " ".join(tag.get_text().split()) if tag else ""


def parse_familia_results(html: str) -> tuple[list[FamiliaCaso], str | None]:
    """Return (casos, error_code); error_code is None on success."""
    html_lower = html.lower()

    if any(k in html_lower for k in _AUTH_ERROR_MSGS):
        return [], "session_error"

    if any(msg in html_lower for msg in _NO_RESULTS_MSGS):
        return [], "no_cases"

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")

    if not rows:
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

        if tr.find("th"):
            continue

        first_td = tds[0]
        if first_td.get("colspan"):
            text = _clean(first_td).lower()
            if any(msg in text for msg in _NO_RESULTS_MSGS):
                return [], "no_cases"
            skipped += 1
            continue

        rit        = _clean(tds[_COL_RIT])        if len(tds) > _COL_RIT        else ""
        tribunal   = _clean(tds[_COL_TRIBUNAL])   if len(tds) > _COL_TRIBUNAL   else ""
        caratulado = _clean(tds[_COL_CARATULADO]) if len(tds) > _COL_CARATULADO else ""
        materia    = _clean(tds[_COL_MATERIA])    if len(tds) > _COL_MATERIA    else ""
        estado     = _clean(tds[_COL_ESTADO])     if len(tds) > _COL_ESTADO     else ""
        fecha      = _clean(tds[_COL_FECHA])      if len(tds) > _COL_FECHA      else None

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

    return (casos, None) if casos else ([], "no_cases")
