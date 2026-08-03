"""`fetch_anexo_list` era el último paso con cuerpo HTML que no lo miraba.

Search, detail y (desde el micro #28) initialize aplican el par
`reject_empty_body` + `detect_blocked`. La lista de anexos no, y su modo de
falla es el peor de los cuatro: el challenge no matchea ninguna tabla, así que
`parse_anexo_list` devuelve `[]` y el worker escribe "No anexo files found" a
nivel INFO — indistinguible de un folio que de verdad no tiene anexos. Los
documentos se pierden sin dejar señal.

La clasificación (infra, re-mint SÍ) vive en los parametrize canónicos de
`test_failure_kind`; acá sólo se afirma el corte en la sesión.
"""

import pytest

from app.failure_kind import BlockedPageError, EmptyResponseError
from app.session import OJVSession
from tests.helpers import AdapterQueGraba

_CHALLENGE = '<html><head><script>window["bobcmn"]="1011";</script></head><body></body></html>'
_TABLA_ANEXOS = (
    "<html><body><table><tr><td>Escrito</td>"
    "<td><a href='#' onclick=\"descargar('tok')\">Descargar</a></td></tr></table></body></html>"
)


@pytest.mark.asyncio
async def test_challenge_en_anexos_levanta_en_vez_de_devolver_lista_vacia():
    adapter = AdapterQueGraba(cuerpo_post=_CHALLENGE.encode("utf-8"))
    session = OJVSession(adapter)

    with pytest.raises(BlockedPageError):
        await session.fetch_anexo_list("civil/modal/anexoCausaCivil.php", "dtaAnexo", "jwt")


@pytest.mark.asyncio
async def test_cuerpo_vacio_en_anexos_es_el_tunel_no_un_bloqueo():
    """`detect_blocked("")` da True: sin el orden cero-bytes-primero, nuestra
    caída de red saldría escrita como challenge de F5. Mismo orden que los
    otros cuatro call sites."""
    adapter = AdapterQueGraba(cuerpo_post=b"")
    session = OJVSession(adapter)

    with pytest.raises(EmptyResponseError):
        await session.fetch_anexo_list("civil/modal/anexoCausaCivil.php", "dtaAnexo", "jwt")


@pytest.mark.asyncio
async def test_tabla_real_pasa_intacta():
    """Control: una respuesta sana sigue llegando al parser sin tocar."""
    adapter = AdapterQueGraba(cuerpo_post=_TABLA_ANEXOS.encode("utf-8"))
    session = OJVSession(adapter)

    html = await session.fetch_anexo_list("civil/modal/anexoCausaCivil.php", "dtaAnexo", "jwt")

    assert html == _TABLA_ANEXOS
    assert [p for p, _ in adapter.posts] == ["/ADIR_871/civil/modal/anexoCausaCivil.php"]
