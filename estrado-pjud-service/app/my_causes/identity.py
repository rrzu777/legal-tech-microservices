"""Canonical identity resolution for public ``Mis Causas`` candidates.

Listing tables expose display labels.  They are useful evidence, but never an
identity by themselves.  This module only promotes them when the bundled,
reviewed PJUD catalog has exactly one matching code and the identifier's book
is present in the exact court/year slice.
"""

from __future__ import annotations

from app.catalogs import CatalogService
from app.my_causes.models import ImportCandidate


# The public intake contract already maps Penal's ordinary RIT prefix ``O`` to
# PJUD book code ``1``.  Other Penal prefixes are deliberately not guessed.
_PENAL_RIT_BOOK_BY_PREFIX = {"O": "1"}


def known_penal_book_code_for_rit(case_number: str) -> str | None:
    """Return only the reviewed prefix mapping; unknown prefixes stay unknown."""
    prefix = case_number.split("-", 1)[0].strip().upper()
    return _PENAL_RIT_BOOK_BY_PREFIX.get(prefix)


def _identifier_parts(candidate: ImportCandidate) -> tuple[str, int] | None:
    parts = candidate.case_number.rsplit("-", 2)
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    return parts[0].upper(), int(parts[2])


def _book_code(candidate: ImportCandidate) -> tuple[str, int] | None:
    parts = _identifier_parts(candidate)
    if parts is None:
        return None
    prefix, year = parts
    if candidate.matter == "penal":
        code = known_penal_book_code_for_rit(candidate.case_number)
        return (code, year) if code is not None else None
    return prefix, year


def resolve_public_import_candidate(
    candidate: ImportCandidate,
    catalog_service: CatalogService,
) -> ImportCandidate:
    """Promote one listing row only through unique, locally loaded catalogs."""

    if candidate.matter == "familia":
        return candidate

    if candidate.matter == "suprema":
        # The listing's constant ``Corte Suprema`` cell is presentation, not a
        # v2 search dimension.  Persisting it violates the closed identity.
        return candidate.model_copy(
            update={
                "court_code": None,
                "court_label": None,
                "tribunal_code": None,
                "tribunal_label": None,
                "libro": None,
            }
        )

    if candidate.matter == "apelaciones":
        court_code = (
            catalog_service.resolve_loaded_court(candidate.court_label)
            if candidate.court_label
            else None
        )
        if court_code is None:
            return candidate
        # Libro is absent from the listing and remains explicitly unresolved;
        # the existing selection/materializer contract enriches it later.
        return candidate.model_copy(
            update={"court_code": court_code, "tribunal_code": None, "libro": None}
        )

    update: dict[str, object] = {}
    if not (candidate.matter == "penal" and candidate.case_type == "ruc"):
        book = _book_code(candidate)
        if book is not None:
            book_code, year = book
            official_hint = catalog_service.resolve_loaded_book_hint(
                candidate.matter, book_code, year,
            )
            if official_hint is not None:
                # RIT prefix and the reviewed year slice prove the book without
                # pretending that an abbreviated tribunal label proves region.
                update["libro"] = official_hint

    if not candidate.tribunal_label:
        return candidate.model_copy(update=update)
    tribunal = catalog_service.resolve_loaded_tribunal(
        candidate.matter, candidate.tribunal_label,
    )
    if tribunal is None:
        return candidate.model_copy(update=update)

    update.update({
        "court_code": tribunal.court_code,
        "tribunal_code": tribunal.tribunal_code,
        "tribunal_label": tribunal.tribunal_label,
    })
    if candidate.matter == "penal" and candidate.case_type == "ruc":
        update["libro"] = None
        return candidate.model_copy(update=update)

    book = _book_code(candidate)
    if book is None:
        return candidate
    book_code, year = book
    official_book = catalog_service.resolve_loaded_book(
        candidate.matter,
        book_code,
        year,
        corte=tribunal.court_code,
    )
    if official_book is None:
        return candidate
    update["libro"] = official_book["code"]

    return candidate.model_copy(update=update)
