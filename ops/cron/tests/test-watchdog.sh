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

# Crontab "vivo" y snapshot idénticos: el chequeo 10 calla salvo en sus tests.
printf '0 11 * * * /opt/estrado-cron/run-cron.sh /api/cron/task-reminders\n' > "$TMP/crontab-base"

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
printf '%s /api/cron/task-reminders - HTTP 200\n' "$HOY" > "$TMP/ct.log"
OUT=$(WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-igual" run "$TMP/ct.log")
expect_missing "comentarios, espaciado y orden no son drift" "$OUT" "crontab"

cat > "$TMP/ct-drift" <<'EOF'
0 11 * * * /opt/estrado-cron/run-cron.sh /api/cron/task-reminders
EOF
OUT=$(WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-drift" run "$TMP/ct.log")
expect_contains "linea ejecutable que falta = drift" "$OUT" "crontab-drift"
expect_contains "el diff muestra la linea faltante"  "$OUT" "estrado-digest.sh"

echo "== chequeo 10: el mismo drift avisa una sola vez, la recaida re-avisa =="
WDS_CT=$(mktemp -d "$TMP/wds-ct-XXXXXX")
OUT=$(WDS="$WDS_CT" WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-drift" run "$TMP/ct.log")
expect_contains "primera vez: avisa" "$OUT" "crontab-drift"
OUT=$(WDS="$WDS_CT" WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-drift" run "$TMP/ct.log")
expect_missing "mismo drift: silencio" "$OUT" "crontab-drift"
OUT=$(WDS="$WDS_CT" WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-igual" run "$TMP/ct.log")
expect_missing "drift resuelto: silencio" "$OUT" "crontab-drift"
OUT=$(WDS="$WDS_CT" WD_CRONTAB_SNAPSHOT="$TMP/ct-snap" WD_CRONTAB_LIVE_FILE="$TMP/ct-drift" run "$TMP/ct.log")
expect_contains "recaida: vuelve a avisar" "$OUT" "crontab-drift"

echo "== chequeo 10: snapshot ilegible =="
OUT=$(WD_CRONTAB_SNAPSHOT="$TMP/ct-no-existe" run "$TMP/ct.log")
expect_contains "snapshot ilegible: avisa la ceguera" "$OUT" "crontab-snapshot-missing"


echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
