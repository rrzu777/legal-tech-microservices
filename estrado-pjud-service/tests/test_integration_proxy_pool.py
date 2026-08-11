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
from tests.helpers import FakeOJVSession, api_settings

DUMMY_BASE = "http://user123:pw_country-cl@geo.example.com:12321"


def _api_settings(store_path):
    return api_settings(store_path, proxy=DUMMY_BASE)


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
    c.MINT_MAX_RETRIES = 3
    return c


_FakeSession = FakeOJVSession


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

    def snapshot_adapter(*_args, cookies=None, **_kwargs):
        adapter = MagicMock()
        adapter.snapshot_cookies.return_value = dict(cookies or {})
        return adapter

    monkeypatch.setattr(wsp, "CookieMinter", FakeMinter)
    monkeypatch.setattr(wsp, "OJVHttpAdapter", snapshot_adapter)
    monkeypatch.setattr(wsp, "OJVSession", _FakeSession)

    pool = SessionPool(_worker_config(store_path, pool_size=3))
    await pool.initialize()

    # El store REAL guarda sólo tokens opacos; jamás credenciales del proxy.
    bundles = CookieStore(str(store_path)).load_all()
    assert len(bundles) == 3
    worker_tokens = {b.proxy_token for b in bundles.values()}
    assert len(worker_tokens) == 3
    raw_store = store_path.read_text()
    assert "user123" not in raw_store
    assert "pw_country" not in raw_store
    # cookies distintas por slot (cada slot minteó lo suyo)
    assert {tuple(b.cookies.items()) for b in bundles.values()} == {
        (("TSPD_101", "cookie0"),), (("TSPD_101", "cookie1"),), (("TSPD_101", "cookie2"),),
    }

    familia_bundle, familia_slot = await pool.acquire_familia_bundle()
    assert familia_bundle.proxy_url == familia_slot.proxy_url
    assert "user123" in familia_bundle.proxy_url
    assert "user123" not in store_path.read_text()
    await pool.release_familia_bundle(familia_slot)

    await pool.close_all()

    # --- API: REAL APISessionPool + REAL store, patch adapter/session ---
    captured_proxies = []

    def capture_adapter(settings, proxy=None, user_agent=None, cookies=None):
        captured_proxies.append(proxy)
        adapter = MagicMock()
        adapter.snapshot_cookies.return_value = dict(cookies or {})
        return adapter

    monkeypatch.setattr(asp, "OJVHttpAdapter", capture_adapter)
    monkeypatch.setattr(asp, "OJVSession", _FakeSession)

    api = APISessionPool(_api_settings(store_path), allow_uncontrolled_proxy=True)

    # 3 acquires sin release => 3 sesiones nuevas, round-robin sobre los 3 bundles.
    for _ in range(3):
        await api.acquire()

    # El API egresó EXACTAMENTE por los proxy_url que minteó el worker: invariante
    # cookie<->IP respetado entre procesos.
    assert len(set(captured_proxies)) == 3
    for proxy_url in captured_proxies:
        assert proxy_url.startswith("http://user123:pw_country-cl_session-")
        assert proxy_url.endswith("_lifetime-1h@geo.example.com:12321")


@pytest.mark.asyncio
async def test_api_no_sale_a_la_calle_si_el_worker_nunca_minteo(tmp_path, monkeypatch):
    """Sin bundles, el API mintea residencial a demanda; jamás usa proxy=None.

    Este test decía lo contrario —"degrada a proxy=None sin crashear"— y ese
    "sin crashear" costaba caro. Medido en el VPS el 1 de agosto de 2026: por la
    IP del datacenter, OJV contesta HTTP 200 con una página de challenge de F5 de
    ~4900 bytes que `detect_blocked` reconoce, así que la ruta la reportaba como
    `blocked`. O sea: NUESTRA decisión de salir sin proxy le llegaba al abogado
    como "OJV bloqueó la consulta". Y de paso gastaba reputación de la IP en cada
    intento.

    El registro interactivo debe seguir funcionando fuera del horario del worker,
    pero la salida directa por el datacenter continúa prohibida.
    """
    store_path = tmp_path / "empty.json"
    captured = []
    minted = []

    class OnDemandMinter:
        def __init__(self, _base_url, proxy=None):
            minted.append(proxy)

        async def mint(self):
            return MintResult(cookies={"TSPD_101": "on-demand"}, user_agent="UA")

    def capture_adapter(settings, proxy=None, user_agent=None, cookies=None):
        captured.append(proxy)
        adapter = MagicMock()
        adapter.snapshot_cookies.return_value = dict(cookies or {})
        return adapter

    monkeypatch.setattr(asp, "OJVHttpAdapter", capture_adapter)
    monkeypatch.setattr(asp, "OJVSession", _FakeSession)
    monkeypatch.setattr(asp, "CookieMinter", OnDemandMinter)

    api = APISessionPool(_api_settings(store_path), allow_uncontrolled_proxy=True)

    await api.acquire()

    assert len(minted) == 1
    assert minted[0] is not None
    assert captured == minted
    assert CookieStore(str(store_path)).load_all()
