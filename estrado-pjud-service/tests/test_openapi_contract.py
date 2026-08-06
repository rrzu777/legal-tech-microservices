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


# ESPEJADO en el repo de la app (tests/unit/pjud-contract.test.ts), como la
# tabla de failure_kind: así un cambio rompedor falla ACÁ, en el CI-de-deploy
# del micro, sin depender de que alguien copie el snapshot a la app.
CONSUMED = {
    "SearchResponse": [
        "found", "match_count", "matches", "blocked", "error", "status", "truncated",
    ],
    "CandidateMatch": [
        "key", "caratulado", "fecha_ingreso", "tribunal", "tribunal_code",
    ],
    "DetailResponse": [
        "metadata", "movements", "litigantes", "ebook_token",
        "certificado_disponible", "suprema_docs", "exhortos", "incompetencia",
        "blocked", "error",
    ],
    "CaseMetadata": [
        "rol", "tribunal", "estado_administrativo", "procedimiento",
        "estado_procesal", "etapa", "fecha",
    ],
    "Movement": [
        "folio", "cuaderno", "etapa", "tramite", "descripcion", "fecha",
        "foja", "documento_url", "sala", "estado",
    ],
    "Litigante": ["rol", "rut", "nombre", "persona"],
    # total_* no los lee la app: los consume ops (curl + watchdog #9). El #29
    # fue exactamente total_bundle_retries desapareciendo en silencio.
    "HealthResponse": [
        "status", "last_successful_request", "uptime_seconds",
        "total_bundle_retries", "total_pool_failures",
    ],
    "FamiliaSyncResponse": ["ok", "casos", "error_code", "error"],
    "FamiliaCaso": ["rit", "tribunal", "caratulado", "materia", "estado", "fecha_ingreso"],
    "CatalogResponse": ["options", "source", "fetched_at"],
    "CatalogOption": ["code", "label"],
}


def test_el_contrato_cubre_lo_que_la_app_consume():
    # Anti-rot del snapshot mismo: si openapi() dejara de emitir schemas, el
    # test de arriba pasaría con un archivo vacío-consistente.
    spec = json.loads(OUT.read_text())
    for path in [
        "/api/v1/search", "/api/v1/detail", "/api/v1/health", "/api/v1/familia/sync",
        "/api/v1/catalogs/courts", "/api/v1/catalogs/tribunals", "/api/v1/catalogs/books",
    ]:
        assert path in spec["paths"], f"el contrato perdió {path}"

    schemas = spec["components"]["schemas"]
    for schema, consumed in CONSUMED.items():
        available = schemas[schema]["properties"]
        missing = [f for f in consumed if f not in available]
        assert not missing, (
            f"{schema} perdió campos que la app lee: {missing} — si es intencional, "
            "actualizar el espejo en la app (tests/unit/pjud-contract.test.ts)"
        )
