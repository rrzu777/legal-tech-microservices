"""El unit de la API no puede volver a publicar uvicorn en internet.

Desde el cutover a TLS (ops/caddy/README.md) el único que entra al 8000 es
Caddy por localhost. Ese bind vive en una línea de un archivo .service que
nadie compila y que ningún test tocaba: un `--host 0.0.0.0` reintroducido en
un merge, o copiado del unit viejo al armar un VPS nuevo, no rompe nada
visible — la API sigue funcionando. Solo vuelve a quedar expuesta, que es
exactamente el estado que este cutover eliminó, y nos enteraríamos por el
journal lleno de scanners, no por un test.

Como deploy.sh corre pytest EN el VPS antes de reiniciar los servicios, esto
también ancla la máquina real: un unit divergente en /etc no pasa este test
recién cuando provision.sh lo copie de vuelta al repo, pero un deploy con el
repo mal editado se frena antes de tocar producción.
"""

from pathlib import Path

UNIT = (
    Path(__file__).resolve().parents[2] / "ops" / "systemd" / "estrado-pjud.service"
)


def exec_start() -> str:
    for line in UNIT.read_text().splitlines():
        if line.startswith("ExecStart="):
            return line
    raise AssertionError(f"{UNIT} no tiene línea ExecStart=")


def test_el_unit_ata_uvicorn_a_localhost():
    assert "--host 127.0.0.1" in exec_start()


def test_el_unit_no_expone_uvicorn_a_todas_las_interfaces():
    # Explícito además del anterior: --host podría venir dos veces, o alguien
    # podría agregar --host 0.0.0.0 después sin borrar el de localhost.
    assert "0.0.0.0" not in exec_start()


def test_el_unit_confia_los_forwarded_headers_solo_de_caddy():
    # Sin --forwarded-allow-ips, --proxy-headers es inerte y el journal loguea
    # 127.0.0.1 como cliente de todo request: se pierde la IP real, que es el
    # instrumento con el que se midió el tráfico de scanners.
    linea = exec_start()
    assert "--proxy-headers" in linea
    assert "--forwarded-allow-ips 127.0.0.1" in linea
