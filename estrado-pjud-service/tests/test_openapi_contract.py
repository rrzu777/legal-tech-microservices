"""El openapi.json comiteado ES el código, siempre.

El snapshot es el contrato con la app (que vendorea una copia y pinnea los
campos que lee). Sin este test, el snapshot driftaría en silencio y el diff
del PR dejaría de mostrar los cambios de API — que es todo su valor: el bug
del #29 (HealthResponse descartando total_bundle_retries) habría aparecido
acá como una property borrada del schema.
"""

import json
from pathlib import Path

from scripts.export_openapi import OUT, spec_json


def test_el_snapshot_esta_al_dia():
    assert OUT.exists(), (
        "falta openapi.json — generarlo: API_KEY=x .venv/bin/python -m scripts.export_openapi"
    )
    assert OUT.read_text() == spec_json(), (
        "openapi.json quedó viejo respecto del código. Regenerar y comitear: "
        "API_KEY=x .venv/bin/python -m scripts.export_openapi "
        "(y avisar en el PR: la app vendorea este contrato)"
    )


def test_el_contrato_cubre_lo_que_la_app_consume():
    # Anti-rot del snapshot mismo: si openapi() dejara de emitir schemas, el
    # test de arriba pasaría con un archivo vacío-consistente.
    spec = json.loads(OUT.read_text())
    for path in ["/api/v1/search", "/api/v1/detail", "/api/v1/health", "/api/v1/familia/sync"]:
        assert path in spec["paths"], f"el contrato perdió {path}"
    health = spec["components"]["schemas"]["HealthResponse"]["properties"]
    # El campo del #29: si un refactor lo vuelve a perder, esto es lo que grita.
    assert "total_bundle_retries" in health
