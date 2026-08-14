"""El pool nunca sale a la calle sin la IP residencial.

Medido en el VPS el 1 de agosto de 2026: por la IP del datacenter, OJV contesta
HTTP 200 con una pagina de challenge de F5 de ~4900 bytes, y `detect_blocked` la
reconoce. O sea que el fallback a `proxy=None` que vivia en `acquire()` terminaba
reportado como "OJV bloqueo la consulta" — una decision NUESTRA facturada al
Poder Judicial. Es el mismo defecto que #55, #21, #59 y #23 vinieron a cerrar,
sobreviviendo en el unico camino que no habiamos revisado.

La evidencia de 70–71 minutos con `OJV_PROXY_STICKY_LIFETIME=1h` (ocho vueltas
de `initialize()` en verde) conserva esos bundles como utilizables. Pero un
bundle de 12,4 horas ya consumió un presupuesto prudente de `2×` el TTL: en
modo proxy se descarta antes de inicializar y dispara un minteo residencial
nuevo. El modo legacy no aplica este límite de edad.
"""

import asyncio
from app.minter import MintResult
from tests.helpers import cookie_bundle, cookie_values, pool_con_store


def permitir_mint_residencial(monkeypatch):
    """Reemplaza el browser por un mint controlado y registra su proxy."""
    from app import session_pool as sp

    proxies = []

    class FakeMinter:
        def __init__(self, _base_url, proxy=None):
            proxies.append(proxy)

        async def mint(self):
            return MintResult(cookies={"TSPD_101": "tok-nuevo"}, user_agent="UA-nuevo")

    monkeypatch.setattr(sp, "CookieMinter", FakeMinter)
    return proxies


def test_bundle_de_70_minutos_sigue_utilizable(monkeypatch):
    pool, _ = pool_con_store(monkeypatch, {"0": cookie_bundle("70m", age_seconds=70 * 60)})
    assert pool._pick_bundle() is not None


def test_bundle_mayor_a_dos_ttl_se_descarta(monkeypatch):
    stale = cookie_bundle("stale", age_seconds=2 * 3600 + 1)
    pool, _ = pool_con_store(monkeypatch, {"0": stale})
    assert pool._pick_bundle() is None


def test_bundle_en_borde_exacto_de_dos_ttl_sigue_utilizable(monkeypatch):
    """Cambiar el comparador de ``>`` a ``>=`` debe romper este test."""
    from app import cookie_store
    from app.cookie_store import CookieBundle

    monkeypatch.setattr(cookie_store.time, "time", lambda: 10_000.0)
    boundary = CookieBundle(
        cookies={"TSPD_101": "tok-boundary"},
        user_agent="UA-boundary",
        saved_at=2_800.0,
        proxy_url="http://u:p@sticky:1",
    )
    pool, _ = pool_con_store(monkeypatch, {"0": boundary})

    assert pool._pick_bundle() is boundary


def test_ttl_de_30m_descarta_despues_de_60m(monkeypatch):
    stale = cookie_bundle("stale", age_seconds=60 * 60 + 1)
    pool, _ = pool_con_store(monkeypatch, {"0": stale}, sticky_lifetime="30m")
    assert pool._pick_bundle() is None


def test_modo_legacy_no_descarta_por_edad(monkeypatch):
    stale = cookie_bundle("legacy", age_seconds=24 * 3600)
    pool, _ = pool_con_store(monkeypatch, {"0": stale}, proxy=None)
    assert pool._pick_bundle() is stale


def test_bundle_de_12_horas_mintea_antes_de_inicializar(monkeypatch):
    stale = cookie_bundle("viejo", age_seconds=int(12.4 * 3600))
    pool, capturados = pool_con_store(monkeypatch, {"0": stale})
    proxies = permitir_mint_residencial(monkeypatch)

    asyncio.run(pool.acquire())

    assert len(capturados) == 1
    assert cookie_values(capturados[0]["cookies"]) == {"TSPD_101": "tok-nuevo"}
    assert capturados[0]["proxy"] == proxies[0]


def test_log_de_bundle_obsoleto_no_filtra_secretos(monkeypatch, caplog):
    from app.cookie_store import CookieBundle
    import logging
    import time

    bundle = CookieBundle(
        cookies={"TSPD_101": "cookie-ultrasecreta"},
        user_agent="UA",
        saved_at=time.time() - 12 * 3600,
        proxy_url="http://usuario:password-ultrasecreto@proxy.test:1234",
        proxy_token="token-ultrasecreto",
    )
    pool, _ = pool_con_store(monkeypatch, {"7": bundle})

    with caplog.at_level(logging.WARNING, logger="app.session_pool"):
        assert pool._usable_bundles() == []

    assert "persisted_bundle_stale slot=7" in caplog.text
    assert "age_seconds=" in caplog.text
    assert "max_age_seconds=7200" in caplog.text
    assert "cookie-ultrasecreta" not in caplog.text
    assert "password-ultrasecreto" not in caplog.text
    assert "token-ultrasecreto" not in caplog.text


def test_bundle_con_saved_at_texto_mintea_y_no_filtra_secretos(monkeypatch, caplog):
    from app.cookie_store import CookieBundle
    import logging

    bundle = CookieBundle(
        cookies={"TSPD_101": "cookie-ultrasecreta"},
        user_agent="UA",
        saved_at="fecha-invalida",
        proxy_url="http://usuario:password-ultrasecreto@proxy.test:1234",
        proxy_token="token-ultrasecreto",
    )
    pool, capturados = pool_con_store(monkeypatch, {"7": bundle})
    proxies = permitir_mint_residencial(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.session_pool"):
        asyncio.run(pool.acquire())

    assert cookie_values(capturados[0]["cookies"]) == {"TSPD_101": "tok-nuevo"}
    assert capturados[0]["proxy"] == proxies[0]
    assert "persisted_bundle_invalid_saved_at slot=7" in caplog.text
    assert "cookie-ultrasecreta" not in caplog.text
    assert "password-ultrasecreto" not in caplog.text
    assert "token-ultrasecreto" not in caplog.text


def test_bundle_con_saved_at_nan_se_descarta(monkeypatch, caplog):
    from app.cookie_store import CookieBundle
    import logging

    bundle = CookieBundle(
        cookies={"TSPD_101": "tok-nan"},
        user_agent="UA",
        saved_at=float("nan"),
        proxy_url="http://u:p@sticky:1",
    )
    pool, _ = pool_con_store(monkeypatch, {"8": bundle})

    with caplog.at_level(logging.WARNING, logger="app.session_pool"):
        assert pool._usable_bundles() == []

    assert "persisted_bundle_invalid_saved_at slot=8" in caplog.text


def test_bundle_con_saved_at_futuro_se_descarta_y_mintea(monkeypatch, caplog):
    """Aceptar una edad negativa evita el límite 2x y debe romper este test."""
    from app.cookie_store import CookieBundle
    import logging
    import time

    bundle = CookieBundle(
        cookies={"TSPD_101": "cookie-futura-ultrasecreta"},
        user_agent="UA",
        saved_at=time.time() + 3600,
        proxy_url="http://usuario:password-futuro@proxy.test:1234",
        proxy_token="token-futuro-ultrasecreto",
    )
    pool, capturados = pool_con_store(monkeypatch, {"9": bundle})
    proxies = permitir_mint_residencial(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.session_pool"):
        asyncio.run(pool.acquire())

    assert cookie_values(capturados[0]["cookies"]) == {"TSPD_101": "tok-nuevo"}
    assert capturados[0]["proxy"] == proxies[0]
    assert "persisted_bundle_invalid_saved_at slot=9" in caplog.text
    assert "cookie-futura-ultrasecreta" not in caplog.text
    assert "password-futuro" not in caplog.text
    assert "token-futuro-ultrasecreto" not in caplog.text


class TestNuncaSalirSinProxy:
    def test_con_proxy_configurado_y_store_vacio_no_sale_a_la_calle(self, monkeypatch):
        pool, capturados = pool_con_store(monkeypatch, {})
        proxies = permitir_mint_residencial(monkeypatch)

        asyncio.run(pool.acquire())

        assert len(proxies) == 1
        assert proxies[0] is not None
        assert capturados[0]["proxy"] == proxies[0]

    def test_un_bundle_sin_proxy_url_no_sirve_en_modo_proxy(self, monkeypatch):
        # El fallback entrando por la puerta de los DATOS. El store sobrevive a
        # los deploys: alcanza un archivo escrito antes del rollout del proxy, o
        # por un worker que corrio sin OJV_PROXY_URL. Ese bundle pasaba el filtro,
        # `acquire()` no lo veia como None, y el adapter salia con proxy=None —
        # o sea por la IP del datacenter, con cookies y todo.
        pool, capturados = pool_con_store(monkeypatch, {"0": cookie_bundle("sin-proxy", proxy_url=None)})
        proxies = permitir_mint_residencial(monkeypatch)

        asyncio.run(pool.acquire())
        assert capturados[0]["proxy"] == proxies[0]
        assert capturados[0]["proxy"] is not None
        assert cookie_values(capturados[0]["cookies"]) == {"TSPD_101": "tok-nuevo"}

    def test_un_proxy_url_vacio_tampoco_sirve(self, monkeypatch):
        # `""` es tan inservible como `None` y egresa igual por la IP del
        # datacenter, pero pasa un chequeo escrito como `is not None`.
        pool, capturados = pool_con_store(monkeypatch, {"0": cookie_bundle("vacio", proxy_url="")})
        proxies = permitir_mint_residencial(monkeypatch)

        asyncio.run(pool.acquire())
        assert capturados[0]["proxy"] == proxies[0]
        assert capturados[0]["proxy"] is not None

    def test_saltea_el_inutilizable_y_se_queda_con_el_bueno(self, monkeypatch):
        # El descarte va ANTES del round-robin: rotar sobre todos entregaba el
        # inutilizable una de cada N veces, o sea un fallo intermitente.
        pool, _ = pool_con_store(
            monkeypatch,
            {
                "0": cookie_bundle("malo0", proxy_url=None),
                "1": cookie_bundle("bueno"),
                "2": cookie_bundle("malo2", proxy_url=None),
            },
        )
        for _ in range(6):
            elegido = pool._pick_bundle()
            assert elegido is not None
            assert cookie_values(elegido.cookies) == {"TSPD_101": "tok-bueno"}

    def test_en_modo_proxy_sigue_rotando_entre_los_utilizables(self, monkeypatch):
        # El control del test de arriba: con un solo bundle bueno, "devolver
        # siempre el mismo" y "rotar bien" son indistinguibles. Acá hay dos
        # utilizables y uno inservible en el medio, así que se ve que el
        # round-robin sigue repartiendo el egreso entre las IPs residenciales
        # —que es para lo que existe— y no colapsó al filtrar.
        pool, _ = pool_con_store(
            monkeypatch,
            {
                "0": cookie_bundle("a", proxy_url="http://u:p@sticky-a:1"),
                "1": cookie_bundle("malo", proxy_url=None),
                "2": cookie_bundle("b", proxy_url="http://u:p@sticky-b:1"),
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
        pool, capturados = pool_con_store(monkeypatch, {}, proxy=None)

        asyncio.run(pool.acquire())

        assert len(capturados) == 1
        assert capturados[0]["proxy"] is None

    def test_un_bundle_con_proxy_si_se_usa(self, monkeypatch):
        # El control. Sin esto, "nunca entrega nada" pasaria todos los tests de
        # arriba y el pool quedaria inutilizado sin que nada avise.
        pool, capturados = pool_con_store(monkeypatch, {"0": cookie_bundle("bueno")})

        asyncio.run(pool.acquire())

        assert capturados[0]["proxy"] == "http://u:p@sticky:1"
        assert cookie_values(capturados[0]["cookies"]) == {"TSPD_101": "tok-bueno"}
