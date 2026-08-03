"""Regenera openapi.json desde el código.

    API_KEY=x .venv/bin/python -m scripts.export_openapi

El snapshot comiteado es el CONTRATO con la app: test_openapi_contract.py
falla si el código y el archivo divergen, así que todo cambio de API queda
visible en el diff del PR — la clase de bug del #29 (pydantic descartando un
campo en silencio) aparece acá como una property que desaparece del schema.
La app vendorea una copia de este archivo y pinnea contra ella los campos que
realmente lee (tests/unit/pjud-contract.test.ts en el repo de la app).
"""

import json
import os
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "openapi.json"


def spec_json() -> str:
    # El import va acá adentro: app.main instancia la app al importarse y las
    # Settings exigen API_KEY — el schema no depende de su valor, así que un
    # placeholder alcanza (y no pisa un API_KEY real si existe).
    os.environ.setdefault("API_KEY", "openapi-export")
    from app.main import create_app

    # sort_keys + indent fijo: el archivo es diffeable y estable entre corridas.
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUT.write_text(spec_json())
    print(f"escrito {OUT}", file=sys.stderr)
