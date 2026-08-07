import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.cookie_store import CookieBundle
from app.minter import MintResult
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from tests.helpers import FakeOJVSession
from worker.proxy_control import ProxyControlSnapshot


class MemoryCookieStore:
    def __init__(self):
        self.slots: dict[str, CookieBundle] = {}

    def load_all(self):
        return dict(self.slots)

    def save_slot(self, slot_id, cookies, user_agent, proxy_token):
        self.slots[str(slot_id)] = CookieBundle(
            cookies=cookies,
            user_agent=user_agent,
            saved_at=0,
            proxy_token=proxy_token,
        )


class RecordingUsageTracker:
    def __init__(self):
        self.operations: list[str] = []

    @asynccontextmanager
    async def track(self, **kwargs):
        self.operations.append(kwargs["operation"])
        yield SimpleNamespace(retry_count=0)


@pytest.mark.asyncio
async def test_empty_proxy_pool_mints_and_persists_one_bundle_on_demand(monkeypatch, tmp_path):
    """Removing interactive lazy mint must make this test fail.

    A user action is not scheduled work: with paid traffic enabled and no
    worker bundle yet, the API must mint through the residential proxy instead
    of returning NoUsableBundleError or falling back to the datacenter IP.
    """
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="manual_canary",
        revision=1,
        source="database",
    )
    usage = RecordingUsageTracker()
    store = MemoryCookieStore()
    minted_proxies: list[str | None] = []
    adapter_calls: list[dict] = []

    class FakeMinter:
        def __init__(self, _base_url, proxy=None):
            minted_proxies.append(proxy)

        async def mint(self):
            return MintResult(cookies={"TSPD_101": "fresh"}, user_agent="fresh-UA")

    def fake_adapter(_settings, **kwargs):
        adapter_calls.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(pool_module, "CookieMinter", FakeMinter, raising=False)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", fake_adapter)
    monkeypatch.setattr(pool_module, "OJVSession", FakeOJVSession)

    pool = APISessionPool(settings, proxy_control=control, proxy_usage=usage)
    pool._store = store

    session = await pool.acquire()

    assert isinstance(session, FakeOJVSession)
    assert len(minted_proxies) == 1
    assert minted_proxies[0] is not None
    assert "geo.iproyal.com:12321" in minted_proxies[0]
    assert len(store.slots) == 1
    assert usage.operations == ["mint"]
    assert adapter_calls == [{
        "proxy": minted_proxies[0],
        "user_agent": "fresh-UA",
        "cookies": {"TSPD_101": "fresh"},
    }]


@pytest.mark.asyncio
async def test_concurrent_empty_pool_mints_only_one_bundle(monkeypatch, tmp_path):
    """Two simultaneous user actions must share one paid mint."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="interactive",
        revision=1,
        source="database",
    )
    store = MemoryCookieStore()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    minted_proxies: list[str | None] = []

    class SlowMinter:
        def __init__(self, _base_url, proxy=None):
            minted_proxies.append(proxy)

        async def mint(self):
            first_started.set()
            await release_first.wait()
            return MintResult(cookies={"TSPD_101": "fresh"}, user_agent="fresh-UA")

    monkeypatch.setattr(pool_module, "CookieMinter", SlowMinter)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(pool_module, "OJVSession", FakeOJVSession)

    pool = APISessionPool(
        settings,
        proxy_control=control,
        proxy_usage=RecordingUsageTracker(),
    )
    pool._store = store

    first = asyncio.create_task(pool.acquire())
    await first_started.wait()
    second = asyncio.create_task(pool.acquire())
    await asyncio.sleep(0)
    release_first.set()
    sessions = await asyncio.gather(first, second)

    assert all(isinstance(session, FakeOJVSession) for session in sessions)
    assert len(minted_proxies) == 1
    assert len(store.slots) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        ProxyBudgetExceededError("global"),
        ProxyUsagePersistenceError("ledger unavailable"),
    ],
)
async def test_cost_guard_fails_before_launching_on_demand_browser(
    monkeypatch, tmp_path, failure,
):
    """No reservation means no browser and therefore no paid proxy bytes."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    class DeniedTracker:
        @asynccontextmanager
        async def track(self, **_kwargs):
            raise failure
            yield

    minter_constructions = []

    class ForbiddenMinter:
        def __init__(self, *_args, **_kwargs):
            minter_constructions.append(True)

    monkeypatch.setattr(pool_module, "CookieMinter", ForbiddenMinter)
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="interactive",
        revision=1,
        source="database",
    )
    pool = APISessionPool(settings, proxy_control=control, proxy_usage=DeniedTracker())
    pool._store = MemoryCookieStore()

    with pytest.raises(type(failure)):
        await pool.acquire()

    assert minter_constructions == []


@pytest.mark.asyncio
async def test_402_during_on_demand_mint_trips_persistent_control(monkeypatch, tmp_path):
    """Provider exhaustion must stop later traffic and remain an ops-only detail."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    class BillingMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            raise httpx.ProxyError("402 Payment Required")

    monkeypatch.setattr(pool_module, "CookieMinter", BillingMinter)
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="interactive",
        revision=1,
        source="database",
    )
    pool = APISessionPool(
        settings,
        proxy_control=control,
        proxy_usage=RecordingUsageTracker(),
    )
    pool._store = MemoryCookieStore()

    with pytest.raises(httpx.ProxyError):
        await pool.acquire()

    control.trip_billing_exhausted.assert_awaited_once()
