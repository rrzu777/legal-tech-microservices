# PJUD Cookie Scope Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permitir que el worker aplane cookies PJUD repetidas con el mismo valor aunque tengan scopes distintos, conservando el rechazo fail-closed cuando los valores difieren.

**Architecture:** Centralizar el contrato de aplanamiento en una función pura compartida por el minter Playwright y el adapter HTTP. Mantener `dict[str, str]` como formato persistido y conservar cleanup, logs redactados y límites de tráfico existentes.

**Tech Stack:** Python 3.11+, Playwright async, httpx, pytest/pytest-asyncio.

## Global Constraints

- Mismo nombre y mismo valor se acepta aunque dominio o path cambien.
- Mismo nombre y valor distinto produce `ValueError("ambiguous_cookie_scope")`.
- Ningún nombre, valor, dominio o path aparece en logs o excepciones.
- Un candidato rechazado no se persiste ni reemplaza la sesión anterior.
- La validación productiva no ejecuta sync manual ni repite tráfico pagado en loop.

---

### Task 1: Contrato compartido de aplanamiento

**Files:**
- Modify: `estrado-pjud-service/app/minter.py`
- Modify: `estrado-pjud-service/app/adapters/http_adapter.py`
- Test: `estrado-pjud-service/tests/test_minter_proxy.py`

**Interfaces:**
- Produces: `cookies_to_dict(records: Iterable[Mapping[str, str]]) -> dict[str, str]`
- Consumes: records Playwright dict-like y objetos `http.cookiejar.Cookie` expuestos por httpx.

- [ ] **Step 1: Escribir regresiones RED**

Agregar casos que demuestren que dominio/path diferentes con valor idéntico se aceptan en `cookies_to_dict` y `snapshot_cookies`, mientras un valor distinto continúa fallando sin filtrar sentinels.

- [ ] **Step 2: Ejecutar RED**

Run: `uv run pytest -q tests/test_minter_proxy.py`

Expected: los nuevos casos equivalentes fallan con `ambiguous_cookie_scope`; los casos de valor distinto siguen pasando.

- [ ] **Step 3: Implementar el mínimo GREEN**

Comparar por `(name, value)` al aplanar. Extraer o reutilizar la función pura compartida para que minter y adapter no mantengan dos políticas divergentes.

- [ ] **Step 4: Ejecutar GREEN focal**

Run: `uv run pytest -q tests/test_minter.py tests/test_minter_proxy.py`

Expected: todos pasan.

---

### Task 2: Integración API/worker y protección del slot

**Files:**
- Test: `estrado-pjud-service/tests/test_api_on_demand_mint.py`
- Test: `estrado-pjud-service/tests/test_session_pool_proxy.py`
- Modify only if required by the behavioral tests: `estrado-pjud-service/app/session_pool.py`, `estrado-pjud-service/worker/session_pool.py`

**Interfaces:**
- Consumes: `cookies_to_dict` y `OJVHttpAdapter.snapshot_cookies` de Task 1.
- Produces: candidatos equivalentes persistibles; candidatos con valores conflictivos cerrados sin swap.

- [ ] **Step 1: Escribir regresiones RED de flujo**

Agregar fixtures reales de cookie jar que reproduzcan el mismo nombre/valor en dos scopes y comprobar que API/worker persisten una sola entrada. Mantener los asserts existentes de rechazo, cierre y preservación para valores distintos.

- [ ] **Step 2: Ejecutar focal de integración**

Run: `uv run pytest -q tests/test_api_on_demand_mint.py tests/test_session_pool_proxy.py tests/test_integration_proxy_pool.py`

Expected: todos pasan después de Task 1; si exponen otra frontera duplicada, corregir sólo esa frontera y repetir RED/GREEN.

- [ ] **Step 3: Gate completo y revisión**

Run:

```bash
uv run pytest -q
uv run python -m compileall -q app worker
git diff --check
```

Solicitar revisión exact-head sobre spec, plan, implementación y pruebas. Corregir todos los hallazgos Critical/High/Medium y repetir la revisión y los gates.

---

### Task 3: Entrega y validación productiva acotada

**Files:**
- No production source changes expected.

**Interfaces:**
- Consumes: commit revisado y exact-head verde.
- Produces: worker desplegado y evidencia agregada de un único ciclo automático.

- [ ] **Step 1: Crear PR y verificar exact-head**

Push, abrir PR, refrescar head/base/mergeability y comprobar los checks disponibles. Mergear sólo el SHA revisado.

- [ ] **Step 2: Desplegar y reiniciar**

Ejecutar `/opt/legal-tech-microservices/ops/deploy.sh`, verificar SHA productivo, `systemctl is-enabled/is-active`, `NRestarts=0` y health agregado.

- [ ] **Step 3: Validar un único ciclo automático**

Sin endpoint manual de sync, observar heartbeat, corridas `scheduled_sync`, avance agregado de `next_sync_at`, eventos/costo/reintentos, control IPRoyal y documentos en cero. Si falla, leer una sola ventana de journal redactada y detener nuevos intentos manuales.

- [ ] **Step 4: Repetir auditoría documental**

Run: `node --env-file=.env.local apps/web/scripts/audit-pjud-document-contract.mjs --phase=after`

Expected: `unsafeCasePayloads=0`, `unsafeMovementPayloads=0`, `directDocumentUrls=0`.
