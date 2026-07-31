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
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0

run() { DRY_RUN=1 WD_STATE_DIR="$TMP" CRON_LOG="$1" bash "$WD" 2>/dev/null; }

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

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
