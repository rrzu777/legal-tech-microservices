"""El pool nunca sale a la calle sin la IP residencial.

Medido en el VPS el 1 de agosto de 2026: por la IP del datacenter, OJV contesta
HTTP 200 con una pagina de challenge de F5 de ~4900 bytes, y `detect_blocked` la
reconoce. O sea que el fallback a `proxy=None` que vivia en `acquire()` terminaba
reportado como "OJV bloqueo la consulta" — una decision NUESTRA facturada al
Poder Judicial. Es el mismo defecto que #55, #21, #59 y #23 vinieron a cerrar,
sobreviviendo en el unico camino que no habiamos revisado.

⚠️ Lo que este archivo NO testea, a proposito: un techo por EDAD del bundle. La
hipotesis de que un bundle deja de servir al vencer su IP sticky esta medida y
REFUTADA — con bundles de 70-71 min y `OJV_PROXY_STICKY_LIFETIME=1h`, ocho
vueltas de `initialize()` dieron 8/8 en verde, y la IP de salida habia cambiado
respecto de la hora anterior. Las cookies TSPD siguieron valiendo desde otra IP.
Un techo por edad habria rechazado bundles que funcionan y, como el worker solo
re-mintea cuando procesa causas, habria fabricado la caida que decia prevenir.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.cookie_store import CookieBundle
from app.failure_kind import NoUsableBundleError
from tests.helpers import FakeOJVSession, api_settings


def _pool(monkeypatch, bundles, *, proxy="http://u:p@residencial:9000"):
    """Pool con el store mockeado y el adapter/session falsos.

    Devuelve (pool, capturados); `capturados` acumula los kwargs con los que se
    construyo el adapter — que es donde se ve si salio con proxy o sin el.
    """
    from app import session_pool as sp
    from app.session_pool import APISessionPool

    pool = APISessionPool(api_settings(proxy=proxy))
    pool._store = MagicMock()
    pool._store.load_all.return_value = bundles

    capturados = []

    def fake_adapter(s, proxy=None, user_agent=None, cookies=None):
        capturados.append({"proxy": proxy, "user_agent": user_agent, "cookies": cookies})
        return MagicMock()

    monkeypatch.setattr(sp, "OJVHttpAdapter", fake_adapter)
    monkeypatch.setattr(sp, "OJVSession", FakeOJVSession)
    return pool, capturados


def _bundle(tag, *, proxy_url="http://u:p@sticky:1"):
    import time

    return CookieBundle(
        cookies={"TSPD_101": f"tok-{tag}"},
        user_agent=f"UA-{tag}",
        saved_at=time.time(),
        proxy_url=proxy_url,
    )


class TestNuncaSalirSinProxy:
    def test_con_proxy_configurado_y_store_vacio_no_sale_a_la_calle(self, monkeypatch):
        pool, capturados = _pool(monkeypatch, {})

        with pytest.raises(NoUsableBundleError):
            asyncio.run(pool.acquire())

        # Lo que de verdad importa: no llego a construir NINGUN adapter. Afirmarlo
        # por ausencia y no por `proxy is None` es lo que impide que el dia que
        # alguien reintroduzca el fallback el test siga pasando.
        assert capturados == []

    def test_un_bundle_sin_proxy_url_no_sirve_en_modo_proxy(self, monkeypatch):
        # El fallback entrando por la puerta de los DATOS. El store sobrevive a
        # los deploys: alcanza un archivo escrito antes del rollout del proxy, o
        # por un worker que corrio sin OJV_PROXY_URL. Ese bundle pasaba el filtro,
        # `acquire()` no lo veia como None, y el adapter salia con proxy=None —
        # o sea por la IP del datacenter, con cookies y todo.
        pool, capturados = _pool(monkeypatch, {"0": _bundle("sin-proxy", proxy_url=None)})

        with pytest.raises(NoUsableBundleError):
            asyncio.run(pool.acquire())
        assert capturados == []

    def test_un_proxy_url_vacio_tampoco_sirve(self, monkeypatch):
        # `""` es tan inservible como `None` y egresa igual por la IP del
        # datacenter, pero pasa un chequeo escrito como `is not None`.
        pool, capturados = _pool(monkeypatch, {"0": _bundle("vacio", proxy_url="")})

        with pytest.raises(NoUsableBundleError):
            asyncio.run(pool.acquire())
        assert capturados == []

    def test_saltea_el_inutilizable_y_se_queda_con_el_bueno(self, monkeypatch):
        # El descarte va ANTES del round-robin: rotar sobre todos entregaba el
        # inutilizable una de cada N veces, o sea un fallo intermitente.
        pool, _ = _pool(
            monkeypatch,
            {
                "0": _bundle("malo0", proxy_url=None),
                "1": _bundle("bueno"),
                "2": _bundle("malo2", proxy_url=None),
            },
        )
        for _ in range(6):
            elegido = pool._pick_bundle()
            assert elegido is not None
            assert elegido.cookies == {"TSPD_101": "tok-bueno"}

    def test_en_modo_proxy_sigue_rotando_entre_los_utilizables(self, monkeypatch):
        # El control del test de arriba: con un solo bundle bueno, "devolver
        # siempre el mismo" y "rotar bien" son indistinguibles. Acá hay dos
        # utilizables y uno inservible en el medio, así que se ve que el
        # round-robin sigue repartiendo el egreso entre las IPs residenciales
        # —que es para lo que existe— y no colapsó al filtrar.
        pool, _ = _pool(
            monkeypatch,
            {
                "0": _bundle("a", proxy_url="http://u:p@sticky-a:1"),
                "1": _bundle("malo", proxy_url=None),
                "2": _bundle("b", proxy_url="http://u:p@sticky-b:1"),
            },
        )
        elegidos = [pool._pick_bundle().proxy_url for _ in range(4)]
        assert elegidos == [
            "http://u:p@sticky-a:1",
            "http://u:p@sticky-b:1",
            "http://u:p@sticky-a:1",
            "http://u:p@sticky-b:1",
        ]

    def test_sin_proxy_configurado_se_conserva_el_modo_legacy(self, monkeypatch):
        # Deploy sin OJV_PROXY_URL: salir directo es el comportamiento previsto,
        # no un fallback. Sin esta rama el fix tumbaria ese modo entero.
        pool, capturados = _pool(monkeypatch, {}, proxy=None)

        asyncio.run(pool.acquire())

        assert len(capturados) == 1
        assert capturados[0]["proxy"] is None

    def test_un_bundle_con_proxy_si_se_usa(self, monkeypatch):
        # El control. Sin esto, "nunca entrega nada" pasaria todos los tests de
        # arriba y el pool quedaria inutilizado sin que nada avise.
        pool, capturados = _pool(monkeypatch, {"0": _bundle("bueno")})

        asyncio.run(pool.acquire())

        assert capturados[0]["proxy"] == "http://u:p@sticky:1"
        assert capturados[0]["cookies"] == {"TSPD_101": "tok-bueno"}
