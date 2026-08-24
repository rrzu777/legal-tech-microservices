from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.my_causes.models import ImportCandidate
from app.my_causes.parser import UpstreamChangedError, parse_my_causes_page


FIXTURES = Path(__file__).parent / "fixtures" / "my_causes"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("matter", "filename", "case_type", "case_number", "filed_at"),
    [
        ("suprema", "suprema_page_1.html", "rol", "12345-2025", date(2025, 8, 3)),
        ("apelaciones", "apelaciones_page_1.html", "rol", "4490-2025", date(2025, 11, 21)),
        ("civil", "civil_page_1.html", "rit", "C-1234-2024", date(2024, 5, 31)),
        ("laboral", "laboral_page_1.html", "rit", "T-500-2024", date(2024, 6, 7)),
        ("penal", "penal_page_1.html", "rit", "O-100-2025", date(2025, 1, 2)),
        ("cobranza", "cobranza_page_1.html", "rit", "C-1000-2024", date(2024, 2, 9)),
        ("familia", "familia_page_1.html", "rit", "C-88-2023", date(2023, 3, 12)),
    ],
)
def test_parses_each_matter_by_exact_header_name(
    matter: str, filename: str, case_type: str, case_number: str, filed_at: date
) -> None:
    candidates = parse_my_causes_page(fixture(filename), matter)

    first = candidates[0]
    assert first.matter == matter
    assert first.case_type == case_type
    assert first.case_number == case_number
    assert first.filed_at == filed_at


def test_maps_reordered_columns_by_header_instead_of_position() -> None:
    candidate = parse_my_causes_page(fixture("reordered_page.html"), "civil")[0]

    assert candidate.case_number == "C-9-2026"
    assert candidate.tribunal_code is None
    assert candidate.tribunal_label == "2º Juzgado Civil"
    assert candidate.upstream_status == "Vigente"


def test_changed_required_header_fails_closed_with_explicit_code() -> None:
    changed = fixture("civil_page_1.html").replace("<th>Rit</th>", "<th>Identificador</th>")

    with pytest.raises(UpstreamChangedError) as exc_info:
        parse_my_causes_page(changed, "civil")

    assert exc_info.value.code == "upstream_changed"


@pytest.mark.parametrize(
    ("matter", "filename", "expected_form"),
    [
        ("suprema", "suprema_page_1.html", "formMisCauSuprema"),
        ("apelaciones", "apelaciones_page_1.html", "formMisCauApelacion"),
        ("civil", "civil_page_1.html", "formMisCauCivil"),
        ("laboral", "laboral_page_1.html", "formMisCauLaboral"),
        ("penal", "penal_page_1.html", "formMisCauPenal"),
        ("cobranza", "cobranza_page_1.html", "formMisCauCobranza"),
        ("familia", "familia_page_1.html", "formMisCauFamilia"),
    ],
)
def test_changed_observed_form_name_fails_closed(
    matter: str, filename: str, expected_form: str
) -> None:
    changed = fixture(filename).replace(expected_form, "formUnexpected", 1)

    with pytest.raises(UpstreamChangedError) as exc_info:
        parse_my_causes_page(changed, matter)

    assert exc_info.value.code == "upstream_changed"


def test_malformed_required_row_fails_closed_instead_of_returning_partial_data() -> None:
    with pytest.raises(UpstreamChangedError) as exc_info:
        parse_my_causes_page(fixture("malformed_page.html"), "civil")

    assert exc_info.value.code == "upstream_changed"


def test_empty_state_is_not_treated_as_schema_drift() -> None:
    assert parse_my_causes_page(fixture("empty_page.html"), "civil") == []


def test_civil_deduplicates_cuadernos_only_within_same_rit_and_tribunal() -> None:
    candidates = parse_my_causes_page(fixture("civil_page_1.html"), "civil")

    assert [(item.case_number, item.tribunal_label) for item in candidates] == [
        ("C-1234-2024", "2º Juzgado Civil"),
        ("C-1234-2024", "3º Juzgado Civil"),
    ]


def test_civil_conflicting_cuaderno_statuses_keep_the_cause_conservatively_open() -> None:
    html = fixture("civil_page_1.html").replace("En tramitación", "Archivada", 1)

    candidates = parse_my_causes_page(html, "civil")

    assert candidates[0].upstream_status == "En tramitación"


def test_apelaciones_missing_libro_is_valid_and_needs_later_enrichment() -> None:
    candidate = parse_my_causes_page(fixture("apelaciones_page_1.html"), "apelaciones")[0]

    assert candidate.libro is None
    assert candidate.court_code is None
    assert candidate.court_label == "C.A. de Santiago"


def test_penal_prefers_rit_when_row_also_exposes_ruc_and_uses_ruc_as_fallback() -> None:
    candidates = parse_my_causes_page(fixture("penal_page_1.html"), "penal")

    assert [(item.case_type, item.case_number) for item in candidates] == [
        ("rit", "O-100-2025"),
        ("ruc", "2500123456-7"),
    ]


def test_ruc_length_matches_existing_public_lookup_contract() -> None:
    html = fixture("penal_page_1.html").replace("2500123456-7", "25001234567-7")

    with pytest.raises(UpstreamChangedError):
        parse_my_causes_page(html, "penal")


def test_empty_tbody_is_schema_drift_not_a_valid_empty_state() -> None:
    html = fixture("empty_page.html").replace(
        '<tr><td colspan="8">No existen causas por el valor ingresado</td></tr>', ""
    )

    with pytest.raises(UpstreamChangedError) as exc_info:
        parse_my_causes_page(html, "civil")

    assert exc_info.value.code == "upstream_changed"


@pytest.mark.parametrize(
    ("matter", "filename", "expected_status"),
    [
        ("suprema", "suprema_page_1.html", "En tramitación"),
        ("apelaciones", "apelaciones_page_1.html", "Fallada"),
        ("civil", "civil_page_1.html", "En tramitación"),
        ("laboral", "laboral_page_1.html", "Concluida"),
        ("penal", "penal_page_1.html", "Vigente"),
        ("cobranza", "cobranza_page_1.html", "Archivada"),
        ("familia", "familia_page_1.html", "Vigente"),
    ],
)
def test_maps_each_matter_specific_open_or_closed_status_header(
    matter: str, filename: str, expected_status: str
) -> None:
    assert parse_my_causes_page(fixture(filename), matter)[0].upstream_status == expected_status


@pytest.mark.parametrize(
    ("matter", "filename"),
    [
        ("suprema", "suprema_page_1.html"),
        ("apelaciones", "apelaciones_page_1.html"),
        ("civil", "civil_page_1.html"),
        ("laboral", "laboral_page_1.html"),
        ("penal", "penal_page_1.html"),
        ("cobranza", "cobranza_page_1.html"),
        ("familia", "familia_page_1.html"),
    ],
)
def test_pagination_markup_never_enters_candidate_output(
    matter: str, filename: str
) -> None:
    html = fixture(filename)
    assert 'class="pagination"' in html
    payload = parse_my_causes_page(html, matter)[0].model_dump()

    assert "pagination" not in json.dumps(payload, default=str)
    assert "data-page" not in json.dumps(payload, default=str)


def test_output_is_closed_allowlist_without_html_links_scripts_or_institution() -> None:
    html = fixture("reordered_page.html").replace(
        "PERSONA O / PERSONA P",
        "<script>secret_cookie_token</script>PERSONA O / PERSONA P",
    )
    candidate = parse_my_causes_page(html, "civil")[0]
    payload = candidate.model_dump(mode="json")
    serialized = json.dumps(payload)

    assert set(payload) == {
        "matter",
        "case_type",
        "case_number",
        "court_code",
        "court_label",
        "tribunal_code",
        "tribunal_label",
        "libro",
        "filed_at",
        "upstream_status",
        "caption",
    }
    assert "documentos.invalid" not in serialized
    assert "token" not in serialized
    assert "cookie" not in serialized
    assert "Instituci" not in serialized


def test_missing_status_fails_closed_so_open_closed_filtering_cannot_guess() -> None:
    html = fixture("cobranza_page_1.html").replace("Archivada", "")

    with pytest.raises(UpstreamChangedError) as exc_info:
        parse_my_causes_page(html, "cobranza")

    assert exc_info.value.code == "upstream_changed"


def test_candidate_model_rejects_unknown_fields_and_unsupported_matter() -> None:
    base = {
        "matter": "civil",
        "case_type": "rit",
        "case_number": "C-1-2026",
        "court_code": None,
        "court_label": None,
        "tribunal_code": 321,
        "tribunal_label": "Tribunal sintético",
        "libro": "Civil",
        "filed_at": None,
        "upstream_status": "Vigente",
        "caption": None,
    }

    with pytest.raises(ValidationError):
        ImportCandidate.model_validate({**base, "raw_html": "<html>secret</html>"})
    with pytest.raises(ValidationError):
        ImportCandidate.model_validate({**base, "matter": "disciplinario"})


def test_parser_rejects_disciplinario_instead_of_remapping_it() -> None:
    with pytest.raises(ValueError, match="unsupported_matter"):
        parse_my_causes_page(fixture("civil_page_1.html"), "disciplinario")
