"""ops/env.inventory anclado a las Settings reales, en las dos direcciones.

El inventario es lo que provision.sh usa para reconstruir el VPS, y es un
snapshot a mano: sin este pin drifta en silencio. Las dos configs usan
extra="ignore", así que un typo en el inventario jamás fallaría en runtime —
solo acá. Y como deploy.sh corre pytest EN el VPS en cada deploy, este pin se
re-verifica solo, gratis, en cada despliegue.

(La dirección .env-real → inventario la cubre provision.sh con su warning de
variables extra: eso necesita el .env del VPS, que un test no tiene.)
"""

from pathlib import Path

from app.config import Settings
from worker.config import WorkerConfig

INVENTORY = Path(__file__).resolve().parents[2] / "ops" / "env.inventory"
ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def nombres_inventario() -> set[str]:
    return {
        line.strip()
        for line in INVENTORY.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def test_el_inventario_no_esta_vacio():
    # Anti-rot: si el parser deja de ver nombres, que falle esto y no que las
    # otras guardas pasen en vacío.
    assert len(nombres_inventario()) >= 10


def test_todo_nombre_del_inventario_existe_en_alguna_config():
    declarados = set(Settings.model_fields) | set(WorkerConfig.model_fields)
    fantasmas = sorted(nombres_inventario() - declarados)
    assert fantasmas == [], (
        f"Nombres del inventario que ninguna Settings declara (¿typo o rename?): {fantasmas}"
    )


def test_toda_variable_sin_default_esta_en_el_inventario():
    sin_default = {
        nombre
        for cls in (Settings, WorkerConfig)
        for nombre, field in cls.model_fields.items()
        if field.is_required()
    }
    faltan = sorted(sin_default - nombres_inventario())
    assert faltan == [], (
        f"Variables OBLIGATORIAS fuera del inventario (una reconstrucción moriría sin ellas): {faltan}"
    )


def test_todos_los_feature_flags_estan_inventariados_y_documentados():
    flags = {
        nombre
        for cls in (Settings, WorkerConfig)
        for nombre in cls.model_fields
        if nombre.startswith("ENABLE_")
    }
    ejemplo = {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.strip() and not line.startswith("#") and "=" in line
    }
    assert flags - nombres_inventario() == set()
    assert flags - ejemplo == set()
    assert "ENABLE_PJUD_PRIVATE_FAMILIA=false" in ENV_EXAMPLE.read_text().splitlines()
