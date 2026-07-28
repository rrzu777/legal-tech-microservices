# A0 — Semántica de `sync_attempts` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que un sync exitoso resetee `sync_attempts` a 0, para que las causas sanas dejen de acumular intentos y de quedar a un error de la suspensión permanente.

**Architecture:** Una sola línea en `worker/engine.py` alinea el path de éxito PJUD con el otro path de éxito que ya resetea correctamente. Después, un backfill manual limpia los contadores inflados que dejó el bug. El fix de código va primero: al revés, el bug vuelve a inflar las filas recién limpiadas.

**Tech Stack:** Python 3.12, pytest (`asyncio_mode = "auto"`), Supabase/postgrest vía REST.

**Spec:** `docs/superpowers/specs/2026-07-26-pool-salud-observabilidad-design.md` (sección A0)

**Rama:** `fix/pool-salud-observabilidad`

---

## Contexto del bug

`sync_attempts` tiene hoy dos semánticas contradictorias:

```python
# worker/engine.py:309 — path de éxito PJUD (el principal)
"sync_attempts": (case.get("sync_attempts") or 0) + 1,   # INCREMENTA al tener éxito

# worker/engine.py:511 — el otro path de éxito
"sync_attempts": 0,                                       # resetea (correcto)

# worker/engine.py:847 — _update_case_error
_MAX_SYNC_ATTEMPTS = 10
if sync_attempts >= _MAX_SYNC_ATTEMPTS:  # lo lee como fallos consecutivos
    tracking_status = "suspended"        # terminal: ningún cron lo recupera
```

Medido en producción el 26 jul: las 12 causas activas sanas tienen `sync_attempts >= 10`
(una en 82). Cualquier error no-infra las suspende de inmediato, sin backoff.

Los errores de proxy **no** disparan esto (van por `_handle_blocked`, que no llama a
`_update_case_error`). El gatillo son los errores de causa.

## Estructura de archivos

- **Modificar:** `worker/engine.py:309` — una línea.
- **Modificar:** `tests/test_engine.py` — un test nuevo en `TestSyncEngine`.
- **Sin archivos nuevos.** El backfill es un paso manual documentado, no código versionado:
  se ejecuta una vez y no tiene por qué vivir en el repo.

---

### Task 1: El sync exitoso resetea `sync_attempts`

**Files:**
- Modify: `worker/engine.py:309`
- Test: `tests/test_engine.py`

- [ ] **Step 1: Escribir el test que falla**

Agregar dentro de la clase `TestSyncEngine` en `tests/test_engine.py`, junto a los otros
tests de sync exitoso:

```python
    @pytest.mark.asyncio
    async def test_sync_success_resets_sync_attempts(self):
        """Un sync exitoso debe resetear sync_attempts a 0, no incrementarlo.

        Regresión: el path de éxito lo incrementaba, así que las causas sanas
        acumulaban intentos hasta cruzar _MAX_SYNC_ATTEMPTS y quedar a un solo
        error de la suspensión permanente.
        """
        engine, mock_pool, mock_sb, mock_notifier, mock_metrics, mock_backoff = _make_engine()

        case = _make_case(sync_attempts=20)

        with patch("worker.engine.search_pjud_via_session", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detail_pjud_via_session", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True

        update_calls = mock_sb.from_.return_value.update.call_args_list
        success_update = None
        for call in update_calls:
            args = call[0] if call[0] else ()
            payload = args[0] if args else {}
            if payload and payload.get("last_sync_status") == "success":
                success_update = payload
                break

        assert success_update is not None, "Se esperaba un update con last_sync_status='success'"
        assert success_update["sync_attempts"] == 0, (
            f"Un sync exitoso debe resetear sync_attempts a 0, "
            f"pero quedó en {success_update['sync_attempts']}"
        )
```

Los targets de patch (`worker.engine.search_pjud_via_session` y
`worker.engine.detail_pjud_via_session`) y los helpers `_make_case` / `_make_engine` /
`_mock_search_response` / `_mock_detail_response` ya existen en `tests/test_engine.py`;
son los mismos que usa `test_sync_success_full_flow` (línea 127). No hay que crear nada.

- [ ] **Step 2: Correr el test para verificar que falla**

```bash
cd ~/Projects/legal-tech-microservices/estrado-pjud-service && .venv/bin/pytest tests/test_engine.py::TestSyncEngine::test_sync_success_resets_sync_attempts -v
```

Esperado: **FAIL** con `assert 21 == 0` — el payload trae `20 + 1`.

Si en cambio falla por el patch o por un `KeyError`, arreglá el mock siguiendo
`test_sync_success_full_flow` y volvé a correr hasta que falle por el assert de
`sync_attempts`. Un test que falla por la razón equivocada no prueba nada.

- [ ] **Step 3: Aplicar el fix**

En `worker/engine.py`, línea 309, dentro del update de éxito:

```python
                    "sync_attempts": 0,
```

Reemplaza a:

```python
                    "sync_attempts": (case.get("sync_attempts") or 0) + 1,
```

Agregar arriba del bloque un comentario que fije la semántica, para que nadie lo revierta:

```python
                    # sync_attempts = fallos CONSECUTIVOS desde el último éxito
                    # (lo lee _update_case_error para decidir backoff vs suspensión).
                    # No es un contador de sincronizaciones totales: incrementarlo acá
                    # suspendía causas sanas tras 10 éxitos.
```

- [ ] **Step 4: Correr el test para verificar que pasa**

```bash
cd ~/Projects/legal-tech-microservices/estrado-pjud-service && .venv/bin/pytest tests/test_engine.py::TestSyncEngine::test_sync_success_resets_sync_attempts -v
```

Esperado: **PASS**

- [ ] **Step 5: Correr la suite completa**

```bash
cd ~/Projects/legal-tech-microservices/estrado-pjud-service && .venv/bin/pytest tests/ -q
```

Esperado: todo verde. Prestá atención especial a
`test_sync_error_suspended_after_max_attempts` (cerca de la línea 905): construye su caso
con `sync_attempts=10` explícito y fuerza un fallo, así que **debe seguir pasando** — el fix
no toca el umbral de suspensión, solo deja de inflar el contador en el camino del éxito.

Si algún test se rompe porque asumía el incremento, ese test estaba codificando el bug:
actualizalo y dejá constancia en el mensaje de commit.

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/legal-tech-microservices && git add estrado-pjud-service/worker/engine.py estrado-pjud-service/tests/test_engine.py && git commit -m "fix(worker): sync_attempts se resetea en éxito, no incrementa

El path de éxito PJUD incrementaba sync_attempts, mientras _update_case_error
lo lee como fallos consecutivos para decidir backoff vs suspensión. Cada sync
exitoso acercaba la causa a su propia suspensión permanente.

Medido en prod: las 12 causas activas sanas tenían sync_attempts >= 10 (una en
82), o sea a un error de la suspensión, que además es terminal (ningún cron la
recupera).

Alinea el path principal con el otro path de éxito, que ya reseteaba a 0."
```

---

### Task 2: Deploy al VPS

**Sin archivos.** Producción viva: el worker se reinicia dentro de una ventana acordada con
el usuario, nunca de forma unilateral.

- [ ] **Step 1: Confirmar la ventana de deploy con el usuario**

No sigas sin un OK explícito. El worker corre bajo `xvfb` y reiniciarlo corta las sesiones
OJV en vuelo (invariante cookie↔IP↔token): un reinicio a destiempo invalida los 3 slots.

- [ ] **Step 2: Desplegar y reiniciar**

```bash
ssh legaltech-vps 'cd /opt/legal-tech-microservices && git pull && systemctl restart estrado-pjud-worker.service && sleep 10 && systemctl is-active estrado-pjud-worker.service'
```

Esperado: `active`

- [ ] **Step 3: Verificar que el worker levantó los 3 slots**

```bash
ssh legaltech-vps 'journalctl -u estrado-pjud-worker --since "-3 min" -o cat | grep -E "Slot [0-9] (minteado|initialized)"'
```

Esperado: `Slot 0/1/2 initialized`. Si un slot falla al mintear, el ~12% de fallo del proxy
puede ser la causa (ver track A1) — reintentá la verificación antes de asumir que el deploy
salió mal.

---

### Task 3: Backfill de los contadores inflados

**Sin archivos.** Es una escritura en producción, ya autorizada por el usuario en el spec.

**Precondición dura:** Task 2 completa. Si backfilleás antes del deploy, el bug vuelve a
inflar las filas en el siguiente ciclo de sync y el trabajo se pierde.

- [ ] **Step 1: Dry-run — contar las filas afectadas**

```bash
ssh legaltech-vps 'set -a; . /opt/legal-tech-microservices/estrado-pjud-service/.env; set +a; curl -s -m 25 "${SUPABASE_URL%/}/rest/v1/cases?select=case_number,sync_attempts,tracking_status&last_sync_status=eq.success&sync_attempts=gt.0" -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY"'
```

Esperado: ~14 filas, todas con `last_sync_status=success`. Medido el 26 jul: 14.

**Gate:** si devuelve muchas más de 14, pará y avisá al usuario antes de escribir. Una
desviación grande significa que el estado cambió desde el diagnóstico y el alcance del
backfill ya no es el que se autorizó.

- [ ] **Step 2: Aplicar el backfill**

```bash
ssh legaltech-vps 'set -a; . /opt/legal-tech-microservices/estrado-pjud-service/.env; set +a; curl -s -X PATCH -m 30 "${SUPABASE_URL%/}/rest/v1/cases?last_sync_status=eq.success&sync_attempts=gt.0" -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" -H "Content-Type: application/json" -H "Prefer: return=representation" -d "{\"sync_attempts\": 0}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"filas actualizadas: {len(d)}\")"'
```

Esperado: `filas actualizadas: 14`

El filtro toca **solo** causas cuyo último sync fue exitoso. Deliberadamente no incluye las
suspendidas ni las que están en error: esas conservan su contador porque su backoff es
legítimo. Las dos suspendidas por bugs se recuperan en el track A2, no acá.

- [ ] **Step 3: Verificar**

```bash
ssh legaltech-vps 'set -a; . /opt/legal-tech-microservices/estrado-pjud-service/.env; set +a; curl -s -m 25 "${SUPABASE_URL%/}/rest/v1/cases?select=case_number,sync_attempts,last_sync_status&sync_attempts=gte.10" -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY"'
```

Esperado: solo causas `suspended` o en `error`. **Ninguna** con `last_sync_status=success`.

- [ ] **Step 4: Verificar que el fix se sostiene tras un ciclo de sync**

Esperar a que corra un ciclo (la cadencia es por prioridad; con esperar ~1h alcanza para las
de prioridad alta) y volver a correr el comando del Step 3.

Esperado: sigue sin aparecer ninguna causa sana con contador alto. Si reaparecen, el deploy
del Task 2 no tomó efecto — verificá que el `git pull` trajo el commit y que el worker
reinició de verdad.

---

## Criterio de completitud

- [ ] `test_sync_success_resets_sync_attempts` pasa.
- [ ] La suite completa pasa, incluido `test_sync_error_suspended_after_max_attempts`.
- [ ] El worker está `active` en el VPS con los 3 slots minteados.
- [ ] Ninguna causa con `last_sync_status=success` tiene `sync_attempts > 0`.
- [ ] La verificación post-ciclo confirma que los contadores no vuelven a inflarse.

## Qué NO entra en este plan

- **Retry de minteo (A1), bugs de parser (A2), métricas (B2):** PR aparte.
- **Reactivar `T-100-2024` y `C-1000-2024`:** va en A2, después de arreglar los bugs que las
  rompieron. Reactivarlas antes solo las haría fallar de nuevo.
- **Archivar `PROTECCION-23483-2025`:** decisión del usuario ya tomada, se ejecuta con A2.
- **Hacer visible el estado `suspended`:** es el track C, en el repo `LegalTech`.
