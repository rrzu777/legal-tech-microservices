"""Deterministic PJUD search-result ranking and v2 response semantics."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.models import CandidateMatch, SearchRequest, SearchResponse
from app.parsers.normalizer import resolve_libro


def normalize_label(value: str) -> str:
    """Normalize only for comparisons; caller-owned display text is untouched."""
    decomposed = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"\W+", " ", decomposed).strip().upper()


def normalize_identifier(value: str) -> str:
    return normalize_label(value)


def is_definitive_not_found(html: str) -> bool:
    """Recognize PJUD's explicit no-results messages across Search and Detail."""
    normalized = normalize_label(html)
    return any(marker in normalized for marker in (
        "NO SE ENCONTRARON CAUSAS",
        "NO SE ENCONTRARON RESULTADOS",
        "NO EXISTEN CAUSAS",
        "SIN RESULTADOS",
    ))


@dataclass(frozen=True)
class RankedMatches:
    total: int
    matches: list[CandidateMatch]
    truncated: bool


def _matches_requested(value: str | None, requested: object | None) -> bool:
    return value is not None and requested is not None and normalize_label(value) == normalize_label(str(requested))


def matches_requested_identifier(candidate_identifier: str, req: SearchRequest) -> bool:
    """Compare PJUD's displayed identifier against the requested canonical one.

    Appellate direct-resource results prepend the official resource label
    (``Protección-4490-2025``) although their request field is only
    ``4490-2025``.  That exception is comparison-only and is constrained by
    the requested `appeals_resource` book; it never changes stored/displayed
    identifiers and does not apply to Penal RITs.
    """
    if normalize_identifier(candidate_identifier) == normalize_identifier(req.case_number):
        return True
    if req.competencia != "apelaciones" or req.search_mode != "appeals_resource":
        return False

    expected_book = resolve_libro(req.competencia, "", req.libro)
    candidate_parts = normalize_identifier(candidate_identifier).split()
    requested_parts = normalize_identifier(req.case_number).split()
    return (
        bool(expected_book)
        and len(candidate_parts) >= 3
        and len(requested_parts) == 2
        and " ".join(candidate_parts[:-2]) == normalize_identifier(expected_book)
        and candidate_parts[-2:] == requested_parts
    )


def candidate_score(candidate: CandidateMatch, req: SearchRequest) -> tuple[int, str]:
    """Score canonical evidence, then use key as a stable deterministic tie-break."""
    score = 0
    if matches_requested_identifier(candidate.rol, req):
        score += 100
    if req.tribunal is not None and candidate.tribunal_code == req.tribunal:
        score += 40
    if req.corte is not None and candidate.corte_code == req.corte:
        score += 20
    expected_book = resolve_libro(req.competencia, "", req.libro)
    if expected_book and (
        _matches_requested(candidate.libro_code, req.libro)
        or _matches_requested(candidate.libro, expected_book)
    ):
        score += 10
    return (-score, candidate.key)


def rank_matches(matches: list[CandidateMatch], request: SearchRequest) -> RankedMatches:
    ranked = sorted(matches, key=lambda candidate: candidate_score(candidate, request))
    total = len(ranked)
    if request.contract_version == 2:
        visible = ranked[:request.max_matches]
    else:
        visible = ranked
    return RankedMatches(total=total, matches=visible, truncated=len(visible) < total)


def build_search_response(
    matches: list[CandidateMatch],
    request: SearchRequest,
    *,
    libro_used: str | None = None,
) -> SearchResponse:
    """Build the one search-response shape used by all successful searches."""
    ranked = rank_matches(matches, request)
    exact = [
        candidate
        for candidate in matches
        if matches_requested_identifier(candidate.rol, request)
    ]

    if not matches:
        status = "not_found"
        found = False
    elif request.contract_version == 1:
        # v1 historically exposed any parsed row as found.  Preserve that
        # behavior while adding an informational, compatible status.
        status = "found"
        found = True
    elif len(exact) == 1:
        status = "found"
        found = True
    else:
        # Several exact rows require a user choice; a non-exact row is never a
        # confirmation of the requested case either.
        status = "needs_disambiguation"
        found = True

    return SearchResponse(
        found=found,
        match_count=ranked.total,
        matches=ranked.matches,
        blocked=False,
        error=None,
        libro_used=libro_used,
        status=status,
        truncated=ranked.truncated,
    )
