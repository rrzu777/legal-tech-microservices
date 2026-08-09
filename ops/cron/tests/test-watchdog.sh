#!/bin/bash
# Tests del watchdog. CORRE EN EL VPS: necesita systemd, el journal y el .env del
# microservicio, porque los chequeos 1-6 se apoyan en eso. En un laptop da falsos positivos.
#
#   scp ops/cron/estrado-watchdog.sh ops/cron/tests/test-watchdog.sh legaltech-vps:/tmp/
#   ssh legaltech-vps 'chmod +x /tmp/test-watchdog.sh && /tmp/test-watchdog.sh /tmp/estrado-watchdog.sh'
#
# Sale 0 si pasa todo. Nunca escribe fuera de un mktemp -d.
set -uo pipefail

WD="${1:-/tmp/estrado-watchdog.sh}"
TMP=$(mktemp -d)
PASS=0; FAIL=0

# El chequeo 9 pide un health por HTTP, así que hace falta uno de verdad: con
# `curl file://` el código sale 000 y no se puede distinguir "contestó mal" de "no
# contestó", que es justamente la distinción que el chequeo hace.
PORT=$((18000 + RANDOM % 1000))
mkdir -p "$TMP/www"
( cd "$TMP/www" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 ) &
HTTPD=$!
trap 'kill "$HTTPD" 2>/dev/null; rm -rf "$TMP"' EXIT
for _ in $(seq 1 50); do curl -sf -m 1 "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break; sleep 0.1; done

# El health de una API SANA pero ociosa: cero tráfico y ni un solo éxito registrado.
# Es el fixture más importante del archivo — ver el test que lo usa.
cat > "$TMP/www/sano.json" <<'EOF'
{"status":"ok","last_successful_request":null,"uptime_seconds":329667,
 "total_requests":0,"search_requests":0,"detail_requests":0,
 "total_errors":0,"total_blocked":0,"blocked_rate":0.0,"total_pool_failures":0}
EOF
SANO="http://127.0.0.1:$PORT/sano.json"
printf 'SUPABASE_URL=http://127.0.0.1:%s/sb\nSUPABASE_SERVICE_KEY=fake-para-el-test\n' "$PORT" > "$TMP/watchdog.env"

# Backup fresco y con peso: el chequeo 11 calla salvo en sus tests.
mkdir -p "$TMP/backups-sanos"
head -c 2048 /dev/zero > "$TMP/backups-sanos/estrado-20990101-000000.tar.gz"

# Crontab "vivo" y snapshot idénticos: el chequeo 10 calla salvo en sus tests.
printf '0 11 * * * /opt/estrado-cron/run-cron.sh /api/cron/task-reminders\n' > "$TMP/crontab-base"

# El journal real del VPS no debe volver no deterministas todos los tests. Los
# casos del chequeo 5 inyectan sus propios eventos debajo.
: > "$TMP/journal-sano"

# Sockets en escucha con el cutover ya aplicado: el chequeo 12 calla salvo en
# sus tests. Es fixture y no `ss` de verdad a propósito — estos tests corren EN
# el VPS, y leer el bind real ataría su resultado al estado del despliegue.
cat > "$TMP/ss-sano" <<'EOF'
State  Recv-Q Send-Q  Local Address:Port   Peer Address:Port
LISTEN 0      4096        127.0.0.1:8000        0.0.0.0:*
LISTEN 0      4096                *:443               *:*
LISTEN 0      128           0.0.0.0:22          0.0.0.0:*
EOF

# Cada corrida estrena directorio de estado. No es prolijidad: el cooldown anti-spam
# de 3h se evalúa ANTES del `if DRY_RUN`, así que dos tests seguidos que produzcan la
# misma firma hacen que el segundo salga vacío y falle por una razón que no tiene nada
# que ver con lo que estaba probando. Con estado compartido, agregar un test podía
# romper el de al lado.
#
# `WDS` es la excepción, para los tests que justamente prueban que algo se avisa una
# sola vez y necesitan que el estado sobreviva entre corridas.
run() {
  local st="${WDS:-$(mktemp -d "$TMP/run-XXXXXX")}"
  DRY_RUN=1 WD_STATE_DIR="$st" CRON_LOG="$1" API_HEALTH_URL="${2:-$SANO}" \
    WD_CRONTAB_SNAPSHOT="${WD_CRONTAB_SNAPSHOT:-$TMP/crontab-base}" \
    WD_CRONTAB_LIVE_FILE="${WD_CRONTAB_LIVE_FILE:-$TMP/crontab-base}" \
    WD_SS_OUTPUT_FILE="${WD_SS_OUTPUT_FILE:-$TMP/ss-sano}" \
    WD_BACKUP_DIR="${WD_BACKUP_DIR:-$TMP/backups-sanos}" \
    WD_JOURNAL_FILE="${WD_JOURNAL_FILE:-$TMP/journal-sano}" \
    WD_NOW_EPOCH="${WD_NOW_EPOCH:-$(date -u -d '2026-08-10T14:00:00Z' +%s)}" \
    WD_STUCK_COUNT="${WD_STUCK_COUNT:-39}" \
    bash "$WD" 2>/dev/null
}

expect_contains() { # <nombre> <salida> <texto esperado>
  if printf '%s' "$2" | grep -qF -- "$3"; then
    echo "  ok   $1"; PASS=$((PASS+1))
  else
    echo "  FAIL $1 — esperaba encontrar: $3"; echo "       salida: ${2:-<vacía>}"; FAIL=$((FAIL+1))
  fi
}

expect_missing() { # <nombre> <salida> <texto que NO debe estar>
  if printf '%s' "$2" | grep -qF -- "$3"; then
    echo "  FAIL $1 — no esperaba: $3"; echo "       salida: $2"; FAIL=$((FAIL+1))
  else
    echo "  ok   $1"; PASS=$((PASS+1))
  fi
}

expect_equals() { # <nombre> <actual> <esperado>
  if [ "$2" = "$3" ]; then
    echo "  ok   $1"; PASS=$((PASS+1))
  else
    echo "  FAIL $1 — esperaba: $3"; echo "       salida: ${2:-<vacía>}"; FAIL=$((FAIL+1))
  fi
}

# El snapshot es el contrato que el chequeo 10 normaliza y compara contra root.
# Desde el checkout se lee el archivo hermano; en el VPS (donde este test se
# copia a /tmp) se usa el snapshot que está desplegado. La variable permite
# probar una copia explícita sin tocar el crontab vivo.
TEST_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CRON_SNAPSHOT_UNDER_TEST="${CRON_SNAPSHOT_UNDER_TEST:-$TEST_DIR/../crontab.snapshot}"
if [ ! -r "$CRON_SNAPSHOT_UNDER_TEST" ]; then
  CRON_SNAPSHOT_UNDER_TEST=/opt/legal-tech-microservices/ops/cron/crontab.snapshot
fi
normalizar_crontab() { grep -vE '^[[:space:]]*(#|$)' | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//' | sort; }

echo "== snapshot: stale-sync-alert en horario de oficina =="
STALE_SYNC_ALERT=$(normalizar_crontab < "$CRON_SNAPSHOT_UNDER_TEST" | grep -F '/api/cron/stale-sync-alert' || true)
expect_equals "solo agenda laboral Berlin para stale-sync-alert" "$STALE_SYNC_ALERT" "0 14,17,20 * * 1-5 /opt/estrado-cron/run-cron.sh /api/cron/stale-sync-alert"

cat > "$TMP/systemctl-worker-disabled" <<'EOF'
#!/bin/bash
if [ "$1" = "is-enabled" ] && [ "$2" = "estrado-pjud-worker.service" ]; then echo disabled; exit 1; fi
if [ "$1" = "is-active" ] && [ "$3" = "estrado-pjud-worker.service" ]; then exit 3; fi
exit 0
EOF
cat > "$TMP/systemctl-worker-enabled-down" <<'EOF'
#!/bin/bash
if [ "$1" = "is-enabled" ] && [ "$2" = "estrado-pjud-worker.service" ]; then echo enabled; exit 0; fi
if [ "$1" = "is-active" ] && [ "$3" = "estrado-pjud-worker.service" ]; then exit 3; fi
exit 0
EOF
cat > "$TMP/systemctl-worker-disabled-active" <<'EOF'
#!/bin/bash
if [ "$1" = "is-enabled" ] && [ "$2" = "estrado-pjud-worker.service" ]; then echo disabled; exit 1; fi
exit 0
EOF
cat > "$TMP/systemctl-worker-unknown" <<'EOF'
#!/bin/bash
if [ "$1" = "is-enabled" ]; then exit 4; fi
if [ "$1" = "is-active" ] && [ "$3" = "estrado-pjud.service" ]; then exit 0; fi
exit 4
EOF
chmod +x "$TMP"/systemctl-worker-*

echo "== chequeo 1: pausa deliberada no es falso incidente =="
OUT=$(WD_SYSTEMCTL="$TMP/systemctl-worker-disabled" run "$TMP/crontab-base")
expect_missing "worker disabled e inactivo es el gate esperado" "$OUT" "worker-down"

echo "== chequeo 1: worker habilitado pero caído sí alerta =="
OUT=$(WD_SYSTEMCTL="$TMP/systemctl-worker-enabled-down" run "$TMP/crontab-base")
expect_contains "worker que debía correr está caído" "$OUT" "worker-down"

echo "== chequeo 1: worker disabled pero activo viola el gate =="
OUT=$(WD_SYSTEMCTL="$TMP/systemctl-worker-disabled-active" run "$TMP/crontab-base")
expect_contains "detecta tráfico activo contra el gate" "$OUT" "worker-gate-violated"

echo "== chequeo 1: estado systemd desconocido nunca se interpreta como pausa =="
OUT=$(WD_SYSTEMCTL="$TMP/systemctl-worker-unknown" run "$TMP/crontab-base")
expect_contains "falla de observación alerta en vez de quedar silenciosa" "$OUT" "worker-state-unknown"

# Fixtures con fechas LOCALES, que es como las escribe run-cron.sh (el VPS es
# Europe/Berlin, no UTC). Si el chequeo comparara contra `date -u` esto fallaría
# cerca de medianoche.
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
expect_contains "reporta el codigo"             "$OUT" "HTTP 404"
expect_contains "la firma incluye el codigo"    "$OUT" "cron-fail:404"
expect_missing  "no reporta el que dio 200"     "$OUT" "trial-emails"

echo "== chequeo 7: distingue codigos distintos =="
cat > "$TMP/mixed.log" <<EOF
$HOY /api/cron/task-reminders - HTTP 401
$HOY /api/cron/event-reminders - HTTP 500
EOF
OUT=$(run "$TMP/mixed.log")
expect_contains "la firma lista los dos codigos" "$OUT" "cron-fail:401,500"

echo "== chequeo 7: un blip que se recupera no despierta a nadie =="
# Con cooldown de 3h y ventana de 24h, alertar por un 404 suelto ya recuperado
# significaria 8 alertas por un blip. Lo que importa es la ULTIMA corrida.
cat > "$TMP/blip.log" <<EOF
$(date -d '-8 hours' '+%Y-%m-%d %H:%M') /api/cron/stale-sync-alert - HTTP 404
$HOY /api/cron/stale-sync-alert - HTTP 200
EOF
OUT=$(run "$TMP/blip.log")
expect_missing "blip recuperado: silencio" "$OUT" "cron-fail"

echo "== chequeo 7: roto de verdad (falla la ultima corrida) =="
cat > "$TMP/broken.log" <<EOF
$(date -d '-8 hours' '+%Y-%m-%d %H:%M') /api/cron/stale-sync-alert - HTTP 200
$HOY /api/cron/stale-sync-alert - HTTP 404
EOF
OUT=$(run "$TMP/broken.log")
expect_contains "ultima corrida rota: alerta" "$OUT" "cron-fail:404"

echo "== chequeo 7: silencio =="
printf '%s /api/cron/task-reminders - HTTP 200\n' "$VIEJO" > "$TMP/silent.log"
OUT=$(run "$TMP/silent.log")
expect_contains "detecta el silencio"       "$OUT" "cron-silent"
expect_missing  "silencio no reporta fallas" "$OUT" "cron-fail"

echo "== chequeo 7: log sano =="
cat > "$TMP/ok.log" <<EOF
$VIEJO /api/cron/task-reminders - HTTP 404
$HOY /api/cron/task-reminders - HTTP 200
EOF
OUT=$(run "$TMP/ok.log")
expect_missing "ignora fallas fuera de la ventana de 24h" "$OUT" "cron-fail"
expect_missing "no grita silencio si hay lineas frescas"  "$OUT" "cron-silent"

echo "== chequeo 7: log ilegible =="
OUT=$(run "$TMP/no-existe.log")
expect_contains "avisa si no puede leer el log" "$OUT" "cron-log-missing"

# --- Rotación del log -------------------------------------------------------
# El 2026-08-01 a las 00:00 logrotate creó el archivo nuevo vacío y a las 00:01 el
# watchdog alertó "ningún cron corrió en 24h" con las 8 corridas del día en HTTP 200
# dentro del .1. Estos tests son ese incidente.
echo "== chequeo 7: el log recién rotado NO es silencio =="
: > "$TMP/rot.log"
printf '%s /api/cron/task-reminders - HTTP 200\n' "$HOY" > "$TMP/rot.log.1"
OUT=$(run "$TMP/rot.log")
expect_missing "lee el .1 cuando el actual está vacío" "$OUT" "cron-silent"

echo "== chequeo 7: la linea diaria del backup NO tapa el silencio de la app =="
cat > "$TMP/solo-backup.log" <<EOF
$VIEJO /api/cron/task-reminders - HTTP 200
$HOY estrado-backup: /root/estrado-backups/estrado-x.tar.gz (8.0K)
EOF
OUT=$(run "$TMP/solo-backup.log")
expect_contains "backup fresco + crons viejos = silencio igual" "$OUT" "cron-silent"

echo "== chequeo 7: silencio de verdad, con rotado viejo =="
: > "$TMP/viejo.log"
printf '%s /api/cron/task-reminders - HTTP 200\n' "$VIEJO" > "$TMP/viejo.log.1"
OUT=$(run "$TMP/viejo.log")
expect_contains "nada fresco en ninguno de los dos: alerta" "$OUT" "cron-silent"

echo "== chequeo 7: el rotado se lee ANTES que el actual =="
# El awk se queda con la ÚLTIMA aparición de cada endpoint. Si los archivos se
# pasaran al revés, el 404 viejo del .1 pisaría al 200 nuevo y un endpoint sano se
# reportaría como roto. Este test es el que mata esa mutación.
printf '%s /api/cron/stale-sync-alert - HTTP 404\n' "$(date -d '-6 hours' '+%Y-%m-%d %H:%M')" > "$TMP/orden.log.1"
printf '%s /api/cron/stale-sync-alert - HTTP 200\n' "$HOY" > "$TMP/orden.log"
OUT=$(run "$TMP/orden.log")
expect_missing "el 404 rotado no pisa al 200 actual" "$OUT" "cron-fail"

echo "== chequeo 7: sin .1 sigue andando =="
printf '%s /api/cron/task-reminders - HTTP 200\n' "$HOY" > "$TMP/solo.log"
OUT=$(run "$TMP/solo.log")
expect_missing "sin rotado, sin silencio" "$OUT" "cron-silent"
expect_missing "sin rotado, sin fallas"   "$OUT" "cron-fail"

# --- Chequeo 9: salud de la API de scraping ---------------------------------
BASE="$TMP/base.log"
printf '%s /api/cron/task-reminders - HTTP 200\n' "$HOY" > "$BASE"

echo "== chequeo 8: stuck respeta la ventana programada del worker =="
# Cada límite mata una mutación distinta: abrir antes de las 10, cerrar a las 18,
# o contar durante el fin de semana. Los epochs son UTC y los resultados están
# verificados contra America/Santiago (UTC-4 en agosto de 2026).
OUT=$(WD_ENV="$TMP/watchdog.env" WD_NOW_EPOCH=$(date -u -d '2026-08-08T14:00:00Z' +%s) run "$BASE")
expect_missing "sábado: no evalúa causas atascadas" "$OUT" "scheduler no las está tomando"
OUT=$(WD_ENV="$TMP/watchdog.env" WD_NOW_EPOCH=$(date -u -d '2026-08-10T13:59:00Z' +%s) run "$BASE")
expect_missing "lunes 09:59: todavía no evalúa causas atascadas" "$OUT" "scheduler no las está tomando"
OUT=$(WD_ENV="$TMP/watchdog.env" WD_NOW_EPOCH=$(date -u -d '2026-08-10T14:00:00Z' +%s) run "$BASE")
expect_contains "lunes 10:00: evalúa causas atascadas" "$OUT" "scheduler no las está tomando"
# En verano Chile está UTC-3: este borde prueba que la ventana no quedó amarrada
# al offset UTC-4 de los casos de agosto.
OUT=$(WD_ENV="$TMP/watchdog.env" WD_NOW_EPOCH=$(date -u -d '2026-01-05T13:00:00Z' +%s) run "$BASE")
expect_contains "lunes 10:00 de verano: evalúa causas atascadas" "$OUT" "scheduler no las está tomando"
OUT=$(WD_ENV="$TMP/watchdog.env" WD_NOW_EPOCH=$(date -u -d '2026-08-14T21:59:00Z' +%s) run "$BASE")
expect_contains "viernes 17:59: sigue evaluando causas atascadas" "$OUT" "scheduler no las está tomando"
OUT=$(WD_ENV="$TMP/watchdog.env" WD_NOW_EPOCH=$(date -u -d '2026-08-14T22:00:00Z' +%s) run "$BASE")
expect_missing "viernes 18:00: deja de evaluar causas atascadas" "$OUT" "scheduler no las está tomando"
OUT=$(WD_ENV="$TMP/watchdog.env" WD_NOW_EPOCH=$(date -u -d '2026-08-08T14:00:00Z' +%s) run "$BASE" "http://127.0.0.1:1/health")
expect_missing "fuera de horario no tapa stuck con health roto" "$OUT" "scheduler no las está tomando"
expect_contains "fuera de horario conserva la alerta api-health" "$OUT" "api-health"

echo "== chequeo 8: consulta sólo causas elegibles =="
STUCK_BLOCK=$(awk '/^# 8\. Causas atascadas/{inside=1} /^# 9\. La API/{inside=0} inside' "$WD")
for literal in \
  'source_system=eq.pjud_ojv' \
  'tracking_status=eq.active' \
  'sync_priority.is.null' \
  'sync_priority.lte.3' \
  'sync_blocked_until.is.null' \
  'sync_blocked_until.lt.' \
  'sync_worker_id.is.null' \
  'sync_claimed_at.is.null' \
  'sync_claimed_at.lt.'; do
  expect_contains "el filtro stuck incluye $literal" "$STUCK_BLOCK" "$literal"
done

echo "== chequeo 8: el lease activo queda fuera del request =="
mkdir -p "$TMP/curl-bin"
printf '%s\n' \
  '#!/bin/bash' \
  'printf "%s\\n" "$*" >> "$WD_CURL_LOG"' \
  'case " $* " in' \
  '  *" -I "*) printf "HTTP/1.1 200 OK\\r\\nContent-Range: 0-0/0\\r\\n\\r\\n" ;;' \
  '  *) printf "[]" ;;' \
  'esac' > "$TMP/curl-bin/curl"
chmod +x "$TMP/curl-bin/curl"
run_with_captured_queries() { # <epoch>
  local state
  state=$(mktemp -d "$TMP/captured-query-state-XXXXXX")
  PATH="$TMP/curl-bin:$PATH" DRY_RUN=1 WD_ENV="$TMP/watchdog.env" WD_STATE_DIR="$state" \
    CRON_LOG="$BASE" API_HEALTH_URL="$SANO" WD_CRONTAB_SNAPSHOT="$TMP/crontab-base" \
    WD_CRONTAB_LIVE_FILE="$TMP/crontab-base" WD_SS_OUTPUT_FILE="$TMP/ss-sano" \
    WD_BACKUP_DIR="$TMP/backups-sanos" WD_JOURNAL_FILE="$TMP/journal-sano" \
    WD_NOW_EPOCH="$1" WD_STUCK_COUNT= bash "$WD" 2>/dev/null
}
STUCK_QUERY_LOG="$TMP/stuck-query.log"
: > "$STUCK_QUERY_LOG"
OUT=$(WD_CURL_LOG="$STUCK_QUERY_LOG" run_with_captured_queries "$(date -u -d '2026-08-10T14:00:00Z' +%s)")
expect_contains "consulta abierta excluye lease activo" "$(<"$STUCK_QUERY_LOG")" \
  'or(sync_worker_id.is.null,sync_claimed_at.is.null,sync_claimed_at.lt.2026-08-10T10:00:00)'
: > "$STUCK_QUERY_LOG"
OUT=$(WD_CURL_LOG="$STUCK_QUERY_LOG" run_with_captured_queries "$(date -u -d '2026-08-08T14:00:00Z' +%s)")
expect_missing "fuera de horario no consulta casos stuck" "$(<"$STUCK_QUERY_LOG")" 'next_sync_at=lt.'

echo "== reloj y estado dry-run =="
INVALID_ERR="$TMP/invalid-now.err"
mkdir -p "$TMP/invalid-state"
if DRY_RUN=1 WD_ENV="$TMP/watchdog.env" WD_STATE_DIR="$TMP/invalid-state" \
  WD_NOW_EPOCH=invalido bash "$WD" >/dev/null 2>"$INVALID_ERR"; then
  echo "  FAIL WD_NOW_EPOCH inválido termina con error"; FAIL=$((FAIL+1))
else
  echo "  ok   WD_NOW_EPOCH inválido termina con error"; PASS=$((PASS+1))
fi
expect_contains "WD_NOW_EPOCH inválido explica el problema" "$(<"$INVALID_ERR")" "WD_NOW_EPOCH inválido"

OVERFLOW_ERR="$TMP/overflow-now.err"
if DRY_RUN=1 WD_ENV="$TMP/watchdog.env" WD_STATE_DIR="$TMP/overflow-state" \
  WD_NOW_EPOCH=9223372036854775807 bash "$WD" >/dev/null 2>"$OVERFLOW_ERR"; then
  echo "  FAIL WD_NOW_EPOCH fuera de rango termina con error"; FAIL=$((FAIL+1))
else
  echo "  ok   WD_NOW_EPOCH fuera de rango termina con error"; PASS=$((PASS+1))
fi
expect_contains "WD_NOW_EPOCH fuera de rango explica el problema" "$(<"$OVERFLOW_ERR")" "WD_NOW_EPOCH inválido"

echo "== WD_NOW_EPOCH con ceros iniciales usa decimal =="
mkdir -p "$TMP/date-bin"
printf '%s\n' \
  '#!/bin/bash' \
  'printf "%s\\n" "$*" >> "$WD_DATE_LOG"' \
  'exec /usr/bin/date "$@"' > "$TMP/date-bin/date"
chmod +x "$TMP/date-bin/date"
run_leading_zero_epoch() { # <epoch>
  local state
  state=$(mktemp -d "$TMP/leading-zero-state-XXXXXX")
  PATH="$TMP/date-bin:$PATH" DRY_RUN=1 WD_ENV="$TMP/watchdog.env" WD_STATE_DIR="$state" \
    CRON_LOG="$BASE" API_HEALTH_URL="$SANO" WD_CRONTAB_SNAPSHOT="$TMP/crontab-base" \
    WD_CRONTAB_LIVE_FILE="$TMP/crontab-base" WD_SS_OUTPUT_FILE="$TMP/ss-sano" \
    WD_BACKUP_DIR="$TMP/backups-sanos" WD_JOURNAL_FILE="$TMP/journal-sano" \
    WD_NOW_EPOCH="$1" WD_STUCK_COUNT=39 bash "$WD" 2>/dev/null
}
EPOCH_DATE_LOG="$TMP/leading-zero-date.log"
: > "$EPOCH_DATE_LOG"
if WD_DATE_LOG="$EPOCH_DATE_LOG" run_leading_zero_epoch 010; then
  echo "  ok   010 se acepta como epoch decimal"; PASS=$((PASS+1))
else
  echo "  FAIL 010 se acepta como epoch decimal"; FAIL=$((FAIL+1))
fi
expect_contains "010 resta dos horas desde 10 decimal" "$(<"$EPOCH_DATE_LOG")" "-d @-7190"
: > "$EPOCH_DATE_LOG"
if WD_DATE_LOG="$EPOCH_DATE_LOG" run_leading_zero_epoch 08; then
  echo "  ok   08 se acepta como epoch decimal"; PASS=$((PASS+1))
else
  echo "  FAIL 08 se acepta como epoch decimal"; FAIL=$((FAIL+1))
fi
expect_contains "08 resta dos horas desde 8 decimal" "$(<"$EPOCH_DATE_LOG")" "-d @-7192"

DEFAULT_STATE_DIR=$(mktemp -d "$TMP/default-state-XXXXXX")
printf 'no tocar\n' > "$DEFAULT_STATE_DIR/estrado-wd-state"
DEFAULT_STATE_CHECKSUM=$(md5sum "$DEFAULT_STATE_DIR/estrado-wd-state")
OUT=$(DRY_RUN=1 WD_ENV="$TMP/watchdog.env" WD_DEFAULT_STATE_DIR="$DEFAULT_STATE_DIR" CRON_LOG="$BASE" API_HEALTH_URL="$SANO" \
  WD_CRONTAB_SNAPSHOT="$TMP/crontab-base" WD_CRONTAB_LIVE_FILE="$TMP/crontab-base" \
  WD_SS_OUTPUT_FILE="$TMP/ss-sano" WD_BACKUP_DIR="$TMP/backups-sanos" \
  WD_JOURNAL_FILE="$TMP/journal-sano" WD_NOW_EPOCH=$(date -u -d '2026-08-10T14:00:00Z' +%s) \
  WD_STUCK_COUNT=39 bash "$WD" 2>/dev/null)
expect_equals "dry-run sin WD_STATE_DIR no toca el estado por defecto" \
  "$(md5sum "$DEFAULT_STATE_DIR/estrado-wd-state")" "$DEFAULT_STATE_CHECKSUM"

EMPTY_STATE_DIR=$(mktemp -d "$TMP/empty-state-XXXXXX")
printf 'no tocar\n' > "$EMPTY_STATE_DIR/estrado-wd-state"
EMPTY_STATE_CHECKSUM=$(md5sum "$EMPTY_STATE_DIR/estrado-wd-state")
OUT=$(DRY_RUN=1 WD_ENV="$TMP/watchdog.env" WD_STATE_DIR="" WD_DEFAULT_STATE_DIR="$EMPTY_STATE_DIR" \
  CRON_LOG="$BASE" API_HEALTH_URL="$SANO" WD_CRONTAB_SNAPSHOT="$TMP/crontab-base" \
  WD_CRONTAB_LIVE_FILE="$TMP/crontab-base" WD_SS_OUTPUT_FILE="$TMP/ss-sano" \
  WD_BACKUP_DIR="$TMP/backups-sanos" WD_JOURNAL_FILE="$TMP/journal-sano" \
  WD_NOW_EPOCH=$(date -u -d '2026-08-10T14:00:00Z' +%s) WD_STUCK_COUNT=39 bash "$WD" 2>/dev/null)
expect_equals "dry-run con WD_STATE_DIR vacío no toca el estado por defecto" \
  "$(md5sum "$EMPTY_STATE_DIR/estrado-wd-state")" "$EMPTY_STATE_CHECKSUM"

mkdir -p "$TMP/mktemp-fail-bin"
printf '%s\n' '#!/bin/bash' 'exit 1' > "$TMP/mktemp-fail-bin/mktemp"
chmod +x "$TMP/mktemp-fail-bin/mktemp"
MKTEMP_ERR="$TMP/mktemp-fail.err"
if env -u WD_STATE_DIR PATH="$TMP/mktemp-fail-bin:$PATH" DRY_RUN=1 WD_ENV="$TMP/watchdog.env" \
  CRON_LOG="$BASE" API_HEALTH_URL="$SANO" WD_CRONTAB_SNAPSHOT="$TMP/crontab-base" \
  WD_CRONTAB_LIVE_FILE="$TMP/crontab-base" WD_SS_OUTPUT_FILE="$TMP/ss-sano" \
  WD_BACKUP_DIR="$TMP/backups-sanos" WD_JOURNAL_FILE="$TMP/journal-sano" \
  WD_NOW_EPOCH=$(date -u -d '2026-08-10T14:00:00Z' +%s) WD_STUCK_COUNT=39 \
  bash "$WD" >/dev/null 2>"$MKTEMP_ERR"; then
  echo "  FAIL mktemp fallido aborta dry-run"; FAIL=$((FAIL+1))
else
  echo "  ok   mktemp fallido aborta dry-run"; PASS=$((PASS+1))
fi
expect_contains "mktemp fallido explica el problema" "$(<"$MKTEMP_ERR")" "estado efímero"

echo "== chequeo 9: una API ociosa NO es una API caída =="
# ESTE es el test que sostiene el diseño. El fixture tiene total_requests=0 y
# last_successful_request=null, que es literalmente lo que devolvía /api/v1/health
# durante el outage — y también lo que devuelve una semana sana, porque en 7 días
# entraron 2 búsquedas reales. Si alguna vez alguien agrega "alertar cuando no hay
# tráfico", este test se pone rojo, y esa es toda su razón de existir.
OUT=$(run "$BASE" "$SANO")
expect_missing "sin tráfico y sin éxitos: silencio" "$OUT" "api-health"
expect_missing "no inventa una falla de pool"       "$OUT" "pool-fail"

echo "== chequeo 9: el pool que no entrega sesión SÍ alerta =="
sed 's/"total_pool_failures":0/"total_pool_failures":7/' "$TMP/www/sano.json" > "$TMP/www/pool.json"
OUT=$(run "$BASE" "http://127.0.0.1:$PORT/pool.json")
expect_contains "reporta la falla del pool" "$OUT" "pool-fail"
expect_contains "dice cuántas veces"        "$OUT" "7 vez"
# El uptime va en el mensaje porque el contador se resetea con el proceso: 7 fallas en
# 2h y 7 en 4 días son incidentes distintos. 329667s del fixture = 91h.
expect_contains "sitúa el conteo en el uptime" "$OUT" "91h"

echo "== chequeo 9: la API que no contesta =="
OUT=$(run "$BASE" "http://127.0.0.1:$PORT/no-existe.json")
expect_contains "contestó mal: alerta"  "$OUT" "api-health:404"
OUT=$(run "$BASE" "http://127.0.0.1:1/health")
expect_contains "no contestó: alerta"   "$OUT" "api-health:000"

echo "== chequeo 9: contador ausente = chequeo ciego, y se dice =="
# Una instancia anterior al #21 no expone total_pool_failures. Sin este aviso el
# chequeo miraría una clave inexistente y se vería igual que uno sano.
jq 'del(.total_pool_failures)' "$TMP/www/sano.json" > "$TMP/www/viejo.json"
WDS=$(mktemp -d "$TMP/st-XXXX")
OUT=$(run "$BASE" "http://127.0.0.1:$PORT/viejo.json")
expect_contains "avisa que está ciego"        "$OUT" "pool-metric-missing"
OUT=$(run "$BASE" "http://127.0.0.1:$PORT/viejo.json")
expect_missing  "y no lo repite en la corrida siguiente" "$OUT" "pool-metric-missing"
OUT=$(run "$BASE" "$SANO")                     # vuelve a aparecer el contador
OUT=$(run "$BASE" "http://127.0.0.1:$PORT/viejo.json")
expect_contains "si desaparece de nuevo, vuelve a avisar" "$OUT" "pool-metric-missing"
unset WDS

# --- Consultas a Supabase que fallan ----------------------------------------
# `curl` que no llega y un `[]` legítimo dejan la lista de IDs vacía exactamente
# igual. Confundirlos costaba dos veces: el fallo quedaba mudo, y además se
# reescribía en vacío el archivo de "ya avisado", así que la corrida siguiente
# volvía a alertar por causas viejas.
echo "== consultar y fallar no es lo mismo que no encontrar nada =="
printf 'SUPABASE_URL=http://127.0.0.1:1\nSUPABASE_SERVICE_KEY=fake-para-el-test\n' > "$TMP/roto.env"
CAIDO=$(mktemp -d "$TMP/caido-XXXX")
FAKE_ID="11111111-2222-3333-4444-555555555555"
printf '%s\n' "$FAKE_ID" > "$CAIDO/estrado-wd-suspended"
OUT=$(DRY_RUN=1 WD_ENV="$TMP/roto.env" WD_STATE_DIR="$CAIDO" CRON_LOG="$BASE" API_HEALTH_URL="$SANO" bash "$WD" 2>/dev/null)
expect_contains "avisa que no pudo consultar las causas" "$OUT" "cases-query-fail"
expect_contains "avisa que no pudo contar"               "$OUT" "count-fail"
expect_missing  "no inventa causas suspendidas"          "$OUT" "monitoreo SUSPENDIDO"
# El de arriba es el síntoma; este es el daño real que se estaba causando.
if grep -qF "$FAKE_ID" "$CAIDO/estrado-wd-suspended"; then
  echo "  ok   NO borra el estado de 'ya avisado'"; PASS=$((PASS+1))
else
  echo "  FAIL NO borra el estado de 'ya avisado' — el archivo quedó sin el ID previo"; FAIL=$((FAIL+1))
fi

echo "== chequeo 10: crontab drift =="
cat > "$TMP/ct-snap" <<'EOF'
# comentario que no cuenta
0 11 * * * /opt/estrado-cron/run-cron.sh /api/cron/task-reminders

0 12 * * * /opt/estrado-cron/estrado-digest.sh >/dev/null 2>&1
EOF
cat > "$TMP/ct-igual" <<'EOF'
0 12 * * *   /opt/estrado-cron/estrado-digest.sh >/dev/null 2>&1
0 11 * * * /opt/estrado-cron/run-cron.sh /api/cron/task-reminders
EOF
OUT=$(WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-igual" run "$BASE")
expect_missing "comentarios, espaciado y orden no son drift" "$OUT" "crontab"

cat > "$TMP/ct-drift" <<'EOF'
0 11 * * * /opt/estrado-cron/run-cron.sh /api/cron/task-reminders
EOF
OUT=$(WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-drift" run "$BASE")
expect_contains "linea ejecutable que falta = drift" "$OUT" "crontab-drift"
expect_contains "el diff muestra la linea faltante"  "$OUT" "estrado-digest.sh"

echo "== chequeo 10: el mismo drift avisa una sola vez, la recaida re-avisa =="
WDS_CT=$(mktemp -d "$TMP/wds-ct-XXXXXX")
OUT=$(WDS="$WDS_CT" WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-drift" run "$BASE")
expect_contains "primera vez: avisa" "$OUT" "crontab-drift"
OUT=$(WDS="$WDS_CT" WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-drift" run "$BASE")
expect_missing "mismo drift: silencio" "$OUT" "crontab-drift"
OUT=$(WDS="$WDS_CT" WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-igual" run "$BASE")
expect_missing "drift resuelto: silencio" "$OUT" "crontab-drift"
OUT=$(WDS="$WDS_CT" WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-drift" run "$BASE")
expect_contains "recaida: vuelve a avisar" "$OUT" "crontab-drift"

echo "== chequeo 10: snapshot ilegible =="
OUT=$(WD_CRONTAB_SNAPSHOT="$TMP/ct-no-existe" run "$BASE")
expect_contains "snapshot ilegible: avisa la ceguera" "$OUT" "crontab-snapshot-missing"

echo "== chequeo 10: crontab vivo ilegible no es drift =="
OUT=$(WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-vivo-no-existe" run "$BASE")
expect_contains "leer y fallar se avisa como fallo" "$OUT" "crontab-live-unreadable"
expect_missing "y NO se disfraza de drift"          "$OUT" "crontab-drift"

echo "== chequeo 5: cuenta eventos, no líneas del traceback =="
cat > "$TMP/journal-un-evento.log" <<'EOF'
Aug 05 worker: {"level": "ERROR", "msg": "Heartbeat failed", "exception": "Traceback (most recent call last)"}
Aug 05 worker: Traceback (most recent call last):
Aug 05 worker: Traceback (most recent call last):
EOF
OUT=$(WD_JOURNAL_FILE="$TMP/journal-un-evento.log" run "$BASE")
expect_missing "un evento con traceback no cruza el umbral" "$OUT" "journal-err"

cat > "$TMP/journal-tres-eventos.log" <<'EOF'
Aug 05 worker: {"level": "ERROR", "msg": "fallo uno"}
Aug 05 worker: {"level": "CRITICAL", "msg": "fallo dos"}
Aug 05 worker: {"level": "ERROR", "msg": "fallo tres"}
EOF
OUT=$(WD_JOURNAL_FILE="$TMP/journal-tres-eventos.log" run "$BASE")
expect_contains "tres eventos estructurados sí alertan" "$OUT" "3 errores del worker"
expect_contains "la firma identifica el chequeo"        "$OUT" "journal-err"

echo "== chequeo 11: el backup como artefacto =="
OUT=$(WD_BACKUP_DIR="$TMP/backups-vacios" run "$BASE")
expect_contains "sin ningún tar: alerta" "$OUT" "backup-missing"

mkdir -p "$TMP/backups-viejos"
head -c 2048 /dev/zero > "$TMP/backups-viejos/estrado-20260101-000000.tar.gz"
touch -d '-30 hours' "$TMP/backups-viejos/estrado-20260101-000000.tar.gz"
OUT=$(WD_BACKUP_DIR="$TMP/backups-viejos" run "$BASE")
expect_contains "tar de 30h: alerta por viejo" "$OUT" "backup-stale"

mkdir -p "$TMP/backups-flacos"
head -c 100 /dev/zero > "$TMP/backups-flacos/estrado-20990101-000000.tar.gz"
OUT=$(WD_BACKUP_DIR="$TMP/backups-flacos" run "$BASE")
expect_contains "tar de 100 bytes: alerta por vacío" "$OUT" "backup-empty"

OUT=$(run "$BASE")
expect_missing "tar fresco y con peso: silencio" "$OUT" "backup-"

# --- Firma del cooldown en los chequeos keyed --------------------------------
echo "== firma del cooldown: una causa nueva dentro de la ventana NO se pierde =="
# El tag de los chequeos keyed lleva el hash del conjunto ACTUAL de claves. Con un
# tag fijo ("suspended" a secas), la causa B suspendida 20 min después de la causa A
# produce la MISMA firma que la alerta de A, el cooldown se la come, y como
# `nuevas_causas` ya la marcó como vista, la alerta de B no sale NUNCA.
# El Supabase falso es el http.server del harness: python ignora el query string,
# así que /sb/rest/v1/cases contesta a cualquier filtro (suspended y track-err ven
# las mismas causas — acá no molesta, y permite testear los dos tags de una).
mkdir -p "$TMP/www/sb/rest/v1"
printf 'SUPABASE_URL=http://127.0.0.1:%s/sb\nSUPABASE_SERVICE_KEY=fake-para-el-test\n' "$PORT" > "$TMP/sb.env"
printf '[{"id":"aaaa-1111","case_number":"C-111-2026"}]\n' > "$TMP/www/sb/rest/v1/cases"
WDS_SIG=$(mktemp -d "$TMP/wds-sig-XXXXXX")
OUT=$(WDS="$WDS_SIG" WD_ENV="$TMP/sb.env" run "$BASE")
expect_contains "la primera causa avisa"              "$OUT" "C-111-2026"
expect_contains "el tag de suspended lleva el hash"   "$OUT" "suspended:"
expect_contains "el de track-err tambien"             "$OUT" "track-err:"
printf '[{"id":"aaaa-1111","case_number":"C-111-2026"},{"id":"bbbb-2222","case_number":"C-222-2026"}]\n' > "$TMP/www/sb/rest/v1/cases"
OUT=$(WDS="$WDS_SIG" WD_ENV="$TMP/sb.env" run "$BASE")
expect_contains "la segunda, DENTRO del cooldown, tambien avisa" "$OUT" "C-222-2026"

echo "== firma del cooldown: lo que persiste ya avisado no re-spamea =="
# La otra mitad: la condición keyed que persiste (deduplicada) tiene que seguir
# ENTRANDO a la firma. Si sale, cualquier anomalía que se re-agrega cada corrida
# (en este entorno falso: count-fail y el heartbeat) cambia la firma y el mismo
# estado se reenvía a los 15 min en vez de a las 3h.
OUT=$(WDS="$WDS_SIG" WD_ENV="$TMP/sb.env" run "$BASE")
expect_missing "mismo estado, dentro del cooldown: silencio total" "$OUT" "ANOMALIES:"

echo "== firma del cooldown: pool-metric-missing persistente tampoco re-spamea =="
WDS_SIG2=$(mktemp -d "$TMP/wds-sig2-XXXXXX")
OUT=$(WDS="$WDS_SIG2" WD_ENV="$TMP/sb.env" run "$BASE" "http://127.0.0.1:$PORT/viejo.json")
expect_contains "primera corrida: el chequeo ciego avisa" "$OUT" "pool-metric-missing"
OUT=$(WDS="$WDS_SIG2" WD_ENV="$TMP/sb.env" run "$BASE" "http://127.0.0.1:$PORT/viejo.json")
expect_missing "persiste: silencio total" "$OUT" "ANOMALIES:"

echo "== chequeo 12: el bind de uvicorn no puede volver a internet =="
# El fixture sano ya se usa en TODAS las corridas de arriba, así que el silencio
# en el caso bueno está probado por construcción; igual se afirma explícito.
OUT=$(run "$BASE")
expect_missing "127.0.0.1:8000 no alerta" "$OUT" "api-bind-expuesto"

sed 's/127\.0\.0\.1:8000/0.0.0.0:8000/' "$TMP/ss-sano" > "$TMP/ss-expuesto"
OUT=$(WD_SS_OUTPUT_FILE="$TMP/ss-expuesto" run "$BASE")
expect_contains "0.0.0.0:8000 alerta"        "$OUT" "api-bind-expuesto"
expect_contains "y dice qué bind encontró"   "$OUT" "0.0.0.0:8000"

# El v6 pelado es el que se olvida: el unit puede estar en 127.0.0.1 y una regla
# vieja dejar el `[::]` escuchando. Tiene que alertar igual.
sed 's/127\.0\.0\.1:8000/[::]:8000/' "$TMP/ss-sano" > "$TMP/ss-v6"
OUT=$(WD_SS_OUTPUT_FILE="$TMP/ss-v6" run "$BASE")
expect_contains "[::]:8000 tambien alerta" "$OUT" "api-bind-expuesto"

# ::1 es loopback: no alerta. Si esto fallara, el chequeo estaría gritando por
# una configuración correcta, que es la forma más rápida de que lo ignoren.
sed 's/127\.0\.0\.1:8000/[::1]:8000/' "$TMP/ss-sano" > "$TMP/ss-v6-loopback"
OUT=$(WD_SS_OUTPUT_FILE="$TMP/ss-v6-loopback" run "$BASE")
expect_missing "[::1]:8000 es loopback, no alerta" "$OUT" "api-bind-expuesto"

echo "== chequeo 12: nadie en el 8000 es cosa del chequeo 9, no de este =="
grep -v ':8000' "$TMP/ss-sano" > "$TMP/ss-sin-api"
OUT=$(WD_SS_OUTPUT_FILE="$TMP/ss-sin-api" run "$BASE")
expect_missing "API caída: este chequeo calla" "$OUT" "api-bind-expuesto"

echo "== chequeo 12: leer y fallar no es lo mismo que no encontrar nada =="
# Sin esta distinción un `ss` ausente se disfraza de "no hay nada expuesto" y el
# chequeo pasa de medir a mentir. Mismo principio que el crontab ilegible.
OUT=$(WD_SS_OUTPUT_FILE="$TMP/no-existe-este-archivo" run "$BASE")
expect_contains "avisa que quedó ciego" "$OUT" "api-bind-ilegible"
expect_missing  "y no dice que está sano" "$OUT" "api-bind-expuesto"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
