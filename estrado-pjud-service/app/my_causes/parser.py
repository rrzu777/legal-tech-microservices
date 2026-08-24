"""Fail-closed parsers for the seven supported OJV ``Mis Causas`` tables.

The parser deliberately maps cells by the observed header text. It never reads
identities from action links and never returns HTML, URLs, form inputs or the
``Institución`` column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, cast

from bs4 import BeautifulSoup, Tag
from pydantic import ValidationError

from app.my_causes.models import ImportCandidate, Matter


_EMPTY_MESSAGES = (
    "no existen causas",
    "no se encontraron causas",
    "sin causas",
)


class UpstreamChangedError(ValueError):
    """The page cannot be interpreted under the reviewed listing contract."""

    code = "upstream_changed"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class _MatterSpec:
    form_name: str
    headers: frozenset[str]
    identifier_headers: tuple[str, ...]
    status_header: str
    location_header: str | None
    parser: Callable[[dict[str, Tag], "_MatterSpec", Matter], ImportCandidate]


def _clean(tag: Tag | None) -> str:
    if tag is None:
        return ""
    return " ".join(tag.get_text(" ", strip=True).split())


def _parse_date(raw: str) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError as exc:
        raise UpstreamChangedError() from exc


def _normalize_rol(raw: str) -> str:
    match = re.fullmatch(r"\s*(\d[\d.]*)\s*[-–—]\s*(\d{4})\s*", raw)
    if not match:
        raise UpstreamChangedError()
    return f"{match.group(1).replace('.', '')}-{match.group(2)}"


def _normalize_rit(raw: str) -> str:
    match = re.fullmatch(
        r"\s*([A-Za-z]+)\s*[-–—]\s*(\d[\d.]*)\s*[-–—]\s*(\d{4})\s*",
        raw,
    )
    if not match:
        raise UpstreamChangedError()
    return f"{match.group(1).upper()}-{match.group(2).replace('.', '')}-{match.group(3)}"


def _normalize_ruc(raw: str) -> str:
    compact = re.sub(r"\s+", "", raw).upper().replace("–", "-").replace("—", "-")
    if not re.fullmatch(r"\d{7,10}-[0-9K]", compact):
        raise UpstreamChangedError()
    return compact


def _candidate(
    cells: dict[str, Tag],
    spec: _MatterSpec,
    matter: Matter,
    *,
    case_type: str,
    case_number: str,
) -> ImportCandidate:
    location = cells.get(spec.location_header) if spec.location_header else None
    court = location if matter in {"suprema", "apelaciones"} else None
    tribunal = location if matter not in {"suprema", "apelaciones"} else None
    status = _clean(cells.get(spec.status_header))
    if not status:
        raise UpstreamChangedError()
    try:
        return ImportCandidate(
            matter=matter,
            case_type=case_type,
            case_number=case_number,
            # Listing cells expose labels, not reviewed stable catalog codes.
            court_code=None,
            court_label=_clean(court) or None,
            tribunal_code=None,
            tribunal_label=_clean(tribunal) or None,
            # OJV listing pages do not expose Libro. Civil's Cuaderno is not a book.
            libro=None,
            filed_at=_parse_date(_clean(cells.get("Fecha Ingreso"))),
            upstream_status=status,
            caption=_clean(cells.get("Caratulado")) or None,
        )
    except ValidationError as exc:
        raise UpstreamChangedError() from exc


def _parse_rol(cells: dict[str, Tag], spec: _MatterSpec, matter: Matter) -> ImportCandidate:
    return _candidate(
        cells,
        spec,
        matter,
        case_type="rol",
        case_number=_normalize_rol(_clean(cells.get("Rol"))),
    )


def _parse_rit(cells: dict[str, Tag], spec: _MatterSpec, matter: Matter) -> ImportCandidate:
    return _candidate(
        cells,
        spec,
        matter,
        case_type="rit",
        case_number=_normalize_rit(_clean(cells.get("Rit"))),
    )


def _parse_penal(cells: dict[str, Tag], spec: _MatterSpec, matter: Matter) -> ImportCandidate:
    rit = _clean(cells.get("Rit"))
    ruc = _clean(cells.get("Ruc"))
    if not rit and not ruc:
        raise UpstreamChangedError()
    return _candidate(
        cells,
        spec,
        matter,
        case_type="rit" if rit else "ruc",
        case_number=_normalize_rit(rit) if rit else _normalize_ruc(ruc),
    )


_COMMON = frozenset({"", "Caratulado", "Fecha Ingreso", "Institución"})
_SPECS: dict[Matter, _MatterSpec] = {
    "suprema": _MatterSpec(
        "formMisCauSuprema",
        _COMMON | {"Rol", "Estado Causa", "Corte"},
        ("Rol",),
        "Estado Causa",
        "Corte",
        _parse_rol,
    ),
    "apelaciones": _MatterSpec(
        "formMisCauApelacion",
        _COMMON | {"Rol", "Corte", "Estado Causa", "Fecha Ubicación", "Ubicación"},
        ("Rol",),
        "Estado Causa",
        "Corte",
        _parse_rol,
    ),
    "civil": _MatterSpec(
        "formMisCauCivil",
        _COMMON | {"Rit", "Tribunal", "Estado Cuaderno", "Cuaderno"},
        ("Rit",),
        "Estado Cuaderno",
        "Tribunal",
        _parse_rit,
    ),
    "laboral": _MatterSpec(
        "formMisCauLaboral",
        _COMMON | {"Rit", "Tribunal", "Estado Causa"},
        ("Rit",),
        "Estado Causa",
        "Tribunal",
        _parse_rit,
    ),
    "penal": _MatterSpec(
        "formMisCauPenal",
        _COMMON | {"Rit", "Ruc", "Tribunal", "Estado Causa"},
        ("Rit", "Ruc"),
        "Estado Causa",
        "Tribunal",
        _parse_penal,
    ),
    "cobranza": _MatterSpec(
        "formMisCauCobranza",
        _COMMON | {"Rit", "Tribunal", "Estado Procesal"},
        ("Rit",),
        "Estado Procesal",
        "Tribunal",
        _parse_rit,
    ),
    "familia": _MatterSpec(
        "formMisCauFamilia",
        _COMMON | {"Rit", "Tribunal", "Estado Procesal"},
        ("Rit",),
        "Estado Procesal",
        "Tribunal",
        _parse_rit,
    ),
}


def _header_map(table: Tag, spec: _MatterSpec) -> tuple[list[str], dict[str, int]]:
    header_row = table.find("thead")
    if not isinstance(header_row, Tag):
        raise UpstreamChangedError()
    headers = [_clean(cast(Tag, th)) for th in header_row.find_all("th")]
    if not headers or len(headers) != len(set(headers)) or frozenset(headers) != spec.headers:
        raise UpstreamChangedError()
    return headers, {header: index for index, header in enumerate(headers)}


def parse_my_causes_page(html: str, matter: Matter | str) -> list[ImportCandidate]:
    """Parse one listing page or raise ``upstream_changed`` without partial output."""

    if matter not in _SPECS:
        raise ValueError("unsupported_matter")
    typed_matter = cast(Matter, matter)
    spec = _SPECS[typed_matter]
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", attrs={"name": spec.form_name})
    if not isinstance(form, Tag):
        raise UpstreamChangedError()
    table = form.find("table")
    if not isinstance(table, Tag):
        raise UpstreamChangedError()
    headers, _ = _header_map(table, spec)
    body = table.find("tbody")
    if not isinstance(body, Tag):
        raise UpstreamChangedError()

    parsed: list[ImportCandidate] = []
    saw_empty_state = False
    for row in body.find_all("tr", recursive=False):
        cells = row.find_all("td", recursive=False)
        row_text = _clean(row).lower()
        if len(cells) == 1 and any(message in row_text for message in _EMPTY_MESSAGES):
            saw_empty_state = True
            continue
        if len(cells) != len(headers):
            raise UpstreamChangedError()
        mapped = {header: cast(Tag, cells[index]) for index, header in enumerate(headers)}
        parsed.append(spec.parser(mapped, spec, typed_matter))

    if saw_empty_state and parsed:
        raise UpstreamChangedError()
    if not parsed:
        if saw_empty_state:
            return []
        raise UpstreamChangedError()

    if typed_matter != "civil":
        return parsed

    # Civil lists one row per cuaderno. The stable cause identity excludes it.
    grouped: dict[tuple[str, tuple[str, int | str | None]], list[ImportCandidate]] = {}
    for candidate in parsed:
        tribunal_identity: tuple[str, int | str | None]
        if candidate.tribunal_code is not None:
            tribunal_identity = ("code", candidate.tribunal_code)
        else:
            tribunal_identity = (
                "label",
                candidate.tribunal_label.casefold() if candidate.tribunal_label else None,
            )
        key = (
            candidate.case_number,
            tribunal_identity,
        )
        grouped.setdefault(key, []).append(candidate)

    closed_statuses = {
        "archivada",
        "archivado",
        "concluida",
        "concluido",
        "fallada",
        "fallado",
        "terminada",
        "terminado",
    }
    unique: list[ImportCandidate] = []
    for candidates in grouped.values():
        statuses = {item.upstream_status for item in candidates if item.upstream_status}
        selected_status = min(
            statuses,
            key=lambda status: (status.casefold() in closed_statuses, status.casefold()),
        )
        unique.append(candidates[0].model_copy(update={"upstream_status": selected_status}))
    return unique
