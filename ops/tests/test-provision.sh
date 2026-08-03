#!/usr/bin/env bash
# Tests de ops/provision.sh. Corren EN EL LAPTOP: systemd y /etc se inyectan
# como stubs/directorios temporales, que es para eso que el script expone
# PROV_*. Mismo bootstrap que test-watchdog.sh y test-deploy.sh, copiado a
# sabiendas: test-watchdog viaja por scp al VPS como archivo único y una lib
# compartida le rompería ese modo de uso; los otros dos aceptan la copia para
# no partir la convención en dos dialectos.
#
#   ./ops/tests/test-provision.sh [ruta-a-provision.sh]
#
# Sale 0 si pasa todo. Nunca escribe fuera de un mktemp -d.
set -uo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROV="${1:-$OPS_DIR/provision.sh}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0

ok()  { echo "  ok   $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL $1"; FAIL=$((FAIL+1)); }
expect_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (esperaba '$3', vino '$2')"; }
expect_contains() {
  printf '%s' "$2" | grep -qF -- "$3" && ok "$1" || bad "$1 (no contiene '$3'; salida: ${2:-<vacía>})"
}
expect_missing() {
  printf '%s' "$2" | grep -qF -- "$3" && bad "$1 (contiene '$3' y no debería)" || ok "$1"
}

setup() { # setup <nombre> — repo falso completo y sano
  local base="$TMP/$1"
  REPO="$base/repo"; SYSD="$base/systemd"; LOG_SYSCTL="$base/systemctl.log"
  ENVF="$base/dotenv"; MON="$base/monitoring"; CRON="$base/estrado-cron"
  mkdir -p "$SYSD" "$MON" "$CRON" "$REPO/estrado-pjud-service/.venv/bin"
  touch "$CRON/run-cron.sh"
  mkdir -p "$REPO/ops"
  cp -R "$OPS_DIR/systemd" "$REPO/ops/systemd"
  cp "$OPS_DIR/env.inventory" "$REPO/ops/env.inventory"
  # .env completo: cada nombre del inventario con un valor de mentira
  grep -vE '^#|^$' "$REPO/ops/env.inventory" | sed 's/$/=valor-secreto-falso/' > "$ENVF"
  touch "$REPO/estrado-pjud-service/.venv/bin/python"
  chmod +x "$REPO/estrado-pjud-service/.venv/bin/python"
  touch "$MON/monitor.py" "$MON/resource-tracker.py"
  printf '#!/bin/bash\necho "$@" >> "%s"\nexit 0\n' "$LOG_SYSCTL" > "$base/systemctl"
  chmod +x "$base/systemctl"
  : > "$LOG_SYSCTL"
  SYSCTL="$base/systemctl"
}

run_prov() {
  OUT=$(PROV_REPO_DIR="$REPO" PROV_SYSTEMD_DIR="$SYSD" PROV_SYSTEMCTL="$SYSCTL" \
        PROV_ENV_FILE="$ENVF" PROV_REQUIRED_USERS="${REQUIRED_USERS:-$(id -un)}" \
        PROV_MONITORING_DIR="$MON" PROV_CRON_DIR="$CRON" bash "$PROV" 2>&1)
  RC=$?
}

reloads() { grep -c '^daemon-reload' "$LOG_SYSCTL" || true; }

echo "== primera corrida: instala todo, un daemon-reload, enable, exit 0"
setup fresh; run_prov
expect_eq "exit 0" "$RC" "0"
expect_eq "un daemon-reload" "$(reloads)" "1"
N_SRC=$(find "$OPS_DIR/systemd" -type f | wc -l | tr -d ' ')
N_DST=$(find "$SYSD" -type f | wc -l | tr -d ' ')
expect_eq "instaló todos los archivos (incluido el drop-in)" "$N_DST" "$N_SRC"
expect_contains "habilita las 4 units derivadas del espejo (sin slice ni drop-in)" "$(cat "$LOG_SYSCTL")" \
  "enable estrado-pjud-worker.service estrado-pjud.service legaltech-monitor.service legaltech-resource-tracker.service"
expect_contains "lo dice" "$OUT" "OK:"

echo "== segunda corrida: idempotente, sin daemon-reload"
run_prov
expect_eq "exit 0" "$RC" "0"
expect_eq "sigue habiendo UN daemon-reload (no re-instaló)" "$(reloads)" "1"
expect_contains "lo dice" "$OUT" "units al día"

echo "== unit editada a mano en el destino: se pisa con la del repo"
echo "# drift manual" >> "$SYSD/estrado-pjud.service"
run_prov
expect_eq "exit 0" "$RC" "0"
expect_eq "segundo daemon-reload por el cambio" "$(reloads)" "2"
if cmp -s "$REPO/ops/systemd/estrado-pjud.service" "$SYSD/estrado-pjud.service"; then
  ok "el destino volvió a ser el del repo"
else
  bad "el destino quedó con el drift"
fi

echo "== .env incompleto: nombra lo que falta, JAMÁS un valor, exit 1"
setup envgap
grep -v '^SUPABASE_SERVICE_KEY' "$ENVF" > "$ENVF.tmp" && mv "$ENVF.tmp" "$ENVF"
run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "nombra la variable faltante" "$OUT" "SUPABASE_SERVICE_KEY"
expect_missing "ningún valor en la salida" "$OUT" "valor-secreto-falso"
expect_contains "y pide re-correr" "$OUT" "INCOMPLETO"

echo "== .env ausente: lo dice y exit 1"
setup noenv; rm "$ENVF"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "lo dice" "$OUT" "FALTA"

echo "== venv ausente: lo dice y exit 1"
setup novenv; rm "$REPO/estrado-pjud-service/.venv/bin/python"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "receta de la venv" "$OUT" "python3 -m venv"

echo "== monitoreo externo ausente: avisa que no vive en este repo"
setup nomon; rm "$MON/monitor.py"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "lo dice" "$OUT" "no viven en este repo"

echo "== usuario requerido ausente: lo nombra y exit 1"
setup nouser
REQUIRED_USERS="$(id -un) usuario-que-no-existe-xyz" run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "nombra el usuario" "$OUT" "usuario-que-no-existe-xyz"

echo "== cron sin instalar: receta de deploy-cron.sh y exit 1"
setup nocron; rm "$CRON/run-cron.sh"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "receta" "$OUT" "deploy-cron.sh"

echo "== .env con variable extra: warning con el nombre, exit sigue 0"
setup extra; echo "VARIABLE_NUEVA_SIN_INVENTARIAR=x" >> "$ENVF"; run_prov
expect_eq "exit 0 (es warning, no error)" "$RC" "0"
expect_contains "la nombra" "$OUT" "VARIABLE_NUEVA_SIN_INVENTARIAR"

echo "== fuente rota (checkout a medias): aborta en vez de decir todo OK"
setup badsrc; rm -r "$REPO/ops/systemd"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "aborta con causa" "$OUT" "ABORTA"
expect_missing "no dice units al día" "$OUT" "units al día"

echo
echo "$PASS ok, $FAIL fail"
[ "$FAIL" -eq 0 ]
