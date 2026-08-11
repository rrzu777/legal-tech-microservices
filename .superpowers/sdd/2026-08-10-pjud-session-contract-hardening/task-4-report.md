# Task 4 report — 503 estable y 500 honesto

## Base y alcance

- Base confirmada antes de cambiar código: `aac32aafe5f01f38079a526002be1c72e270e822`.
- No hubo rebase, push, PR, producción ni trabajo de Task 5.
- Se revisaron las tres rutas que pasan por `pool_guard`: `search`, `detail` y
  `familia`. Las dos primeras comparten `acquire_or_alert`; Familia conserva su
  guardia de bundles.

## Implementación

- Se agregó `PoolUnavailableError` con códigos allowlisted y
  `is_expected_acquisition_failure()`.
- `APISessionPool.acquire()` y `acquire_familia_bundle()` traducen sólo el
  agotamiento operacional conocido: bundles agotados, challenge F5, fallos de
  minter, transporte no-402 y deadline. Los códigos resultantes son
  `mint_exhausted`, `session_blocked`, `upstream_unavailable`,
  `proxy_transport` o `deadline_exceeded`.
- `ValueError`, `AssertionError`, billing 402, control de tráfico, presupuesto
  y telemetría preservan su instancia/tipo. Las transiciones existentes de
  presupuesto y telemetría siguen ocurriendo antes del 503 cuando corresponde.
- `pool_guard` responde 503 sólo para indisponibilidad conocida, con el detalle
  público fijo `Servicio de sincronizacion temporalmente no disponible`.
  Excepciones inesperadas se relanzan para que FastAPI las trate como 500.
- Las alertas y logs de adquisición usan exclusivamente
  `pool_failure=<codigo-seguro>`; no interpolan el texto de la excepción.

## TDD

RED inicial:

```bash
cd estrado-pjud-service
uv run pytest -q tests/test_pool_guard.py tests/test_api_on_demand_mint.py tests/test_familia_routes.py
```

Resultado: 3 errores de colección esperados porque aún no existía la interfaz
`PoolUnavailableError`.

GREEN focal tras la implementación:

```bash
uv run pytest -q tests/test_pool_guard.py tests/test_api_on_demand_mint.py tests/test_familia_routes.py
```

Resultado: `44 passed`.

El gate extendido reveló dos expectativas heredadas en
`test_challenge_en_initialize.py` que esperaban las excepciones crudas. Se
actualizaron para afirmar el nuevo borde público del pool:
`session_blocked` tras agotar bundles/minteos y `proxy_transport` tras un
`ReadTimeout`; las aserciones de límite de intentos permanecen intactas.

## Revisión

- Revisión independiente: ningún hallazgo CRITICAL/HIGH/MEDIUM; un LOW de
  documentación de `pool_guard` que todavía describía el contrato 500 previo.
- Fix: documentación alineada con 503 para indisponibilidad operacional y 500
  para defectos inesperados.
- Re-revisión independiente: sin hallazgos nuevos.
- Re-revisión final del ajuste de regresiones heredadas: sin hallazgos; confirmó
  que no se modificó comportamiento productivo ni se eludieron controles de
  billing/control.

## Verificación final

```bash
cd estrado-pjud-service
uv run pytest -q tests/test_pool_guard.py tests/test_api_on_demand_mint.py tests/test_familia_routes.py tests/test_challenge_en_initialize.py tests/test_integration_proxy_pool.py
uv run python -m compileall -q app worker
git diff --check
```

Resultado: `67 passed in 0.77s`; `compileall` y `git diff --check` terminaron
con exit code 0.
