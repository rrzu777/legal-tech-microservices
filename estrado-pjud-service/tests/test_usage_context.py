from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from app.usage_context import (
    CASE_ID_HEADER,
    LAW_FIRM_ID_HEADER,
    LOOKUP_ATTEMPT_ID_HEADER,
    SYNC_RUN_ID_HEADER,
    PjudUsageContextMiddleware,
    current_usage_scope,
)
from app.request_id import REQUEST_ID_HEADER, RequestIdMiddleware

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()


def _app() -> TestClient:
    app = FastAPI()
    app.state.proxy_control_required = True
    app.add_middleware(PjudUsageContextMiddleware)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/api/v1/search")
    async def scope(_request: Request):
        return current_usage_scope()

    app.add_api_route("/api/v1/search/penal-books", scope)

    return TestClient(app)


def test_headers_become_request_local_proxy_attribution():
    client = _app()
    response = client.get("/api/v1/search", headers={
        LAW_FIRM_ID_HEADER: "11111111-1111-4111-8111-111111111111",
        CASE_ID_HEADER: "22222222-2222-4222-8222-222222222222",
        SYNC_RUN_ID_HEADER: "33333333-3333-4333-8333-333333333333",
    })

    assert response.status_code == 200
    assert response.json() == {
        "law_firm_id": "11111111-1111-4111-8111-111111111111",
        "case_id": "22222222-2222-4222-8222-222222222222",
        "lookup_attempt_id": None,
        "sync_run_id": "33333333-3333-4333-8333-333333333333",
    }


def test_invalid_or_partial_attribution_is_rejected_before_paid_work():
    client = _app()

    assert client.get("/api/v1/search", headers=AUTH).status_code == 422
    assert client.get("/api/v1/search", headers={**AUTH, CASE_ID_HEADER: "not-a-uuid"}).status_code == 422
    assert client.get("/api/v1/search", headers={
        **AUTH,
        CASE_ID_HEADER: "22222222-2222-4222-8222-222222222222",
    }).status_code == 422
    assert client.get("/api/v1/search/penal-books", headers=AUTH).status_code == 422


def test_lookup_attempt_is_an_exclusive_durable_paid_subject():
    client = _app()
    headers = {
        **AUTH,
        LAW_FIRM_ID_HEADER: "11111111-1111-4111-8111-111111111111",
        LOOKUP_ATTEMPT_ID_HEADER: "44444444-4444-4444-8444-444444444444",
    }
    response = client.get("/api/v1/search/penal-books", headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "law_firm_id": "11111111-1111-4111-8111-111111111111",
        "case_id": None,
        "lookup_attempt_id": "44444444-4444-4444-8444-444444444444",
        "sync_run_id": None,
    }
    assert client.get("/api/v1/search/penal-books", headers={
        **headers,
        CASE_ID_HEADER: "22222222-2222-4222-8222-222222222222",
    }).status_code == 422


def test_early_attribution_rejection_keeps_request_id_correlation():
    response = _app().get("/api/v1/search", headers={
        **AUTH,
        REQUEST_ID_HEADER: "rid-attribution-123",
    })

    assert response.status_code == 422
    assert response.headers[REQUEST_ID_HEADER] == "rid-attribution-123"
