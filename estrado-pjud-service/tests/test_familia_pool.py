# tests/test_familia_pool.py
"""Checkout de bundle F5 para el path Familia: presta el bundle de un slot
sin tomar la guest OJVSession, con la misma semántica de re-mint reactivo."""
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from app.cookie_store import CookieBundle
from app.minter import MintResult
from worker.trial_scope import TrialScope


def _rpc_supabase():
    sb = MagicMock()
    chain = MagicMock()
    sb.rpc.return_value = chain
    sb.from_.return_value = chain
    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.limit.return_value = chain
    return sb


def _response(data):
    return MagicMock(data=data)


def _reservation(number: int) -> dict:
    return {
        "allowed": True,
        "reservation_id": f"66666666-6666-4666-8666-{number:012d}",
        "claim_status": "claimed",
        "blocking_scope": None,
    }


def _trial_scope() -> TrialScope:
    return TrialScope(
        capability=SecretStr("a" * 64),
        runtime_generation="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        trial_grant_id="11111111-1111-4111-8111-111111111111",
        job_id="22222222-2222-4222-8222-222222222222",
        claim_token="33333333-3333-4333-8333-333333333333",
        worker_id="import-worker",
        law_firm_id="44444444-4444-4444-8444-444444444444",
        credential_id="55555555-5555-4555-8555-555555555555",
        expected_credentials_updated_at=datetime(
            2026, 9, 1, 12, 0, tzinfo=timezone.utc,
        ),
    )


def _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=3):
    config = MagicMock()
    config.COOKIE_STORE_PATH = "/tmp/does-not-matter-familia.json"
    config.PJUD_BASE_URL = "https://x"
    config.RATE_LIMIT_MS = 0
    config.SESSION_MAX_AGE_S = 1500
    config.POOL_SIZE = 1
    config.OJV_PROXY_URL = proxy_url
    config.OJV_PROXY_STICKY_LIFETIME = "1h"
    config.OJV_PROXY_POOL_SIZE = proxy_pool_size
    config.BLOCK_PAUSE_S = 30
    config.MINT_MAX_RETRIES = 3
    return config


class _FakeSession:
    def __init__(self, adapter):
        self.adapter = adapter
        self._age = 0.0
        self.closed = False

    async def initialize(self):
        pass

    async def close(self):
        self.closed = True

    @property
    def age_seconds(self):
        return self._age


def _patch(monkeypatch, sp):
    async def instant_sleep(_s):
        return None
    monkeypatch.setattr(sp.asyncio, "sleep", instant_sleep)

    class FakeMinter:
        def __init__(self, base_url, proxy=None):
            self.proxy = proxy

        async def mint(self):
            return MintResult(cookies={"TSPD_101": f"tok-{self.proxy}"}, user_agent="UA")

    monkeypatch.setattr(sp, "CookieMinter", FakeMinter)
    monkeypatch.setattr(sp, "Settings", lambda **k: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(sp, "OJVSession", _FakeSession)

    store = MagicMock()
    store.save_slot = MagicMock()
    store.load_slot = MagicMock(
        return_value=CookieBundle(
            cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0.0,
            proxy_url="http://user:pw@geo.iproyal.com:12321",
        )
    )
    monkeypatch.setattr(sp, "CookieStore", lambda path: store)
    return store


@pytest.mark.asyncio
async def test_acquire_familia_bundle_returns_bundle_and_slot(monkeypatch):
    from worker import session_pool as sp

    _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    bundle, slot = await pool.acquire_familia_bundle()
    assert [(cookie.name, cookie.value) for cookie in bundle.cookies] == [
        ("TSPD_101", "x"),
    ]
    assert bundle.user_agent == "UA"
    assert bundle.proxy_url == slot.proxy_url
    assert slot.busy is True  # slot tomado, nadie más lo usa
    await pool.release_familia_bundle(slot, healthy=True)
    assert slot.busy is False


@pytest.mark.asyncio
async def test_familia_checkout_respects_semaphore(monkeypatch):
    import asyncio
    from worker import session_pool as sp

    _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    _, slot = await pool.acquire_familia_bundle()
    # N=1 y el único slot está tomado → un segundo checkout debe bloquear.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pool.acquire_familia_bundle(), timeout=0.2)
    await pool.release_familia_bundle(slot, healthy=True)
    # Tras liberar, procede.
    _, slot2 = await asyncio.wait_for(pool.acquire_familia_bundle(), timeout=0.5)
    assert slot2 is slot


@pytest.mark.asyncio
async def test_release_unhealthy_remints_the_slot(monkeypatch):
    from worker import session_pool as sp

    _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    _, slot = await pool.acquire_familia_bundle()
    proxy_before = slot.proxy_url
    session_before = slot.session

    await pool.release_familia_bundle(slot, healthy=False)

    assert slot.busy is False
    assert slot.proxy_url != proxy_before  # IP nueva
    assert slot.session is not session_before


@pytest.mark.asyncio
async def test_acquire_familia_bundle_releases_on_load_failure(monkeypatch):
    """Si load_slot() tira tras tomar el semáforo, el permiso y el slot NO
    deben quedar colgados (sin leak de capacidad)."""
    import asyncio
    from worker import session_pool as sp

    store = _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    store.load_slot = MagicMock(side_effect=RuntimeError("store corrupto"))
    sem_before = pool._sem._value

    with pytest.raises(RuntimeError):
        await pool.acquire_familia_bundle()

    assert pool._sem._value == sem_before  # semáforo no se filtró
    assert all(not s.busy for s in pool._slots)  # slot liberado


@pytest.mark.asyncio
async def test_trial_scope_reaches_mint_before_first_provider_byte(monkeypatch):
    from worker import session_pool as sp

    order = []
    store = _patch(monkeypatch, sp)
    original_minter = sp.CookieMinter

    class OrderedMinter(original_minter):
        async def mint(self):
            order.append("provider")
            return await super().mint()

    monkeypatch.setattr(sp, "CookieMinter", OrderedMinter)
    calls = []
    tracker = MagicMock()

    @asynccontextmanager
    async def track(**kwargs):
        calls.append(kwargs)
        order.append("reserve")
        yield MagicMock(retry_count=0)

    tracker.track.side_effect = track
    pool = sp.SessionPool(
        _make_config(proxy_pool_size=1), proxy_usage=tracker,
    )
    await pool.initialize(prewarm=False)
    scope = _trial_scope()

    _, slot = await pool.acquire_familia_bundle(trial_scope=scope)

    assert order[:2] == ["reserve", "provider"]
    assert calls[0]["operation"] == "mint"
    assert calls[0]["trial_scope"] is scope
    await pool.release_familia_bundle(
        slot, healthy=True, trial_scope=scope,
    )
    store.load_slot.assert_called()


@pytest.mark.asyncio
async def test_trial_budget_denial_prevents_mint_and_does_not_leak_scope(monkeypatch):
    from worker import session_pool as sp
    from app.proxy_cost import ProxyUsagePersistenceError

    provider_calls = []
    _patch(monkeypatch, sp)
    original_minter = sp.CookieMinter

    class CountingMinter(original_minter):
        async def mint(self):
            provider_calls.append(1)
            return await super().mint()

    monkeypatch.setattr(sp, "CookieMinter", CountingMinter)
    calls = []
    tracker = MagicMock()

    @asynccontextmanager
    async def track(**kwargs):
        calls.append(kwargs)
        if kwargs.get("trial_scope") is not None:
            raise ProxyUsagePersistenceError("trial claim denied")
        yield MagicMock(retry_count=0)

    tracker.track.side_effect = track
    pool = sp.SessionPool(
        _make_config(proxy_pool_size=1), proxy_usage=tracker,
    )
    await pool.initialize(prewarm=False)
    scope = _trial_scope()

    with pytest.raises(ProxyUsagePersistenceError, match="trial claim denied"):
        await pool.acquire_familia_bundle(trial_scope=scope)

    assert provider_calls == []
    assert calls[0]["trial_scope"] is scope

    # A subsequent normal checkout remains normal; no ambient capability leaks.
    _, slot = await pool.acquire_familia_bundle()
    assert provider_calls == [1]
    assert "trial_scope" not in calls[1]
    await pool.release_familia_bundle(slot, healthy=True)


@pytest.mark.asyncio
async def test_trial_scope_reaches_reactive_remint(monkeypatch):
    from worker import session_pool as sp

    _patch(monkeypatch, sp)
    calls = []
    tracker = MagicMock()

    @asynccontextmanager
    async def track(**kwargs):
        calls.append(kwargs)
        yield MagicMock(retry_count=0)

    tracker.track.side_effect = track
    pool = sp.SessionPool(
        _make_config(proxy_pool_size=1), proxy_usage=tracker,
    )
    await pool.initialize(prewarm=False)
    scope = _trial_scope()
    _, slot = await pool.acquire_familia_bundle(trial_scope=scope)

    await pool.release_familia_bundle(
        slot, healthy=False, trial_scope=scope,
    )

    assert [call["operation"] for call in calls] == ["mint", "mint"]
    assert all(call["trial_scope"] is scope for call in calls)


@pytest.mark.asyncio
async def test_trial_scope_reaches_reuse_health_probe(monkeypatch):
    from worker import session_pool as sp

    store = _patch(monkeypatch, sp)
    store.load_slot.return_value = CookieBundle(
        cookies={"TSPD_101": "x"},
        user_agent="UA",
        saved_at=time.time() - 60,
        proxy_token="sticky",
    )
    store.replace_slot_cookies_if_current.return_value = True

    class Adapter:
        def __init__(self, *_args, cookies=None, **_kwargs):
            self.cookies = cookies

        def snapshot_cookies(self):
            return self.cookies

    class ValidatingSession(_FakeSession):
        async def revalidate_once(self):
            return None

    monkeypatch.setattr(sp, "OJVHttpAdapter", Adapter)
    monkeypatch.setattr(sp, "OJVSession", ValidatingSession)
    calls = []
    tracker = MagicMock()

    @asynccontextmanager
    async def track(**kwargs):
        calls.append(kwargs)
        yield MagicMock(retry_count=0)

    tracker.track.side_effect = track
    config = _make_config(proxy_pool_size=1)
    config.WORKER_SESSION_REUSE_VALIDATION_ENABLED = True
    config.SESSION_SOFT_VERIFY_AGE_S = 1200
    config.session_hard_effective_age_s = 3000
    config.MINT_TRAFFIC_BUDGET_S = 35
    pool = sp.SessionPool(config, proxy_usage=tracker)
    await pool.initialize(prewarm=False)
    scope = _trial_scope()

    _, slot = await pool.acquire_familia_bundle(trial_scope=scope)

    assert [call["operation"] for call in calls] == ["health"]
    assert calls[0]["trial_scope"] is scope
    await pool.release_familia_bundle(
        slot, healthy=True, trial_scope=scope,
    )


@pytest.mark.asyncio
async def test_real_trial_tracker_binds_missing_bundle_mint_and_reactive_remint(
    monkeypatch,
):
    from worker import session_pool as sp
    from worker.proxy_usage import ProxyUsageTracker

    _patch(monkeypatch, sp)
    normal = _rpc_supabase()
    trial = _rpc_supabase()
    tracker = ProxyUsageTracker(
        normal, trial_supabase=trial, enabled=True,
    )
    pool = sp.SessionPool(
        _make_config(proxy_pool_size=1), proxy_usage=tracker,
    )
    await pool.initialize(prewarm=False)
    scope = _trial_scope()

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response(_reservation(1)),
        _response(True),
        _response(_reservation(2)),
        _response(True),
    ]):
        _, slot = await pool.acquire_familia_bundle(trial_scope=scope)
        await pool.release_familia_bundle(
            slot, healthy=False, trial_scope=scope,
        )

    assert [call.args[0] for call in trial.rpc.call_args_list] == [
        "pjud_proxy_reserve_trial_budget",
        "pjud_proxy_finalize_trial_budget_reservation",
        "pjud_proxy_reserve_trial_budget",
        "pjud_proxy_finalize_trial_budget_reservation",
    ]
    for call in trial.rpc.call_args_list[::2]:
        payload = call.args[1]
        assert payload["p_trial_grant_id"] == str(scope.trial_grant_id)
        assert payload["p_job_id"] == str(scope.job_id)
        assert payload["p_import_claim_token"] == str(scope.claim_token)
        assert payload["p_worker_id"] == scope.worker_id
        assert payload["p_operation"] == "mint"
    normal.rpc.assert_not_called()
    normal.from_.assert_not_called()


@pytest.mark.asyncio
async def test_real_trial_tracker_binds_reuse_health_probe(monkeypatch):
    from worker import session_pool as sp
    from worker.proxy_usage import ProxyUsageTracker

    store = _patch(monkeypatch, sp)
    store.load_slot.return_value = CookieBundle(
        cookies={"TSPD_101": "x"},
        user_agent="UA",
        saved_at=time.time() - 60,
        proxy_token="sticky",
    )
    store.replace_slot_cookies_if_current.return_value = True

    class Adapter:
        def __init__(self, *_args, cookies=None, **_kwargs):
            self.cookies = cookies

        def snapshot_cookies(self):
            return self.cookies

    class ValidatingSession(_FakeSession):
        async def revalidate_once(self):
            return None

    monkeypatch.setattr(sp, "OJVHttpAdapter", Adapter)
    monkeypatch.setattr(sp, "OJVSession", ValidatingSession)
    normal = _rpc_supabase()
    trial = _rpc_supabase()
    tracker = ProxyUsageTracker(
        normal, trial_supabase=trial, enabled=True,
    )
    config = _make_config(proxy_pool_size=1)
    config.WORKER_SESSION_REUSE_VALIDATION_ENABLED = True
    config.SESSION_SOFT_VERIFY_AGE_S = 1200
    config.session_hard_effective_age_s = 3000
    config.MINT_TRAFFIC_BUDGET_S = 35
    pool = sp.SessionPool(config, proxy_usage=tracker)
    await pool.initialize(prewarm=False)
    scope = _trial_scope()

    with patch("worker.proxy_usage.run_query", side_effect=[
        _response(_reservation(1)),
        _response(True),
    ]):
        _, slot = await pool.acquire_familia_bundle(trial_scope=scope)
        await pool.release_familia_bundle(
            slot, healthy=True, trial_scope=scope,
        )

    assert [call.args[0] for call in trial.rpc.call_args_list] == [
        "pjud_proxy_reserve_trial_budget",
        "pjud_proxy_finalize_trial_budget_reservation",
    ]
    reserve = trial.rpc.call_args_list[0].args[1]
    assert reserve["p_operation"] == "health"
    assert reserve["p_job_id"] == str(scope.job_id)
    assert reserve["p_import_claim_token"] == str(scope.claim_token)
    normal.rpc.assert_not_called()
    normal.from_.assert_not_called()


def _api_pool():
    """`Settings` de verdad, no un MagicMock.

    Estos dos tests construían el pool con `MagicMock(...)` sin fijar
    `OJV_PROXY_URL`, así que ese atributo era un Mock auto-generado —o sea, no
    `None`— y `self._proxy_mode` quedaba en `True` POR ACCIDENTE. Pasaban igual
    porque su bundle ya traía `proxy_url`, pero nadie había elegido ese modo: un
    mock hace pasar por configuración cualquier cosa, y con eso el test deja de
    medir el código y pasa a medir el mock.
    """
    from app.session_pool import APISessionPool
    from tests.helpers import api_settings

    return APISessionPool(api_settings("/tmp/x.json"))


def test_api_pick_familia_bundle_none_when_empty():
    pool = _api_pool()
    pool._store = MagicMock()
    pool._store.load_all = MagicMock(return_value={})
    assert pool.pick_familia_bundle() is None


def test_api_pick_familia_bundle_returns_bundle():
    pool = _api_pool()
    b = CookieBundle(
        cookies={"TSPD_101": "z"},
        user_agent="UA",
        saved_at=time.time(),
        proxy_url="http://p",
    )
    pool._store = MagicMock()
    pool._store.load_all = MagicMock(return_value={"0": b})
    assert pool.pick_familia_bundle() is b


def test_api_no_le_presta_a_familia_un_bundle_sin_proxy():
    """El agujero que esta PR cierra, por el camino de Familia.

    Un bundle sin `proxy_url` con el pool en modo proxy egresa por la IP del
    datacenter — y acá encima con el login de Clave PJ montado encima.
    `familia_bundle_or_alert` convierte el None en 503 (infra), que es lo
    correcto: antes de #23 salía como `error_code="blocked"` con HTTP 200 y
    terminaba sumándole fallas a la causa hasta suspenderla.
    """
    pool = _api_pool()
    sin_proxy = CookieBundle(
        cookies={"TSPD_101": "z"}, user_agent="UA", saved_at=0.0, proxy_url=None
    )
    pool._store = MagicMock()
    pool._store.load_all = MagicMock(return_value={"0": sin_proxy})
    assert pool.pick_familia_bundle() is None
