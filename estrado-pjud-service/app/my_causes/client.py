"""Authenticated, bounded discovery of OJV ``Mis Causas`` listings.

Endpoint, form, and status values are an explicit snapshot of the authenticated
OJV forms verified on 2026-08-23.  Suprema and Apelaciones expose no reviewed
single "open" option, so open-only discovery queries every observed status and
applies the conservative product ruling in ``_TERMINAL_STATUS_LABELS``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, cast

import httpx
from bs4 import BeautifulSoup

from pydantic import BaseModel, ConfigDict

from app.ojv.errors import OjvTimeoutError, SessionError
from app.ojv.session import OjvSession, decode_ojv_html
from app.my_causes.models import ImportCandidate, Matter
from app.my_causes.parser import UpstreamChangedError, parse_my_causes_page
from app.parsers.search_parser import detect_blocked


logger = logging.getLogger(__name__)

_OJV_BASE = "https://oficinajudicialvirtual.pjud.cl"
_AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{_OJV_BASE}/indexN.php#",
    "Content-Type": "application/x-www-form-urlencoded",
}
_MAX_PAGES_HARD = 100
_TRANSIENT_ATTEMPTS = 2


@dataclass(frozen=True)
class _MatterForm:
    endpoint: str
    prefix: str
    all_statuses: tuple[str, ...]
    open_statuses: tuple[str, ...]
    has_type: bool


_FORMS: dict[Matter, _MatterForm] = {
    "suprema": _MatterForm(
        "/misCausas/suprema/consultaMisCausasSuprema.php",
        "Sup",
        ("13", "11", "160", "40", "166", "170", "163", "41", "5", "165", "777", "171", "0", "169", "159", "162", "168", "164", "161", "6", "167", "139", "138", "158"),
        (),
        False,
    ),
    "apelaciones": _MatterForm(
        "/misCausas/apelaciones/consultaMisCausasApelaciones.php",
        "Ape",
        ("13", "184", "34", "56", "36", "35", "10", "39", "183", "33", "40", "41", "5", "60", "186", "1", "147", "45", "0", "20", "18", "3", "185", "22", "17"),
        (),
        False,
    ),
    "civil": _MatterForm(
        "/misCausas/civil/consultaMisCausasCivil.php",
        "Civ", ("5", "9", "8", "11", "2", "4", "7", "0", "3", "1"), ("1",), True,
    ),
    "laboral": _MatterForm(
        "/misCausas/laboral/consultaMisCausasLaboral.php",
        "Lab", ("5", "2", "6", "4", "7", "0", "3", "1"), ("1",), True,
    ),
    "penal": _MatterForm(
        "/misCausas/penal/consultaMisCausasPenal.php",
        "Pen", ("0", "3", "1", "6", "4", "2"), ("2",), True,
    ),
    "cobranza": _MatterForm(
        "/misCausas/cobranza/consultaMisCausasCobranza.php",
        "Cob", ("5", "7", "2", "6", "0", "3", "1"), ("1",), True,
    ),
    "familia": _MatterForm(
        "/misCausas/familia/consultaMisCausasFamilia.php",
        "Fam", ("5", "8", "7", "12", "2", "10", "9", "0", "11", "3", "1", "6"), ("1",), True,
    ),
}

# Product ruling, not an upstream fact: only these labels are safe to hide in
# open-only mode. Ambiguous, reactivated, challenged, or suspended labels stay.
_TERMINAL_STATUS_LABELS: dict[Matter, frozenset[str]] = {
    "suprema": frozenset({"archivado", "fallada", "terminada masiva"}),
    "apelaciones": frozenset(
        {
            "fallada",
            "fallada-terminada",
            "termino computacional",
            "devuelto al tribunal",
        }
    ),
}

_NEXT_PAGE_RE = re.compile(r"\bpagina(?:Ant|Sig)?\(\s*(\d+)\s*,", re.IGNORECASE)


DiscoveryStatus = Literal[
    "ok",
    "credential_invalid",
    "session_expired",
    "waf",
    "timeout",
    "upstream_changed",
]


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[ImportCandidate]
    page_count: int
    status: DiscoveryStatus


def _fold_status(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return " ".join(
        "".join(character for character in normalized if not unicodedata.combining(character))
        .casefold()
        .split()
    )


def _form_payload(
    spec: _MatterForm,
    identity: tuple[str, str],
    *,
    include_closed: bool,
    page: int,
) -> list[tuple[str, str]]:
    prefix = spec.prefix
    rut_digits, dv = identity
    payload = [
        (f"rutMisCau{prefix}", rut_digits),
        (f"dvMisCau{prefix}", dv),
    ]
    if spec.has_type:
        payload.append((f"tipoMisCau{prefix}", "0"))
    payload.extend(
        [
            (f"rolMisCau{prefix}", ""),
            (f"anhoMisCau{prefix}", ""),
            (f"tipCausaMisCau{prefix}[]", "M"),
        ]
    )
    statuses = spec.all_statuses if include_closed or not spec.open_statuses else spec.open_statuses
    payload.extend((f"estadoCausaMisCau{prefix}[]", value) for value in statuses)
    payload.extend(
        [
            (f"fecDesdeMisCau{prefix}", ""),
            (f"fecHastaMisCau{prefix}", ""),
            (f"nombreMisCau{prefix}", ""),
            (f"apePatMisCau{prefix}", ""),
            (f"apeMatMisCau{prefix}", ""),
        ]
    )
    if page > 1:
        payload.append(("pagina", str(page)))
    return payload


def _is_login_response(response: httpx.Response, html: str) -> bool:
    path = response.url.path.casefold()
    body = html.casefold()
    return (
        "login" in path
        or "iniciar sesión" in body
        or "rut o contraseña" in body
        or re.search(r">\s*login\s*<", body) is not None
    )


def _next_page(html: str, current_page: int) -> int | None:
    soup = BeautifulSoup(html, "html.parser")
    pages: set[int] = set()
    for link in soup.select("a[data-page]"):
        raw = link.get("data-page")
        if isinstance(raw, str) and raw.isdigit():
            pages.add(int(raw))
    for tag in soup.select("[onclick]"):
        raw = tag.get("onclick")
        if not isinstance(raw, str):
            continue
        pages.update(int(match) for match in _NEXT_PAGE_RE.findall(raw))
    following = [page for page in pages if page > current_page]
    return min(following) if following else None


def _fingerprint(candidates: list[ImportCandidate]) -> str:
    stable = "\n".join(
        "|".join(
            (
                item.matter,
                item.case_type,
                item.case_number,
                str(item.court_code or ""),
                item.court_label or "",
                str(item.tribunal_code or ""),
                item.tribunal_label or "",
                item.upstream_status or "",
            )
        )
        for item in candidates
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _candidate_key(candidate: ImportCandidate) -> tuple[object, ...]:
    return (
        candidate.matter,
        candidate.case_type,
        candidate.case_number,
        candidate.court_code,
        candidate.court_label.casefold() if candidate.court_label else None,
        candidate.tribunal_code,
        candidate.tribunal_label.casefold() if candidate.tribunal_label else None,
    )


async def _post_with_bounded_retry(
    session: OjvSession, endpoint: str, payload: list[tuple[str, str]]
) -> httpx.Response | None:
    for attempt in range(_TRANSIENT_ATTEMPTS):
        try:
            response = await session.post_form(
                f"{_OJV_BASE}{endpoint}",
                payload,
                headers=_AJAX_HEADERS,
            )
            if response.status_code < 500 and response.status_code != 408:
                return response
            if detect_blocked(response.text):
                return response
        except (OjvTimeoutError, httpx.TimeoutException, httpx.TransportError):
            pass
        if attempt + 1 == _TRANSIENT_ATTEMPTS:
            return None
    return None


def _result(
    candidates: list[ImportCandidate], page_count: int, status: DiscoveryStatus
) -> DiscoveryResult:
    logger.info(
        "my_causes terminal status=%s pages=%d count=%d",
        status,
        page_count,
        len(candidates),
    )
    return DiscoveryResult(candidates=candidates, page_count=page_count, status=status)


async def discover_my_causes(
    session: OjvSession,
    matters: tuple[Matter, ...],
    include_closed: bool,
    max_pages: int = 100,
) -> DiscoveryResult:
    page_limit = min(max_pages, _MAX_PAGES_HARD)
    if page_limit < 1:
        return _result([], 0, "upstream_changed")
    try:
        identity = session.authenticated_form_identity()
    except SessionError:
        return _result([], 0, "session_expired")

    collected: list[ImportCandidate] = []
    candidate_keys: set[tuple[object, ...]] = set()
    page_count = 0

    for raw_matter in matters:
        if raw_matter not in _FORMS:
            return _result(collected, page_count, "upstream_changed")
        matter = cast(Matter, raw_matter)
        spec = _FORMS[matter]
        seen_fingerprints: set[str] = set()
        current_page = 1

        while True:
            if page_count >= page_limit:
                return _result(collected, page_count, "upstream_changed")
            response = await _post_with_bounded_retry(
                session,
                spec.endpoint,
                _form_payload(
                    spec,
                    identity,
                    include_closed=include_closed,
                    page=current_page,
                ),
            )
            if response is None:
                return _result(collected, page_count, "timeout")

            html = decode_ojv_html(response)
            if response.status_code in {403, 429} or detect_blocked(html):
                return _result(collected, page_count, "waf")
            if response.status_code in {401, 419}:
                return _result(collected, page_count, "session_expired")
            if _is_login_response(response, html):
                return _result(collected, page_count, "session_expired")
            if response.status_code >= 400:
                return _result(collected, page_count, "upstream_changed")

            try:
                parsed = parse_my_causes_page(html, matter)
            except UpstreamChangedError:
                return _result(collected, page_count, "upstream_changed")

            fingerprint = _fingerprint(parsed)
            if fingerprint in seen_fingerprints:
                return _result(collected, page_count, "upstream_changed")
            seen_fingerprints.add(fingerprint)
            page_count += 1

            terminal = _TERMINAL_STATUS_LABELS.get(matter, frozenset())
            for candidate in parsed:
                if not include_closed and _fold_status(candidate.upstream_status) in terminal:
                    continue
                key = _candidate_key(candidate)
                if key not in candidate_keys:
                    candidate_keys.add(key)
                    collected.append(candidate)

            logger.info(
                "my_causes page=%d count=%d",
                current_page,
                len(parsed),
            )
            next_page = _next_page(html, current_page)
            if next_page is None:
                break
            current_page = next_page

    return _result(collected, page_count, "ok")
