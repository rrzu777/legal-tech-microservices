"""La ruta de Familia: de quién es la culpa, y que el fallo no salga mudo."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cookie_store import CookieBundle
from app.familia.models import FamiliaSyncRequest


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
    pool.pick_familia_bundle = MagicMock(return_value=None)
    request = MagicMock()
    request.app.state.alerter = None

    with pytest.raises(HTTPException) as exc:
        await mod.familia_bundle_or_alert(pool, request)

    assert exc.value.status_code == 503
    assert api_metrics.snapshot()["total_pool_failures"] == 1


@pytest.mark.asyncio
async def test_run_sync_blocked_when_login_challenged(monkeypatch):
    from app.routes import familia as mod
    from app.familia.auth import FamiliaBlockedError

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=FamiliaBlockedError("F5"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    req = FamiliaSyncRequest(rut="11111111-1", password="p", auth_type="clave_pj")
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle())

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

    req = FamiliaSyncRequest(
        rut="11111111-1", password="p", auth_type="clave_pj",
        cases=[FamiliaCaseFilter(rit="100", year="2024")],
    )
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle())

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

    req = FamiliaSyncRequest(rut="11111111-1", password="p", auth_type="clave_pj")
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle())

    assert resp.ok is False
    assert resp.error_code == "session_error"


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

    req = FamiliaSyncRequest(
        rut="11111111-1", password="p", auth_type="clave_pj",
        cases=[FamiliaCaseFilter(rit="100", year="2024")],
    )
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle())

    assert resp.ok is False
    assert resp.error_code == "session_error"


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

    req = FamiliaSyncRequest(
        rut="11111111-1", password="p", auth_type="clave_pj",
        cases=[FamiliaCaseFilter(rit="100", year="2024")],
    )
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle())

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

    req = FamiliaSyncRequest(
        rut="11111111-1", password="p", auth_type="clave_pj",
        cases=[FamiliaCaseFilter(rit="100", year="2024")],
    )
    resp = await mod._run_sync(req, rate_s=0.0, bundle=_bundle())

    # Se traga la excepcion y sigue, como siempre: `ok=True` sin casos es "no
    # esta en el portal", que la app cuenta como falla de la causa.
    assert resp.ok is True
    assert resp.casos == []
