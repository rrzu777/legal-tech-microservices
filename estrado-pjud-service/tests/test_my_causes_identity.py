from __future__ import annotations

from pathlib import Path

import pytest

from app.catalogs import CatalogService
from app.my_causes.identity import resolve_public_import_candidate
from app.my_causes.parser import parse_my_causes_page
from worker.import_jobs import _candidate_payloads


FIXTURES = Path(__file__).parent / "fixtures" / "my_causes"


def _parsed(matter: str, filename: str):
    return parse_my_causes_page(
        (FIXTURES / filename).read_text(encoding="utf-8"), matter,
    )


@pytest.mark.parametrize(
    ("matter", "filename", "expected"),
    [
        (
            "apelaciones",
            "apelaciones_page_1.html",
            (90, None, None),
        ),
        (
            "civil",
            "civil_page_1.html",
            (None, None, "C"),
        ),
        (
            "laboral",
            "laboral_page_1.html",
            (None, None, "T"),
        ),
        (
            "cobranza",
            "cobranza_page_1.html",
            (None, None, "C"),
        ),
        (
            "penal",
            "penal_page_1.html",
            (None, None, "1"),
        ),
    ],
)
def test_intact_abbreviated_listing_stays_territorially_unresolved_but_keeps_proven_book(
    matter: str,
    filename: str,
    expected: tuple[int, int | None, str | None],
) -> None:
    candidate = _parsed(matter, filename)[0]

    resolved = resolve_public_import_candidate(candidate, CatalogService(None))

    assert (resolved.court_code, resolved.tribunal_code, resolved.libro) == expected


def test_penal_ruc_stays_territorially_unresolved_and_never_adds_a_book() -> None:
    candidate = _parsed("penal", "penal_page_1.html")[1]

    resolved = resolve_public_import_candidate(candidate, CatalogService(None))

    assert (resolved.court_code, resolved.tribunal_code, resolved.libro) == (None, None, None)


def test_suprema_discards_constant_display_court_instead_of_persisting_irrelevant_identity() -> None:
    candidate = _parsed("suprema", "suprema_page_1.html")[0]

    resolved = resolve_public_import_candidate(candidate, CatalogService(None))

    assert resolved.court_code is None
    assert resolved.court_label is None
    assert resolved.tribunal_code is None
    assert resolved.tribunal_label is None
    assert resolved.libro is None


def test_ambiguous_official_label_stays_incomplete_and_never_selectable_by_guess() -> None:
    candidate = _parsed("civil", "civil_page_1.html")[0].model_copy(
        update={"tribunal_label": "Tribunal duplicado"},
    )
    snapshot = {
        "tribunals": {
            "civil:90:1": {
                "options": [
                    {"code": "260", "label": "Tribunal duplicado"},
                    {"code": "261", "label": "Tribunal duplicado"},
                ]
            }
        }
    }

    resolved = resolve_public_import_candidate(candidate, CatalogService(None, snapshot=snapshot))

    assert resolved.court_code is None
    assert resolved.tribunal_code is None
    assert resolved.libro is None
    assert resolved.tribunal_label == "Tribunal duplicado"


def test_familia_private_candidate_is_not_rewritten_by_public_catalog_resolution() -> None:
    candidate = _parsed("familia", "familia_page_1.html")[0]

    resolved = resolve_public_import_candidate(candidate, CatalogService(None))

    assert resolved == candidate


def test_provisional_rows_with_same_abbreviated_identity_keep_distinct_captions() -> None:
    candidate = resolve_public_import_candidate(
        _parsed("civil", "civil_page_1.html")[0], CatalogService(None),
    )
    other_region_evidence = candidate.model_copy(update={"caption": "OTRA EMPRESA / PERSONA"})

    batch = _candidate_payloads([candidate, candidate, other_region_evidence])
    payloads = batch.payloads

    assert len(payloads) == 2
    assert batch.total_unique == 2
    assert batch.truncated is False
    assert {payload["caption"] for payload in payloads} == {
        "EMPRESA E / PERSONA F", "OTRA EMPRESA / PERSONA",
    }
    assert len({payload["source_hash"] for payload in payloads}) == 2


@pytest.mark.parametrize(
    ("matter", "prefix"),
    [
        *[("civil", code) for code in ("C", "V", "E", "A", "F", "I")],
        *[("laboral", code) for code in ("O", "T", "M", "E", "S", "U", "V", "I")],
        *[("cobranza", code) for code in ("A", "C", "D", "E", "J", "L", "P", "R")],
    ],
)
def test_all_official_first_instance_book_hints_come_from_loaded_catalog(
    matter: str, prefix: str,
) -> None:
    filenames = {
        "civil": "civil_page_1.html",
        "laboral": "laboral_page_1.html",
        "cobranza": "cobranza_page_1.html",
    }
    candidate = _parsed(matter, filenames[matter])[0].model_copy(
        update={"case_number": f"{prefix}-100-2024"},
    )

    resolved = resolve_public_import_candidate(candidate, CatalogService(None))

    assert resolved.libro == prefix
