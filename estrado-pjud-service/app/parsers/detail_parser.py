"""Parser for PJUD case detail HTML pages."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

from bs4 import BeautifulSoup, Tag

from app.parsers.normalizer import normalize_date

_WS_RE = re.compile(r"\s+")

# Div IDs for movements per competencia type
_MOVEMENT_DIV_IDS = [
    "historiaCiv", "movimientoLab", "historiaCob",
    "movimientosSup", "movimientosApe",
    "historiaPen", "movimientosPen",
]

# Div IDs for litigantes/intervinientes per competencia type
_LITIGANTE_DIV_IDS = [
    "litigantesCiv", "litigantesLab", "litigantesCob",
    "litigantesSup", "litigantesApe",
    "intervinientesPen", "litigantesPen",
]


def _clean(text: str | None) -> str:
    """Strip and collapse whitespace."""
    if not text:
        return ""
    return _WS_RE.sub(" ", text.strip())


def _int_or_none(text: str | None) -> int | None:
    """Parse an integer from text, returning None on failure."""
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _extract_text_after_strong(td: Tag, label: str) -> str:
    """Extract the text content following a <strong> tag with the given label."""
    strong = td.find("strong", string=re.compile(re.escape(label), re.IGNORECASE))
    if not strong:
        return ""
    # For fields where value is inside a sibling <span>
    span = td.find("span", class_="topTool")
    if span and label in ("Est. Adm.",):
        return _clean(span.get_text())
    # Otherwise get all text in the td and strip the label prefix
    full_text = _clean(td.get_text())
    # Remove the label part (e.g. "ROL:" or "Proc.:")
    strong_text = _clean(strong.get_text())
    if full_text.startswith(strong_text):
        return full_text[len(strong_text):].strip()
    return full_text


def _parse_metadata(soup: BeautifulSoup) -> dict:
    """Extract metadata fields from the first table-titulos."""
    metadata: dict[str, str] = {}

    tables = soup.find_all("table", class_="table-titulos")
    if not tables:
        logger.warning("No table.table-titulos found for metadata")
        return metadata

    # Metadata lives in the first table-titulos
    table = tables[0]
    rows = table.find_all("tr")

    # Log all strong labels for diagnostics
    all_labels = [_clean(s.get_text()) for s in table.find_all("strong")]
    logger.info("Metadata strong labels: %s", all_labels)

    for row in rows:
        tds = row.find_all("td")
        for td in tds:
            text = _clean(td.get_text())
            if not text:
                continue

            # ROL (civil) — case-insensitive
            if td.find("strong", string=re.compile(r"^ROL", re.IGNORECASE)):
                metadata["rol"] = _extract_text_after_strong(td, "ROL:")
                # Extract libro prefix from ROL: "C-1234-2024" → "C"
                rol_val = metadata.get("rol", "")
                if "-" in rol_val:
                    metadata.setdefault("libro", rol_val.split("-")[0].strip())

            # RIT (laboral/cobranza/penal) — maps to rol; handles "RIT :" and "RIT:"
            if td.find("strong", string=re.compile(r"^RIT\s*:")):
                strong_tag = td.find("strong", string=re.compile(r"^RIT\s*:"))
                metadata["rol"] = _extract_text_after_strong(td, _clean(strong_tag.get_text()))
                rit_val = metadata.get("rol", "")
                if "-" in rit_val:
                    metadata.setdefault("libro", rit_val.split("-")[0].strip())

            # RUC (cobranza/penal) — handles "RUC :" and "RUC:"
            if td.find("strong", string=re.compile(r"^RUC\s*:")):
                strong_tag = td.find("strong", string=re.compile(r"^RUC\s*:"))
                metadata["ruc"] = _extract_text_after_strong(td, _clean(strong_tag.get_text()))

            # Libro (suprema/apelaciones) — maps to rol
            if td.find("strong", string=re.compile(r"^Libro")):
                metadata["rol"] = _extract_text_after_strong(td, "Libro :")
                metadata["libro"] = _extract_text_after_strong(td, "Libro :")

            # Estado Administrativo (civil)
            if td.find("strong", string=re.compile(r"Est\. Adm\.")):
                metadata["estado_administrativo"] = _extract_text_after_strong(td, "Est. Adm.")

            # Estado Recurso (apelaciones) — maps to estado_administrativo
            if td.find("strong", string=re.compile(r"Estado Recurso")):
                metadata["estado_administrativo"] = _extract_text_after_strong(td, "Estado Recurso:")

            # Procedimiento (civil)
            if td.find("strong", string=re.compile(r"Proc\.:")) and "Estado" not in text.split("Proc")[0]:
                strong_tag = td.find("strong", string=re.compile(r"Proc\.:"))
                if strong_tag:
                    strong_text = _clean(strong_tag.get_text())
                    if strong_text == "Proc.:":
                        metadata["procedimiento"] = _extract_text_after_strong(td, "Proc.:")

            # Estado Procesal
            if td.find("strong", string=re.compile(r"Estado Proc")):
                # Handle both "Estado Proc.:" (civil) and "Estado Procesal:" (suprema/apelaciones)
                strong_tag = td.find("strong", string=re.compile(r"Estado Proc"))
                if strong_tag:
                    strong_text = _clean(strong_tag.get_text())
                    metadata["estado_procesal"] = _extract_text_after_strong(td, strong_text)

            # Etapa (civil)
            if td.find("strong", string=re.compile(r"^Etapa:")):
                metadata["etapa"] = _extract_text_after_strong(td, "Etapa:")

            # Ubicación (suprema/apelaciones) — maps to etapa
            if td.find("strong", string=re.compile(r"Ubicaci")):
                strong_tag = td.find("strong", string=re.compile(r"Ubicaci"))
                if strong_tag:
                    strong_text = _clean(strong_tag.get_text())
                    full_text = _clean(td.get_text())
                    if full_text.startswith(strong_text):
                        metadata["ubicacion"] = full_text[len(strong_text):].strip()
                    else:
                        metadata["ubicacion"] = full_text

            # Tribunal (civil)
            if td.find("strong", string=re.compile(r"Tribunal:")):
                metadata["tribunal"] = _extract_text_after_strong(td, "Tribunal:")

            # Corte (apelaciones) — maps to tribunal
            if td.find("strong", string=re.compile(r"^Corte:")):
                metadata["tribunal"] = _extract_text_after_strong(td, "Corte:")

            # Fecha (suprema/apelaciones)
            if td.find("strong", string=re.compile(r"^Fecha :")):
                metadata["fecha"] = _extract_text_after_strong(td, "Fecha :")

            # Caratulado (suprema)
            if td.find("strong", string=re.compile(r"Caratulado")):
                metadata["caratulado"] = _extract_text_after_strong(td, "Caratulado:")

            # Tipo (suprema)
            if td.find("strong", string=re.compile(r"^Tipo:")):
                metadata["tipo"] = _extract_text_after_strong(td, "Tipo:")

            # Recurso (apelaciones)
            if td.find("strong", string=re.compile(r"^Recurso:")):
                metadata["recurso"] = _extract_text_after_strong(td, "Recurso:")

    return metadata


def _normalize_movement_date(raw: str) -> str | None:
    """Normalize a movement date, handling formats like '11/10/2024 (10/10/2024)'."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Take only the first date if there's a parenthetical second date
    first_date = raw.split(" ")[0].split("(")[0].strip()
    return normalize_date(first_date)


def _get_selected_cuaderno(soup: BeautifulSoup) -> str:
    """Extract the currently selected cuaderno name from the dropdown."""
    select = soup.find("select", id="selCuaderno")
    if not select:
        return ""
    option = select.find("option", selected=True)
    if not option:
        # Try first option as fallback
        option = select.find("option")
    if not option:
        return ""
    text = _clean(option.get_text())
    # Remove the leading number and dash, e.g. "1 - Principal" -> "Principal"
    parts = text.split(" - ", 1)
    if len(parts) == 2:
        return parts[1].strip()
    return text


def _find_div(soup: BeautifulSoup, div_ids: list[str]) -> tuple[Tag | None, str]:
    """Find the first existing div from a list of candidate IDs.

    Returns (div_tag, div_id) or (None, "") if none found.
    """
    for div_id in div_ids:
        div = soup.find("div", id=div_id)
        if div:
            return div, div_id
    return None, ""


# Column mapping per div_id: indices for each field, -1 means not present
_MOVEMENT_COLUMN_MAP: dict[str, dict] = {
    "movimientosSup": {
        "min_cols": 10, "folio": 0, "doc": 1, "fecha": 4,
        "tramite": 5, "descripcion": 6, "etapa": -1, "foja": -1,
    },
    "movimientosApe": {
        "min_cols": 9, "folio": 0, "doc": 1, "fecha": 5,
        "tramite": 3, "descripcion": 4, "etapa": -1, "foja": -1,
    },
    "historiaPen": {
        "min_cols": 8, "folio": 0, "doc": 1, "fecha": 6,
        "tramite": 4, "descripcion": 5, "etapa": 3, "foja": 7,
    },
    "movimientosPen": {
        "min_cols": 8, "folio": 0, "doc": 1, "fecha": 6,
        "tramite": 4, "descripcion": 5, "etapa": 3, "foja": 7,
    },
}

# Default column mapping for civil, laboral, cobranza (and fallback)
_CIVIL_COLS: dict = {
    "min_cols": 8, "folio": 0, "doc": 1, "fecha": 6,
    "tramite": 4, "descripcion": 5, "etapa": 3, "foja": 7,
}


def _parse_movements(soup: BeautifulSoup) -> list[dict]:
    """Extract movements from the Historia/Movimientos tab table."""
    movements: list[dict] = []

    mov_div, div_id = _find_div(soup, _MOVEMENT_DIV_IDS)
    if not mov_div:
        all_divs = [d.get("id") for d in soup.find_all("div", id=True)]
        logger.warning("No movement div found. Known IDs: %s. All div IDs in HTML: %s", _MOVEMENT_DIV_IDS, all_divs)
        return movements

    logger.info("Movement div found: id=%s", div_id)

    table = mov_div.find("table", class_="table-bordered")
    if not table:
        logger.warning("No table.table-bordered inside div#%s", div_id)
        return movements

    cuaderno = _get_selected_cuaderno(soup)
    cols = _MOVEMENT_COLUMN_MAP.get(div_id, _CIVIL_COLS)

    tbody = table.find("tbody")
    if not tbody:
        logger.warning("No tbody inside table in div#%s", div_id)
        return movements

    rows = tbody.find_all("tr")
    logger.info("div#%s: found %d rows, expecting min_cols=%d", div_id, len(rows), cols["min_cols"])

    for row in rows:
        tds = row.find_all("td")
        if len(tds) < cols["min_cols"]:
            if len(rows) <= 5 or rows.index(row) == 0:
                logger.warning("Row has %d cols (expected %d), skipping. First cells: %s",
                    len(tds), cols["min_cols"],
                    [_clean(td.get_text())[:30] for td in tds[:4]])
            continue

        folio = _int_or_none(tds[cols["folio"]].get_text())
        tramite = _clean(tds[cols["tramite"]].get_text())
        descripcion = _clean(tds[cols["descripcion"]].get_text())
        fecha = _normalize_movement_date(_clean(tds[cols["fecha"]].get_text()))
        etapa = _clean(tds[cols["etapa"]].get_text()) if cols["etapa"] >= 0 else ""
        foja = _int_or_none(tds[cols["foja"]].get_text()) if cols["foja"] >= 0 else None

        doc_form = tds[cols["doc"]].find("form")
        documento_url = None
        documento_token = None
        documento_param = None
        if doc_form:
            action = doc_form.get("action", "")
            if action:
                documento_url = action
            for param_name in ("dtaDoc", "valorDoc"):
                token_input = doc_form.find("input", {"name": param_name})
                if token_input:
                    documento_token = token_input.get("value")
                    documento_param = param_name
                    break

        movements.append({
            "folio": folio,
            "cuaderno": cuaderno,
            "etapa": etapa,
            "tramite": tramite,
            "descripcion": descripcion,
            "fecha": fecha,
            "foja": foja,
            "documento_url": documento_url,
            "documento_token": documento_token,
            "documento_param": documento_param,
        })

    return movements


def _parse_litigantes(soup: BeautifulSoup) -> list[dict]:
    """Extract litigantes from the Litigantes tab table."""
    litigantes: list[dict] = []

    lit_div, _ = _find_div(soup, _LITIGANTE_DIV_IDS)
    if not lit_div:
        return litigantes

    table = lit_div.find("table", class_="table-bordered")
    if not table:
        return litigantes

    tbody = table.find("tbody")
    if not tbody:
        return litigantes

    for row in tbody.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) < 4:
            continue

        rol = _clean(tds[0].get_text())
        rut = _clean(tds[1].get_text())
        nombre = _clean(tds[3].get_text())

        litigantes.append({"rol": rol, "rut": rut, "nombre": nombre})

    return litigantes


def parse_detail(html: str) -> dict:
    """Parse a PJUD case detail HTML page.

    Returns a dict with keys: metadata, movements, litigantes.
    """
    soup = BeautifulSoup(html, "html.parser")

    movements = _parse_movements(soup)
    litigantes = _parse_litigantes(soup)

    if not movements and not litigantes:
        # Log all div IDs in the HTML to help discover the correct ones
        all_divs = [div.get("id") for div in soup.find_all("div", id=True)]
        logger.warning("Parser found 0 movements and 0 litigantes. Div IDs in HTML: %s", all_divs)

    return {
        "metadata": _parse_metadata(soup),
        "movements": movements,
        "litigantes": litigantes,
    }
