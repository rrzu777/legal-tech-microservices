"""Sin token CSRF, `detail()` no sale — y la falla es NUESTRA, no de la causa.

La medición contra OJV que sostiene todo esto (405 con cero bytes sin token, 200
con expediente con token) está en el docstring de `MissingCsrfTokenError`; no se
recopia acá para que no le pase lo que ya le pasó a la nota del token, que quedó
repetida en tres lados y desactualizada en los tres.

Lo único que agrega este archivo: el regex de `initialize()` matchea HOY —medido
el 2 de agosto de 2026, 5 hits en la página de 186 KB—, así que esto no arregla
ninguna caída en curso. Desarma la trampa para el día que PJUD cambie el markup.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.failure_kind import MissingCsrfTokenError, slot_still_healthy
from app.session import OJVSession
from tests.helpers import AdapterQueGraba
from tests.test_engine import _make_case, _mock_search_response


@pytest.mark.parametrize("token", [None, ""], ids=["None", "cadena-vacia"])
async def test_detail_sin_token_no_sale_a_la_calle(token):
    adapter = AdapterQueGraba()
    session = OJVSession(adapter)
    session.csrf_token = token

    with pytest.raises(MissingCsrfTokenError):
        await session.detail("civil", "jwt.de.mentira")

    assert adapter.posts == []


async def test_detail_con_token_manda_el_token_en_el_cuerpo():
    """Control: con token sí sale, y el token viaja donde OJV lo espera."""
    adapter = AdapterQueGraba()
    session = OJVSession(adapter)
    session.csrf_token = "a" * 32

    await session.detail("civil", "jwt.de.mentira")

    assert len(adapter.posts) == 1
    _, kwargs = adapter.posts[0]
    assert kwargs["data"] == {"dtaCausa": "jwt.de.mentira", "token": "a" * 32}


def test_sin_token_el_slot_f5_no_se_re_mintea():
    """El re-mint corre el mismo regex contra el mismo markup: no puede ayudar.

    Sin esto el arreglo cambia una factura por otra — deja de suspender causas y
    empieza a quemar una IP residencial y 30 s de cooldown por causa, para
    siempre. La contracara la fija `test_failure_kind`: todo lo demás sí re-mintea.
    """
    assert slot_still_healthy(MissingCsrfTokenError("no se pudo extraer")) is True


@pytest.mark.asyncio
async def test_el_worker_no_re_mintea_el_slot_ni_penaliza_la_causa():
    """El cableado, no sólo el predicado.

    `sync_case` traduce el veredicto en dos efectos que se deciden en lugares
    distintos —`release(healthy=...)` y `_update_case_error`— y el predicado
    suelto no prueba que estén bien conectados. Este test es el que se rompe si
    alguien vuelve a poner `session_healthy = False` fijo en ese branch.
    """
    from worker.engine import SyncEngine

    mock_session = AsyncMock()
    mock_pool = AsyncMock()
    mock_pool.acquire = AsyncMock(return_value=mock_session)
    mock_pool.release = AsyncMock()
    mock_pool.enforce_global_rate_limit = AsyncMock()

    mock_sb = MagicMock()
    chain = MagicMock()
    mock_sb.from_.return_value = chain
    for metodo in ("insert", "select", "single", "update", "eq"):
        getattr(chain, metodo).return_value = chain
    chain.execute.return_value = MagicMock(data={"id": "sync-run-1"}, count=0)

    engine = SyncEngine(
        pool=mock_pool,
        supabase=mock_sb,
        notifier=AsyncMock(),
        metrics=MagicMock(),
        backoff=MagicMock(),
        config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
    )

    with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
         patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail, \
         patch.object(engine, "_update_case_error", new_callable=AsyncMock) as mock_update_error:
        mock_search.return_value = _mock_search_response()
        mock_detail.side_effect = MissingCsrfTokenError("el regex no matcheo")
        result = await engine.sync_case(_make_case())

    assert result["success"] is False
    mock_pool.release.assert_awaited_once_with(mock_session, healthy=True)
    # Y sigue sin ser culpa de la causa: eso es lo que evita el `suspended`.
    mock_update_error.assert_not_called()
