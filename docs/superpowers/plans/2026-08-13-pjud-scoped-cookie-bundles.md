# PJUD Scoped Cookie Bundles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preservar cookies PJUD homónimas por dominio/path en todo el ciclo y validar el worker una sola vez fuera de horario sin dejar tráfico permanente.

**Architecture:** Un módulo canónico modela, valida, serializa e instala `CookieRecord` en CookieJar. El store migra en lectura/escritura desde dict legacy a JSON v2; minter, adapter, API, worker y Familia usan el mismo tipo. Un modo de worker explícito permite exactamente un lote de una causa en una unidad transitoria sin restart.

**Tech Stack:** Python 3.11+, dataclasses, `http.cookiejar`, httpx, Playwright async, Pydantic Settings, pytest/pytest-asyncio, systemd.

**Spec:** `docs/superpowers/specs/2026-08-13-pjud-scoped-cookie-bundles-design.md`

## Global Constraints

- El store nuevo escribe exclusivamente schema v2 y lee schema legacy.
- Las cookies legacy se atan explícitamente al host de `OJV_BASE_URL` y path `/`.
- Duplicados de `(name, domain, path)` con valores distintos fallan como `ambiguous_cookie_scope`; scopes distintos coexisten.
- Ningún dato de cookie aparece en logs, alertas o telemetría.
- El modo `PJUD_OFF_HOURS_VALIDATION_ONCE` procesa como máximo una causa y no se instala en la unidad permanente.
- Billing, budget, deadline, cleanup y máximo de IPs sticky no cambian.

---

### Task 1: Modelo CookieRecord y CookieJar compartido

**Files:**
- Replace: `estrado-pjud-service/app/cookie_scope.py`
- Modify: `estrado-pjud-service/app/minter.py`
- Modify: `estrado-pjud-service/app/adapters/http_adapter.py`
- Test: `estrado-pjud-service/tests/test_cookie_scope.py`
- Test: `estrado-pjud-service/tests/test_minter_proxy.py`
- Test: `estrado-pjud-service/tests/test_http_adapter_inject.py`

**Interfaces:**
- Produces: `CookieRecord`, `normalize_cookie_records`, `playwright_cookie_records`, `cookie_jar_from_records`, `cookie_records_from_jar`.
- Consumers receive `tuple[CookieRecord, ...]`, never a name-only dict.

- [ ] **Step 1: RED modelo y scopes**

Agregar tests con dos `PHPSESSID` de valores distintos en `/` y `/consultaUnificada.php`, round-trip jar, secure/expiry, duplicado conflictivo del mismo scope y datos inválidos.

- [ ] **Step 2: Ejecutar RED**

Run: `uv run pytest -q tests/test_cookie_scope.py tests/test_minter_proxy.py tests/test_http_adapter_inject.py`

Expected: imports/contratos nuevos faltantes y casos homónimos fallan.

- [ ] **Step 3: GREEN mínimo**

Implementar dataclass inmutable, validación allowlisted y conversión `http.cookiejar.Cookie`. Cambiar `MintResult.cookies` y adapter para usar registros completos.

- [ ] **Step 4: Verificar focal**

Run: `uv run pytest -q tests/test_cookie_scope.py tests/test_minter.py tests/test_minter_proxy.py tests/test_http_adapter_inject.py`

Expected: PASS.

---

### Task 2: Store v2 y migración legacy

**Files:**
- Modify: `estrado-pjud-service/app/cookie_store.py`
- Test: `estrado-pjud-service/tests/test_cookie_store.py`
- Test: `estrado-pjud-service/tests/test_cookie_store_multi.py`
- Test: `estrado-pjud-service/tests/test_cookie_store_concurrency.py`

**Interfaces:**
- `CookieStore(path, legacy_cookie_domain=..., legacy_cookie_secure=...)`.
- `save_slot(..., cookies: Sequence[CookieRecord], ...)` escribe root `version: 2`.
- `load_all()` devuelve `CookieBundle.cookies: tuple[CookieRecord, ...]`.

- [ ] **Step 1: RED schema y compatibilidad**

Cubrir round-trip v2 con scopes homónimos, lectura single/multi legacy, migración de todos los slots en la primera escritura, slot inválido aislado, permisos/atomicidad y concurrencia.

- [ ] **Step 2: Ejecutar RED**

Run: `uv run pytest -q tests/test_cookie_store.py tests/test_cookie_store_multi.py tests/test_cookie_store_concurrency.py`

- [ ] **Step 3: GREEN store v2**

Normalizar `CookieBundle` en construcción, parsear v1/v2, serializar sólo v2 y conservar lock/read-modify-write.

- [ ] **Step 4: Verificar store**

Repetir el comando focal y comprobar JSON literal, modo `0640` y ausencia de proxy URL persistido.

---

### Task 3: Integración API, worker y Familia

**Files:**
- Modify: `estrado-pjud-service/app/session_pool.py`
- Modify: `estrado-pjud-service/worker/session_pool.py`
- Modify: `estrado-pjud-service/app/familia/auth.py`
- Modify: `estrado-pjud-service/app/routes/familia.py` only if its type contract requires it.
- Test: `estrado-pjud-service/tests/test_api_on_demand_mint.py`
- Test: `estrado-pjud-service/tests/test_session_pool_proxy.py`
- Test: `estrado-pjud-service/tests/test_integration_proxy_pool.py`
- Test: `estrado-pjud-service/tests/test_familia_pool.py`
- Test: `estrado-pjud-service/tests/test_familia_routes.py`

**Interfaces:**
- Todos pasan `Sequence[CookieRecord]` al adapter/cliente Familia.
- Los snapshots posteriores a initialize se persisten sin aplanar.

- [ ] **Step 1: RED end-to-end local**

Agregar un jar homónimo distinto en API y worker y comprobar persistencia/reload. Familia debe enviar ambos scopes mediante su CookieJar real.

- [ ] **Step 2: GREEN consumidores**

Actualizar stores/configuración y reutilizar `cookie_jar_from_records` en Familia.

- [ ] **Step 3: Verificar integración**

Run: `uv run pytest -q tests/test_api_on_demand_mint.py tests/test_session_pool_proxy.py tests/test_integration_proxy_pool.py tests/test_familia_pool.py tests/test_familia_routes.py`

---

### Task 4: Validación one-shot fuera de horario

**Files:**
- Modify: `estrado-pjud-service/worker/config.py`
- Modify: `estrado-pjud-service/worker/scheduler.py`
- Modify: `estrado-pjud-service/worker/__main__.py`
- Test: `estrado-pjud-service/tests/test_worker_config.py`
- Test: `estrado-pjud-service/tests/test_scheduler.py`
- Test: `estrado-pjud-service/tests/test_worker_main.py`
- Test: `estrado-pjud-service/tests/test_worker_parallel.py`

**Interfaces:**
- `WorkerConfig.PJUD_OFF_HOURS_VALIDATION_ONCE: bool = False`.
- `Scheduler.get_next_batch` usa límite efectivo 1 y permite off-hours sólo en ese modo.
- Main termina después del primer lote o del fallo de inicialización en validation mode.

- [ ] **Step 1: RED límites**

Probar default off-hours bloqueado, validation permite claim `p_limit=1`, `process_batch` permite ese lote y main no espera/repite luego del resultado.

- [ ] **Step 2: GREEN one-shot**

Pasar un único predicado de ventana desde config a scheduler y batch; cortar main tras la primera vuelta. No alterar defaults.

- [ ] **Step 3: Verificar worker**

Run: `uv run pytest -q tests/test_worker_config.py tests/test_scheduler.py tests/test_worker_main.py tests/test_worker_parallel.py`

---

### Task 5: Revisión, entrega y prueba real

**Files:**
- No additional product files expected.

- [ ] **Step 1: Gates completos**

Run:

```bash
uv run pytest -q
uv run python -m compileall -q app worker
git diff --check
```

- [ ] **Step 2: Dos revisiones exact-head**

Revisar seguridad del store, compatibilidad, selección HTTP, cleanup, límites de tráfico y modo one-shot. Corregir Critical/High/Medium y repetir gates/re-review.

- [ ] **Step 3: PR/merge/deploy**

Refrescar exact head/base/mergeability, mergear sólo SHA revisado y ejecutar deploy. Confirmar SHA, servicios, health y control proxy.

- [ ] **Step 4: Prueba transitoria**

Ejecutar desde una sola sesión root del VPS. El trap restaura el worker permanente
ante éxito o error; la unidad transitoria replica las propiedades necesarias y no
tiene `Restart`:

```bash
restore_worker() {
  systemctl enable --now estrado-pjud-worker.service
}
trap restore_worker EXIT INT TERM
systemctl disable --now estrado-pjud-worker.service
systemd-run --unit=estrado-pjud-validation-once --wait --collect --service-type=exec \
  --property=User=estrado --property=Group=estrado \
  --property=WorkingDirectory=/opt/legal-tech-microservices/estrado-pjud-service \
  --property=EnvironmentFile=/opt/legal-tech-microservices/estrado-pjud-service/.env \
  --property=StateDirectory=estrado-pjud --property=StateDirectoryMode=0770 \
  --property=NoNewPrivileges=true --property=ProtectSystem=strict \
  --property=ProtectHome=true --property=PrivateTmp=true \
  --property=ReadWritePaths=/opt/legal-tech-microservices/estrado-pjud-service/logs \
  --setenv=PYTHONUNBUFFERED=1 --setenv=PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
  --setenv=HOME=/tmp --setenv=PJUD_OFF_HOURS_VALIDATION_ONCE=true \
  /usr/bin/xvfb-run -a \
  /opt/legal-tech-microservices/estrado-pjud-service/.venv/bin/python -m worker
if systemctl is-active --quiet estrado-pjud-validation-once.service; then
  echo "La unidad transitoria no terminó" >&2
  exit 1
fi
trap - EXIT INT TERM
restore_worker
```

Antes de ejecutarlo, confirmar que el checkout está en el SHA mergeado y que el
control proxy está `enabled`. Inspeccionar sólo agregados seguros. Si el minteo o
la corrida falla, no repetir: conservar la única ventana redactada y restaurar el
worker normal.

- [ ] **Step 5: Auditoría final**

Exigir: al menos una corrida `scheduled_sync` exitosa, `next_sync_at` avanzado, `ambiguous_cookie_scope=0`, documentos=0, costo/reintentos agregados, proxy enabled, unidad transitoria ausente, worker normal `idle_off_hours` y auditoría documental 0/0/0.
