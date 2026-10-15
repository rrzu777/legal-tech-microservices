"""Regression shapes observed in OJV, without real parties or session keys."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import CandidateMatch, SearchRequest
from tests.helpers import find_update_payload
from tests.test_engine import _make_case, _make_engine, _mock_detail_response, _mock_search_response
from worker.engine import _detail_matches_confirmed_identity


COURTS = {"C.A. de Santiago": 90, "C.A. de Valparaíso": 30,
          "C.A. de Concepción": 46, "C.A. de Copiapó": 20}


def request(number="9029-2026", corte=90, book="34"):
    return SearchRequest(contract_version=2, competencia="apelaciones", case_type="rol",
                         case_number=number, corte=corte, libro=book,
                         search_mode="appeals_resource", allow_broad=False)


def candidate(number="9029-2026", corte="C.A. de Santiago", book="34", location="Archivo"):
    label = {"34": "Protección", "35": "Amparo", "28": "Civil"}[book]
    return {"key": "synthetic-session-key", "rol": f"{label}-{number}",
            "tribunal": location, "corte": corte, "libro": label,
            "libro_code": book, "caratulado": "PARTE A/PARTE B",
            "fecha_ingreso": "2026-01-01"}


def detail(row):
    result = _mock_detail_response()
    rol = row["rol"].replace("-", " - ")
    result["metadata"] = {"rol": rol, "libro": rol, "tribunal": row["corte"]}
    return result


def catalog():
    result = MagicMock()
    result.resolve_loaded_court.side_effect = COURTS.get
    return result


@pytest.mark.parametrize("number,court,book", [
    ("3949-2026", "C.A. de Santiago", "35"),
    ("7089-2026", "C.A. de Valparaíso", "34"),
    ("4206-2026", "C.A. de Valparaíso", "35"),
    ("7218-2026", "C.A. de Valparaíso", "34"),
    ("5135-2025", "C.A. de Valparaíso", "34"),
    ("745-2026", "C.A. de Concepción", "35"),
    ("4-2026", "C.A. de Copiapó", "34"),
])
@pytest.mark.asyncio
async def test_known_appeal_stays_active_with_real_court_and_composite_book(number, court, book):
    engine, _, sb, _, metrics, _ = _make_engine(catalog_service=catalog())
    row = candidate(number, court, book)
    case = _make_case(matter="apelaciones", case_number=number, court_code=COURTS[court],
                      libro=book, pjud_search_mode="appeals_resource", tribunal_unknown=False,
                      pjud_contract_version=2)
    engine._upsert_movements = AsyncMock(return_value=1)
    with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock,
               return_value=_mock_search_response(matches=[row])), \
         patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock,
               return_value=detail(row)):
        result = await engine.sync_case(case)
    assert result["success"] is True
    engine._upsert_movements.assert_awaited_once()
    assert find_update_payload(sb, status="success") is not None
    assert find_update_payload(sb, tracking_status="needs_confirmation") is None
    metrics.record_sync.assert_called_once()


@pytest.mark.parametrize("number", ["9029-2026", "8575-2026"])
@pytest.mark.asyncio
async def test_legacy_protection_does_not_take_civil_homonym_first(number):
    engine, _, _, _, _, _ = _make_engine(catalog_service=catalog())
    correct = candidate(number)
    wrong = {**candidate(number, book="28"), "key": "wrong-civil-key"}
    case = _make_case(matter="apelaciones", case_number=f"PROTECCIÓN-{number}",
                      court_code=90, libro="34", pjud_search_mode=None,
                      tribunal_unknown=False, pjud_contract_version=1)
    engine._upsert_movements = AsyncMock(return_value=1)
    with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock,
               return_value=_mock_search_response(matches=[wrong, correct])), \
         patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock,
               return_value=detail(correct)) as get_detail:
        result = await engine.sync_case(case)
    assert result["success"] is True
    assert get_detail.call_args.args[2] == correct["key"]


@pytest.mark.parametrize("changed", [
    {"rol": "Civil - 9029 - 2026"},
    {"rol": "Protección - 9030 - 2026"},
    {"rol": "Protección - 9029 - 2025"},
    {"rol": ""},
    {"tribunal": "C.A. de Valparaíso"},
    {"corte": "C.A. de Valparaíso"},
    {"libro": "Civil - 9029 - 2026"},
    {"libro": "Protección - 9029 - 2026 - OTHER"},
    {"libro_code": "28"},
    {"corte_code": 30},
])
@pytest.mark.asyncio
async def test_detail_conflict_never_publishes_or_records_success(changed):
    engine, _, sb, _, metrics, _ = _make_engine(catalog_service=catalog())
    row = candidate()
    body = detail(row)
    body["metadata"].update(changed)
    case = _make_case(matter="apelaciones", case_number="9029-2026", court_code=90,
                      libro="34", pjud_search_mode="appeals_resource", tribunal_unknown=False)
    engine._upsert_movements = AsyncMock(return_value=1)
    with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock,
               return_value=_mock_search_response(matches=[row])), \
         patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock,
               return_value=body):
        result = await engine.sync_case(case)
    assert result["success"] is False
    assert result["status"] == "detail_identity_mismatch"
    engine._upsert_movements.assert_not_awaited()
    assert find_update_payload(sb, status="success") is None
    metrics.record_sync.assert_not_called()


def test_contradictory_role_is_rejected_even_without_optional_court_or_book():
    row = candidate()
    official = CandidateMatch(**row, corte_code=90)
    assert not _detail_matches_confirmed_identity(
        {"metadata": {"rol": "Civil - 9029 - 2026"}}, official, request(), catalog())


@pytest.mark.parametrize("changes", [{"corte": "C.A. de Valparaíso"},
                                    {"libro_code": "28"}, {"libro_code": None}, {"corte": ""}])
@pytest.mark.asyncio
async def test_missing_or_conflicting_search_identity_stays_pending(changes):
    engine, _, sb, _, metrics, _ = _make_engine(catalog_service=catalog())
    row = {**candidate(), **changes}
    case = _make_case(matter="apelaciones", case_number="9029-2026", court_code=90,
                      libro="34", pjud_search_mode="appeals_resource", tribunal_unknown=False)
    engine._upsert_movements = AsyncMock(return_value=1)
    with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock,
               return_value=_mock_search_response(matches=[row])), \
         patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as get_detail:
        result = await engine.sync_case(case)
    assert result == {"success": False, "new_movements": 0, "status": "needs_confirmation"}
    get_detail.assert_not_awaited()
    engine._upsert_movements.assert_not_awaited()
    assert find_update_payload(sb, status="needs_confirmation") is not None
    assert find_update_payload(sb, status="success") is None
    metrics.record_sync.assert_not_called()


def test_suprema_composite_rol_is_supported_but_different_number_is_rejected():
    req = SearchRequest(contract_version=2, competencia="suprema", case_type="rol",
                        case_number="5869-2026", search_mode="supreme_resource")
    row = CandidateMatch(key="synthetic", rol="5869-2026", tribunal="Corte Suprema",
                         caratulado="PARTE A/PARTE B", fecha_ingreso="2026-01-01")
    assert _detail_matches_confirmed_identity({"metadata": {"rol": "Civil / 5869 - 2026", "libro": "Civil / 5869 - 2026"}}, row, req)
    assert not _detail_matches_confirmed_identity({"metadata": {"rol": "Civil / 5870 - 2026"}}, row, req)


@pytest.mark.parametrize("ruc", ["", "2400100002-5"])
def test_ruc_detail_cannot_omit_or_change_the_requested_identifier(ruc):
    req = SearchRequest(contract_version=2, competencia="penal", case_type="ruc",
                        case_number="2400100001-5", corte=90, tribunal=1222)
    row = CandidateMatch(key="synthetic", rol="O-243-2025", ruc="2400100001-5",
                         tribunal="3° Juzgado de Garantía de Santiago", corte_code=90,
                         tribunal_code=1222, caratulado="A/B", fecha_ingreso="2025-01-01")
    assert not _detail_matches_confirmed_identity({"metadata": {"ruc": ruc}}, row, req)


@pytest.mark.parametrize("metadata", [
    {"rol": "Civil / 5869 - 2026", "tribunal": "C.A. de Santiago"},
    {"rol": "Civil / 5869 - 2026", "corte": "C.A. de Santiago"},
])
def test_suprema_rejects_explicit_foreign_court(metadata):
    req = SearchRequest(contract_version=2, competencia="suprema", case_type="rol",
                        case_number="5869-2026", search_mode="supreme_resource")
    row = CandidateMatch(key="synthetic", rol="5869-2026", tribunal="Corte Suprema",
                         caratulado="A/B", fecha_ingreso="2026-01-01")
    assert not _detail_matches_confirmed_identity({"metadata": metadata}, row, req)


@pytest.mark.parametrize("matter,rol,book", [
    ("civil", "C-1234-2024", "C"), ("laboral", "O-1234-2024", "O"),
    ("cobranza", "A-1234-2024", "A"), ("penal", "Ordinaria-1234-2024", "1"),
])
@pytest.mark.asyncio
async def test_book_is_derived_from_official_candidate_rol_without_copying_request(matter, rol, book):
    from types import SimpleNamespace
    cat = catalog()
    cat.resolve_loaded_tribunal.return_value = SimpleNamespace(tribunal_code=321, court_code=90)
    engine, *_ = _make_engine(catalog_service=cat)
    req = SearchRequest(contract_version=2, competencia=matter,
                        case_type="rit" if matter in {"penal", "laboral", "cobranza"} else "rol",
                        case_number="O-1234-2024" if matter == "penal" else rol,
                        corte=90, tribunal=321, libro=book)
    matches = await engine._enrich_candidates([{"rol": rol, "tribunal": "Tribunal oficial"}], req)
    assert matches[0]["libro_code"] == book
