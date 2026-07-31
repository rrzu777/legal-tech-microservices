"""De quién es la culpa cuando una causa queda bloqueada.

El worker escribía `"Acceso bloqueado por OJV"` para TODO lo que llamara a
`_handle_blocked`, y eso incluye tres caminos que no son de OJV: timeout de
transporte, pool sin bundle F5 y sesión Familia que no levanta. La app leía esa
columna y le mostraba al abogado que el Poder Judicial nos había bloqueado
mientras el caído era nuestro propio servicio.

Ojo con el espejo: estos strings están duplicados en
`packages/constants/src/index.ts` del repo LegalTech, que es donde vive el
traductor (`humanizeSyncError`). Si cambian de un lado sin el otro, la app deja
de traducir lo que escribe el worker.
"""

import pytest

from worker.sync_messages import (
    OJV_BLOCKED_ERROR,
    SERVICE_UNAVAILABLE_PREFIX,
    blocked_error_message,
)


def test_un_bloqueo_de_ojv_conserva_el_texto_exacto_de_siempre():
    # Literal y no la constante: las filas escritas antes de este cambio dicen
    # esto, y el traductor de la app matchea por substring. Si alguien reescribe
    # la constante, este test avisa que deja de traducir el pasado.
    assert blocked_error_message("ojv") == "Acceso bloqueado por OJV"


def test_una_caida_nuestra_no_culpa_a_ojv():
    message = blocked_error_message("infra", "infra: ConnectTimeout: ojv.pjud.cl")

    # Literal y no la constante, igual que el caso de OJV de arriba: el worker es
    # el que ESCRIBE la columna que la app traduce, asi que un cambio de la
    # constante de este lado tiene que romper acá. Contra la constante, cambiarla
    # no rompe nada y la app deja de traducir en silencio.
    assert message.startswith("Servicio de sincronizacion no disponible")
    # El invariante que de verdad importa, afirmado aparte del prefijo: no
    # aparece la frase que la app traduce como "OJV bloqueo temporalmente".
    assert OJV_BLOCKED_ERROR not in message


def test_el_detalle_sobrevive_para_el_diagnostico():
    # La ficha muestra prosa, pero /admin/sync y la columna directa tienen que
    # seguir diciendo qué pasó: si se perdiera acá, se perdería para siempre —
    # `case_sync_runs` rota, `cases` no.
    assert "Pool sin bundle F5" in blocked_error_message("infra", "Pool sin bundle F5")


def test_el_detalle_no_filtra_urls_internas_de_ojv():
    # Nuevo con este cambio: hasta ahora el camino de bloqueo escribía una
    # constante fija, así que NUNCA llegaba texto de excepción a esta columna.
    # Ahora sí, y la columna se renderiza en la ficha y sale por PostgREST a
    # cualquier miembro del estudio. El camino de la API ya redactaba lo mismo.
    message = blocked_error_message(
        "infra",
        "Clave PJ: unexpected redirect to https://oficinajudicialvirtual.pjud.cl/ADIR_871/x.php?t=1",
    )

    assert "oficinajudicialvirtual" not in message
    assert "[redacted]" in message


def test_un_traceback_entero_no_termina_en_la_ficha_de_la_causa():
    # `last_sync_error` es TEXT y aguanta cualquier cosa, así que el tope no es
    # por la base: es porque esto se PINTA en pantalla.
    message = blocked_error_message("infra", "boom " * 500)

    assert len(message) < 400


@pytest.mark.parametrize("detail", [None, "", "   "])
def test_sin_detalle_lo_dice_en_vez_de_dejar_el_prefijo_colgando(detail):
    # `str(e)` de una excepción sin mensaje es "" y llega de verdad: el propio
    # engine tiene un `str(e) or "Timeout Familia sync"` por eso mismo. Sin el
    # fallback la columna terminaba en ": " y el mensaje parecía truncado.
    assert blocked_error_message("infra", detail) == f"{SERVICE_UNAVAILABLE_PREFIX}: sin detalle"


def test_cualquier_causa_que_no_sea_ojv_se_trata_como_nuestra():
    # El default seguro va para el lado de NO acusar a un tercero. Si mañana
    # aparece una causa nueva y alguien olvida mapearla, el peor resultado
    # posible es decir "fue nuestro" de más — nunca acusar al Poder Judicial.
    assert blocked_error_message("vaya-uno-a-saber", "x").startswith(SERVICE_UNAVAILABLE_PREFIX)
