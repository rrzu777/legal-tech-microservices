from unittest.mock import AsyncMock, MagicMock
import httpx
import pytest


def _make_pool(proxy=None, mint_max_retries=3):
    from worker.session_pool import SessionPool
    config = MagicMock()
    config.COOKIE_STORE_PATH = "/tmp/does-not-matter.json"
    config.PJUD_BASE_URL = "https://x"
    config.RATE_LIMIT_MS = 0
    config.SESSION_MAX_AGE_S = 1500
    config.POOL_SIZE = 1
    config.OJV_PROXY_URL = proxy
    config.OJV_PROXY_STICKY_LIFETIME = "1h"
    config.OJV_PROXY_POOL_SIZE = 3
    config.BLOCK_PAUSE_S = 30
    config.MINT_MAX_RETRIES = mint_max_retries
    return SessionPool(config)


@pytest.fixture
def _sin_dormir(monkeypatch):
    """El backoff entre reintentos no debe hacer dormir a la suite."""
    import asyncio as _asyncio
    dormidas = []

    async def fake_sleep(s):
        dormidas.append(s)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    return dormidas


@pytest.mark.asyncio
async def test_refresh_keeps_old_session_when_new_init_fails(monkeypatch, _sin_dormir):
    from worker import session_pool as sp
    from app.minter import MintResult

    pool = _make_pool()
    old = MagicMock()
    old.close = AsyncMock()
    slot = sp._Slot(index=0, token=None, proxy_url=None, session=old)
    pool._slots = [slot]

    class FakeMinter:
        def __init__(self, base_url, proxy=None):
            pass

        async def mint(self):
            return MintResult(cookies={"TSPD_101": "a"}, user_agent="UA")

    monkeypatch.setattr(sp, "CookieMinter", FakeMinter)
    monkeypatch.setattr(sp, "Settings", lambda **k: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", lambda *a, **k: MagicMock())

    class FailingSession:
        def __init__(self, adapter): pass
        async def initialize(self):
            raise RuntimeError("init failed")
        async def close(self):
            pass
    monkeypatch.setattr(sp, "OJVSession", FailingSession)

    with pytest.raises(RuntimeError):
        await pool._refresh_slot(slot)

    # The old session must NOT have been closed, and must still be on the slot.
    old.close.assert_not_awaited()
    assert slot.session is old


# --- A1: retry + backoff en el minteo -------------------------------------
# El proxy residencial falla ~12% de las veces, uniforme, desde el dia 1 del pool.
# Sin reintento, cada uno de esos fallos le cuesta una sincronizacion a una causa.


def _stub_camino_de_minteo(monkeypatch, mint_fn, session_cls=None):
    """Mockea todo el camino de minteo salvo `mint`, que lo define cada test."""
    from worker import session_pool as sp

    class FakeMinter:
        def __init__(self, base_url, proxy=None):
            self.proxy = proxy

        async def mint(self):
            return await mint_fn(self)

    class OkSession:
        def __init__(self, adapter):
            pass

        async def initialize(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(sp, "CookieMinter", FakeMinter)
    monkeypatch.setattr(sp, "Settings", lambda **k: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(sp, "OJVSession", session_cls or OkSession)


@pytest.mark.asyncio
async def test_mint_reintenta_y_sale_adelante(monkeypatch, _sin_dormir, tmp_path):
    """Un fallo transitorio del proxy no debe costar una sincronizacion."""
    from worker import session_pool as sp
    from app.minter import MintResult

    pool = _make_pool(proxy="http://user:pass@geo.iproyal.com:12321")
    pool._store = MagicMock()
    intentos = {"n": 0}

    async def mint_flaky(_self):
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise httpx.ConnectError("ERR_TUNNEL_CONNECTION_FAILED")
        return MintResult(cookies={"TSPD_101": "a"}, user_agent="UA")

    _stub_camino_de_minteo(monkeypatch, mint_flaky)

    slot = sp._Slot(index=0)
    await pool._mint_slot(slot)

    assert intentos["n"] == 3
    assert slot.session is not None
    assert len(_sin_dormir) == 2, "un backoff entre intento y intento"
    assert _sin_dormir[1] > _sin_dormir[0], "el backoff tiene que crecer"


@pytest.mark.asyncio
async def test_cada_reintento_pide_una_ip_nueva(monkeypatch, _sin_dormir):
    """Reintentar contra la MISMA IP sticky que acaba de fallar no arregla nada:
    el fallo es de la IP, no del intento."""
    from worker import session_pool as sp
    from app.minter import MintResult

    pool = _make_pool(proxy="http://user:pass@geo.iproyal.com:12321")
    pool._store = MagicMock()
    proxies_vistos = []

    async def mint_flaky(minter):
        proxies_vistos.append(minter.proxy)
        if len(proxies_vistos) < 3:
            raise httpx.ConnectError("ERR_TUNNEL_CONNECTION_FAILED")
        return MintResult(cookies={"TSPD_101": "a"}, user_agent="UA")

    _stub_camino_de_minteo(monkeypatch, mint_flaky)

    await pool._mint_slot(sp._Slot(index=0))

    assert len(proxies_vistos) == 3
    assert len(set(proxies_vistos)) == 3, f"reintento con la misma IP: {proxies_vistos}"


@pytest.mark.asyncio
async def test_agotados_los_reintentos_conserva_la_sesion_vieja(monkeypatch, _sin_dormir):
    """Swap-then-close: si el minteo falla del todo, el slot NO puede quedar con una
    sesion muerta. La vieja es vieja pero sirve."""
    from worker import session_pool as sp

    pool = _make_pool(proxy="http://user:pass@geo.iproyal.com:12321")
    pool._store = MagicMock()
    vieja = MagicMock()
    vieja.close = AsyncMock()
    intentos = {"n": 0}

    async def mint_siempre_falla(_self):
        intentos["n"] += 1
        raise httpx.ConnectError("ERR_TUNNEL_CONNECTION_FAILED")

    _stub_camino_de_minteo(monkeypatch, mint_siempre_falla)

    slot = sp._Slot(index=0, session=vieja)
    slot.last_mint_ts = -10_000  # evita el cooldown BLOCK_PAUSE_S

    with pytest.raises(httpx.ConnectError):
        await pool._mint_slot(slot)

    assert intentos["n"] == 3, "tiene que agotar MINT_MAX_RETRIES"
    assert slot.session is vieja
    vieja.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_el_retry_no_dispara_el_cooldown_de_bloqueo(monkeypatch, _sin_dormir):
    """BLOCK_PAUSE_S existe para que un slot que re-mintea EN LOOP no queme IPs.
    Los reintentos internos de un mismo minteo no son ese caso: si contaran, el
    primer mint de un slot nuevo esperaria 30s por nada."""
    from worker import session_pool as sp
    from app.minter import MintResult

    pool = _make_pool(proxy="http://user:pass@geo.iproyal.com:12321")
    pool._store = MagicMock()
    intentos = {"n": 0}

    async def mint_flaky(_self):
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise httpx.ConnectError("proxy unavailable")
        return MintResult(cookies={"TSPD_101": "a"}, user_agent="UA")

    _stub_camino_de_minteo(monkeypatch, mint_flaky)
    await pool._mint_slot(sp._Slot(index=0))

    assert all(s < 30 for s in _sin_dormir), (
        f"algun backoff se comio el BLOCK_PAUSE_S de 30s: {_sin_dormir}"
    )


@pytest.mark.asyncio
async def test_cuenta_intentos_y_fallos_de_minteo(monkeypatch, _sin_dormir):
    """B2: sin estos contadores no hay forma de ver si el proxy se degrada."""
    from worker import session_pool as sp
    from app.minter import MintResult

    pool = _make_pool(proxy="http://user:pass@geo.iproyal.com:12321")
    pool._store = MagicMock()
    intentos = {"n": 0}

    async def mint_flaky(_self):
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise httpx.ConnectError("proxy unavailable")
        return MintResult(cookies={"TSPD_101": "a"}, user_agent="UA")

    _stub_camino_de_minteo(monkeypatch, mint_flaky)
    await pool._mint_slot(sp._Slot(index=0))

    assert pool.mint_attempts == 3
    assert pool.mint_failures == 2
