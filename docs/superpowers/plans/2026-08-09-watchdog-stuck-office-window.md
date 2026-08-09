# Watchdog Stuck Office Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminar las alertas falsas y repetitivas de causas `stuck` fuera del horario automático PJUD sin silenciar el resto del watchdog ni las sincronizaciones manuales.

**Architecture:** `estrado-watchdog.sh` tendrá un reloj UTC inyectable y una función pura de ventana chilena que gatea solamente el chequeo 8. La consulta `stuck` reflejará los filtros del RPC de claim y el modo dry-run usará estado efímero por defecto; el harness fijará tiempo y conteo para pruebas deterministas.

**Tech Stack:** Bash 5/GNU coreutils en VPS, PostgREST/Supabase, systemd, cron, shell test harness.

## Global Constraints

- Worker automático: lunes a viernes, 08:00–17:59 `America/Santiago`.
- `stuck`: lunes a viernes, 10:00–17:59 `America/Santiago`.
- Todos los demás chequeos permanecen 24/7.
- Sincronización manual permanece 24/7.
- Sólo causas `active`, `pjud_ojv`, prioridad nula o `<=3`, sin bloqueo futuro y vencidas por más de dos horas.
- `WD_NOW_EPOCH` debe ser un entero UTC no negativo y alimentar ventana y cutoff.
- `DRY_RUN=1` sin `WD_STATE_DIR` no puede modificar el cooldown real.
- No migraciones ni mutaciones de datos.

---

### Task 1: Gate horario, elegibilidad y dry-run seguro

**Files:**
- Modify: `ops/cron/tests/test-watchdog.sh`
- Modify: `ops/cron/estrado-watchdog.sh`
- Modify: `ops/cron/README.md`

**Interfaces:**
- Consumes: `WD_NOW_EPOCH?: string`, `WD_STATE_DIR?: path`, `WD_DEFAULT_STATE_DIR?: path`, `DRY_RUN=0|1`.
- Produces: `stuck_window_open() -> exit status`, `WATCHDOG_NOW_EPOCH`, `STUCK_CUTOFF` y una consulta PostgREST elegible.
- Test-only: `WD_STUCK_COUNT` se acepta únicamente cuando `DRY_RUN=1`; fuera de dry-run se ignora.

- [ ] **Step 1: Hacer determinista el helper y escribir regresiones RED**

Modificar `run()` para pasar una hora hábil fija y un conteo simulado:

```bash
WD_NOW_EPOCH="${WD_NOW_EPOCH:-$(date -u -d '2026-08-10T14:00:00Z' +%s)}" \
WD_STUCK_COUNT="${WD_STUCK_COUNT:-39}" \
bash "$WD" 2>/dev/null
```

Agregar pruebas para sábado, lunes 09:59, lunes 10:00, viernes 17:59 y viernes 18:00. Fuera de horario con health roto, afirmar ausencia de `scheduler no las está tomando` y presencia de `api-health`.

Agregar una prueba estructural que exija en el bloque `STUCK` los literales `source_system=eq.pjud_ojv`, `tracking_status=eq.active`, `sync_priority.is.null`, `sync_priority.lte.3`, `sync_blocked_until.is.null` y `sync_blocked_until.lt.`.

Agregar `WD_NOW_EPOCH=invalido` esperando exit no-cero y `WD_NOW_EPOCH inválido`. Agregar dry-run sin `WD_STATE_DIR`: preparar `WD_DEFAULT_STATE_DIR` con marcador, ejecutar con los fixtures existentes y comprobar que su checksum no cambia. Mantener las pruebas de cooldown con `WDS` explícito.

- [ ] **Step 2: Ejecutar RED en el VPS**

```bash
scp ops/cron/estrado-watchdog.sh ops/cron/tests/test-watchdog.sh legaltech-vps:/tmp/
ssh legaltech-vps 'chmod +x /tmp/test-watchdog.sh && /tmp/test-watchdog.sh /tmp/estrado-watchdog.sh'
```

Expected: fallan las nuevas pruebas porque aún no existen gate, filtros, validación ni estado efímero.

- [ ] **Step 3: Implementar reloj y estado dry-run**

Antes de construir `NOW`:

```bash
WATCHDOG_NOW_EPOCH="${WD_NOW_EPOCH:-$(date -u +%s)}"
case "$WATCHDOG_NOW_EPOCH" in
  ''|*[!0-9]*) echo "WD_NOW_EPOCH inválido: debe ser un epoch UTC entero no negativo." >&2; exit 2 ;;
esac
NOW=$(date -u -d "@$WATCHDOG_NOW_EPOCH" +%Y-%m-%dT%H:%M:%S)
```

Antes de definir `STATE`:

```bash
if [ "${DRY_RUN:-0}" = "1" ] && [ -z "${WD_STATE_DIR+x}" ]; then
  WD_DRY_STATE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/estrado-watchdog-dry-run.XXXXXX")
  trap 'rm -rf "$WD_DRY_STATE_DIR"' EXIT
  WD_STATE_DIR="$WD_DRY_STATE_DIR"
else
  WD_STATE_DIR="${WD_STATE_DIR:-${WD_DEFAULT_STATE_DIR:-/var/tmp}}"
fi
```

- [ ] **Step 4: Implementar gate y consulta elegible**

```bash
stuck_window_open() {
  local dow hour
  dow=$(TZ=America/Santiago date -d "@$WATCHDOG_NOW_EPOCH" +%u)
  hour=$(TZ=America/Santiago date -d "@$WATCHDOG_NOW_EPOCH" +%H)
  [ "$dow" -le 5 ] && [ "$hour" -ge 10 ] && [ "$hour" -lt 18 ]
}
```

En el chequeo 8:

```bash
STUCK_CUTOFF=$(date -u -d "@$((WATCHDOG_NOW_EPOCH - 7200))" +%Y-%m-%dT%H:%M:%S)
STUCK_FILTER="cases?select=id&tracking_status=eq.active&source_system=eq.pjud_ojv&next_sync_at=lt.$STUCK_CUTOFF&and=(or(sync_priority.is.null,sync_priority.lte.3),or(sync_blocked_until.is.null,sync_blocked_until.lt.$NOW))"
```

Evaluar sólo si `stuck_window_open`. Usar `WD_STUCK_COUNT` únicamente bajo `DRY_RUN=1`; en otra ejecución llamar `cnt "$STUCK_FILTER"`.

- [ ] **Step 5: Ejecutar GREEN y sintaxis**

```bash
scp ops/cron/estrado-watchdog.sh ops/cron/tests/test-watchdog.sh legaltech-vps:/tmp/
ssh legaltech-vps 'chmod +x /tmp/test-watchdog.sh && /tmp/test-watchdog.sh /tmp/estrado-watchdog.sh'
bash -n ops/cron/estrado-watchdog.sh ops/cron/tests/test-watchdog.sh
git diff --check
```

Expected: suite watchdog verde; sintaxis y diff limpios.

- [ ] **Step 6: Documentar y commit**

Actualizar `ops/cron/README.md` con ventana `stuck`, `WD_NOW_EPOCH` y dry-run efímero.

```bash
git add ops/cron/estrado-watchdog.sh ops/cron/tests/test-watchdog.sh ops/cron/README.md
git commit -m "fix(watchdog): respect PJUD processing window"
```

### Task 2: Verificación, merge y despliegue

**Files:**
- Verify: `ops/cron/estrado-watchdog.sh`
- Verify: `ops/cron/tests/test-watchdog.sh`
- Verify: `ops/cron/crontab.snapshot`

**Interfaces:**
- Consumes: commit de Task 1 y VPS `legaltech-vps`.
- Produces: PR mergeado, cron exacto desplegado y `stuck` silencioso fuera de horario.

- [ ] **Step 1: Ejecutar suite completa**

```bash
cd estrado-pjud-service
uv sync --frozen --extra dev
.venv/bin/python -m pytest -q
```

Expected: `1035 passed`.

- [ ] **Step 2: Revisión independiente**

Comparar contra `origin/main`, confirmar que no cambia crontab ni horario del worker, y revisar horario, filtros, anti-spam y efectos laterales.

- [ ] **Step 3: Push, PR y gates exactos**

```bash
git push -u origin feature/pjud-watchdog-office-window
gh pr create --base main --head feature/pjud-watchdog-office-window --body-file .pr-body.md
```

Esperar checks y refrescar `headRefOid`, mergeabilidad, comentarios y checks antes de mergear con `--match-head-commit`.

- [ ] **Step 4: Desplegar main y cron**

```bash
ssh legaltech-vps '/opt/legal-tech-microservices/ops/deploy.sh'
ops/cron/deploy-cron.sh legaltech-vps
```

No reinstalar `crontab.snapshot` salvo drift: este cambio no debe modificarlo.

- [ ] **Step 5: Verificación operacional**

Fuera de horario:

```bash
ssh legaltech-vps 'DRY_RUN=1 /opt/estrado-cron/estrado-watchdog.sh'
```

Expected: no aparece `scheduler no las está tomando`; otras anomalías siguen visibles.

Dentro de horario simulado, sin Telegram:

```bash
ssh legaltech-vps 'WD_NOW_EPOCH=$(date -u -d "2026-08-10T14:00:00Z" +%s) WD_STUCK_COUNT=39 DRY_RUN=1 /opt/estrado-cron/estrado-watchdog.sh'
```

Expected: aparece `39 causa(s)` y `stuck`; el cooldown real no cambia. Verificar también API/worker activos, cero reinicios, heartbeat fresco y journal sin errores nuevos.
