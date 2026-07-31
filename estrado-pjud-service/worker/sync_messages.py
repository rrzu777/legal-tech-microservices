"""Qué se le escribe a `cases.last_sync_error` cuando una causa queda bloqueada.

Módulo aparte y con tests propios porque es el punto de espejo de un contrato de
strings entre dos repos, y el único lugar donde ese contrato está testeado.
Enterrarlo en un `engine.py` de 900 líneas lo volvería ingrepeable. (No es por lo
mismo que `sync-error-patch.ts` del lado de la app: allá el módulo existe porque
`"use server"` prohíbe exportar funciones puras, y en Python esa restricción no
existe.)

⚠️ ESTOS DOS STRINGS ESTÁN DUPLICADOS A PROPÓSITO. Los canónicos viven en
`packages/constants/src/index.ts` del repo LegalTech (`OJV_BLOCKED_ERROR`,
`SERVICE_UNAVAILABLE_PREFIX`), y este servicio no puede importar de allá —igual
que pasa con `MAX_CONSECUTIVE_SYNC_FAILURES`. Los escriben LOS DOS servicios y
los lee un tercero (`humanizeSyncError`, en la app), así que si cambian de un
lado hay que cambiarlos del otro o la app deja de traducir lo que escribe el
worker y el abogado ve el string crudo.
"""

from typing import Literal

from app.errors import safe_error

BlockCause = Literal["ojv", "infra"]

# Tope de lo que se persiste. `last_sync_error` es TEXT y aguanta un traceback
# entero, pero esta columna se RENDERIZA en la ficha y en el dashboard y sale por
# PostgREST a cualquier miembro del estudio. El detalle completo ya vive en
# `case_sync_runs.error_message` y en el journal, que es donde ops lo necesita.
_MAX_DETAIL_CHARS = 300

OJV_BLOCKED_ERROR = "Acceso bloqueado por OJV"
SERVICE_UNAVAILABLE_PREFIX = "Servicio de sincronizacion no disponible"


def blocked_error_message(cause: BlockCause, detail: str | None = None) -> str:
    """El texto según quién bloqueó.

    `cause="ojv"` es el WAF de la Oficina Judicial Virtual cortándonos de verdad,
    y conserva el texto exacto de siempre para que las filas viejas se sigan
    traduciendo igual. Cualquier otra cosa es una caída NUESTRA camino a OJV
    —transporte, timeout, pool sin bundle F5, sesión Familia que no levanta— y
    lleva el prefijo que la app reconoce, con el detalle crudo detrás.

    El detalle pasa por `safe_error` antes de guardarse, y eso es nuevo con este
    cambio: hasta ahora el camino de bloqueo escribía una constante fija, así que
    NUNCA llegaba texto de excepción a esta columna. Ahora sí, y hay excepciones
    que traen URLs internas de OJV con su query string —
    `SessionError(f"Clave PJ: unexpected redirect to {final_url[:80]}")`, por
    ejemplo. El camino de la API ya redactaba lo mismo (`routes/familia.py`); que
    el worker no lo hiciera era un dato con dos políticas según quién lo tomara.
    """
    if cause == "ojv":
        return OJV_BLOCKED_ERROR

    trimmed = safe_error(detail).strip() if detail else ""
    return f"{SERVICE_UNAVAILABLE_PREFIX}: {trimmed[:_MAX_DETAIL_CHARS] or 'sin detalle'}"
