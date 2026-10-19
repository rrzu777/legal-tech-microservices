"""Fresh, session-affine Mis Causas detail, never public ADIR detail.

The two modal endpoints, dtaCausa argument and session CSRF token were read
from the authenticated official misCausas.php JavaScript on 2026-10-19.
Capabilities live only in this operation and never enter ImportCandidate.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import AsyncContextManager

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import SecretStr

from app.catalogs import CatalogService
from app.models import CaseMetadata, Litigante, PenalBookSafeDetail, PenalBookSafeMovement
from app.my_causes.client import _AJAX_HEADERS, _FORMS, _form_payload, _is_login_response, _next_page
from app.my_causes.identity import resolve_public_import_candidate
from app.my_causes.models import ImportCandidate
from app.my_causes.parser import parse_my_causes_page, UpstreamChangedError
from app.my_causes.private_models import AuthenticatedImportError
from app.ojv.session import OjvSession, decode_ojv_html
from app.parsers.detail_parser import _get_selected_cuaderno, _parse_metadata
from app.parsers.normalizer import normalize_date
from app.parsers.search_parser import detect_blocked

BASE = "https://oficinajudicialvirtual.pjud.cl"
_DETAIL = {
    "civil": ("detalleMisCausaCivil", "/misCausas/civil/modal/misCausasCivil.php"),
    "apelaciones": ("detalleMisCausaApelaciones", "/misCausas/apelaciones/modal/misCausasApelaciones.php"),
}
RequestScope = Callable[[str, str], AsyncContextManager[None]]


def _fold(value: str) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).casefold().split())


def _header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(value))


def _accepted_html(response: httpx.Response) -> str:
    html = decode_ojv_html(response)
    if response.url.host != "oficinajudicialvirtual.pjud.cl" or _is_login_response(response, html):
        raise AuthenticatedImportError("session_expired")
    if response.status_code in {403, 429} or detect_blocked(html):
        raise AuthenticatedImportError("waf")
    if response.status_code >= 500 or response.status_code == 408:
        raise AuthenticatedImportError("timeout")
    if response.status_code >= 400 or not html.strip() or len(response.content) > 5_000_000:
        raise AuthenticatedImportError("upstream_changed")
    return html


def _session_token(html: str, matter: str) -> SecretStr:
    name, endpoint = _DETAIL[matter]
    found = re.search(rf"function\s+{name}\s*\(val\)\s*\{{(.*?)\.done\(", html, re.S)
    if not found or "'.." + endpoint + "'" not in found.group(1):
        raise AuthenticatedImportError("upstream_changed")
    tokens = re.findall(r"\btoken:\s*'([a-f0-9]{32})'", found.group(1))
    if len(tokens) != 1 or not re.search(r"\bdtaCausa:\s*dtaCausa\b", found.group(1)):
        raise AuthenticatedImportError("upstream_changed")
    return SecretStr(tokens[0])


def _same_candidate(actual: ImportCandidate, expected: ImportCandidate) -> bool:
    for field in ("matter", "case_type", "case_number", "court_code", "tribunal_code"):
        if getattr(expected, field) is not None and getattr(actual, field) != getattr(expected, field):
            return False
    for field in ("court_label", "tribunal_label", "caption"):
        value = getattr(expected, field)
        if value and _fold(getattr(actual, field) or "") != _fold(value):
            return False
    return expected.filed_at is None or actual.filed_at == expected.filed_at


def _selected_actions(html: str, expected: ImportCandidate, catalogs: CatalogService) -> list[tuple[ImportCandidate, SecretStr]]:
    # Validate the whole page before looking at individual rows, preventing a
    # malformed/partial page from being accepted as a single exact match.
    if not parse_my_causes_page(html, expected.matter):
        return []
    soup = BeautifulSoup(html, "html.parser")
    actions: list[tuple[ImportCandidate, SecretStr]] = []
    name, _ = _DETAIL[expected.matter]
    action_pattern = re.compile(rf"^(?:javascript:\s*)?(?:return\s+)?{name}\(\s*['\"]([A-Za-z0-9_.=-]{{16,8192}})['\"]\s*\)\s*;?$")
    for row in soup.find_all("tr"):
        # The official endpoint emits reviewed headerless row fragments.
        if not row.find_all("td", recursive=False) or row.select_one(".pagination") is not None:
            continue
        parsed = parse_my_causes_page(str(row), expected.matter)
        if len(parsed) != 1:
            raise AuthenticatedImportError("upstream_changed")
        current = resolve_public_import_candidate(parsed[0], catalogs)
        if not _same_candidate(current, expected):
            continue
        found = []
        for element in row.find_all(True):
            for attr in ("href", "onclick"):
                match = action_pattern.fullmatch(str(element.get(attr, "")).strip())
                if match:
                    found.append(SecretStr(match.group(1)))
        if len(found) != 1:
            raise AuthenticatedImportError("upstream_changed")
        actions.append((current, found[0]))
    return actions


_MOVEMENT_HEADERS = {
    "folio": {"folio"}, "tramite": {"tramite"},
    "descripcion": {"desctramite", "descripcion", "descripciontramite"},
    "fecha": {"fecha", "fectramite", "fechatramite"},
    "etapa": {"etapa"}, "foja": {"foja", "fojas"},
    "sala": {"sala"}, "estado": {"estado", "estadofirma"},
}
_PARTY_HEADERS = {
    "rol": {"participante", "sujeto", "litigante", "tipolitigante", "tipo", "calidad"},
    "rut": {"rut"}, "persona": {"persona", "tipopersona"}, "nombre": {"nombre", "nombreorazonsocial"},
}


def _table_rows(soup: BeautifulSoup, aliases: dict[str, set[str]], required: set[str]) -> tuple[dict[str, int], list[Tag]]:
    found = []
    for table in soup.find_all("table"):
        headers = [_header(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        indices = {key: i for key, names in aliases.items() for i, name in enumerate(headers) if name in names}
        if required <= indices.keys():
            if any(sum(header in names for header in headers) > 1 for names in aliases.values()):
                raise AuthenticatedImportError("upstream_changed")
            body = table.find("tbody")
            if body is None:
                raise AuthenticatedImportError("private_evidence_incomplete")
            found.append((indices, list(body.find_all("tr", recursive=False))))
    if len(found) != 1:
        raise AuthenticatedImportError("private_evidence_incomplete")
    return found[0]


def parse_authenticated_detail(html: str, selected: ImportCandidate, catalogs: CatalogService) -> tuple[ImportCandidate, PenalBookSafeDetail]:
    soup = BeautifulSoup(html, "html.parser")
    metadata = _parse_metadata(soup)
    # Private modals render e.g. "Protección - 1234 - 2023". Only
    # whitespace around literal separators is presentation, never digit content.
    identifier = re.sub(r"\s*-\s*", "-", metadata.get("rol", "").strip())
    if not identifier or not re.fullmatch(r"(?:.+-)?" + re.escape(selected.case_number), identifier, re.I):
        raise AuthenticatedImportError("private_identifier_mismatch")
    if selected.matter == "civil" and identifier.upper() != selected.case_number.upper():
        raise AuthenticatedImportError("private_identifier_mismatch")
    if selected.matter == "apelaciones":
        if catalogs.resolve_loaded_court(metadata.get("tribunal", "")) != selected.court_code:
            raise AuthenticatedImportError("private_tribunal_mismatch")
        prefix = identifier[: -len(selected.case_number)].rstrip("- ")
        book = catalogs.resolve_loaded_book_label("apelaciones", prefix, int(selected.case_number.rsplit("-", 1)[1]), corte=selected.court_code)
        if book is None:
            raise AuthenticatedImportError("private_evidence_incomplete")
        selected = selected.model_copy(update={"libro": book["code"]})
    else:
        tribunal = catalogs.resolve_loaded_tribunal("civil", metadata.get("tribunal", ""))
        if tribunal is None or (selected.tribunal_code is not None and tribunal.tribunal_code != selected.tribunal_code) or (selected.court_code is not None and tribunal.court_code != selected.court_code):
            raise AuthenticatedImportError("private_tribunal_mismatch")
        book_code = selected.case_number.split("-", 1)[0]
        if catalogs.resolve_loaded_book("civil", book_code, int(selected.case_number.rsplit("-", 1)[1]), corte=tribunal.court_code) is None:
            raise AuthenticatedImportError("private_evidence_incomplete")
        selected = selected.model_copy(update={"tribunal_code": tribunal.tribunal_code, "court_code": tribunal.court_code, "tribunal_label": tribunal.tribunal_label, "libro": book_code})
    if any(len(value) > 4000 for value in metadata.values()):
        raise AuthenticatedImportError("private_evidence_incomplete")
    caption = metadata.get("caratulado") or selected.caption or ""
    if len(caption) < 4 or not re.search(r"[\w]", caption, re.UNICODE) or len(caption) > 500:
        raise AuthenticatedImportError("private_evidence_incomplete")
    metadata["caratulado"] = caption
    selected = selected.model_copy(update={"caption": caption})

    indices, rows = _table_rows(soup, _MOVEMENT_HEADERS, {"folio", "tramite", "descripcion", "fecha"})
    movements = []
    for row in rows:
        cells = row.find_all("td", recursive=False)
        if len(cells) <= max(indices.values()):
            raise AuthenticatedImportError("private_evidence_incomplete")
        values = {key: " ".join(cells[i].get_text(" ", strip=True).split()) for key, i in indices.items()}
        if any(len(value) > 4000 for value in values.values()):
            raise AuthenticatedImportError("private_evidence_incomplete")
        date = normalize_date(values["fecha"].split(" ")[0])
        if not date or not (values["tramite"] or values["descripcion"]):
            raise AuthenticatedImportError("private_evidence_incomplete")
        movements.append(PenalBookSafeMovement(
            folio=int(values["folio"]) if values["folio"].isdigit() else None,
            cuaderno=_get_selected_cuaderno(soup), etapa=values.get("etapa", ""),
            tramite=values["tramite"], descripcion=values["descripcion"], fecha=date,
            foja=int(values["foja"]) if values.get("foja", "").isdigit() else None,
            sala=values.get("sala", ""), estado=values.get("estado", ""),
        ))
    indices, rows = _table_rows(soup, _PARTY_HEADERS, {"rol", "rut", "nombre"})
    parties = []
    for row in rows:
        cells = row.find_all("td", recursive=False)
        if len(cells) <= max(indices.values()):
            raise AuthenticatedImportError("private_evidence_incomplete")
        values = {key: " ".join(cells[i].get_text(" ", strip=True).split()) for key, i in indices.items()}
        if not values["nombre"] or len(values["nombre"]) > 500 or len(values["rut"]) > 40 or len(values["rol"]) > 100 or len(values.get("persona", "")) > 100:
            raise AuthenticatedImportError("private_evidence_incomplete")
        parties.append(Litigante(**values))
    if not movements or not parties or len(movements) > 5000 or len(parties) > 500:
        raise AuthenticatedImportError("private_evidence_incomplete")
    return selected, PenalBookSafeDetail(metadata=CaseMetadata(**metadata), movements=movements, litigantes=parties, libro=selected.libro)


async def resolve_authenticated_detail(session: OjvSession, expected: ImportCandidate, scope: RequestScope) -> tuple[ImportCandidate, PenalBookSafeDetail]:
    if expected.matter not in _DETAIL:
        raise AuthenticatedImportError("private_identifier_mismatch")
    catalogs = CatalogService(None)
    async with scope("other", "form"):
        await session._wait()
        html = _accepted_html(await session._get(BASE + "/misCausas.php"))
    csrf = _session_token(html, expected.matter)
    html = ""
    spec = _FORMS[expected.matter]
    number, year = expected.case_number.rsplit("-", 2)[-2:]
    actions = []
    for page in range(1, 6):
        payload = _form_payload(spec, session.authenticated_form_identity(), include_closed=True, page=page)
        payload = [(key, number if key == f"rolMisCau{spec.prefix}" else year if key == f"anhoMisCau{spec.prefix}" else value) for key, value in payload]
        async with scope("search", f"listing:{page}"):
            html = _accepted_html(await session.post_form(BASE + spec.endpoint, payload, headers=_AJAX_HEADERS))
        try:
            actions.extend(_selected_actions(html, expected, catalogs))
        except UpstreamChangedError:
            raise AuthenticatedImportError("upstream_changed") from None
        if _next_page(html, page) is None:
            break
    else:
        raise AuthenticatedImportError("private_evidence_incomplete")
    if not actions:
        raise AuthenticatedImportError("private_not_found")
    if expected.matter == "apelaciones" and len(actions) != 1:
        raise AuthenticatedImportError("private_ambiguous")
    if len(actions) > 20:
        raise AuthenticatedImportError("private_evidence_incomplete")
    all_movements = []
    all_parties = []
    verified = None
    detail = None
    try:
        for index, (selected, action) in enumerate(actions):
            async with scope("detail", f"detail:{index}"):
                html = _accepted_html(await session.post_form(BASE + _DETAIL[expected.matter][1], [("dtaCausa", action.get_secret_value()), ("token", csrf.get_secret_value())], headers=_AJAX_HEADERS))
            current, parsed = parse_authenticated_detail(html, selected, catalogs)
            if verified is not None and current != verified:
                raise AuthenticatedImportError("private_ambiguous")
            verified, detail = current, parsed
            all_movements.extend(parsed.movements)
            all_parties.extend(parsed.litigantes)
        if verified is None or detail is None:
            raise AuthenticatedImportError("private_evidence_incomplete")
        # Normalized writers deduplicate movements by their canonical identities.
        if len(all_movements) > 5000 or len(all_parties) > 500:
            raise AuthenticatedImportError("private_evidence_incomplete")
        detail.movements = all_movements
        detail.litigantes = list({tuple(p.model_dump().values()): p for p in all_parties}.values())
        return verified, detail
    finally:
        html = ""
        csrf = None
        actions.clear()
