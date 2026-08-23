"""Control-flow de _sync_familia_case tras el rework: guard clave_unica,
préstamo de bundle, y anti-apagón (block/timeout no penalizan)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.cookie_store import CookieBundle
from app.familia.auth import FamiliaBlockedError, InvalidCredentialsError, SessionError
from app.ojv.errors import OjvUpstreamChangedError
from app.familia.models import FamiliaCaso

from tests.helpers import find_update_payload


def _bundle():
    return CookieBundle(cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0.0, proxy_url="http://p")


def _make_engine(
    stub_update_error: bool = True,
    stub_terminal_error: bool = True,
    stub_report_invalid: bool = True,
    config=None,
):
    """Permite dejar reales los writers cuyo payload necesita inspeccionar el test."""
    from worker.engine import SyncEngine
    pool = MagicMock()
    pool.acquire_familia_bundle = AsyncMock(return_value=(_bundle(), MagicMock()))
    pool.release_familia_bundle = AsyncMock()
    engine = SyncEngine(
        pool=pool, supabase=MagicMock(), notifier=MagicMock(),
        metrics=MagicMock(), backoff=MagicMock(),
        config=config or MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
    )
    engine._finish_run = AsyncMock()
    if stub_terminal_error:
        engine._terminal_error = AsyncMock()
    engine._handle_blocked = AsyncMock()
    if stub_update_error:
        engine._update_case_error = AsyncMock()
    # Por defecto stub: sale de la maquina. Un `MagicMock` de config da una URL
    # truthy, asi que sin esto cada test del login pegaria de verdad.
    if stub_report_invalid:
        engine._report_invalid_credential = AsyncMock()
    return engine


_CASE = {
    "id": "c1", "case_number": "C-100-2024", "law_firm_id": "lf1",
    "ojv_credential_id": "cred1", "consecutive_sync_failures": 0, "matter": "familia",
}


@pytest.mark.asyncio
async def test_familia_scopes_credential_fetch_to_case_tenant():
    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(return_value=None)

    await engine._sync_familia_case(_CASE, None, MagicMock())

    engine._get_decrypted_credential.assert_awaited_once_with("cred1", "lf1")


@pytest.mark.asyncio
async def test_terminal_error_suspends_instead_of_user_pausing():
    engine = _make_engine(stub_terminal_error=False)

    await engine._terminal_error("c1", "Credencial OJV inactiva o no encontrada")

    payload = find_update_payload(engine._sb, tracking_status="suspended")
    assert payload == {
        "tracking_status": "suspended",
        "last_sync_status": "error",
        "last_sync_error": "Credencial OJV inactiva o no encontrada",
    }


@pytest.mark.asyncio
async def test_clave_unica_credential_is_terminal_not_crash():
    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_unica"}
    )

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    engine._terminal_error.assert_awaited_once()
    # No debe tocar el pool ni penalizar como bloqueo.
    engine._pool.acquire_familia_bundle.assert_not_awaited()
    engine._handle_blocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_block_does_not_penalize_and_remints(monkeypatch):
    import worker.engine as eng

    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_poder_judicial"}
    )

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=FamiliaBlockedError("F5"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    # monkeypatch (no asignación cruda) → se restaura al terminar el test.
    monkeypatch.setattr(eng, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    # "ojv": `FamiliaBlockedError` ES el portal cortandonos. Es el unico de los
    # cuatro tipos que atrapa ese `except` que de verdad culpa a OJV.
    engine._handle_blocked.assert_awaited_once_with(
        "c1", "ojv", "OJV request blocked"
    )
    assert engine._finish_run.await_args.kwargs["error_code"] == "ojv_blocked"
    engine._update_case_error.assert_not_awaited()  # NO penaliza
    # Release requests replacement of the slot.
    _, kwargs = engine._pool.release_familia_bundle.call_args
    assert kwargs.get("disposition") == "replace_before_reuse"


@pytest.mark.asyncio
async def test_session_error_no_le_echa_la_culpa_al_portal(monkeypatch):
    """El mismo `except` atrapa cuatro tipos y solo UNO es de OJV.

    Agruparlos para el retry esta bien: las cuatro son transitorias y ninguna
    penaliza a la causa. Agruparlos para el MENSAJE era inventar la culpa — una
    sesion nuestra que no levanta salia en pantalla como "OJV bloqueo
    temporalmente la consulta"."""
    import worker.engine as eng

    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_poder_judicial"}
    )

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=SessionError("no se pudo abrir sesion"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(eng, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    engine._handle_blocked.assert_awaited_once_with(
        "c1", "infra", "OJV session unavailable"
    )
    assert engine._finish_run.await_args.kwargs["error_code"] == "infra_unavailable"
    # Y sigue sin penalizar y sigue re-minteando: la clasificacion cambia el
    # texto, no el manejo.
    engine._update_case_error.assert_not_awaited()
    _, kwargs = engine._pool.release_familia_bundle.call_args
    assert kwargs.get("disposition") == "replace_before_reuse"


@pytest.mark.asyncio
async def test_upstream_change_keeps_slot_and_closed_run_taxonomy(monkeypatch):
    import worker.engine as eng

    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={
            "rut": "1-9",
            "password": "p",
            "password_type": "clave_poder_judicial",
        }
    )
    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=OjvUpstreamChangedError())
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(eng, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    assert engine._finish_run.await_args.kwargs["error_code"] == "upstream_changed"
    _, kwargs = engine._pool.release_familia_bundle.call_args
    assert kwargs.get("disposition") == "healthy"


@pytest.mark.asyncio
@pytest.mark.parametrize(("typed_error", "expected_code"), [
    (
        httpx.RemoteProtocolError("response lost after request"),
        "remote_protocol_disconnect",
    ),
    (TimeoutError("Familia timed out"), "pjud_timeout"),
])
async def test_familia_specific_run_code_precedes_infra_fallback(
    monkeypatch, typed_error, expected_code,
):
    import worker.engine as eng

    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={
            "rut": "1-9",
            "password": "p",
            "password_type": "clave_poder_judicial",
        }
    )
    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=typed_error)
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        eng, "FamiliaAuthSession", MagicMock(return_value=fake_session),
    )

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    engine._handle_blocked.assert_awaited_once()
    assert engine._handle_blocked.await_args.args[1] == "infra"
    assert engine._finish_run.await_args.kwargs["error_code"] == expected_code


@pytest.mark.asyncio
async def test_proxy_402_trips_persistent_control_without_remint(monkeypatch):
    import httpx
    import worker.engine as eng

    engine = _make_engine()
    engine._proxy_control = AsyncMock()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_poder_judicial"}
    )
    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=httpx.ProxyError("402 Payment Required"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(eng, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["status"] == "proxy_billing_exhausted"
    engine._proxy_control.trip_billing_exhausted.assert_awaited_once()
    engine._backoff.open_permanently.assert_called_once_with("billing_exhausted")
    engine._backoff.record_failure.assert_not_called()
    engine._metrics.record_error.assert_called_once_with("infra")
    assert engine._finish_run.await_args.args[4] == "infra_unavailable"
    _, kwargs = engine._pool.release_familia_bundle.call_args
    assert kwargs == {
        "disposition": "replace_before_reuse",
        "remint": False,
    }


@pytest.mark.asyncio
async def test_invalid_credentials_is_terminal_and_releases_healthy(monkeypatch):
    import worker.engine as eng

    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_poder_judicial"}
    )

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=InvalidCredentialsError("bad"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(eng, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    engine._terminal_error.assert_awaited_once()
    engine._handle_blocked.assert_not_awaited()
    # El veredicto va tambien a la CREDENCIAL. Sin esto la causa quedaba
    # suspendida y la credencial seguia con badge verde "Activa" en la app: el
    # abogado veia N causas muertas y nada que le dijera que la contrasena era
    # el problema. Y este es el unico camino que llega — la app no reintenta
    # una causa `suspended`, asi que su propio cableado no vuelve a pasar.
    engine._report_invalid_credential.assert_awaited_once_with("cred1")
    _, kwargs = engine._pool.release_familia_bundle.call_args
    # An invalid credential is not the residential IP's fault.
    assert kwargs.get("disposition") == "healthy"


# NO hay un test "un bloqueo F5 NO reporta la credencial", aunque sea la
# invariante que mas importa: reportar por una caida NUESTRA le corta el sync a
# todas las causas de esa credencial y le manda al abogado un mail pidiendole
# que cambie una contrasena que esta bien. Es que ese test no puede fallar.
# `FamiliaBlockedError` y `SessionError` los atrapa el `except` de mas afuera y
# retornan mucho antes de que se evalue el `except InvalidCredentialsError`, asi
# que el assert pasaria hasta con el reporte atornillado. Una guarda que no
# puede fallar es peor que ninguna: da confianza sin cubrir nada. Lo que de
# verdad fija ese `return` son `test_login_block_does_not_penalize_and_remints`
# y `test_session_error_no_le_echa_la_culpa_al_portal`, arriba.


@pytest.mark.asyncio
async def test_sync_success_resets_el_contador(monkeypatch):
    """Un sync Familia exitoso también debe resetear consecutive_sync_failures a 0.

    Espejo de test_sync_success_resets_el_contador en test_engine.py (path
    PJUD): la invariante "éxito resetea consecutive_sync_failures" vale en los dos únicos
    lugares que la escriben, y este era el que quedaba sin cobertura.
    """
    import worker.engine as eng

    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_poder_judicial"}
    )

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(return_value=None)
    fake_session.search_familia = AsyncMock(return_value="<html></html>")
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(eng, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    caso = FamiliaCaso(
        rit="100-2024",
        tribunal="Juzgado de Familia",
        caratulado="TEST vs TEST",
        materia="Alimentos",
        estado="En tramitación",
        fecha_ingreso="2024-01-15",
    )
    monkeypatch.setattr(eng, "parse_familia_results", MagicMock(return_value=([caso], None)))

    case = {
        **_CASE,
        "consecutive_sync_failures": 20,
        "latest_movement_date": "2024-01-15",
    }
    result = await engine._sync_familia_case(case, None, MagicMock())

    assert result["success"] is True

    success_update = find_update_payload(engine._sb, last_sync_status="success")

    assert success_update is not None, "Se esperaba un update con last_sync_status='success'"
    assert success_update["consecutive_sync_failures"] == 0, (
        f"Un sync Familia exitoso debe resetear consecutive_sync_failures a 0, "
        f"pero quedó en {success_update['consecutive_sync_failures']}"
    )
    engine._sb.rpc.assert_called_with("schedule_pjud_case_after_sync", {
        "p_case_id": "c1",
        "p_latest_movement_date": "2024-01-15",
    })


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parse_result",
    [([], "boom"), ([], None)],
    ids=["error_de_parseo", "causa_no_encontrada"],
)
async def test_el_error_familia_incrementa_el_contador_de_la_causa(monkeypatch, parse_result):
    """Los dos caminos de error de Familia también penalizan con el contador REAL.

    Espejo de TestElContadorSaleDeLaCausa en test_engine.py, que solo recorre los
    caminos PJUD. Estos dos eran los últimos call sites de `_update_case_error`
    sin un test que mirara el contador: el resto de este archivo lo mockea, así
    que verificaba que se llamara pero no con qué.
    """
    import worker.engine as eng

    engine = _make_engine(stub_update_error=False)
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_poder_judicial"}
    )

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(return_value=None)
    fake_session.search_familia = AsyncMock(return_value="<html></html>")
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(eng, "FamiliaAuthSession", MagicMock(return_value=fake_session))
    monkeypatch.setattr(eng, "parse_familia_results", MagicMock(return_value=parse_result))

    case = {**_CASE, "consecutive_sync_failures": 7}
    result = await engine._sync_familia_case(case, None, MagicMock())

    assert result["success"] is False

    error_update = find_update_payload(engine._sb, tracking_status="error")

    assert error_update is not None, "Se esperaba un update con tracking_status='error'"
    assert error_update["consecutive_sync_failures"] == 8, (
        f"Con la causa en 7 el error tiene que dejarla en 8, no en "
        f"{error_update['consecutive_sync_failures']}."
    )


class TestReportInvalidCredential:
    """El aviso a la app: es lo unico que convierte el veredicto en algo que el
    abogado puede ver y arreglar."""

    @staticmethod
    def _engine(url="https://app.test", key="k"):
        return _make_engine(
            stub_report_invalid=False,
            config=SimpleNamespace(
                OJV_TIMEOUT_S=25,
                R2_ENABLED=False,
                VERCEL_APP_URL=url,
                INTERNAL_CREDENTIALS_API_KEY=key,
            ),
        )

    @pytest.mark.asyncio
    async def test_postea_a_la_ruta_interna_con_el_bearer(self):
        engine = self._engine()
        with patch("worker.engine.httpx.AsyncClient") as mock_client:
            instance = mock_client.return_value
            instance.request = AsyncMock(return_value=MagicMock(status_code=200))
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)

            await engine._report_invalid_credential("cred1")

        args, kwargs = instance.request.call_args
        assert args == ("POST", "https://app.test/api/internal/credentials/cred1/invalidate")
        assert kwargs["headers"]["Authorization"] == "Bearer k"

    @pytest.mark.asyncio
    async def test_decrypt_envia_el_tenant_autoritativo_sin_otros_metadatos(self):
        engine = self._engine()
        with patch("worker.engine.httpx.AsyncClient") as mock_client:
            instance = mock_client.return_value
            response = MagicMock(status_code=200)
            response.json.return_value = {"password_type": "clave_poder_judicial"}
            instance.request = AsyncMock(return_value=response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)

            await engine._get_decrypted_credential("cred1", "lf1")

        _args, kwargs = instance.request.call_args
        assert kwargs["headers"] == {
            "Authorization": "Bearer k",
            "X-Law-Firm-Id": "lf1",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url,key",
        [("", "k"), ("https://app.test", "")],
        ids=["sin_url", "sin_api_key"],
    )
    async def test_sin_configuracion_no_sale_ninguna_request(self, url, key):
        engine = self._engine(url=url, key=key)
        with patch("worker.engine.httpx.AsyncClient") as mock_client:
            await engine._report_invalid_credential("cred1")
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_un_fallo_de_red_no_se_propaga(self):
        """La causa ya se marco terminal antes de llamar acá.

        Si esta excepcion escapara, se llevaria puesto el `return` que cierra el
        camino y la falla saldria por el `except Exception` de mas afuera —o
        sea, un veredicto de credencial reclasificado como error transitorio,
        que es exactamente lo que la serie de atribucion vino a arreglar.
        """
        import httpx

        engine = self._engine()
        with patch("worker.engine.httpx.AsyncClient") as mock_client:
            instance = mock_client.return_value
            instance.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("boom"))
            instance.__aexit__ = AsyncMock(return_value=False)

            await engine._report_invalid_credential("cred1")  # no levanta
