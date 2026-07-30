"""Control-flow de _sync_familia_case tras el rework: guard clave_unica,
préstamo de bundle, y anti-apagón (block/timeout no penalizan)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cookie_store import CookieBundle
from app.familia.auth import FamiliaBlockedError, InvalidCredentialsError
from app.familia.models import FamiliaCaso

from tests.helpers import find_update_payload


def _bundle():
    return CookieBundle(cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0.0, proxy_url="http://p")


def _make_engine(stub_update_error: bool = True):
    """`stub_update_error=False` deja el `_update_case_error` de verdad, para los
    tests que necesitan mirar el payload que sale a Supabase en vez de solo
    verificar que se llamó."""
    from worker.engine import SyncEngine
    pool = MagicMock()
    pool.acquire_familia_bundle = AsyncMock(return_value=(_bundle(), MagicMock()))
    pool.release_familia_bundle = AsyncMock()
    engine = SyncEngine(
        pool=pool, supabase=MagicMock(), notifier=MagicMock(),
        metrics=MagicMock(), backoff=MagicMock(),
        config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
    )
    engine._finish_run = AsyncMock()
    engine._terminal_error = AsyncMock()
    engine._handle_blocked = AsyncMock()
    if stub_update_error:
        engine._update_case_error = AsyncMock()
    return engine


_CASE = {
    "id": "c1", "case_number": "C-100-2024", "law_firm_id": "lf1",
    "ojv_credential_id": "cred1", "consecutive_sync_failures": 0, "matter": "familia",
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
    engine._handle_blocked.assert_awaited_once_with("c1")
    engine._update_case_error.assert_not_awaited()  # NO penaliza
    # release con healthy=False (re-mint del slot).
    _, kwargs = engine._pool.release_familia_bundle.call_args
    assert kwargs.get("healthy") is False


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
    _, kwargs = engine._pool.release_familia_bundle.call_args
    assert kwargs.get("healthy") is True  # credencial inválida NO es culpa de la IP


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

    case = {**_CASE, "consecutive_sync_failures": 20}
    result = await engine._sync_familia_case(case, None, MagicMock())

    assert result["success"] is True

    success_update = find_update_payload(engine._sb, last_sync_status="success")

    assert success_update is not None, "Se esperaba un update con last_sync_status='success'"
    assert success_update["consecutive_sync_failures"] == 0, (
        f"Un sync Familia exitoso debe resetear consecutive_sync_failures a 0, "
        f"pero quedó en {success_update['consecutive_sync_failures']}"
    )


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
