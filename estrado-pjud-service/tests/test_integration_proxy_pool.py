"""Test de integración de la costura worker -> store -> API con componentes REALES.

Solo se mockea el browser/red (CookieMinter, OJVHttpAdapter, OJVSession). El
CookieStore es REAL (archivo en tmp) y la construcción de URLs sticky (app/proxy)
es real. Verifica el invariante cookie<->IP end-to-end entre procesos: el proceso
API egresa por los MISMOS proxy_url que el worker minteó por slot. Los mocks
aislados de cada tarea no garantizan esta costura; este test sí.
"""
from unittest.mock import MagicMock

import pytest

import worker.session_pool as wsp
import app.session_pool as asp
from app.minter import MintResult
from app.cookie_store import CookieStore
from worker.session_pool import SessionPool
from app.session_pool import APISessionPool

DUMMY_BASE = "http://user123:pw_country-cl@geo.example.com:12321"


def _worker_config(store_path, pool_size=3):
    c = MagicMock()
    c.COOKIE_STORE_PATH = str(store_path)
    c.PJUD_BASE_URL = "https://pjud.example"
    c.RATE_LIMIT_MS = 0
    c.SESSION_MAX_AGE_S = 1500
    c.POOL_SIZE = 1
    c.OJV_PROXY_URL = DUMMY_BASE
    c.OJV_PROXY_STICKY_LIFETIME = "1h"
    c.OJV_PROXY_POOL_SIZE = pool_size
    c.BLOCK_PAUSE_S = 30
    return c


class _FakeSession:
    def __init__(self, adapter=None):
        self.adapter = adapter
        self.closed = False

    async def initialize(self):
        pass

    async def close(self):
        self.closed = True

    @property
    def age_seconds(self):
        return 0.0


@pytest.mark.asyncio
async def test_worker_writes_slots_api_egresses_same_proxy_urls(tmp_path, monkeypatch):
    store_path = tmp_path / "cookies.json"

    # --- WORKER: patch browser/net, keep the store REAL ---
    async def instant_sleep(_seconds):
        return None
    monkeypatch.setattr(wsp.asyncio, "sleep", instant_sleep)

    counter = {"n": 0}

    class FakeMinter:
        def __init__(self, base_url, proxy=None):
            self.proxy = proxy

        async def mint(self):
            i = counter["n"]
            counter["n"] += 1
            return MintResult(cookies={"TSPD_101": f"cookie{i}"}, user_agent=f"UA{i}")

    monkeypatch.setattr(wsp, "CookieMinter", FakeMinter)
    monkeypatch.setattr(wsp, "OJVHttpAdapter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(wsp, "OJVSession", _FakeSession)

    pool = SessionPool(_worker_config(store_path, pool_size=3))
    await pool.initialize()

    # El store REAL quedó con 3 bundles, cada uno con su proxy_url sticky distinto.
    bundles = CookieStore(str(store_path)).load_all()
    assert len(bundles) == 3
    worker_proxies = {b.proxy_url for b in bundles.values()}
    assert len(worker_proxies) == 3  # 3 IPs distintas (tokens distintos)
    for pu in worker_proxies:
        assert pu.startswith("http://user123:pw_country-cl_session-")
        assert pu.endswith("_lifetime-1h@geo.example.com:12321")
    # cookies distintas por slot (cada slot minteó lo suyo)
    assert {tuple(b.cookies.items()) for b in bundles.values()} == {
        (("TSPD_101", "cookie0"),), (("TSPD_101", "cookie1"),), (("TSPD_101", "cookie2"),),
    }

    await pool.close_all()

    # --- API: REAL APISessionPool + REAL store, patch adapter/session ---
    captured_proxies = []

    def capture_adapter(settings, proxy=None, user_agent=None, cookies=None):
        captured_proxies.append(proxy)
        return MagicMock()

    monkeypatch.setattr(asp, "OJVHttpAdapter", capture_adapter)
    monkeypatch.setattr(asp, "OJVSession", _FakeSession)

    settings = MagicMock()
    settings.SESSION_POOL_SIZE = 2
    settings.SESSION_MAX_AGE_S = 1500
    settings.COOKIE_STORE_PATH = str(store_path)
    api = APISessionPool(settings)

    # 3 acquires sin release => 3 sesiones nuevas, round-robin sobre los 3 bundles.
    for _ in range(3):
        await api.acquire()

    # El API egresó EXACTAMENTE por los proxy_url que minteó el worker: invariante
    # cookie<->IP respetado entre procesos.
    assert set(captured_proxies) == worker_proxies


@pytest.mark.asyncio
async def test_api_falls_back_when_worker_never_minted(tmp_path, monkeypatch):
    """Sin bundles en el store (worker aún no minteó), el API degrada a proxy=None
    sin crashear."""
    store_path = tmp_path / "empty.json"
    captured = []

    def capture_adapter(settings, proxy=None, user_agent=None, cookies=None):
        captured.append(proxy)
        return MagicMock()

    monkeypatch.setattr(asp, "OJVHttpAdapter", capture_adapter)
    monkeypatch.setattr(asp, "OJVSession", _FakeSession)

    settings = MagicMock()
    settings.SESSION_POOL_SIZE = 2
    settings.SESSION_MAX_AGE_S = 1500
    settings.COOKIE_STORE_PATH = str(store_path)
    api = APISessionPool(settings)
    await api.acquire()
    assert captured == [None]
