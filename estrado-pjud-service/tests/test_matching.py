from app.matching import build_search_response, is_definitive_not_found, normalize_label, rank_matches
from app.models import CandidateMatch, SearchRequest


def _candidate(
    index: int,
    *,
    rol: str = "O-243-2025",
    ruc: str | None = None,
    tribunal_code: int | None = None,
    corte_code: int | None = None,
    libro_code: str | None = None,
) -> CandidateMatch:
    return CandidateMatch(
        key=f"key-{index:03d}",
        rol=rol,
        ruc=ruc,
        tribunal=f"{index}º Juzgado de Garantía de Santiago",
        tribunal_code=tribunal_code,
        corte_code=corte_code,
        libro_code=libro_code,
        caratulado=f"Persona {index}",
        fecha_ingreso="2025-01-01",
    )


def test_exact_tribunal_and_rol_rank_first_and_results_are_capped():
    request = SearchRequest(
        contract_version=2,
        case_type="rit",
        case_number="O-243-2025",
        competencia="penal",
        corte=90,
        tribunal=321,
        libro="1",
        max_matches=10,
    )
    matches = [_candidate(index) for index in range(170)]
    matches.append(_candidate(170, tribunal_code=321))

    ranked = rank_matches(matches, request)

    assert ranked.matches[0].tribunal_code == 321
    assert len(ranked.matches) == 10
    assert ranked.total == 171
    assert ranked.truncated is True


def test_broad_window_returns_all_64_matches_without_truncation_at_100():
    request = SearchRequest(
        contract_version=2,
        case_type="rol",
        case_number="C-561-2025",
        competencia="civil",
        libro="C",
        allow_broad=True,
        max_matches=100,
    )
    matches = [
        _candidate(
            index,
            rol="C-561-2025",
            corte_code=90,
            tribunal_code=300 + index,
            libro_code="C",
        )
        for index in range(64)
    ]

    response = build_search_response(matches, request, libro_used="C")

    assert response.match_count == 64
    assert len(response.matches) == 64
    assert response.truncated is False


def test_multiple_exact_candidates_are_needs_disambiguation_not_not_found():
    request = SearchRequest(
        contract_version=2,
        case_type="rit",
        case_number="O-243-2025",
        competencia="penal",
        corte=90,
        tribunal=321,
        libro="1",
    )
    response = build_search_response(
        [
            _candidate(1, corte_code=90, tribunal_code=321, libro_code="1"),
            _candidate(2, corte_code=90, tribunal_code=321, libro_code="1"),
        ],
        request,
    )

    assert response.status == "needs_disambiguation"
    assert response.found is True
    assert response.match_count == 2


def test_normalization_is_only_for_comparison_and_never_rewrites_penal_rit():
    candidate = _candidate(1, rol=" o-243-2025 ", tribunal_code=321)
    request = SearchRequest(
        contract_version=2,
        case_type="rit",
        case_number="O-243-2025",
        competencia="penal",
        corte=90,
        tribunal=321,
        libro="1",
    )

    ranked = rank_matches([candidate], request)

    assert normalize_label("Juzgado de Garantía") == normalize_label("JUZGADO DE GARANTIA")
    assert ranked.matches[0].rol == " o-243-2025 "
    assert "Ordinaria" not in ranked.matches[0].rol


def test_appeals_resource_matches_its_official_book_prefix_without_rewriting_display():
    request = SearchRequest(
        contract_version=2,
        case_type="rol",
        case_number="4490-2025",
        competencia="apelaciones",
        corte=90,
        libro="34",
        search_mode="appeals_resource",
    )
    candidate = _candidate(
        1, rol="Protección-4490-2025", corte_code=90, libro_code="34"
    )

    response = build_search_response([candidate], request)

    assert response.status == "found"
    assert response.matches[0].rol == "Protección-4490-2025"


def test_appeals_ranking_uses_resolved_court_and_official_book_code():
    request = SearchRequest(
        contract_version=2,
        case_type="rol",
        case_number="4490-2025",
        competencia="apelaciones",
        corte=90,
        libro="34",
        search_mode="appeals_resource",
    )
    wrong_affinity = _candidate(
        1, rol="Protección-4490-2025"
    ).model_copy(update={"corte_code": 90, "libro_code": "31"})
    requested_affinity = _candidate(
        2, rol="Protección-4490-2025"
    ).model_copy(update={"corte_code": 90, "libro_code": "34"})

    ranked = rank_matches([wrong_affinity, requested_affinity], request)

    assert ranked.matches[0].key == "key-002"


def test_explicit_pjud_no_results_is_not_parser_drift():
    assert is_definitive_not_found("<div>No se encontraron causas</div>") is True
    assert is_definitive_not_found("<div>markup desconocido</div>") is False


def test_ruc_request_confirms_only_candidate_ruc_not_its_rit():
    request = SearchRequest(
        contract_version=2,
        case_type="ruc",
        case_number="2500100001-5",
        competencia="penal",
        corte=90,
        tribunal=321,
    )
    matching_ruc = _candidate(
        1, rol="O-999-2025", ruc="2500100001-5", corte_code=90, tribunal_code=321
    )
    same_rit_wrong_ruc = _candidate(
        2, rol="O-243-2025", ruc="2500100002-5", corte_code=90, tribunal_code=321
    )

    response = build_search_response([same_rit_wrong_ruc, matching_ruc], request)

    assert response.status == "found"
    assert response.matches[0].ruc == "2500100001-5"


def test_ruc_request_does_not_confirm_a_different_ruc_even_when_rit_matches():
    request = SearchRequest(
        contract_version=2,
        case_type="ruc",
        case_number="2500100001-5",
        competencia="penal",
        corte=90,
        tribunal=321,
    )

    response = build_search_response([
        _candidate(
            1, ruc="2500100002-5", corte_code=90, tribunal_code=321
        )
    ], request)

    assert response.status == "not_found"
    assert response.matches == []


def test_v2_known_identity_excludes_same_identifier_from_wrong_territory():
    """Catches identifier-only matching leaking a different tribunal to confirmation."""
    request = SearchRequest(
        contract_version=2,
        case_type="rol",
        case_number="C-1234-2025",
        competencia="civil",
        corte=90,
        tribunal=321,
        libro="C",
    )
    wrong_court = _candidate(
        1, rol="C-1234-2025", corte_code=91, tribunal_code=321, libro_code="C"
    )
    wrong_tribunal = _candidate(
        2, rol="C-1234-2025", corte_code=90, tribunal_code=999, libro_code="C"
    )

    response = build_search_response([wrong_court, wrong_tribunal], request)

    assert response.status == "not_found"
    assert response.found is False
    assert response.match_count == 0
    assert response.matches == []


def test_v2_wrong_book_is_excluded_and_absent_from_match_count():
    """Catches a same-ROL row from another official book becoming confirmable."""
    request = SearchRequest(
        contract_version=2,
        case_type="rit",
        case_number="O-243-2025",
        competencia="penal",
        corte=90,
        tribunal=321,
        libro="1",
    )
    wrong_book = _candidate(
        1, corte_code=90, tribunal_code=321, libro_code="2"
    )
    requested_book = _candidate(
        2, corte_code=90, tribunal_code=321, libro_code="1"
    )

    response = build_search_response([wrong_book, requested_book], request)

    assert response.status == "found"
    assert response.match_count == 1
    assert [candidate.key for candidate in response.matches] == ["key-002"]


def test_v2_direct_appeal_excludes_same_resource_from_different_court():
    """Catches a direct appeal drifting to another Court of Appeals."""
    request = SearchRequest(
        contract_version=2,
        case_type="rol",
        case_number="4490-2025",
        competencia="apelaciones",
        corte=90,
        libro="34",
        search_mode="appeals_resource",
    )
    candidate = _candidate(
        1,
        rol="Protección-4490-2025",
        corte_code=91,
        libro_code="34",
    )

    response = build_search_response([candidate], request)

    assert response.status == "not_found"
    assert response.found is False
    assert response.matches == []


def test_v2_broad_ambiguity_contains_only_identifier_and_book_compatible_rows():
    """Catches raw parser rows inflating broad ambiguity with unrelated identities."""
    request = SearchRequest(
        contract_version=2,
        case_type="rol",
        case_number="C-1234-2025",
        competencia="civil",
        libro="C",
        allow_broad=True,
    )
    eligible_one = _candidate(
        1, rol="C-1234-2025", corte_code=90, tribunal_code=321, libro_code="C"
    )
    eligible_two = _candidate(
        2, rol="C-1234-2025", corte_code=91, tribunal_code=400, libro_code="C"
    )
    wrong_identifier = _candidate(
        3, rol="C-9999-2025", corte_code=90, tribunal_code=321, libro_code="C"
    )
    wrong_book = _candidate(
        4, rol="C-1234-2025", corte_code=90, tribunal_code=321, libro_code="V"
    )

    response = build_search_response(
        [wrong_identifier, eligible_two, wrong_book, eligible_one], request
    )

    assert response.status == "needs_disambiguation"
    assert response.match_count == 2
    assert {candidate.key for candidate in response.matches} == {"key-001", "key-002"}


def test_v1_keeps_all_parsed_rows_and_legacy_found_semantics():
    """Catches v2 eligibility filtering accidentally changing the v1 contract."""
    request = SearchRequest(
        case_type="rol",
        case_number="C-1234-2025",
        competencia="civil",
    )
    unrelated = _candidate(1, rol="C-9999-2025")

    response = build_search_response([unrelated], request)

    assert response.status == "found"
    assert response.found is True
    assert response.match_count == 1
    assert response.matches == [unrelated]


def test_v2_nonappeal_uses_identifier_book_when_request_omits_explicit_libro():
    """Catches treating an effective C-book search as bookless identity."""
    request = SearchRequest(
        contract_version=2,
        case_type="rol",
        case_number="C-1234-2025",
        competencia="civil",
        corte=90,
        tribunal=321,
    )
    candidate = _candidate(
        1, rol="C-1234-2025", corte_code=90, tribunal_code=321, libro_code="C"
    )

    response = build_search_response([candidate], request, libro_used="C")

    assert response.status == "found"
    assert response.matches == [candidate]
