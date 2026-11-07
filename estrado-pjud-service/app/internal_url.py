"""Transporte del canal interno de credenciales. FUENTE ÚNICA.

Por ese canal vuelve ``{rut, password}`` en claro: la clave del Poder Judicial
de un abogado, lo más sensible que custodia el producto. La decisión "tiene que
ir por TLS" estaba escrita dos veces y en dos formas distintas —
``app/my_causes/private_fence.py`` la exigía y ``worker/sync_credentials.py``
solo pedía "string no vacío" — que es exactamente la forma en que las dos
mitades llegaron a discrepar.

Se valida en DOS alturas a propósito, y no es redundancia:

* En la config, como validator de pydantic: un ``.env`` con ``http://`` no
  arranca. ``ops/deploy.sh`` corre pytest en el VPS contra ese ``.env`` y tiene
  rollback por ``wait_health`` y ``wm_verify_restarted``, así que un boot
  fallido deja al binario anterior sirviendo — el costo en el camino de deploy
  es cero. Para un canal que lleva una credencial en claro, caído es el modo de
  falla correcto.
* En el punto de uso, como predicado: los tests construyen la config con
  ``SimpleNamespace`` y los llamadores reciben el valor por ``getattr``, así que
  el validator no es el único camino hasta el ``httpx.AsyncClient``.

Lo que NO se hace es dejar solo el chequeo por request: devuelve
``infra_unavailable``, que es el mismo balde que una caída del PJUD. Esa
confusión —un error de configuración NUESTRO presentándose como que el Poder
Judicial no responde— es la que costó el outage de dos meses y medio.
"""

from typing import Annotated

from pydantic import AfterValidator

INTERNAL_BASE_URL_REQUIRES_HTTPS = "internal_base_url_requires_https"


def is_secure_internal_base_url(value: object) -> bool:
    """True solo para una base URL usable: string no vacío y con TLS.

    Comparación exacta y sin normalizar: ``HTTPS://`` en mayúsculas no pasa, y
    está bien que no pase — un ``.env`` escrito así es un ``.env`` a revisar, no
    algo que este predicado deba adivinar.
    """
    return isinstance(value, str) and value.startswith("https://")


def validate_internal_base_url(value: str) -> str:
    """Validator de config: vacío es válido (el canal queda deshabilitado)."""
    if value and not is_secure_internal_base_url(value):
        raise ValueError(INTERNAL_BASE_URL_REQUIRES_HTTPS)
    return value


OptionalHttpsUrl = Annotated[str, AfterValidator(validate_internal_base_url)]
