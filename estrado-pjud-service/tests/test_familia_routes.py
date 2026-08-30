"""La ruta de Familia: de quién es la culpa, y que el fallo no salga mudo."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cookie_store import CookieBundle
from app.failure_kind import PoolUnavailableError
from app.familia.models import FamiliaSyncRequest
from tests.sync_claim_helpers import PAYLOAD, rpc_client
from worker.sync_credentials import SyncCredentialClient


def _sync_request(**overrides):
    return FamiliaSyncRequest.model_validate({**PAYLOAD, **overrides})


def _claims():
    return SyncCredentialClient(rpc_client([True, True]))
from app.ojv.errors import OjvTimeoutError, OjvUpstreamChangedError, SessionExpiredError
from app.pool_guard import PUBLIC_POOL_UNAVAILABLE_DETAIL


def _bundle():
    return CookieBundle(cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0.0, proxy_url="http://p")


@pytest.mark.asyncio
async def test_sin_bundle_f5_es_503_y_no_un_bloqueo_de_ojv():
    """El escenario exacto del 31 de julio de 2026.

    La ruta contestaba a "el pool no tiene bundle F5" con `error_code="blocked"`
    y **HTTP 200**. La app leía eso como una falla más de la causa, le sumaba al
    contador y a las 10 la dejaba `suspended` — estado terminal del que solo se
    sale reactivando a mano, por una caída NUESTRA.

    El 503 es lo que la hace clasificarlo como infra. Y de paso el fallo deja
    rastro: métrica propia y alerta por el HECHO, que es lo que faltaba cuando
    `/api/v1/health` mostró `total_requests: 0` durante 3 días y 18 horas.
    """
    from app.metrics import api_metrics
    from app.routes import familia as mod

    pool = MagicMock()
    pool.acquire_familia_bundle = AsyncMock(return_value=None)
    request = MagicMock()
    request.app.state.alerter = None

    with pytest.raises(HTTPException) as exc:
        await mod.familia_bundle_or_alert(pool, request)

    assert exc.value.status_code == 503
    assert exc.value.detail == PUBLIC_POOL_UNAVAILABLE_DETAIL
    assert api_metrics.snapshot()["total_pool_failures"] == 1


@pytest.mark.asyncio
async def test_familia_operational_pool_failure_is_safe_503_without_exception_text():
    """Familia must use the same public failure boundary as search and detail."""
    from app.routes import familia as mod

    pool = MagicMock()
    pool.acquire_familia_bundle = AsyncMock(
        side_effect=PoolUnavailableError("session_blocked"),
    )
    request = MagicMock()
    request.app.state.alerter = None

    with pytest.raises(HTTPException) as exc_info:
        await mod.familia_bundle_or_alert(pool, request)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == PUBLIC_POOL_UNAVAILABLE_DETAIL


@pytest.mark.asyncio
async def test_familia_known_ojv_5xx_during_bundle_acquisition_is_safe_503(tmp_path):
    """Familia must not turn a known exhausted OJV outage into a public 500."""
    import httpx

    from app.config import Settings
    from app.session_pool import APISessionPool
    from app.routes import familia as mod

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    pool = APISessionPool(settings, allow_uncontrolled_proxy=True)
    pool._mint_on_demand = AsyncMock(side_effect=httpx.HTTPStatusError(
        "upstream unavailable",
        request=httpx.Request("GET", "https://ojv.test"),
        response=httpx.Response(503),
    ))
    request = MagicMock()
    request.app.state.alerter = None

    with pytest.raises(HTTPException) as exc_info:
        await mod.familia_bundle_or_alert(pool, request)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == PUBLIC_POOL_UNAVAILABLE_DETAIL


@pytest.mark.asyncio
async def test_run_sync_blocked_when_login_challenged(monkeypatch):
    from app.routes import familia as mod
    from app.familia.auth import FamiliaBlockedError

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=FamiliaBlockedError("F5"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    req = _sync_request(rut="11111111-1", password="p", auth_type="clave_pj")
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle(), claims=_claims())

    assert resp.ok is False
    assert resp.error_code == "blocked"


@pytest.mark.asyncio
async def test_run_sync_multicase_block_aborts_batch(monkeypatch):
    from app.routes import familia as mod
    from app.familia.auth import FamiliaBlockedError
    from app.familia.models import FamiliaCaseFilter

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(return_value=None)
    fake_session.search_familia = AsyncMock(side_effect=FamiliaBlockedError("F5"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    req = _sync_request(
        rut="11111111-1", password="p", auth_type="clave_pj",
        cases=[FamiliaCaseFilter(rit="100", year="2024")],
    )
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle(), claims=_claims())

    # No debe reportar ok=True ocultando el bloqueo.
    assert resp.ok is False
    assert resp.error_code == "blocked"


@pytest.mark.asyncio
async def test_una_sesion_que_no_levanta_no_se_reporta_como_bloqueo(monkeypatch):
    """`session_error` y `blocked` son dos códigos distintos a propósito.

    La app los trata a los dos como transitorios y a ninguno le suma una falla,
    pero les escribe textos distintos: uno dice que nos bloqueó OJV y el otro que
    el caído es nuestro servicio. Si esta rama devolviera `blocked`, la ficha del
    abogado volvería a acusar al Poder Judicial de una caída nuestra.
    """
    from app.routes import familia as mod
    from app.familia.auth import SessionError

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=SessionError("Clave PJ: unexpected redirect"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    req = _sync_request(rut="11111111-1", password="p", auth_type="clave_pj")
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle(), claims=_claims())

    assert resp.ok is False
    assert resp.error_code == "session_error"


@pytest.mark.asyncio
async def test_unexpected_authenticated_login_never_serializes_secret_or_traceback(monkeypatch, caplog):
    from app.routes import familia as mod

    secret = "11.111.111-1 synthetic-password OJVID=synthetic-cookie <html>PERSONA A</html>"
    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=RuntimeError(secret))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    resp = await mod._run_sync(
        _sync_request(rut="11111111-1", password="synthetic-password", auth_type="clave_pj"),
        rate_s=0.0,
        bundle=_bundle(), claims=_claims(),
    )

    assert resp.model_dump() == {
        "ok": False,
        "casos": [],
        "error_code": "session_error",
        "error": "No se pudo establecer sesión con OJV",
    }
    rendered = caplog.text + resp.model_dump_json()
    for forbidden in ("11.111.111-1", "synthetic-password", "OJVID", "<html>", "PERSONA A"):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_case_scoped_unknown_failure_logs_no_rit_or_upstream_message(monkeypatch, caplog):
    from app.familia.models import FamiliaCaseFilter
    from app.routes import familia as mod

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(return_value=None)
    fake_session.search_familia = AsyncMock(
        side_effect=RuntimeError("OJVID=secret <html>PERSONA A / PERSONA B</html>")
    )
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    response = await mod._run_sync(
        _sync_request(
            rut="11111111-1", password="synthetic-password", auth_type="clave_pj",
            cases=[FamiliaCaseFilter(rit="987654", year="2026")],
        ),
        rate_s=0.0,
        bundle=_bundle(), claims=_claims(),
    )

    assert response.ok is False
    assert response.error_code == "session_error"
    rendered = caplog.text + response.model_dump_json()
    for forbidden in ("987654", "OJVID", "<html>", "PERSONA A", "PERSONA B"):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_un_fallo_de_red_en_el_rit_no_se_disfraza_de_causa_inexistente(monkeypatch):
    """El camino que la app usa SIEMPRE, y el que quedaba abierto.

    `syncFamiliaCase` manda siempre `cases: [{rit, year}]`, asi que la unica
    rama que corre en produccion es el loop por RIT. Ese loop se tragaba toda
    excepcion que no fuera `FamiliaBlockedError`, seguia, y devolvia `ok=True`
    con `casos=[]` y sin `error_code` — indistinguible de "esa causa no esta en
    el portal". La app entonces le sumaba una falla a la causa y a las 10 la
    suspendia, por un proxy que se cayo medio segundo.
    """
    import httpx

    from app.routes import familia as mod
    from app.familia.models import FamiliaCaseFilter

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(return_value=None)
    fake_session.search_familia = AsyncMock(side_effect=httpx.ProxyError("proxy down"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    req = _sync_request(
        rut="11111111-1", password="p", auth_type="clave_pj",
        cases=[FamiliaCaseFilter(rit="100", year="2024")],
    )
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle(), claims=_claims())

    assert resp.ok is False
    assert resp.error_code == "session_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_error",
    [
        pytest.param(SessionExpiredError(), id="expired"),
        pytest.param(OjvTimeoutError(), id="timeout"),
        pytest.param(OjvUpstreamChangedError(), id="upstream-changed"),
    ],
)
async def test_common_session_failure_does_not_become_a_missing_familia_case(
    monkeypatch, session_error
):
    from app.familia.models import FamiliaCaseFilter
    from app.routes import familia as mod

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(return_value=None)
    fake_session.search_familia = AsyncMock(side_effect=session_error)
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    response = await mod._run_sync(
        _sync_request(
            rut="11111111-1",
            password="p",
            auth_type="clave_pj",
            cases=[FamiliaCaseFilter(rit="100", year="2024")],
        ),
        rate_s=0.0,
        bundle=_bundle(), claims=_claims(),
    )

    assert response.ok is False
    assert response.error_code == "session_error"


@pytest.mark.asyncio
async def test_familia_402_trips_control_and_never_returns_provider_detail(monkeypatch):
    import httpx

    from app.routes import familia as mod
    from app.familia.models import FamiliaCaseFilter

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(return_value=None)
    fake_session.search_familia = AsyncMock(
        side_effect=httpx.ProxyError("402 Payment Required")
    )
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))
    control = AsyncMock()

    resp = await mod._run_sync(
        _sync_request(
            rut="11111111-1",
            password="p",
            auth_type="clave_pj",
            cases=[FamiliaCaseFilter(rit="100", year="2024")],
        ),
        rate_s=0.0,
        bundle=_bundle(), claims=_claims(),
        proxy_control=control,
    )

    control.trip_billing_exhausted.assert_awaited_once()
    assert resp.error_code == "session_error"
    assert "402" not in resp.error
    assert "payment" not in resp.error.lower()


@pytest.mark.asyncio
async def test_un_5xx_de_ojv_en_el_rit_se_reporta_como_bloqueo(monkeypatch):
    """La otra mitad: si el que contesto que no fue el portal, la culpa es de
    ellos y el texto tiene que decir eso, no "nuestro servicio no esta"."""
    from app.routes import familia as mod
    from app.familia.models import FamiliaCaseFilter
    from tests.helpers import http_status_error

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(return_value=None)
    fake_session.search_familia = AsyncMock(side_effect=http_status_error(503))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    req = _sync_request(
        rut="11111111-1", password="p", auth_type="clave_pj",
        cases=[FamiliaCaseFilter(rit="100", year="2024")],
    )
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle(), claims=_claims())

    assert resp.ok is False
    assert resp.error_code == "blocked"


@pytest.mark.asyncio
async def test_un_error_de_parseo_del_rit_si_es_de_la_causa(monkeypatch):
    """El default que tiene que sobrevivir: sin esto, "nada penaliza nunca" y el
    techo de suspension queda muerto."""
    from app.routes import familia as mod
    from app.familia.models import FamiliaCaseFilter

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(return_value=None)
    fake_session.search_familia = AsyncMock(side_effect=ValueError("parser roto"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    req = _sync_request(
        rut="11111111-1", password="p", auth_type="clave_pj",
        cases=[FamiliaCaseFilter(rit="100", year="2024")],
    )
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle(), claims=_claims())

    # Unknown upstream failures must not masquerade as genuinely empty results.
    assert resp.error_code == "session_error"
    assert resp.ok is False
    assert resp.casos == []
