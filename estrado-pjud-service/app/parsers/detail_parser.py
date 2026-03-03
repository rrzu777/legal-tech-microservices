"""Parser for PJUD case detail HTML pages."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from app.parsers.normalizer import normalize_date

_WS_RE = re.compile(r"\s+")


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
    """Extract the 6 metadata fields from the first table-titulos."""
    metadata: dict[str, str] = {}

    tables = soup.find_all("table", class_="table-titulos")
    if not tables:
        return metadata

    # Metadata lives in the first table-titulos
    table = tables[0]
    rows = table.find_all("tr")

    for row in rows:
        tds = row.find_all("td")
        for td in tds:
            text = _clean(td.get_text())
            if not text:
                continue

            # ROL
            if td.find("strong", string=re.compile(r"ROL")):
                metadata["rol"] = _extract_text_after_strong(td, "ROL:")

            # Estado Administrativo
            if td.find("strong", string=re.compile(r"Est\. Adm\.")):
                metadata["estado_administrativo"] = _extract_text_after_strong(td, "Est. Adm.")

            # Procedimiento
            if td.find("strong", string=re.compile(r"Proc\.:")) and "Estado" not in text.split("Proc")[0]:
                # Make sure this is "Proc.:" and not "Estado Proc.:"
                strong_tag = td.find("strong", string=re.compile(r"Proc\.:"))
                if strong_tag:
                    strong_text = _clean(strong_tag.get_text())
                    if strong_text == "Proc.:":
                        metadata["procedimiento"] = _extract_text_after_strong(td, "Proc.:")

            # Estado Procesal
            if td.find("strong", string=re.compile(r"Estado Proc\.")):
                metadata["estado_procesal"] = _extract_text_after_strong(td, "Estado Proc.:")

            # Etapa
            if td.find("strong", string=re.compile(r"^Etapa:")):
                metadata["etapa"] = _extract_text_after_strong(td, "Etapa:")

            # Tribunal
            if td.find("strong", string=re.compile(r"Tribunal:")):
                metadata["tribunal"] = _extract_text_after_strong(td, "Tribunal:")

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


def _parse_movements(soup: BeautifulSoup) -> list[dict]:
    """Extract movements from the Historia tab table."""
    movements: list[dict] = []

    historia_div = soup.find("div", id="historiaCiv")
    if not historia_div:
        return movements

    table = historia_div.find("table", class_="table-bordered")
    if not table:
        return movements

    cuaderno = _get_selected_cuaderno(soup)

    tbody = table.find("tbody")
    if not tbody:
        return movements

    for row in tbody.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) < 8:
            continue

        # Column indices: 0=Folio, 1=Doc, 2=Anexo, 3=Etapa, 4=Tramite,
        #                 5=Desc Tramite, 6=Fec Tramite, 7=Foja, 8=Georref

        folio = _int_or_none(tds[0].get_text())
        etapa = _clean(tds[3].get_text())
        tramite = _clean(tds[4].get_text())
        descripcion = _clean(tds[5].get_text())
        fecha = _normalize_movement_date(_clean(tds[6].get_text()))
        foja = _int_or_none(tds[7].get_text())

        # Check for document URL in the Doc column
        doc_form = tds[1].find("form")
        documento_url = None
        if doc_form:
            action = doc_form.get("action", "")
            if action:
                documento_url = action

        movements.append(
            {
                "folio": folio,
                "cuaderno": cuaderno,
                "etapa": etapa,
                "tramite": tramite,
                "descripcion": descripcion,
                "fecha": fecha,
                "foja": foja,
                "documento_url": documento_url,
            }
        )

    return movements


def _parse_litigantes(soup: BeautifulSoup) -> list[dict]:
    """Extract litigantes from the Litigantes tab table."""
    litigantes: list[dict] = []

    lit_div = soup.find("div", id="litigantesCiv")
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

    return {
        "metadata": _parse_metadata(soup),
        "movements": _parse_movements(soup),
        "litigantes": _parse_litigantes(soup),
    }
