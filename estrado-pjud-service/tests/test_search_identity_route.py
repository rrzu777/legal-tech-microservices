"""Canonical v2 identity tests for the public PJUD search route."""

from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.catalogs import CatalogService
from tests.helpers import api_settings


AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def client():
    from app.main import create_app

    return TestClient(create_app())


def _session():
    session = MagicMock(age_seconds=0)
    session.search = AsyncMock(return_value="<html>resultados PJUD</html>")
    session.close = AsyncMock()
    return session


def _pool(session):
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=session)
    pool.release = AsyncMock()
    return pool


def _snapshot(*, duplicate_civil_label: bool = False):
    generated = "2026-08-06T00:00:00+00:00"
    tribunals = {}
    books = {}
    for competencia, book_code, book_label in (
        ("civil", "C", "C"),
        ("laboral", "O", "O"),
        ("penal", "1", "Ordinaria"),
        ("cobranza", "C", "C"),
    ):
        first_label = (
            "Juzgado Duplicado"
            if competencia == "civil" and duplicate_civil_label
            else f"Tribunal {competencia} Santiago"
        )
        second_label = (
            "Juzgado Duplicado"
            if competencia == "civil" and duplicate_civil_label
            else f"Tribunal {competencia} San Miguel"
        )
        tribunals[f"{competencia}:90:1"] = {
            "fetched_at": generated,
            "options": [
                {"code": "321", "label": first_label},
                {"code": "999", "label": f"Otro tribunal {competencia} Santiago"},
            ],
        }
        tribunals[f"{competencia}:91:1"] = {
            "fetched_at": generated,
            "options": [{"code": "400", "label": second_label}],
        }
        for court in (90, 91):
            books[f"{competencia}:{court}:2025"] = {
                "fetched_at": generated,
                "options": [
                    {"code": book_code, "label": book_label},
                    {"code": "V" if competencia != "penal" else "2", "label": "Otro"},
                ],
            }

    tribunals["apelaciones:90:1"] = {
        "fetched_at": generated,
        "options": [{"code": "321", "label": "2º Juzgado Civil de Santiago"}],
    }
    tribunals["apelaciones:91:1"] = {
        "fetched_at": generated,
        "options": [{"code": "400", "label": "1º Juzgado Civil de San Miguel"}],
    }
    return {
        "generated_at": generated,
        "courts": {
            "1": {
                "fetched_at": generated,
                "options": [
                    {"code": "90", "label": "C.A. de Santiago"},
                    {"code": "91", "label": "C.A. de San Miguel"},
                ],
            }
        },
        "tribunals": tribunals,
        "books": books,
    }


def _raw_match(key, rol, tribunal, **extra):
    return {
        "key": key,
        "rol": rol,
        "tribunal": tribunal,
        "caratulado": f"Parte {key}",
        "fecha_ingreso": "2025-01-01",
        **extra,
    }


def _post(client, payload, matches, *, snapshot=None):
    from app.routes import search as search_route

    session = _session()
    pool = _pool(session)
    client.app.state.session_pool = pool
    client.app.state.catalog_service = CatalogService(
        pool, snapshot=snapshot or _snapshot()
    )
    with patch.object(search_route, "parse_search_results", return_value=matches):
        response = client.post("/api/v1/search", json=payload, headers=AUTH)
    return response, session, pool


def test_broad_civil_exact_returns_loaded_court_tribunal_and_book_identity(client):
    """Catches broad results returning null codes that the web cannot confirm."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "C-1234-2025",
            "competencia": "civil",
            "libro": "C",
            "allow_broad": True,
        },
        [_raw_match("jwt-1", "C-1234-2025", "Tribunal civil Santiago")],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "found"
    assert body["match_count"] == 1
    assert body["matches"][0]["corte_code"] == 90
    assert body["matches"][0]["tribunal_code"] == 321
    assert body["matches"][0]["libro_code"] == "C"
    assert body["matches"][0]["libro"] == "C"


def test_broad_ambiguity_returns_only_identifier_and_book_compatible_candidates(client):
    """Catches raw PJUD rows leaking into v2 ambiguity and match_count."""
    matches = [
        _raw_match("eligible-90", "C-1234-2025", "Tribunal civil Santiago"),
        _raw_match("eligible-91", "C-1234-2025", "Tribunal civil San Miguel"),
        _raw_match("wrong-rol", "C-9999-2025", "Tribunal civil Santiago"),
        _raw_match(
            "wrong-book", "C-1234-2025", "Tribunal civil Santiago",
            libro="V", libro_code="V",
        ),
    ]

    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "C-1234-2025",
            "competencia": "civil",
            "libro": "C",
            "allow_broad": True,
        },
        matches,
    )

    body = response.json()
    assert body["status"] == "needs_disambiguation"
    assert body["match_count"] == 2
    assert {match["key"] for match in body["matches"]} == {"eligible-90", "eligible-91"}
    assert all(match["corte_code"] is not None for match in body["matches"])
    assert all(match["tribunal_code"] is not None for match in body["matches"])


def test_duplicate_loaded_tribunal_label_fails_closed_as_upstream_changed(client):
    """Catches an ambiguous official label being exposed as a null-code candidate."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "C-1234-2025",
            "competencia": "civil",
            "libro": "C",
            "allow_broad": True,
        },
        [_raw_match("jwt-1", "C-1234-2025", "Juzgado Duplicado")],
        snapshot=_snapshot(duplicate_civil_label=True),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "upstream_changed"
    assert response.json()["matches"] == []


def test_known_court_resolves_repeated_tribunal_label_inside_requested_court(client):
    """Catches global ambiguity rejecting a tribunal that is unique in known court."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "C-1234-2025",
            "competencia": "civil",
            "corte": 90,
            "tribunal": 321,
            "libro": "C",
        },
        [_raw_match("known-court", "C-1234-2025", "Juzgado Duplicado")],
        snapshot=_snapshot(duplicate_civil_label=True),
    )

    body = response.json()
    assert body["status"] == "found"
    assert body["matches"][0]["corte_code"] == 90
    assert body["matches"][0]["tribunal_code"] == 321


def test_appeals_first_instance_broad_keeps_court_and_resolves_tribunal(client):
    """Catches the tribunal-unknown toggle erasing the required appeals court."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "4490-2025",
            "competencia": "apelaciones",
            "corte": 90,
            "search_mode": "first_instance",
            "allow_broad": True,
        },
        [
            _raw_match(
                "jwt-appeal", "4490-2025", "2º Juzgado Civil de Santiago",
                corte="C.A. de Santiago",
            )
        ],
    )

    body = response.json()
    assert body["status"] == "found"
    assert body["matches"][0]["corte_code"] == 90
    assert body["matches"][0]["tribunal_code"] == 321
    assert body["matches"][0]["libro_code"] is None


def test_appeals_first_instance_excludes_candidate_from_different_court(client):
    """Catches a resolved lower tribunal overriding the requested appeals court."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "4490-2025",
            "competencia": "apelaciones",
            "corte": 90,
            "search_mode": "first_instance",
            "allow_broad": True,
        },
        [
            _raw_match(
                "wrong-court", "4490-2025", "2º Juzgado Civil de Santiago",
                corte="C.A. de San Miguel",
            )
        ],
    )

    body = response.json()
    assert body["status"] == "not_found"
    assert body["found"] is False
    assert body["matches"] == []


def test_appeals_first_instance_globally_resolves_tribunal_from_other_court(client):
    """Catches an out-of-court tribunal being mislabeled as catalog drift."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "4490-2025",
            "competencia": "apelaciones",
            "corte": 90,
            "search_mode": "first_instance",
            "allow_broad": True,
        },
        [
            _raw_match(
                "other-court", "4490-2025", "1º Juzgado Civil de San Miguel",
                corte="C.A. de San Miguel",
            )
        ],
    )

    body = response.json()
    assert body["status"] == "not_found"
    assert body["found"] is False
    assert body["matches"] == []


def test_direct_appeal_from_different_court_is_truthful_not_found(client):
    """Catches a unique resource identifier overriding the requested court."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "4490-2025",
            "competencia": "apelaciones",
            "corte": 90,
            "libro": "34",
            "search_mode": "appeals_resource",
        },
        [
            _raw_match(
                "jwt-appeal", "Protección-4490-2025", "Corte de Apelaciones",
                corte="C.A. de San Miguel", libro="Protección", libro_code="34",
            )
        ],
    )

    body = response.json()
    assert body["status"] == "not_found"
    assert body["found"] is False
    assert body["matches"] == []


def test_direct_appeal_without_parsed_book_is_upstream_changed(client):
    """Catches parser drift being downgraded to a truthful identity miss."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "4490-2025",
            "competencia": "apelaciones",
            "corte": 90,
            "libro": "34",
            "search_mode": "appeals_resource",
        },
        [
            _raw_match(
                "jwt-appeal", "Etiqueta nueva-4490-2025", "Corte de Apelaciones",
                corte="C.A. de Santiago",
            )
        ],
    )

    assert response.status_code == 200
    assert response.json()["status"] == "upstream_changed"
    assert response.json()["matches"] == []


@pytest.mark.parametrize(
    ("competencia", "case_type", "case_number", "libro"),
    [
        ("civil", "rol", "C-1234-2025", "C"),
        ("laboral", "rit", "T-1234-2025", "O"),
        ("penal", "rit", "O-1234-2025", "1"),
        ("cobranza", "rol", "C-1234-2025", "C"),
    ],
)
def test_known_nonappeal_identity_fails_closed_for_candidate_from_other_court(
    client, competencia, case_type, case_number, libro
):
    """Catches identifier-only confirmation from an out-of-scope court."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": case_type,
            "case_number": case_number,
            "competencia": competencia,
            "corte": 90,
            "tribunal": 321,
            "libro": libro,
        },
        [
            _raw_match(
                "wrong-territory", case_number, f"Tribunal {competencia} San Miguel"
            )
        ],
    )

    body = response.json()
    assert body["status"] == "not_found"
    assert body["found"] is False
    assert body["matches"] == []


@pytest.mark.parametrize(
    ("competencia", "case_type", "case_number", "libro"),
    [
        ("civil", "rol", "C-1234-2025", "C"),
        ("laboral", "rit", "T-1234-2025", "O"),
        ("penal", "rit", "O-1234-2025", "1"),
        ("cobranza", "rol", "C-1234-2025", "C"),
    ],
)
def test_known_nonappeal_identity_excludes_other_tribunal_in_requested_court(
    client, competencia, case_type, case_number, libro
):
    """Catches identifier-only confirmation from another tribunal in the court."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": case_type,
            "case_number": case_number,
            "competencia": competencia,
            "corte": 90,
            "tribunal": 321,
            "libro": libro,
        },
        [
            _raw_match(
                "wrong-tribunal", case_number,
                f"Otro tribunal {competencia} Santiago",
            )
        ],
    )

    body = response.json()
    assert body["status"] == "not_found"
    assert body["found"] is False
    assert body["matches"] == []


def test_wrong_book_is_excluded_and_absent_book_is_canonically_populated(client):
    """Catches overwriting a contradictory book or leaving the requested book null."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rit",
            "case_number": "O-1234-2025",
            "competencia": "penal",
            "corte": 90,
            "tribunal": 321,
            "libro": "1",
        },
        [
            _raw_match(
                "wrong-book", "O-1234-2025", "Tribunal penal Santiago",
                libro="Exhorto", libro_code="2",
            ),
            _raw_match("right-book", "O-1234-2025", "Tribunal penal Santiago"),
        ],
    )

    body = response.json()
    assert body["status"] == "found"
    assert body["match_count"] == 1
    assert body["matches"][0]["key"] == "right-book"
    assert body["matches"][0]["libro_code"] == "1"
    assert body["matches"][0]["libro"] == "Ordinaria"


def test_nonappeal_identifier_book_is_populated_when_libro_is_implicit(client):
    """Catches losing the effective book derived from a canonical ROL prefix."""
    response, _session_mock, _pool_mock = _post(
        client,
        {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "C-1234-2025",
            "competencia": "civil",
            "corte": 90,
            "tribunal": 321,
        },
        [_raw_match("implicit-book", "C-1234-2025", "Tribunal civil Santiago")],
    )

    body = response.json()
    assert body["status"] == "found"
    assert body["libro_used"] == "C"
    assert body["matches"][0]["libro_code"] == "C"
    assert body["matches"][0]["libro"] == "C"


def test_v1_route_never_resolves_catalogs_and_keeps_raw_result(client):
    """Catches canonical v2 filtering or catalog lookup leaking into contract v1."""
    from app.routes import search as search_route

    session = _session()
    pool = _pool(session)
    catalog = MagicMock()
    catalog.resolve_loaded_court.side_effect = AssertionError("v1 must not resolve catalogs")
    catalog.resolve_loaded_tribunal.side_effect = AssertionError("v1 must not resolve catalogs")
    catalog.resolve_loaded_book.side_effect = AssertionError("v1 must not resolve catalogs")
    client.app.state.session_pool = pool
    client.app.state.catalog_service = catalog
    raw = [_raw_match("legacy", "C-9999-2025", "Tribunal desconocido")]

    with patch.object(search_route, "parse_search_results", return_value=raw):
        response = client.post(
            "/api/v1/search",
            json={
                "case_type": "rol",
                "case_number": "C-1234-2025",
                "competencia": "civil",
            },
            headers=AUTH,
        )

    body = response.json()
    assert body["status"] == "found"
    assert body["match_count"] == 1
    assert body["matches"][0]["key"] == "legacy"


def test_real_one_slot_api_pool_uses_only_loaded_catalog_memory(client):
    """Catches nested catalog acquisition while the search owns the only slot."""
    from app.routes import search as search_route
    from app.session_pool import APISessionPool

    session = _session()
    pool = APISessionPool(api_settings(proxy=None))
    pool._max_size = 1
    pool._pool = deque([session])
    catalog = CatalogService(pool, snapshot=_snapshot())
    catalog.courts = AsyncMock(side_effect=AssertionError("nested live courts call"))
    catalog.tribunals = AsyncMock(side_effect=AssertionError("nested live tribunals call"))
    catalog.books = AsyncMock(side_effect=AssertionError("nested live books call"))
    client.app.state.session_pool = pool
    client.app.state.catalog_service = catalog
    raw = [_raw_match("jwt-1", "C-1234-2025", "Tribunal civil Santiago")]

    with patch.object(search_route, "parse_search_results", return_value=raw):
        response = client.post(
            "/api/v1/search",
            json={
                "contract_version": 2,
                "case_type": "rol",
                "case_number": "C-1234-2025",
                "competencia": "civil",
                "libro": "C",
                "allow_broad": True,
            },
            headers=AUTH,
        )

    assert response.status_code == 200
    assert response.json()["status"] == "found"
    assert response.json()["matches"][0]["tribunal_code"] == 321
    assert len(pool._pool) == 1
