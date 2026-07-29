# B1 + B1b — Watchdog: salud de los crons de la app, chequeos muertos y versionado

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** que el watchdog del VPS deje de mentir — que vigile los crons de la app (hoy no los mira), que sus dos chequeos muertos vuelvan a servir, y que el script viva en git en vez de existir solo en `/opt`.

**Architecture:** los scripts de `/opt/estrado-cron/` se importan **verbatim** a `ops/cron/` en un primer commit sin cambios de comportamiento (para que el diff de todo lo que sigue sea legible), y a partir de ahí cada chequeo es un commit. El watchdog gana tres variables inyectables (`DRY_RUN`, `CRON_LOG`, `WD_STATE_DIR`) que lo vuelven testeable sin mandar nada a Telegram ni gastar tokens de Luna. Los tests corren **en el VPS** contra una copia en `/tmp` — es el único lugar donde existen `systemctl`, el journal y el `.env`. Recién cuando pasan, `deploy-cron.sh` instala en `/opt` con backup previo.

**Tech Stack:** bash (`set -uo pipefail`), gawk, curl contra PostgREST, journalctl, logrotate, cron de root.

---

## Contexto: por qué existe este plan

Tres agujeros distintos, todos verificados con evidencia el 29 jul 2026.

**1. Los crons de la app estuvieron caídos 120 días y nada avisó.**
`run-cron.sh` apuntaba a `legal-tech-flax.vercel.app`, un dominio viejo que devuelve 404.
Evidencia en `/var/log/estrado-cron.log`:

```
último 200 antes del apagón:   2026-03-31 12:00 /api/cron/trial-emails - HTTP 200
primer 404 sostenido:          2026-04-01 11:00 /api/cron/task-reminders - HTTP 404
1183 líneas 404 consecutivas hasta 2026-07-29 05:41
```

La causa raíz ya está arreglada (`APP_URL="https://juristrack.cl"`, editado en el VPS el 29 jul
05:21, con `.bak-20260729` al lado) y desde las 05:41 todo vuelve a dar 200. **Lo que sigue roto
es que nadie vigila ese log.** El watchdog mira systemd y el sync PJUD; el log de crons no lo
abre nunca.

`vercel.json` solo tiene dos crons de respaldo (`task-reminders` y `pjud-sync`). O sea que
durante 4 meses **no corrió ninguna vez**: `deadline-reminders`, `event-reminders`,
`trial-emails`, `product-metrics`, `stale-sync-alert`, `stale-sync-recovery`,
`purge-deleted-firms`.

Detalle que conecta con el resto del proyecto: **`stale-sync-alert` es justamente la alerta de
"causa sin sincronizar hace >48h"**. Los 17 días de causas caídas en silencio que motivaron todo
este track tenían dos causas independientes, no una: el filtro de `suspended` en la app (cerrado
en el PR #34) **y** este 404. Arreglar solo la primera habría dejado la mitad del agujero abierto.

**2. El chequeo #3 nunca matcheó una fila.** Consulta
`last_sync_status=eq.error&tracking_status=eq.active`, y esa combinación no existe: cuando una
causa falla, el worker le mueve el `tracking_status`. Medido hoy, con 3 causas genuinamente rotas
en la base:

```
last_sync_status=error AND tracking_status=active  -> content-range: */0
last_sync_status=error (solo)                      -> content-range: 0-2/3
tracking_status=suspended                          -> content-range: 0-2/3
```

**3. El chequeo #5 nunca matcheó una línea.** Usa `journalctl -p err`, pero el worker sale por
stdout vía `xvfb-run`, así que todo entra como `PRIORITY=6` (info). Devuelve **0 líneas en 7
días** habiendo 40 errores reales.

---

## File Structure

| Archivo | Responsabilidad |
|---|---|
| `ops/cron/estrado-watchdog.sh` | El watchdog. Único archivo con lógica de chequeos. |
| `ops/cron/run-cron.sh` | Llama a un endpoint de cron de la app y lo loguea. Sin secretos adentro. |
| `ops/cron/estrado-digest.sh` | Digest diario (se importa verbatim, no se toca en este plan). |
| `ops/cron/hermes-backup.sh` | Backup de Hermes (se importa verbatim, no se toca). |
| `ops/cron/logrotate/estrado-cron` | Rotación de `/var/log/estrado-cron.log`. |
| `ops/cron/deploy-cron.sh` | Instala en el VPS con backup previo y modo correcto. |
| `ops/cron/tests/test-watchdog.sh` | Corre EN EL VPS. Fabrica logs y verifica cada chequeo. |
| `ops/cron/README.md` | Cómo se despliega y por qué los scripts viven acá. |

---

## Task 1: Importar los scripts a `ops/cron/` verbatim

Sin un commit-baseline idéntico a producción, el diff de las tareas siguientes es ilegible y no
hay forma de probar que no cambiamos algo sin querer.

**Files:**
- Create: `ops/cron/estrado-watchdog.sh`, `ops/cron/estrado-digest.sh`, `ops/cron/hermes-backup.sh`, `ops/cron/run-cron.sh`

- [ ] **Step 1: Traer los 4 archivos tal cual del VPS**

```bash
mkdir -p ops/cron
for f in estrado-watchdog.sh estrado-digest.sh hermes-backup.sh run-cron.sh; do
  ssh legaltech-vps "cat /opt/estrado-cron/$f" > "ops/cron/$f"
  chmod +x "ops/cron/$f"
done
```

- [ ] **Step 2: Sacar el secreto de `run-cron.sh` ANTES de commitear**

`run-cron.sh` trae `CRON_SECRET` en texto plano. **No puede entrar a git así.** Reemplazá esas
dos líneas de configuración por lectura de un archivo de entorno:

```bash
# Config fuera del script: /etc/estrado-cron.env (modo 600 root) define
# APP_URL y CRON_SECRET. Vivían hardcodeados acá y por eso un cambio de dominio
# tumbó todos los crons 4 meses sin dejar rastro en ningún repo.
ENV_FILE="${ESTRADO_CRON_ENV:-/etc/estrado-cron.env}"
if [ ! -r "$ENV_FILE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M') ERROR: falta $ENV_FILE" >> "${CRON_LOG:-/var/log/estrado-cron.log}"
    exit 1
fi
set -a; . "$ENV_FILE"; set +a
: "${APP_URL:?APP_URL no definido en $ENV_FILE}"
: "${CRON_SECRET:?CRON_SECRET no definido en $ENV_FILE}"
```

El resto del script queda igual (el `curl`, el log, el `exit 1` si no es 200).

- [ ] **Step 3: Verificar que no quedó ningún secreto**

```bash
grep -rnE "(eyJ|[0-9a-f]{40,}|SECRET=\"?[A-Za-z0-9])" ops/cron/ && echo "!!! HAY SECRETO !!!" || echo "limpio"
```

Esperado: `limpio`. Si imprime algo, sacalo antes de seguir — un secreto commiteado no se
borra con un `git rm`.

- [ ] **Step 4: Escribir `ops/cron/README.md`**

```markdown
# ops/cron

Scripts que corren en el crontab de **root** del VPS `legaltech-vps`, en `/opt/estrado-cron/`.

Hasta julio 2026 existían **solo** ahí: no estaban en ningún repo, no tenían historia y no había
forma de revisar un cambio. Un dominio hardcodeado en `run-cron.sh` tuvo todos los crons de la
app devolviendo 404 durante 120 días sin que nadie se enterara. Por eso viven acá ahora.

## Desplegar

    ./ops/cron/deploy-cron.sh            # a legaltech-vps
    ./ops/cron/deploy-cron.sh otro-host

Hace backup de lo que haya en `/opt/estrado-cron/` antes de pisar nada.

## Configuración con secretos

`run-cron.sh` lee `APP_URL` y `CRON_SECRET` de `/etc/estrado-cron.env` (modo 600 root). Ese
archivo **no** está en el repo y `deploy-cron.sh` no lo toca si ya existe.

## Tests

`tests/test-watchdog.sh` corre **en el VPS** (necesita systemd, journal y el `.env` del
microservicio). Ver la cabecera del archivo.

## Lo que NO está acá

El crontab de root en sí. `crontab -l > ops/cron/crontab.snapshot` cuando cambie.
```

- [ ] **Step 5: Commit**

```bash
git add ops/cron
git commit -m "chore(ops): versionar los scripts de /opt/estrado-cron (baseline)"
```

---

## Task 2: Hacer el watchdog testeable (`DRY_RUN`, rutas inyectables)

Sin esto no hay forma de probar un chequeo sin mandarle un Telegram al usuario y quemar 120s de
Luna.

**Files:**
- Modify: `ops/cron/estrado-watchdog.sh`

- [ ] **Step 1: Parametrizar las rutas**

Reemplazá la línea del `.env` y la del `STATE` por versiones inyectables:

```bash
ENV="${WD_ENV:-/opt/legal-tech-microservices/estrado-pjud-service/.env}"
set -a; . "$ENV"; set +a
```

```bash
WD_STATE_DIR="${WD_STATE_DIR:-/var/tmp}"
STATE="$WD_STATE_DIR/estrado-wd-state"
COOLDOWN="${WD_COOLDOWN:-10800}"   # 3h: no re-alertar la MISMA anomalía dentro de esta ventana
```

- [ ] **Step 2: Cortar antes de Luna/Telegram cuando `DRY_RUN=1`**

Justo después del bloque anti-spam (después de `echo "$HASH $(date -u +%s)" > "$STATE"`) y antes
de `PROMPT_FILE=$(mktemp ...)`:

```bash
# Modo prueba: imprime lo que habría alertado y sale. No llama a Luna ni a Telegram.
if [ "${DRY_RUN:-0}" = "1" ]; then
  printf 'ANOMALIES:\n%sSIG: %s\n' "$ANOMALIES" "$SIG"
  exit 0
fi
```

- [ ] **Step 3: Probar que en el VPS, hoy, sale en silencio**

```bash
scp ops/cron/estrado-watchdog.sh legaltech-vps:/tmp/wd.sh
ssh legaltech-vps 'chmod +x /tmp/wd.sh && DRY_RUN=1 WD_STATE_DIR=/tmp /tmp/wd.sh; echo "exit=$?"'
```

Esperado: **ninguna salida** y `exit=0`. Si imprime algo, el `add` que se disparó es un falso
positivo preexistente — anotalo, no lo tapes.

- [ ] **Step 4: Probar que `DRY_RUN` corta de verdad**

```bash
ssh legaltech-vps 'DRY_RUN=1 WD_STATE_DIR=/tmp CRON_LOG=/dev/null /tmp/wd.sh'
```

Todavía no existe el chequeo #7, así que esto también sale vacío. Es esperado; el test real es
la Task 3.

- [ ] **Step 5: Commit**

```bash
git add ops/cron/estrado-watchdog.sh
git commit -m "feat(watchdog): DRY_RUN y rutas inyectables para poder testearlo"
```

---

## Task 3: Chequeo #7 — crons de la app fallando

**Files:**
- Modify: `ops/cron/estrado-watchdog.sh`

- [ ] **Step 1: Escribir el test primero**

Creá `ops/cron/tests/test-watchdog.sh`:

```bash
#!/bin/bash
# Tests del watchdog. CORRE EN EL VPS: necesita systemd, journal y el .env del
# microservicio (los chequeos 1-6 se apoyan en eso). En un laptop da falsos positivos.
#
#   scp ops/cron/estrado-watchdog.sh ops/cron/tests/test-watchdog.sh legaltech-vps:/tmp/
#   ssh legaltech-vps 'chmod +x /tmp/test-watchdog.sh && /tmp/test-watchdog.sh /tmp/estrado-watchdog.sh'
set -uo pipefail

WD="${1:-/tmp/estrado-watchdog.sh}"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0

run() { DRY_RUN=1 WD_STATE_DIR="$TMP" CRON_LOG="$1" bash "$WD" 2>/dev/null; }

expect_contains() { # <nombre> <salida> <texto esperado>
  if printf '%s' "$2" | grep -qF -- "$3"; then
    echo "  ok   $1"; PASS=$((PASS+1))
  else
    echo "  FAIL $1 — esperaba encontrar: $3"; echo "      salida: ${2:-<vacía>}"; FAIL=$((FAIL+1))
  fi
}

expect_missing() { # <nombre> <salida> <texto que NO debe estar>
  if printf '%s' "$2" | grep -qF -- "$3"; then
    echo "  FAIL $1 — no esperaba: $3"; echo "      salida: $2"; FAIL=$((FAIL+1))
  else
    echo "  ok   $1"; PASS=$((PASS+1))
  fi
}

# Fixtures con fechas LOCALES, que es como las escribe run-cron.sh.
HOY=$(date '+%Y-%m-%d %H:%M')
VIEJO=$(date -d '-3 days' '+%Y-%m-%d %H:%M')

echo "== chequeo 7: fallas =="
cat > "$TMP/fail.log" <<EOF
$HOY /api/cron/task-reminders - HTTP 404
$HOY /api/cron/event-reminders - HTTP 404
$HOY /api/cron/trial-emails - HTTP 200
EOF
OUT=$(run "$TMP/fail.log")
expect_contains "reporta el endpoint que falla" "$OUT" "/api/cron/task-reminders"
expect_contains "reporta el código"             "$OUT" "HTTP 404"
expect_contains "firma incluye el código"       "$OUT" "cron-fail:404"
expect_missing  "no reporta el que dio 200"     "$OUT" "trial-emails"

echo "== chequeo 7: silencio =="
printf '%s /api/cron/task-reminders - HTTP 200\n' "$VIEJO" > "$TMP/silent.log"
OUT=$(run "$TMP/silent.log")
expect_contains "detecta el silencio" "$OUT" "cron-silent"

echo "== chequeo 7: log sano =="
cat > "$TMP/ok.log" <<EOF
$VIEJO /api/cron/task-reminders - HTTP 404
$HOY /api/cron/task-reminders - HTTP 200
EOF
OUT=$(run "$TMP/ok.log")
expect_missing "ignora fallas fuera de la ventana de 24h" "$OUT" "cron-fail"
expect_missing "no grita silencio si hay líneas frescas"  "$OUT" "cron-silent"

echo "== chequeo 7: log ilegible =="
OUT=$(run "$TMP/no-existe.log")
expect_contains "avisa si no puede leer el log" "$OUT" "cron-log-missing"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 2: Correrlo y verlo fallar**

```bash
scp ops/cron/estrado-watchdog.sh ops/cron/tests/test-watchdog.sh legaltech-vps:/tmp/
ssh legaltech-vps 'chmod +x /tmp/test-watchdog.sh && /tmp/test-watchdog.sh /tmp/estrado-watchdog.sh'
```

Esperado: fallan los 4 `expect_contains` del chequeo #7 (todavía no existe). Los
`expect_missing` pasan por accidente — es normal, los cubre el Step 4.

- [ ] **Step 3: Implementar el chequeo #7**

Va después del bloque `# 6. Salud del propio Hermes`, antes de `# Sano → salir en silencio`:

```bash
# 7. Salud de los crons de la app (los que `run-cron.sh` le pega a Vercel).
#    Nadie miraba este log: `run-cron.sh` apuntó a un dominio viejo del 2026-04-01
#    al 2026-07-29 y las 1183 corridas que devolvieron 404 pasaron inadvertidas.
#
#    ZONA HORARIA: run-cron.sh escribe con `date` LOCAL (el VPS es Europe/Berlin),
#    mientras que NOW de acá arriba es UTC. Comparar una contra otra da mal cerca de
#    medianoche. Acá el corte se calcula con `date` LOCAL y, como el formato es
#    YYYY-MM-DD HH:MM, el orden lexicográfico ES el cronológico: alcanza un awk,
#    sin mktime (que en mawk no existe) y sin un fork de `date` por línea.
CRON_LOG="${CRON_LOG:-/var/log/estrado-cron.log}"
if [ ! -r "$CRON_LOG" ]; then
  add "No puedo leer $CRON_LOG — los crons de la app quedan sin vigilancia." "cron-log-missing"
else
  CRON_CUTOFF=$(date -d '-24 hours' '+%Y-%m-%d %H:%M')
  CRON_RECENT=$(awk -v c="$CRON_CUTOFF" 'substr($0,1,16) >= c' "$CRON_LOG" 2>/dev/null || true)
  # Se mira la ULTIMA corrida de cada endpoint, no todas: la pregunta es "¿esto está
  # roto AHORA?". Con cooldown de 3h y ventana de 24h, un 404 suelto ya recuperado
  # alertaría 8 veces. Un endpoint roto de verdad falla también en su última corrida.
  CRON_BAD=$(printf '%s\n' "$CRON_RECENT" | grep -F ' - HTTP ' \
              | awk '{ultimo[$3] = $NF} END {for (e in ultimo) if (ultimo[e] != 200) print e, ultimo[e]}' \
              | sort || true)
  if [ -n "${CRON_BAD// }" ]; then
    CRON_DETAIL=$(printf '%s\n' "$CRON_BAD" | awk '{printf "    %s -> HTTP %s\n", $1, $2}' || true)
    # La firma lleva los códigos: 404 en todo (APP_URL roto) y 401 (CRON_SECRET
    # desincronizado con Vercel) son incidentes distintos, y si el segundo aparece
    # mientras el primero está en cooldown NO puede quedar tapado.
    CRON_CODES=$(printf '%s\n' "$CRON_BAD" | awk '{print $2}' | sort -u | paste -sd, - || true)
    add "Crons de la app fallando (últimas 24h):"$'\n'"$CRON_DETAIL""    404 en todos = APP_URL roto; 401 = CRON_SECRET desincronizado con Vercel; 000 = no hubo conexión." "cron-fail:$CRON_CODES"
  fi
fi
```

- [ ] **Step 4: Correr el test y verlo pasar**

```bash
scp ops/cron/estrado-watchdog.sh legaltech-vps:/tmp/
ssh legaltech-vps '/tmp/test-watchdog.sh /tmp/estrado-watchdog.sh'
```

Esperado: `PASS=6 FAIL=2` — pasa todo lo de fallas y de log ilegible; siguen fallando los dos
del silencio, que es la Task 4.

- [ ] **Step 5: Verificar contra el log REAL de hoy**

```bash
ssh legaltech-vps 'DRY_RUN=1 WD_STATE_DIR=/tmp /tmp/estrado-watchdog.sh; echo "exit=$?"'
```

Esperado: sin salida, `exit=0`. Hoy las 12 corridas dieron 200. Si aparece `cron-fail`, hay un
cron fallando de verdad **ahora mismo** — pará y avisá.

- [ ] **Step 6: Commit**

```bash
git add ops/cron/estrado-watchdog.sh ops/cron/tests/test-watchdog.sh
git commit -m "feat(watchdog): chequeo 7 — crons de la app devolviendo != 200"
```

---

## Task 4: Chequeo #7 — silencio total

El modo de falla que un chequeo de "¿hay errores?" **no** atrapa: si el crontab se borra o
`run-cron.sh` pierde el `+x`, el log deja de crecer y todo "se ve sano".

**Files:**
- Modify: `ops/cron/estrado-watchdog.sh`

- [ ] **Step 1: El test ya está escrito** (bloque `== chequeo 7: silencio ==` de la Task 3).
Confirmá que sigue fallando:

```bash
ssh legaltech-vps '/tmp/test-watchdog.sh /tmp/estrado-watchdog.sh' | grep -A1 silencio
```

- [ ] **Step 2: Implementarlo**

Dentro del `else` del chequeo #7, envolvé lo de la Task 3 y agregá la rama del silencio:

```bash
  if [ -z "${CRON_RECENT// }" ]; then
    # El piso normal son ~10 corridas por día. Cero en 24h no es "poca carga":
    # es el crontab borrado, run-cron.sh sin permiso de ejecución o el disco lleno.
    add "Ningún cron de la app corrió en las últimas 24h (el piso normal son ~10). Revisar el crontab de root y los permisos de /opt/estrado-cron/run-cron.sh." "cron-silent"
  else
    ... (todo el bloque CRON_BAD de la Task 3) ...
  fi
```

- [ ] **Step 3: Correr el test**

```bash
scp ops/cron/estrado-watchdog.sh legaltech-vps:/tmp/ && ssh legaltech-vps '/tmp/test-watchdog.sh /tmp/estrado-watchdog.sh'
```

Esperado: `PASS=8 FAIL=0`.

- [ ] **Step 4: Verificar de nuevo contra el log real**

```bash
ssh legaltech-vps 'DRY_RUN=1 WD_STATE_DIR=/tmp /tmp/estrado-watchdog.sh; echo "exit=$?"'
```

Esperado: sin salida, `exit=0`.

- [ ] **Step 5: Commit**

```bash
git add ops/cron/estrado-watchdog.sh
git commit -m "feat(watchdog): chequeo 7 — silencio total del log de crons"
```

---

## Task 5: Reparar el chequeo #3

**Files:**
- Modify: `ops/cron/estrado-watchdog.sh`

- [ ] **Step 1: Reemplazar el bloque #3 entero**

Sacá estas dos líneas:

```bash
SYNC_ERR=$(cnt "cases?select=id&last_sync_status=eq.error&tracking_status=eq.active")
[ "${SYNC_ERR:-0}" -ge 3 ] && add "${SYNC_ERR} causas activas con last_sync_status=error." "sync-err"
```

y poné:

```bash
# 3. Causas rotas.
#    El chequeo viejo cruzaba last_sync_status=error CON tracking_status=active y esa
#    combinación no existe: cuando una causa falla, el worker le mueve el tracking_status.
#    Verificado el 29 jul con 3 causas rotas en la base: devolvía */0.
#    `tracking_status` es la señal de calidad — los errores de infra no lo tocan.
SUSPENDED=$(cnt "cases?select=id&tracking_status=eq.suspended")
[ "${SUSPENDED:-0}" -ge 1 ] && add "${SUSPENDED} causa(s) con monitoreo SUSPENDIDO. Es terminal: no se reintenta sola, hay que reactivarla a mano desde la ficha." "suspended"
TRACK_ERR=$(cnt "cases?select=id&tracking_status=eq.error")
[ "${TRACK_ERR:-0}" -ge 3 ] && add "${TRACK_ERR} causas con tracking_status=error." "track-err"
```

El chequeo de `sync_blocked_until` (`BLOCKED`) queda como está.

- [ ] **Step 2: Verificar contra la base real**

```bash
scp ops/cron/estrado-watchdog.sh legaltech-vps:/tmp/
ssh legaltech-vps 'DRY_RUN=1 WD_STATE_DIR=/tmp /tmp/estrado-watchdog.sh'
```

Esperado ahora **sí** hay salida: `- 3 causa(s) con monitoreo SUSPENDIDO...` y
`SIG: suspended;`. Son `C-1000-2024`, `T-100-2024` y `PROTECCION-23483-2025`, las tres
genuinamente rotas. Que aparezcan es la prueba de que el chequeo dejó de estar muerto.

- [ ] **Step 3: Commit**

```bash
git add ops/cron/estrado-watchdog.sh
git commit -m "fix(watchdog): chequeo 3 nunca matcheó — separar suspendidas de error"
```

---

## Task 6: Reparar el chequeo #5

**Files:**
- Modify: `ops/cron/estrado-watchdog.sh`

- [ ] **Step 1: Reemplazar el bloque #5**

Sacá:

```bash
JERR=$(journalctl -u estrado-pjud.service -u estrado-pjud-worker.service --since "-1 hour" -p err --no-pager 2>/dev/null | grep -viE "^-- " | tail -8)
[ -n "$JERR" ] && add "Errores en journal (última hora):"$'\n'"$JERR" "journal-err"
```

y poné:

```bash
# 5. Errores reales del worker en la última hora.
#    Tres trampas, las tres pisadas alguna vez:
#    (a) `-p err` no sirve: el worker sale por stdout vía xvfb-run, así que TODO entra
#        como PRIORITY=6. El chequeo devolvía 0 líneas en 7 días habiendo 40 errores.
#    (b) `grep -i` tampoco: las URLs de PostgREST llevan `tracking_status=in.(active,error,
#        blocked)` dentro de líneas INFO → 19.565 falsos positivos en 7 días contra 40 reales.
#        Case-sensitive sobre el campo "level" del log JSON da exactamente los 40.
#    (c) alertar por cualquier error inunda: el refresh de slot fallido es el ~12% de fallo
#        conocido del proxy residencial, se auto-cura ("usando la sesión existente") y ya se
#        decidió que va como número en el digest, no como ping. Se excluye y se pone umbral.
#        Sin ese ruido quedan 0 errores en 24h, así que 3 en una hora es señal de verdad.
JERR=$(journalctl -u estrado-pjud.service -u estrado-pjud-worker.service --since "-1 hour" --no-pager 2>/dev/null \
        | grep -E '"level": "(ERROR|CRITICAL)"|Traceback' \
        | grep -vE 'Refresh de slot [0-9]+ fall' || true)
JERR_N=$(printf '%s' "$JERR" | grep -c . || true)
if [ "${JERR_N:-0}" -ge 3 ]; then
  add "${JERR_N} errores del worker en la última hora (excluye el ruido conocido del pool):"$'\n'"$(printf '%s' "$JERR" | tail -5 | cut -c1-200)" "journal-err"
fi
```

- [ ] **Step 2: Verificar los tres números en el VPS**

```bash
ssh legaltech-vps 'J(){ journalctl -u estrado-pjud.service -u estrado-pjud-worker.service --since "$1" --no-pager 2>/dev/null; }
echo -n "grep -i (malo, 7d): "; J "-7 days" | grep -icE "(ERROR|Traceback|CRITICAL)"
echo -n "level JSON  (7d):   "; J "-7 days" | grep -cE "\"level\": \"(ERROR|CRITICAL)\"|Traceback"
echo -n "sin ruido pool 24h: "; J "-24 hours" | grep -E "\"level\": \"(ERROR|CRITICAL)\"|Traceback" | grep -vE "Refresh de slot [0-9]+ fall" | wc -l'
```

Esperado, medido el 29 jul: `19565`, `40`, `0`.

- [ ] **Step 3: Probar que dispara cuando debe**

Bajá el umbral a 1 temporalmente y confirmá que con `--since "-7 days"` sí encuentra líneas:

```bash
ssh legaltech-vps 'journalctl -u estrado-pjud-worker.service --since "-7 days" --no-pager | grep -E "\"level\": \"(ERROR|CRITICAL)\"" | grep -vE "Refresh de slot [0-9]+ fall" | head -3'
```

Esperado: al menos la línea `Re-mint reactivo de slot 0 falló` del 28 jul. Confirma que el
filtro no excluye de más.

- [ ] **Step 4: Commit**

```bash
git add ops/cron/estrado-watchdog.sh
git commit -m "fix(watchdog): chequeo 5 estaba ciego — filtrar por level, no por prioridad"
```

---

## Task 7: Chequeo #8 — causas atascadas

**Files:**
- Modify: `ops/cron/estrado-watchdog.sh`

- [ ] **Step 1: Implementar**

Después del chequeo #7:

```bash
# 8. Causas atascadas: next_sync_at vencido hace más de 2h.
#    Se elige `next_sync_at` vencido y no "N horas sin sincronizar" porque la cadencia
#    es por prioridad (_compute_next_sync_at): un umbral fijo daría falsos positivos en
#    las causas de cadencia diaria. Las 2h son margen para un scheduler unos minutos atrasado.
STUCK=$(cnt "cases?select=id&tracking_status=eq.active&next_sync_at=lt.$(date -u -d '-2 hours' +%Y-%m-%dT%H:%M:%S)")
[ "${STUCK:-0}" -ge 1 ] && add "${STUCK} causa(s) activa(s) con next_sync_at vencido hace más de 2h — el scheduler no las está tomando." "stuck"
```

- [ ] **Step 2: Verificar que hoy da 0**

```bash
scp ops/cron/estrado-watchdog.sh legaltech-vps:/tmp/
ssh legaltech-vps 'DRY_RUN=1 WD_STATE_DIR=/tmp /tmp/estrado-watchdog.sh'
```

Esperado: aparece lo de las suspendidas (Task 5) pero **no** `stuck`. Medido hoy: `*/0`, y el
`next_sync_at` más viejo de las activas es futuro (`2026-07-29T23:34Z`).

- [ ] **Step 3: Commit**

```bash
git add ops/cron/estrado-watchdog.sh
git commit -m "feat(watchdog): chequeo 8 — causas activas con next_sync_at vencido"
```

---

## Task 8: Anti-spam por causa para las suspendidas

El anti-spam actual es por firma global con cooldown de 3h. Como la suspensión es terminal, una
causa suspendida re-alertaría cada 3h **para siempre** hasta que alguien la reactive.

**Files:**
- Modify: `ops/cron/estrado-watchdog.sh`

- [ ] **Step 1: Reemplazar el bloque de suspendidas de la Task 5**

```bash
# Suspendidas: alerta UNA vez por causa. El anti-spam global es por cooldown de 3h y
# la suspensión es terminal, así que sin esto la misma causa avisaría cada 3h hasta que
# alguien la reactive. El archivo guarda los IDs ya avisados; si una causa se reactiva y
# vuelve a caer, vuelve a avisar (deja de estar en la lista de suspendidas y reingresa).
SUSP_STATE="$WD_STATE_DIR/estrado-wd-suspended"
SUSP_JSON=$(curl -s -m 20 "$API/cases?select=id,case_number&tracking_status=eq.suspended" "${AUTH[@]}" 2>/dev/null || true)
SUSP_IDS=$(printf '%s' "$SUSP_JSON" | jq -r '.[]?.id' 2>/dev/null | sort || true)
if [ -n "${SUSP_IDS// }" ]; then
  touch "$SUSP_STATE"
  NEW_IDS=$(comm -23 <(printf '%s\n' "$SUSP_IDS") <(sort "$SUSP_STATE") || true)
  if [ -n "${NEW_IDS// }" ]; then
    NEW_NUMS=$(printf '%s' "$SUSP_JSON" | jq -r --arg ids "$NEW_IDS" \
                 '[.[] | select(.id | inside($ids))] | map(.case_number) | join(", ")' 2>/dev/null || true)
    add "Causa(s) con monitoreo SUSPENDIDO: ${NEW_NUMS:-$NEW_IDS}. Es terminal: no se reintenta sola, hay que reactivarla a mano desde la ficha." "suspended"
  fi
fi
# Se reescribe siempre (no se appendea): si una causa se reactiva tiene que salir del archivo.
printf '%s\n' "$SUSP_IDS" | grep -v '^$' | sort > "$SUSP_STATE" || true
```

**Ojo:** `jq --arg ids` con `inside` es frágil si un id es subcadena de otro. Usá esta versión,
que compara exacto:

```bash
    NEW_NUMS=$(printf '%s' "$SUSP_JSON" \
                | jq -r --argjson want "$(printf '%s\n' "$NEW_IDS" | jq -R . | jq -s .)" \
                     '[.[] | select(.id as $i | $want | index($i))] | map(.case_number) | join(", ")' 2>/dev/null || true)
```

- [ ] **Step 2: Probar el ciclo completo en el VPS**

```bash
scp ops/cron/estrado-watchdog.sh legaltech-vps:/tmp/
ssh legaltech-vps 'rm -f /tmp/estrado-wd-suspended /tmp/estrado-wd-state
echo "--- 1ra corrida: DEBE avisar ---"
DRY_RUN=1 WD_STATE_DIR=/tmp /tmp/estrado-watchdog.sh
echo "--- estado guardado ---"; cat /tmp/estrado-wd-suspended
echo "--- 2da corrida: NO debe avisar de suspendidas ---"
DRY_RUN=1 WD_STATE_DIR=/tmp /tmp/estrado-watchdog.sh
echo "--- simulo causa nueva: borro un id del estado ---"
head -2 /tmp/estrado-wd-suspended > /tmp/x && mv /tmp/x /tmp/estrado-wd-suspended
echo "--- 3ra corrida: DEBE avisar solo de la que falta ---"
DRY_RUN=1 WD_STATE_DIR=/tmp /tmp/estrado-watchdog.sh'
```

Esperado: la 1ra lista las 3 causas; el archivo queda con 3 ids; la 2da no menciona
`SUSPENDIDO`; la 3ra menciona **una sola** causa.

- [ ] **Step 3: Commit**

```bash
git add ops/cron/estrado-watchdog.sh
git commit -m "feat(watchdog): anti-spam por causa para suspendidas (terminal, no re-alertar cada 3h)"
```

---

## Task 9: logrotate

`/var/log/estrado-cron.log` viene creciendo sin rotar desde marzo (76 KB, 1381 líneas).

**Files:**
- Create: `ops/cron/logrotate/estrado-cron`

- [ ] **Step 1: Escribir el archivo**

```
# /etc/logrotate.d/estrado-cron
# run-cron.sh appendea con `>>` y reabre el archivo en cada corrida, así que
# `create` alcanza: no hace falta copytruncate.
/var/log/estrado-cron.log {
    # Sin esto logrotate SALTA el archivo: /var/log es root:syslog group-writable
    # y se planta con "parent directory has insecure permissions".
    su root root
    monthly
    rotate 6
    compress
    delaycompress
    missingok
    notifempty
    create 0644 root root
}
```

- [ ] **Step 2: Validar la sintaxis sin aplicar nada**

```bash
scp ops/cron/logrotate/estrado-cron legaltech-vps:/tmp/lr-estrado
ssh legaltech-vps 'logrotate -d /tmp/lr-estrado 2>&1 | tail -20'
```

Esperado: `rotating pattern: /var/log/estrado-cron.log monthly ...` y ningún `error:`.
`-d` es debug/dry-run: no rota nada.

- [ ] **Step 3: Commit**

```bash
git add ops/cron/logrotate/estrado-cron
git commit -m "feat(ops): logrotate mensual para /var/log/estrado-cron.log"
```

---

## Task 10: `deploy-cron.sh`

**Files:**
- Create: `ops/cron/deploy-cron.sh`

- [ ] **Step 1: Escribirlo**

```bash
#!/usr/bin/env bash
# Instala los scripts de ops/cron/ en el VPS. Hace backup antes de pisar nada.
#   ./ops/cron/deploy-cron.sh [host]
set -euo pipefail

HOST="${1:-legaltech-vps}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP=$(date +%Y%m%d%H%M)

# Guarda: nunca desplegar con un secreto adentro.
if grep -rqE '(eyJ[A-Za-z0-9_-]{20,}|SECRET="[A-Za-z0-9])' "$SRC"/*.sh; then
  echo "ABORTA: hay un secreto en ops/cron/*.sh" >&2; exit 1
fi

echo "==> backup en $HOST:/opt/estrado-cron/"
ssh "$HOST" "mkdir -p /opt/estrado-cron/backup-$STAMP && cp -a /opt/estrado-cron/*.sh /opt/estrado-cron/backup-$STAMP/ 2>/dev/null || true"

for f in estrado-watchdog.sh estrado-digest.sh hermes-backup.sh run-cron.sh; do
  echo "==> $f"
  scp -q "$SRC/$f" "$HOST:/tmp/$f"
  ssh "$HOST" "install -o root -g root -m 700 /tmp/$f /opt/estrado-cron/$f && rm -f /tmp/$f"
done

echo "==> logrotate"
scp -q "$SRC/logrotate/estrado-cron" "$HOST:/tmp/lr-estrado"
ssh "$HOST" "logrotate -d /tmp/lr-estrado >/dev/null && install -o root -g root -m 644 /tmp/lr-estrado /etc/logrotate.d/estrado-cron && rm -f /tmp/lr-estrado"

echo "==> /etc/estrado-cron.env (solo si falta)"
ssh "$HOST" 'test -r /etc/estrado-cron.env && echo "   ya existe, no se toca" || echo "   FALTA: crealo a mano con APP_URL y CRON_SECRET, modo 600 root"'

echo "==> watchdog en dry-run"
ssh "$HOST" 'DRY_RUN=1 WD_STATE_DIR=/tmp /opt/estrado-cron/estrado-watchdog.sh; echo "   exit=$?"'
echo "listo. backup en /opt/estrado-cron/backup-$STAMP/"
```

- [ ] **Step 2: `chmod +x` y commit**

```bash
chmod +x ops/cron/deploy-cron.sh
git add ops/cron/deploy-cron.sh
git commit -m "feat(ops): deploy-cron.sh con backup previo y guarda anti-secretos"
```

---

## Task 11: Merge y despliegue

**PARÁ ACÁ Y PEDÍ AUTORIZACIÓN AL USUARIO ANTES DEL STEP 3.** Los pasos 1-2 son del repo; del
3 en adelante se escribe en producción.

- [ ] **Step 1: Correr toda la suite una última vez**

```bash
scp ops/cron/estrado-watchdog.sh ops/cron/tests/test-watchdog.sh legaltech-vps:/tmp/
ssh legaltech-vps '/tmp/test-watchdog.sh /tmp/estrado-watchdog.sh'
```

Esperado: `PASS=8 FAIL=0`.

- [ ] **Step 2: PR contra `main`**

```bash
git push -u origin fix/watchdog-cron-health
gh pr create --title "fix(ops): el watchdog no miraba los crons de la app (404 durante 120 días)" --body "..."
```

En el cuerpo del PR van las tres evidencias: las 1183 líneas 404, el `*/0` del chequeo #3 y las
0 líneas del chequeo #5.

- [ ] **Step 3: Crear `/etc/estrado-cron.env` en el VPS** (requiere OK del usuario)

Los valores salen del `run-cron.sh` que hoy está en el VPS. **Hacer esto ANTES de desplegar el
`run-cron.sh` nuevo**, o los crons se caen otra vez.

```bash
ssh legaltech-vps 'APP_URL=$(grep "^APP_URL=" /opt/estrado-cron/run-cron.sh | cut -d= -f2- | tr -d "\"")
SECRET=$(grep "^CRON_SECRET=" /opt/estrado-cron/run-cron.sh | cut -d= -f2- | tr -d "\"")
printf "APP_URL=%s\nCRON_SECRET=%s\n" "$APP_URL" "$SECRET" > /etc/estrado-cron.env
chmod 600 /etc/estrado-cron.env; chown root:root /etc/estrado-cron.env
echo "creado, $(wc -l < /etc/estrado-cron.env) lineas, modo $(stat -c %a /etc/estrado-cron.env)"'
```

- [ ] **Step 4: Desplegar** (requiere OK del usuario)

```bash
./ops/cron/deploy-cron.sh legaltech-vps
```

- [ ] **Step 5: Verificar que `run-cron.sh` sigue funcionando**

El paso que no se puede saltear: si el `.env` quedó mal, **todos** los crons vuelven a caerse y
esta vez sin dominio viejo que culpar.

```bash
ssh legaltech-vps '/opt/estrado-cron/run-cron.sh /api/cron/stale-sync-recovery; echo "exit=$?"; tail -1 /var/log/estrado-cron.log'
```

Esperado: `exit=0` y una línea nueva con `HTTP 200`. Si da otra cosa, restaurá:
`cp /opt/estrado-cron/backup-*/run-cron.sh /opt/estrado-cron/run-cron.sh`.

- [ ] **Step 6: Confirmar que el watchdog real quedó sano**

```bash
ssh legaltech-vps '/opt/estrado-cron/estrado-watchdog.sh; echo "exit=$?"'
```

Sin `DRY_RUN`: debería mandar **un** Telegram con las 3 causas suspendidas (que es correcto —
llevan semanas rotas y nadie avisó) y después quedarse callado. Confirmá con el usuario que le
llegó.

- [ ] **Step 7: Snapshot del crontab**

```bash
ssh legaltech-vps 'crontab -l' > ops/cron/crontab.snapshot
git add ops/cron/crontab.snapshot
git commit -m "chore(ops): snapshot del crontab de root"
```

---

## Fuera de alcance (a propósito)

- **Rehabilitar `pjud-sync` en el crontab** — está comentado por rate limiting de OJV y el
  worker systemd ya cubre el sync. Tocarlo es otra decisión.
- **El chequeo de spike del proxy (>35% de fallo de minteo)** — depende de B2, que todavía no
  publica esas métricas. Queda inerte hasta el plan 3.
- **Líneas malformadas en el log** (`ERROR: No endpoint provided`): 0 en 1381 líneas. El
  chequeo #7 las ignora; si alguna vez aparecen, es un cron sin argumento.
