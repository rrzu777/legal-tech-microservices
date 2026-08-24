import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

from app.models import CandidateMatch, DetailResponse, PenalBookBatchSearchRequest, SearchResponse
from app.routes import search as search_route
from app.parsers.detail_parser import parse_detail


def response(*matches: CandidateMatch, status: str = "found") -> SearchResponse:
    return SearchResponse(
        found=bool(matches), match_count=len(matches), matches=list(matches),
        blocked=False, error=None, case_type="rit",
        status=status if matches else "not_found",
    )


def match(*, book: str, court: int = 90, tribunal: int = 1226) -> CandidateMatch:
    return CandidateMatch(
        key=f"key-{book}-{court}", rol="E-77-2025", ruc=None,
        tribunal="7º Juzgado de Garantía de Santiago", tribunal_code=tribunal,
        corte="C.A. de Santiago", corte_code=court,
        libro="Exhorto", libro_code=book,
        caratulado="MINISTERIO PÚBLICO / PERSONA E", fecha_ingreso=None,
    )


def request() -> PenalBookBatchSearchRequest:
    return PenalBookBatchSearchRequest(
        contract_version=2, competencia="penal", case_type="rit",
        case_number="E-77-2025", books=["1", "2", "3", "4", "5"],
        tribunal_label="7º Juzgado de Garantía",
        caption="MINISTERIO PÚBLICO / PERSONA E",
    )


@pytest.mark.asyncio
async def test_batch_fans_out_official_books_and_refreshes_exact_key(monkeypatch):
    calls = []
    detail_calls = []

    async def fake_search(req, _request, *, session):
        assert session is shared_session
        calls.append(req)
        if req.allow_broad:
            return response(match(book="2")) if req.libro == "2" else response()
        return response(match(book="2"))

    monkeypatch.setattr(search_route, "_search_case", fake_search)
    async def fake_detail(session, candidate, _request):
        assert session is shared_session
        detail_calls.append(candidate)
        return DetailResponse(
            metadata={
                "rol": "E-77-2025", "tribunal": "7º Juzgado de Garantía de Santiago",
                "libro": "E", "caratulado": "MINISTERIO PÚBLICO / PERSONA E",
            },
            movements=[{
                "folio": 1, "cuaderno": "Principal", "etapa": "", "tramite": "Ingreso",
                "descripcion": "Ingreso", "fecha": "2025-01-02", "foja": None,
                "documento_url": "https://secret.example/doc", "documento_token": "doc-secret",
            }],
            litigantes=[{"rol": "Imputado", "rut": "1-9", "nombre": "PERSONA E"}],
            libro="E", blocked=False, error=None, ebook_token="ebook-secret",
            suprema_docs=[{"token": "suprema-secret"}],
        )
    monkeypatch.setattr(search_route, "_fetch_penal_detail", fake_detail)
    shared_session = SimpleNamespace()
    result = await search_route._run_penal_book_batch(
        request(), SimpleNamespace(), session=shared_session,
    )

    assert result.match_count == 1
    assert result.verified_match.libro_code == "2"
    assert result.detail.metadata.rol == "E-77-2025"
    serialized = result.model_dump()
    assert "key" not in str(serialized).lower()
    assert "secret" not in str(serialized).lower()
    assert result.detail.movements[0].documento_url is None
    assert [call.libro for call in calls] == ["1", "2", "3", "4", "5"]
    assert len(detail_calls) == 1
    assert detail_calls[0].key == "key-2-90"


@pytest.mark.asyncio
async def test_batch_never_picks_between_two_compatible_identities(monkeypatch):
    async def fake_search(req, _request, *, session):
        if req.libro == "2":
            return response(match(book="2", court=90), match(book="2", court=30, tribunal=205))
        return response()

    monkeypatch.setattr(search_route, "_search_case", fake_search)
    result = await search_route._run_penal_book_batch(
        request(), SimpleNamespace(), session=SimpleNamespace(),
    )

    assert result.status == "needs_disambiguation"
    assert result.match_count == 2


@pytest.mark.asyncio
async def test_shared_session_timeout_is_retired_by_batch_owner(monkeypatch):
    shared_session = SimpleNamespace()
    pool = SimpleNamespace(release=AsyncMock())

    async def fake_acquire(_pool, _request, _operation):
        return shared_session

    async def fake_search(_req, _request, *, session):
        assert session is shared_session
        return SearchResponse(
            found=False, match_count=0, matches=[], blocked=False,
            error="PJUD request timed out", case_type="rit",
            status="pjud_timeout",
        )

    monkeypatch.setattr(search_route, "acquire_or_alert", fake_acquire)
    monkeypatch.setattr(search_route, "_search_case", fake_search)
    http_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_pool=pool,
    )))

    result = await search_route._run_penal_book_batch(request(), http_request)

    assert result.status == "pjud_timeout"
    pool.release.assert_awaited_once_with(shared_session, healthy=False)


@pytest.mark.asyncio
async def test_real_batch_cancellation_retires_shared_session(monkeypatch):
    shared_session = SimpleNamespace()
    pool = SimpleNamespace(release=AsyncMock())

    async def fake_acquire(_pool, _request, _operation):
        return shared_session

    async def blocked_search(_req, _request, *, session):
        assert session is shared_session
        await asyncio.Event().wait()

    monkeypatch.setattr(search_route, "acquire_or_alert", fake_acquire)
    monkeypatch.setattr(search_route, "_search_case", blocked_search)
    http_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_pool=pool,
    )))

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await search_route._run_penal_book_batch(request(), http_request)

    pool.release.assert_awaited_once_with(shared_session, healthy=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("detail_kind", ["error", "mismatch"])
async def test_detail_failure_retires_shared_session(monkeypatch, detail_kind):
    shared_session = SimpleNamespace()
    pool = SimpleNamespace(release=AsyncMock())

    async def fake_acquire(_pool, _request, _operation):
        return shared_session

    async def fake_search(req, _request, *, session):
        assert session is shared_session
        return response(match(book="2")) if req.libro == "2" else response()

    async def fake_detail(session, candidate, _request):
        assert session is shared_session
        if detail_kind == "error":
            return DetailResponse(
                metadata={}, movements=[], litigantes=[], libro=None,
                blocked=False, error="upstream changed",
            )
        return DetailResponse(
            metadata={
                "rol": candidate.rol, "tribunal": "Otro tribunal",
                "libro": "E",
            },
            movements=[], litigantes=[], libro="E", blocked=False, error=None,
        )

    monkeypatch.setattr(search_route, "acquire_or_alert", fake_acquire)
    monkeypatch.setattr(search_route, "_search_case", fake_search)
    monkeypatch.setattr(search_route, "_fetch_penal_detail", fake_detail)
    http_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_pool=pool,
    )))

    result = await search_route._run_penal_book_batch(request(), http_request)

    assert result.status == "upstream_changed"
    pool.release.assert_awaited_once_with(shared_session, healthy=False)


def test_batch_contract_rejects_any_book_set_except_official_closed_set():
    payload = request().model_dump()
    payload["books"] = ["1", "2", "3", "4"]
    with pytest.raises(ValueError):
        PenalBookBatchSearchRequest(**payload)


def test_detail_binding_requires_identifier_tribunal_book_and_caption():
    candidate = match(book="2")
    base = DetailResponse(
        metadata={
            "rol": candidate.rol,
            "tribunal": candidate.tribunal,
            "libro": "E",
            "caratulado": candidate.caratulado,
        },
        movements=[], litigantes=[], libro="E",
        blocked=False, error=None,
    )
    assert search_route._penal_detail_matches_listing(base, candidate, request())
    for field, value in [
        ("rol", "E-78-2025"),
        ("tribunal", "8º Juzgado de Garantía de Santiago"),
        ("libro", "Militar"),
        ("caratulado", "OTRA / PERSONA"),
    ]:
        payload = base.model_dump()
        payload["metadata"][field] = value
        if field == "libro":
            payload["libro"] = value
        changed = DetailResponse.model_validate(payload)
        assert not search_route._penal_detail_matches_listing(
            changed, candidate, request(),
        ), field


def test_real_penal_detail_fixture_binds_without_inventing_caption_or_book_label():
    fixture = Path(__file__).parent / "fixtures" / "detail_Penal_O_100_2025.html"
    parsed = parse_detail(fixture.read_text())
    detail = DetailResponse(
        metadata=parsed["metadata"], movements=parsed["movements"],
        litigantes=parsed["litigantes"], libro=parsed["metadata"]["libro"],
        blocked=False, error=None,
    )
    candidate = CandidateMatch(
        key="in-memory-only", rol="O-100-2025", ruc="2500100001-5",
        tribunal="4º Juzgado de Garantía de Santiago", tribunal_code=1223,
        corte="C.A. de Santiago", corte_code=90,
        libro="Ordinaria", libro_code="1",
        caratulado="MINISTERIO PÚBLICO / PERSONA O", fecha_ingreso=None,
    )
    observed = request().model_copy(update={
        "case_number": "O-100-2025",
        "caption": candidate.caratulado,
        "tribunal_label": "4º Juzgado de Garantía",
    })

    assert search_route._penal_detail_matches_listing(detail, candidate, observed)
    safe = search_route._safe_penal_detail(detail, candidate)
    assert safe.libro == "1"
    assert "key" not in str(safe.model_dump()).lower()
