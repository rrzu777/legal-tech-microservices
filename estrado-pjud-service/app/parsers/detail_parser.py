"""Parser for PJUD case detail HTML pages."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

from bs4 import BeautifulSoup, Tag

from app.parsers.normalizer import normalize_date

_WS_RE = re.compile(r"\s+")

# Regex to extract function name + JWT from anexo onclick handlers like:
#   anexoEscritoApelaciones('eyJ...')
#   anexoSolicitudCivil('eyJ...')
_ANEXO_RE = re.compile(r"(anexo\w+)\(\s*'(eyJ[^']+)'\s*\)")

# Regex for Suprema top-level document onclick handlers:
#   textoSuprema('eyJ...')  tomoSuprema('eyJ...')  documentosSuprema('eyJ...')
_SUPREMA_DOC_RE = re.compile(r"(textoSuprema|tomoSuprema|documentosSuprema)\(\s*'(eyJ[^']+)'\s*\)")

# Map function names to human-readable types
_SUPREMA_DOC_TYPES = {
    "textoSuprema": "texto_recurso",
    "tomoSuprema": "tomo",
    "documentosSuprema": "documentos",
}

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
        "min_cols": 10, "folio": 0, "doc": 1, "anexo": 2, "fecha": 4,
        "tramite": 5, "descripcion": 6, "etapa": -1, "foja": -1,
        "sala": 8, "estado": 9,
    },
    "movimientosApe": {
        "min_cols": 9, "folio": 0, "doc": 1, "anexo": 2, "fecha": 5,
        "tramite": 3, "descripcion": 4, "etapa": -1, "foja": -1,
        "sala": 6, "estado": 7,
    },
    "historiaPen": {
        "min_cols": 8, "folio": 0, "doc": 1, "anexo": 2, "fecha": 6,
        "tramite": 4, "descripcion": 5, "etapa": 3, "foja": 7,
        "sala": -1, "estado": -1,
    },
    "movimientosPen": {
        "min_cols": 8, "folio": 0, "doc": 1, "anexo": 2, "fecha": 6,
        "tramite": 4, "descripcion": 5, "etapa": 3, "foja": 7,
        "sala": -1, "estado": -1,
    },
}

# Default column mapping for civil, laboral, cobranza (and fallback)
_CIVIL_COLS: dict = {
    "min_cols": 8, "folio": 0, "doc": 1, "anexo": 2, "fecha": 6,
    "tramite": 4, "descripcion": 5, "etapa": 3, "foja": 7,
    "sala": -1, "estado": -1,
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
        sala = _clean(tds[cols["sala"]].get_text()) if cols.get("sala", -1) >= 0 and len(tds) > cols["sala"] else ""
        estado = _clean(tds[cols["estado"]].get_text()) if cols.get("estado", -1) >= 0 and len(tds) > cols["estado"] else ""

        # --- Doc column: extract ALL forms (main doc + certificate, etc.) ---
        doc_td = tds[cols["doc"]]
        all_forms = doc_td.find_all("form")
        documento_url = None
        documento_token = None
        documento_param = None
        documentos_adicionales: list[dict] = []

        # Known main-document param names (first match wins as primary doc)
        _MAIN_PARAMS = ("dtaDoc", "valorDoc", "valorFile")
        # Known additional-document param names
        _EXTRA_PARAMS = ("dtaCert",)

        for form in all_forms:
            action = form.get("action", "")
            if not action:
                continue

            # Try to find a token input in this form
            form_token = None
            form_param = None
            for pname in (*_MAIN_PARAMS, *_EXTRA_PARAMS):
                inp = form.find("input", {"name": pname})
                if inp:
                    form_token = inp.get("value")
                    form_param = pname
                    break

            if not form_token:
                continue

            if form_param in _MAIN_PARAMS and documento_url is None:
                # First main-document form → primary
                documento_url = action
                documento_token = form_token
                documento_param = form_param
            else:
                # Additional document (certificate or subsequent main forms)
                documentos_adicionales.append({
                    "url": action,
                    "token": form_token,
                    "param": form_param,
                })

        # --- Anexo column: extract function name + JWT from onclick handler ---
        anexo_token = None
        anexo_func = None
        anexo_idx = cols.get("anexo", -1)
        if anexo_idx >= 0 and anexo_idx < len(tds):
            anexo_td = tds[anexo_idx]
            # Look for an <a> tag with an onclick that calls an anexo function
            anexo_link = anexo_td.find("a", onclick=_ANEXO_RE)
            if anexo_link:
                onclick_val = anexo_link.get("onclick", "")
                m = _ANEXO_RE.search(onclick_val)
                if m:
                    anexo_func = m.group(1)
                    anexo_token = m.group(2)

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
            "documentos_adicionales": documentos_adicionales,
            "anexo_token": anexo_token,
            "anexo_func": anexo_func,
            "sala": sala,
            "estado": estado,
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
        persona = _clean(tds[2].get_text())
        nombre = _clean(tds[3].get_text())

        litigantes.append({"rol": rol, "rut": rut, "nombre": nombre, "persona": persona})

    return litigantes


def _parse_ebook_token(soup: BeautifulSoup) -> str:
    """Extract the ebook JWT token from the dtaEbook hidden input."""
    # Look for the ebook form input by name
    inp = soup.find("input", {"name": "dtaEbook"})
    if inp:
        val = inp.get("value", "")
        if val and val.startswith("eyJ"):
            return val
    return ""


def _parse_certificado_disponible(soup: BeautifulSoup) -> bool:
    """Check if the Certificado de Envío is downloadable (not disabled).

    Available: there's a <form> containing a dtaCert input near the
    "Certificado de Envío" strong label at the case level (not per-movement).

    Disabled (Suprema): the fa-ban icon is shown instead of a form.
    """
    # Find all <strong> tags mentioning "Certificado de Envío"
    cert_strongs = soup.find_all("strong", string=re.compile(r"Certificado de Env", re.IGNORECASE))
    for strong in cert_strongs:
        # Walk up to the containing td
        td = strong.find_parent("td")
        if not td:
            continue
        # This should be a top-level certificate (in table-titulos), not a per-movement one.
        # Check if the parent table is a table-titulos
        parent_table = td.find_parent("table", class_="table-titulos")
        if not parent_table:
            continue
        # Check for disabled indicator (fa-ban icon)
        ban_icon = td.find("i", class_=re.compile(r"fa-ban"))
        if ban_icon:
            return False
        # Check for a form with dtaCert — means it's downloadable
        form = td.find("form")
        if form:
            cert_input = form.find("input", {"name": "dtaCert"})
            if cert_input and cert_input.get("value"):
                return True
    return False


def _parse_suprema_docs(soup: BeautifulSoup) -> list[dict]:
    """Extract Suprema top-level document tokens (textoSuprema, tomoSuprema, documentosSuprema).

    Returns a list of dicts with keys: tipo, token, func.
    """
    docs: list[dict] = []
    # Find all links with onclick matching the suprema doc pattern
    for link in soup.find_all("a", onclick=_SUPREMA_DOC_RE):
        onclick_val = link.get("onclick", "")
        m = _SUPREMA_DOC_RE.search(onclick_val)
        if m:
            func_name = m.group(1)
            token = m.group(2)
            docs.append({
                "tipo": _SUPREMA_DOC_TYPES.get(func_name, func_name),
                "token": token,
                "func": func_name,
            })
    return docs


# Div IDs for exhortos per competencia type
_EXHORTO_DIV_IDS = [
    "exhortosCiv", "ExhortosApe", "exhortosLab", "exhortosCob",
]

# Div IDs for incompetencia per competencia type
_INCOMPETENCIA_DIV_IDS = [
    "IncompetenciaApe", "incompetenciaApe",
]


def _parse_table_rows(div: Tag) -> list[dict]:
    """Parse a generic table inside a div, returning rows as dicts keyed by header text."""
    table = div.find("table", class_="table-bordered")
    if not table:
        return []

    # Extract header names
    thead = table.find("thead")
    if not thead:
        return []
    headers = [_clean(th.get_text()) for th in thead.find_all("th")]
    if not headers:
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    rows: list[dict] = []
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        row_data: dict[str, str] = {}
        for i, td in enumerate(tds):
            key = headers[i] if i < len(headers) else f"col_{i}"
            row_data[key] = _clean(td.get_text())
        rows.append(row_data)
    return rows


def _parse_exhortos(soup: BeautifulSoup) -> list[dict]:
    """Extract exhortos from the Exhortos tab table."""
    div, _ = _find_div(soup, _EXHORTO_DIV_IDS)
    if not div:
        return []
    return _parse_table_rows(div)


def _parse_incompetencia(soup: BeautifulSoup) -> list[dict]:
    """Extract incompetencia records from the Incompetencia tab table."""
    div, _ = _find_div(soup, _INCOMPETENCIA_DIV_IDS)
    if not div:
        return []
    return _parse_table_rows(div)


def _parse_observacion(soup: BeautifulSoup) -> dict[str, str]:
    """Extract Observación tab fields (Suprema).

    Returns a dict with keys: naturaleza_recurso, numero_oficio,
    abogado_suspendido, tabla.
    """
    obs: dict[str, str] = {}
    obs_div = soup.find("div", id="observacionSup")
    if not obs_div:
        return obs

    # Find strong labels and extract the text after them
    for strong in obs_div.find_all("strong"):
        label = _clean(strong.get_text())
        # Get the parent div (col-sm-*) to extract value
        parent = strong.find_parent("div", class_=re.compile(r"col-"))
        if not parent:
            continue
        # Get all text in the parent div, remove the label portion
        full_text = _clean(parent.get_text())
        strong_text = label
        if full_text.startswith(strong_text):
            value = full_text[len(strong_text):].strip()
        else:
            value = full_text

        if "Naturaleza del Recurso" in label:
            obs["naturaleza_recurso"] = value
        elif "mero de Oficio" in label or "Número de Oficio" in label:
            obs["numero_oficio"] = value
        elif "Abogado Suspendido" in label:
            obs["abogado_suspendido"] = value
        elif label.startswith("Tabla:") or label == "Tabla:":
            obs["tabla"] = value

    return obs


def parse_detail(html: str) -> dict:
    """Parse a PJUD case detail HTML page.

    Returns a dict with keys: metadata, movements, litigantes,
    ebook_token, certificado_disponible, suprema_docs,
    exhortos, incompetencia.
    """
    soup = BeautifulSoup(html, "html.parser")

    movements = _parse_movements(soup)
    litigantes = _parse_litigantes(soup)

    if not movements and not litigantes:
        # Log all div IDs in the HTML to help discover the correct ones
        all_divs = [div.get("id") for div in soup.find_all("div", id=True)]
        logger.warning("Parser found 0 movements and 0 litigantes. Div IDs in HTML: %s", all_divs)

    metadata = _parse_metadata(soup)

    # Merge Observación fields into metadata (Suprema)
    obs = _parse_observacion(soup)
    metadata.update(obs)

    return {
        "metadata": metadata,
        "movements": movements,
        "litigantes": litigantes,
        "ebook_token": _parse_ebook_token(soup),
        "certificado_disponible": _parse_certificado_disponible(soup),
        "suprema_docs": _parse_suprema_docs(soup),
        "exhortos": _parse_exhortos(soup),
        "incompetencia": _parse_incompetencia(soup),
    }
