import asyncio
from unittest.mock import AsyncMock, MagicMock
import httpx
import pytest

from app.cookie_store import CookieStoreLockTimeoutError
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError


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
    config.WORKER_SESSION_REUSE_VALIDATION_ENABLED = False
    config.SESSION_SOFT_VERIFY_AGE_S = 1200
    config.session_hard_effective_age_s = 3000
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


def _reuse_pool(*, proxy="http://user:pass@geo.iproyal.com:12321"):
    pool = _make_pool(proxy=proxy, mint_max_retries=3)
    pool._pool_size = 1
    pool._sem = asyncio.Semaphore(1)
    pool._config.WORKER_SESSION_REUSE_VALIDATION_ENABLED = True
    pool._config.SESSION_SOFT_VERIFY_AGE_S = 1200
    pool._config.session_hard_effective_age_s = 3000
    return pool


class _ReusableSession:
    def __init__(self, adapter=None, *, revalidation_error=None):
        self.adapter = adapter
        self.revalidation_error = revalidation_error
        self.revalidate_count = 0
        self.close_count = 0

    async def revalidate_once(self):
        self.revalidate_count += 1
        if self.revalidation_error is not None:
            raise self.revalidation_error

    async def close(self):
        self.close_count += 1

    @property
    def age_seconds(self):
        # Deliberately fresh: the optimized path must use durable wall-clock age.
        return 0


class _ReuseAdapter:
    def __init__(self, _settings, *, cookies=None, **_kwargs):
        self.cookies = cookies

    def snapshot_cookies(self):
        return {"PHPSESSID": "renewed"}


class _ReuseStore:
    def __init__(self, bundle, *, cas_result=True):
        self.bundle = bundle
        self.cas_result = cas_result
        self.loaded_slots = []
        self.cas_calls = []

    def load_slot(self, slot_id):
        self.loaded_slots.append(slot_id)
        return self.bundle

    def replace_slot_cookies_if_current(self, slot_id, **kwargs):
        self.cas_calls.append((slot_id, kwargs))
        return self.cas_result


def _bundle(saved_at, *, expires=None):
    from app.cookie_scope import CookieRecord
    from app.cookie_store import CookieBundle

    return CookieBundle(
        cookies=(CookieRecord(
            "PHPSESSID",
            "persisted",
            "oficinajudicialvirtual.pjud.cl",
            expires=expires,
        ),),
        user_agent="persisted-UA",
        saved_at=saved_at,
        proxy_token="sticky",
    )


def _patch_revalidation_candidate(monkeypatch, sp, *, error=None):
    created = []

    class Candidate(_ReusableSession):
        def __init__(self, adapter):
            super().__init__(adapter, revalidation_error=error)
            created.append(self)

    monkeypatch.setattr(sp, "Settings", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", _ReuseAdapter)
    monkeypatch.setattr(sp, "OJVSession", Candidate)
    return created


@pytest.mark.asyncio
async def test_soft_age_revalidates_and_reuses_without_mint(monkeypatch):
    """Replacing the wall clock with OJVSession.age_seconds would skip validation."""
    from worker import session_pool as sp

    now = 10_000.0
    monkeypatch.setattr(sp.time, "time", lambda: now)
    created = _patch_revalidation_candidate(monkeypatch, sp)
    pool = _reuse_pool()
    store = _ReuseStore(_bundle(now - 1300))
    pool._store = store
    old = _ReusableSession()
    slot = sp._Slot(
        index=0,
        token="sticky",
        proxy_url="http://old-proxy",
        session=old,
        bundle_saved_at=now - 1300,
    )
    pool._slots = [slot]
    pool._mint_slot = AsyncMock()

    acquired = await pool.acquire()

    assert acquired is created[0]
    assert created[0].revalidate_count == 1
    assert old.close_count == 1
    assert store.cas_calls[0][1]["expected_saved_at"] == now - 1300
    assert store.cas_calls[0][1]["expected_proxy_token"] == "sticky"
    pool._mint_slot.assert_not_awaited()
    await pool.release(acquired)

    acquired_again = await pool.acquire()
    assert acquired_again is created[0]
    assert len(created) == 1
    await pool.release(acquired_again)


@pytest.mark.asyncio
async def test_old_session_close_failure_does_not_buy_a_new_ip(monkeypatch):
    """Cleanup after a committed swap is not a failed session validation."""
    from worker import session_pool as sp

    now = 10_000.0
    monkeypatch.setattr(sp.time, "time", lambda: now)
    created = _patch_revalidation_candidate(monkeypatch, sp)
    pool = _reuse_pool()
    pool._store = _ReuseStore(_bundle(now - 1300))

    class OldSessionWithBrokenClose(_ReusableSession):
        async def close(self):
            raise httpx.ConnectError("old adapter already disconnected")

    slot = sp._Slot(
        index=0,
        token="sticky",
        proxy_url="http://old-proxy",
        session=OldSessionWithBrokenClose(),
        bundle_saved_at=now - 1300,
    )
    pool._slots = [slot]
    pool._mint_slot = AsyncMock()

    acquired = await pool.acquire()

    assert acquired is created[0]
    pool._mint_slot.assert_not_awaited()
    await pool.release(acquired)


@pytest.mark.asyncio
async def test_failed_revalidation_mints_once_and_release_cannot_mint_again(monkeypatch):
    """A failed validation plus unhealthy release must not buy two replacements."""
    from app.failure_kind import BlockedPageError
    from worker import session_pool as sp

    now = 10_000.0
    monkeypatch.setattr(sp.time, "time", lambda: now)
    created = _patch_revalidation_candidate(
        monkeypatch, sp, error=BlockedPageError("challenge"),
    )
    pool = _reuse_pool()
    pool._store = _ReuseStore(_bundle(now - 1300))
    old = _ReusableSession()
    slot = sp._Slot(
        index=0,
        token="sticky",
        proxy_url="http://old-proxy",
        session=old,
        bundle_saved_at=now - 1300,
    )
    pool._slots = [slot]
    fresh = _ReusableSession()
    mint_calls = []

    async def mint_once(target, *, max_attempts=None):
        mint_calls.append(max_attempts)
        target.session = fresh
        target.bundle_saved_at = now

    pool._mint_slot = mint_once
    pool._refresh_slot = AsyncMock()

    acquired = await pool.acquire()
    await pool.release(acquired, healthy=False)

    assert created[0].close_count == 1
    assert mint_calls == [1]
    pool._refresh_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_age_mints_once_without_revalidation(monkeypatch):
    """A fresh OJVSession object cannot hide a durable bundle past hard age."""
    from worker import session_pool as sp

    now = 10_000.0
    monkeypatch.setattr(sp.time, "time", lambda: now)
    created = _patch_revalidation_candidate(monkeypatch, sp)
    pool = _reuse_pool()
    pool._store = _ReuseStore(_bundle(now - 3000))
    old = _ReusableSession()
    slot = sp._Slot(
        index=0,
        token="sticky",
        proxy_url="http://old-proxy",
        session=old,
        bundle_saved_at=now - 3000,
    )
    pool._slots = [slot]
    fresh = _ReusableSession()
    calls = []

    async def mint_once(target, *, max_attempts=None):
        calls.append(max_attempts)
        target.session = fresh
        target.bundle_saved_at = now

    pool._mint_slot = mint_once

    acquired = await pool.acquire()

    assert acquired is fresh
    assert calls == [1]
    assert created == []
    await pool.release(acquired)


@pytest.mark.asyncio
async def test_expired_cookie_mints_once_without_revalidation(monkeypatch):
    """An explicit cookie expiry wins even while the bundle is below soft age."""
    from worker import session_pool as sp

    now = 10_000.0
    monkeypatch.setattr(sp.time, "time", lambda: now)
    created = _patch_revalidation_candidate(monkeypatch, sp)
    pool = _reuse_pool()
    pool._store = _ReuseStore(_bundle(now - 60, expires=int(now - 1)))
    old = _ReusableSession()
    slot = sp._Slot(
        index=0,
        token="sticky",
        proxy_url="http://old-proxy",
        session=old,
        bundle_saved_at=now - 60,
        cookie_expires_at=int(now - 1),
    )
    pool._slots = [slot]
    fresh = _ReusableSession()
    calls = []

    async def mint_once(target, *, max_attempts=None):
        calls.append(max_attempts)
        target.session = fresh
        target.bundle_saved_at = now
        target.cookie_expires_at = None

    pool._mint_slot = mint_once

    acquired = await pool.acquire()

    assert acquired is fresh
    assert calls == [1]
    assert created == []
    await pool.release(acquired)


@pytest.mark.asyncio
async def test_startup_reuses_persisted_numeric_slot(monkeypatch):
    """Startup must validate worker slot 0 instead of minting or reading API namespaces."""
    from worker import session_pool as sp

    now = 10_000.0
    monkeypatch.setattr(sp.time, "time", lambda: now)
    created = _patch_revalidation_candidate(monkeypatch, sp)
    pool = _reuse_pool()
    store = _ReuseStore(_bundle(now - 60))
    pool._store = store
    pool._mint_slot = AsyncMock()

    await pool.initialize()

    assert store.loaded_slots == [0]
    assert pool._slots[0].session is created[0]
    assert created[0].revalidate_count == 1
    pool._mint_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_cas_loss_closes_candidate_preserves_old_session_and_does_not_mint(monkeypatch):
    """Installing after a lost CAS would detach the session from its durable cookies."""
    from worker import session_pool as sp

    now = 10_000.0
    monkeypatch.setattr(sp.time, "time", lambda: now)
    created = _patch_revalidation_candidate(monkeypatch, sp)
    pool = _reuse_pool()
    pool._store = _ReuseStore(_bundle(now - 1300), cas_result=False)
    old = _ReusableSession()
    slot = sp._Slot(
        index=0,
        token="sticky",
        proxy_url="http://old-proxy",
        session=old,
        bundle_saved_at=now - 1300,
    )
    pool._slots = [slot]
    pool._mint_slot = AsyncMock()

    with pytest.raises(
        CookieStoreLockTimeoutError,
        match="cookie_store_compare_and_set_failed",
    ):
        await pool.acquire()

    assert created[0].close_count == 1
    assert slot.session is old
    assert old.close_count == 0
    pool._mint_slot.assert_not_awaited()
    assert slot.busy is False
    assert pool._sem._value == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    CookieStoreLockTimeoutError(),
    ProxyBudgetExceededError("budget denied"),
    ProxyUsagePersistenceError("telemetry unavailable"),
])
async def test_local_budget_or_telemetry_failure_does_not_mint(monkeypatch, failure):
    """Local control failures cannot be repaired by spending on another IP."""
    from worker import session_pool as sp

    now = 10_000.0
    monkeypatch.setattr(sp.time, "time", lambda: now)
    created = _patch_revalidation_candidate(monkeypatch, sp, error=failure)
    pool = _reuse_pool()
    pool._store = _ReuseStore(_bundle(now - 1300))
    old = _ReusableSession()
    slot = sp._Slot(
        index=0,
        token="sticky",
        proxy_url="http://old-proxy",
        session=old,
        bundle_saved_at=now - 1300,
    )
    pool._slots = [slot]
    pool._mint_slot = AsyncMock()

    with pytest.raises(type(failure)):
        await pool.acquire()

    assert created[0].close_count == 1
    assert slot.session is old
    pool._mint_slot.assert_not_awaited()
    assert slot.busy is False
    assert pool._sem._value == 1


@pytest.mark.asyncio
async def test_cancelled_revalidation_closes_candidate_and_releases_checkout(monkeypatch):
    """Cancellation after marking a slot busy must not leak the slot or candidate."""
    from worker import session_pool as sp

    started = asyncio.Event()
    closed = asyncio.Event()
    created = []

    class BlockingCandidate(_ReusableSession):
        def __init__(self, adapter):
            super().__init__(adapter)
            created.append(self)

        async def revalidate_once(self):
            started.set()
            await asyncio.Event().wait()

        async def close(self):
            self.close_count += 1
            closed.set()

    monkeypatch.setattr(sp, "Settings", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", _ReuseAdapter)
    monkeypatch.setattr(sp, "OJVSession", BlockingCandidate)
    now = 10_000.0
    monkeypatch.setattr(sp.time, "time", lambda: now)
    pool = _reuse_pool()
    pool._store = _ReuseStore(_bundle(now - 1300))
    old = _ReusableSession()
    slot = sp._Slot(
        index=0,
        token="sticky",
        proxy_url="http://old-proxy",
        session=old,
        bundle_saved_at=now - 1300,
    )
    pool._slots = [slot]
    pool._mint_slot = AsyncMock()

    task = asyncio.create_task(pool.acquire())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed.is_set()
    assert created[0].close_count == 1
    assert slot.session is old
    assert slot.busy is False
    assert pool._sem._value == 1
    pool._mint_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_and_mint_share_one_acquisition_deadline(monkeypatch):
    """Starting a mint must not reset a budget partly consumed by validation."""
    from app.failure_kind import BlockedPageError, MintUnavailableError
    from worker import session_pool as sp

    created = []

    class SlowRejectedCandidate(_ReusableSession):
        def __init__(self, adapter):
            super().__init__(adapter)
            created.append(self)

        async def revalidate_once(self):
            await asyncio.sleep(0.015)
            raise BlockedPageError("challenge")

    monkeypatch.setattr(sp, "Settings", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", _ReuseAdapter)
    monkeypatch.setattr(sp, "OJVSession", SlowRejectedCandidate)
    now = 10_000.0
    monkeypatch.setattr(sp.time, "time", lambda: now)
    pool = _reuse_pool()
    pool._config.MINT_TRAFFIC_BUDGET_S = 0.025
    pool._store = _ReuseStore(_bundle(now - 1300))
    slot = sp._Slot(
        index=0,
        token="sticky",
        proxy_url="http://old-proxy",
        session=_ReusableSession(),
        bundle_saved_at=now - 1300,
    )
    pool._slots = [slot]
    mint_started = asyncio.Event()
    mint_cancelled = asyncio.Event()

    async def blocking_mint(_target, *, max_attempts=None):
        assert max_attempts == 1
        mint_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            mint_cancelled.set()
            raise

    pool._mint_slot = blocking_mint

    with pytest.raises(MintUnavailableError) as exc_info:
        await pool.acquire()

    assert exc_info.value.code == "deadline_exceeded"
    assert mint_started.is_set()
    assert mint_cancelled.is_set()
    assert created[0].close_count == 1
    assert slot.busy is False
    assert pool._sem._value == 1
