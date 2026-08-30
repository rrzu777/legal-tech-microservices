from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tests.sync_claim_helpers import CLAIM, PAYLOAD, VERSION, WEB_CLAIM, rpc_client


@pytest.fixture
def app_and_session(monkeypatch):
    from app.config import get_settings
    from app.main import create_app
    from app.routes import familia
    monkeypatch.setenv("API_KEY", "test-key")
    get_settings.cache_clear()
    app = create_app()
    app.state.session_pool = SimpleNamespace(acquire_familia_bundle=AsyncMock(return_value=SimpleNamespace(proxy_url="http://proxy", cookies={}, user_agent="UA")))
    app.state.sync_credentials = None
    app.state.proxy_supabase = rpc_client([True, True, True])
    app.state.alerter = None
    app.state.proxy_control_required = False
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    session.search_familia.return_value = "<test>"
    monkeypatch.setattr(familia, "FamiliaAuthSession", MagicMock(return_value=session))
    monkeypatch.setattr(familia, "parse_familia_results", lambda _: ([], None))
    yield app, session
    get_settings.cache_clear()


HEADERS = {"Authorization": "Bearer test-key", "X-JurisTrack-Law-Firm-ID": CLAIM["law_firm_id"],
           "X-JurisTrack-Case-ID": CLAIM["case_id"], "X-JurisTrack-Sync-Run-ID": CLAIM["run_id"]}


async def request(app, payload=PAYLOAD, headers=HEADERS, method="POST"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        return await client.request(method, "/api/v1/familia/sync", json=payload, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes,login,search", [([False], 0, 0), ([True, False], 0, 0), ([True, True, False], 1, 0)])
async def test_stale_check_before_pool_after_wait_and_before_search(app_and_session, outcomes, login, search):
    app, session = app_and_session
    app.state.proxy_supabase = rpc_client(outcomes)
    response = await request(app)
    assert response.status_code == 200
    assert response.json()["error_code"] == "sync_claim_stale"
    assert response.json()["casos"] == []
    assert session.login.await_count == login
    assert session.search_familia.await_count == search
    assert app.state.session_pool.acquire_familia_bundle.await_count == (len(outcomes) > 1)


@pytest.mark.asyncio
async def test_guard_runs_after_pool_wait_before_any_login(app_and_session):
    app, session = app_and_session
    events = []
    sb = rpc_client([True, False])
    app.state.proxy_supabase = sb
    original = sb.rpc.side_effect
    def rpc(name, args):
        events.append("check")
        assert args == {"p_context": WEB_CLAIM, "p_credential_version": VERSION}
        return original(name, args)
    sb.rpc.side_effect = rpc
    async def pool():
        events.append("pool-wait")
        return SimpleNamespace(proxy_url="http://proxy", cookies={}, user_agent="UA")
    app.state.session_pool.acquire_familia_bundle.side_effect = pool
    assert (await request(app)).json()["error_code"] == "sync_claim_stale"
    assert events == ["check", "pool-wait", "check"]
    session.login.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{"Authorization": "Bearer test-key"},
    {**HEADERS, "X-JurisTrack-Case-ID": CLAIM["credential_id"]},
    {**HEADERS, "X-JurisTrack-Sync-Run-ID": CLAIM["credential_id"]}])
async def test_missing_or_mismatched_attribution_rejected_before_pool(app_and_session, headers):
    app, session = app_and_session
    response = await request(app, headers=headers)
    assert response.status_code == 403
    session.login.assert_not_awaited()
    app.state.session_pool.acquire_familia_bundle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,headers,payload,status", [
    ("POST", {}, PAYLOAD, 401), ("POST", {"Authorization": "Bearer wrong"}, PAYLOAD, 401),
    ("POST", HEADERS, {**PAYLOAD, "cases": []}, 422),
    ("POST", HEADERS, {**PAYLOAD, "sync_claim": {**WEB_CLAIM, "claim_token": "raw-secret-bad"}}, 422),
    *[(method, HEADERS, PAYLOAD, 405) for method in ("GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS")],
])
async def test_every_private_http_rejection_is_no_store(app_and_session, method, headers, payload, status):
    app, session = app_and_session
    response = await request(app, payload, headers, method)
    assert response.status_code == status
    assert response.headers["cache-control"] == "private, no-store"
    assert "synthetic-password" not in response.text
    assert "raw-secret-bad" not in response.text
    session.login.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes", [None, [RuntimeError("raw-secret-bad")], [True, RuntimeError("raw-secret-bad")]])
async def test_missing_sql_or_error_is_safe_503(app_and_session, outcomes):
    app, session = app_and_session
    app.state.proxy_supabase = None if outcomes is None else rpc_client(outcomes)
    response = await request(app)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "private, no-store"
    assert "raw-secret-bad" not in response.text
    session.login.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_checks_all_three_boundaries_and_closes_session(app_and_session):
    app, session = app_and_session
    response = await request(app)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert app.state.proxy_supabase.rpc.call_count == 3
    session.search_familia.assert_awaited_once()
    session.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_session_infrastructure_is_http503(app_and_session):
    app, session = app_and_session
    session.login.side_effect = httpx.ConnectError("raw-secret-bad")
    response = await request(app)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "private, no-store"
    assert "raw-secret-bad" not in response.text
    session.__aexit__.assert_awaited_once()
