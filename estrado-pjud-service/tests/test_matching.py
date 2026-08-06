from app.matching import build_search_response, is_definitive_not_found, normalize_label, rank_matches
from app.models import CandidateMatch, SearchRequest


def _candidate(
    index: int,
    *,
    rol: str = "O-243-2025",
    ruc: str | None = None,
    tribunal_code: int | None = None,
) -> CandidateMatch:
    return CandidateMatch(
        key=f"key-{index:03d}",
        rol=rol,
        ruc=ruc,
        tribunal=f"{index}º Juzgado de Garantía de Santiago",
        tribunal_code=tribunal_code,
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
        [_candidate(1, tribunal_code=321), _candidate(2, tribunal_code=321)],
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
    candidate = _candidate(1, rol="Protección-4490-2025")

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
    matching_ruc = _candidate(1, rol="O-999-2025", ruc="2500100001-5")
    same_rit_wrong_ruc = _candidate(2, rol="O-243-2025", ruc="2500100002-5")

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

    response = build_search_response([_candidate(1, ruc="2500100002-5")], request)

    assert response.status == "needs_disambiguation"
