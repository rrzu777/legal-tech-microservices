from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.pool_guard import acquire_or_alert, classify_and_alert
from app.session_pool import APISessionPool, ProxyTrafficDisabledError
from worker.proxy_control import ProxyControlSnapshot


def test_empty_proxy_env_health_reports_direct_mode_available(monkeypatch):
    monkeypatch.setenv("API_KEY", "t")
    monkeypatch.setenv("OJV_PROXY_URL", "")
    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            body = client.get("/api/v1/health").json()
            assert body["pjud_available"] is True
            assert client.app.state.proxy_control_required is False
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_empty_proxy_url_acquires_in_direct_mode_without_control(monkeypatch, tmp_path):
    from app import session_pool as pool_module

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )

    class DirectSession:
        def __init__(self, adapter):
            self.adapter = adapter

        async def initialize(self):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(pool_module, "OJVHttpAdapter", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(pool_module, "OJVSession", DirectSession)
    pool = APISessionPool(settings)

    session = await pool.acquire()

    assert isinstance(session, DirectSession)
    assert pool._proxy_mode is False


@pytest.mark.asyncio
async def test_api_pool_denies_before_creating_session_when_control_is_paused(tmp_path):
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://proxy.invalid",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=False, status="paused", reason_code="ops_pause", revision=2, source="database",
    )
    pool = APISessionPool(settings, proxy_control=control)

    with pytest.raises(ProxyTrafficDisabledError):
        await pool.acquire()


@pytest.mark.asyncio
async def test_pool_guard_returns_generic_503_without_provider_status():
    pool = AsyncMock()
    pool.acquire.side_effect = ProxyTrafficDisabledError("billing_exhausted")
    request = MagicMock()
    request.app.state.alerter = None

    with pytest.raises(HTTPException) as caught:
        await acquire_or_alert(pool, request, "search")

    assert caught.value.status_code == 503
    assert "402" not in str(caught.value.detail)
    assert "billing" not in str(caught.value.detail).lower()


@pytest.mark.asyncio
async def test_api_402_trips_shared_persistent_control():
    request = MagicMock()
    request.app.state.alerter = None
    request.app.state.proxy_control = AsyncMock()
    error = httpx.ProxyError("CONNECT failed: 402 Payment Required")

    kind = await classify_and_alert(error, request, "search")

    assert kind == "infra"
    request.app.state.proxy_control.trip_billing_exhausted.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_pool_initialization_402_also_trips_control():
    pool = AsyncMock()
    pool.acquire.side_effect = httpx.ProxyError("402 Payment Required")
    request = MagicMock()
    request.app.state.alerter = None
    request.app.state.proxy_control = AsyncMock()

    with pytest.raises(httpx.ProxyError):
        await acquire_or_alert(pool, request, "search")

    request.app.state.proxy_control.trip_billing_exhausted.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_api_pool_402_stops_before_trying_next_bundle(monkeypatch, tmp_path):
    from app import session_pool as pool_module
    from tests.helpers import cookie_bundle

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://proxy.invalid",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True, status="enabled", reason_code=None, revision=1, source="database",
    )
    attempts = []

    class BillingSession:
        def __init__(self, _adapter):
            attempts.append(1)

        async def initialize(self):
            raise httpx.ProxyError("402 Payment Required")

        async def close(self):
            return None

    monkeypatch.setattr(pool_module, "OJVHttpAdapter", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(pool_module, "OJVSession", BillingSession)
    pool = APISessionPool(settings, proxy_control=control)
    pool._store = MagicMock()
    pool._store.load_all.return_value = {
        "0": cookie_bundle("a"),
        "1": cookie_bundle("b"),
    }

    with pytest.raises(httpx.ProxyError):
        await pool.acquire()

    assert len(attempts) == 1
    control.trip_billing_exhausted.assert_awaited_once()
