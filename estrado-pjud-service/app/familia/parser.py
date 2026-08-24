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

from app.familia.models import (
    FamiliaCaso,
    PrivateCauseResolution,
)
from app.my_causes.parser import UpstreamChangedError, parse_my_causes_page

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


class PrivateResolutionError(ValueError):
    """Safe, machine-readable rejection of non-materializable private evidence."""


def resolve_private_familia_html(
    html: str,
    *,
    expected_case_number: str,
    expected_tribunal_code: int | None,
    expected_tribunal_label: str,
    resolve_tribunal,
) -> PrivateCauseResolution:
    """Resolve exactly one authenticated listing row without returning raw data."""

    try:
        rows = parse_my_causes_page(html, "familia")
    except UpstreamChangedError as exc:
        raise PrivateResolutionError("upstream_changed") from exc
    exact_identifier = [row for row in rows if row.case_number == expected_case_number]
    if not exact_identifier:
        if rows:
            raise PrivateResolutionError("private_identifier_mismatch")
        raise PrivateResolutionError("private_not_found")
    if len(exact_identifier) != 1:
        raise PrivateResolutionError("private_ambiguous")
    row = exact_identifier[0]
    if row.tribunal_label is None or " ".join(row.tribunal_label.split()).casefold() \
       != " ".join(expected_tribunal_label.split()).casefold():
        raise PrivateResolutionError("private_tribunal_mismatch")
    resolved_code = resolve_tribunal(row.tribunal_label)
    if expected_tribunal_code is not None and resolved_code is not None \
       and resolved_code != expected_tribunal_code:
        raise PrivateResolutionError("private_tribunal_mismatch")

    # The listing proves only that one private identity is visible to this
    # session. It is not authenticated cause detail and exposes no reviewed
    # movement contract. Until the row action/detail request and Familia
    # tribunal mapping are captured as sanitized fixtures, never fabricate a
    # materializable payload from listing columns.
    raise PrivateResolutionError("upstream_changed")
