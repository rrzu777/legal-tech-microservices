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
expect_same_file() { # <nombre> <esperado> <actual>
  cmp -s "$2" "$3" && ok "$1" || bad "$1 (los archivos difieren)"
}

setup() { # setup <nombre> — repo falso completo y sano
  local base="$TMP/$1"
  REPO="$base/repo"; SYSD="$base/systemd"; LOG_SYSCTL="$base/systemctl.log"
  SYSTEM_UNITS="$base/system-units"; SYSTEM_UNIT_USERS="$base/system-unit-users"
  USER_UNITS="$base/hermes-user-units"
  LOG_INSTALL="$base/install.log"; LOG_CHOWN="$base/chown.log"; LOG_CHMOD="$base/chmod.log"
  LOG_ID="$base/id.log"; LOG_PS="$base/ps.log"
  ENVF="$base/dotenv"; MON="$base/monitoring"; CRON="$base/estrado-cron"
  MON_ENV="$base/legaltech-monitoring.env"
  MON_STATE="$base/var/lib/legaltech-monitor"; MON_LOG="$base/var/log/legaltech"
  RESOURCE_CSV="$MON_LOG/resources.csv"
  LOGROTATE_DEST="$base/etc/logrotate.d/legaltech-resources"
  mkdir -p "$SYSD" "$MON" "$CRON" "$REPO/estrado-pjud-service/.venv/bin"
  touch "$CRON/run-cron.sh"
  mkdir -p "$REPO/ops"
  cp -R "$OPS_DIR/systemd" "$REPO/ops/systemd"
  cp -R "$OPS_DIR/systemd-templates" "$REPO/ops/systemd-templates"
  cp -R "$OPS_DIR/monitoring" "$REPO/ops/monitoring"
  if [ -d "$OPS_DIR/logrotate" ]; then
    cp -R "$OPS_DIR/logrotate" "$REPO/ops/logrotate"
  else
    mkdir -p "$REPO/ops/logrotate"
  fi
  mkdir -p "$REPO/ops/monitoring/runtime"
  printf '# recursive runtime fixture\n' > "$REPO/ops/monitoring/runtime/helper.py"
  cp "$OPS_DIR/env.inventory" "$REPO/ops/env.inventory"
  # .env completo: cada nombre del inventario con un valor de mentira
  grep -vE '^#|^$' "$REPO/ops/env.inventory" | sed 's/$/=valor-secreto-falso/' > "$ENVF"
  sed -i.bak 's|^COOKIE_STORE_PATH=.*|COOKIE_STORE_PATH=/var/lib/estrado-pjud/cookies.json|' "$ENVF"
  rm "$ENVF.bak"
  chmod 600 "$ENVF"
  touch "$REPO/estrado-pjud-service/.venv/bin/python"
  chmod +x "$REPO/estrado-pjud-service/.venv/bin/python"
  printf 'ssh.service enabled\n' > "$SYSTEM_UNITS"
  printf 'ssh.service root\n' > "$SYSTEM_UNIT_USERS"
  printf 'hermes-gateway.service enabled\nhermes-dashboard.service enabled\n' > "$USER_UNITS"
  cat > "$base/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "$LOG_SYSCTL"
if [ "\$1" = "list-unit-files" ]; then
  [ ! -e "$base/system-units.fail" ] || exit 1
  cat "$SYSTEM_UNITS"
  exit 0
fi
if [ "\$1" = "show" ]; then
  [ ! -e "$base/system-unit-users.fail" ] || exit 1
  awk -v unit="\$2" '\$1 == unit { print \$2; found=1 } END { exit !found }' "$SYSTEM_UNIT_USERS"
  exit \$?
fi
if [ "\$1" = "--user" ]; then
  [ ! -e "$base/user-units.fail" ] || exit 1
  cat "$USER_UNITS"
  exit 0
fi
if [ "\$1" = "disable" ]; then
  shift
  for unit in "\$@"; do
    [ "\$unit" = "--now" ] && continue
    unlink "$base/multi-user.target.wants/\$unit" 2>/dev/null || true
  done
  exit 0
fi
exit 0
EOF
  chmod +x "$base/systemctl"
  : > "$LOG_SYSCTL"
  SYSCTL="$base/systemctl"
  : > "$LOG_INSTALL"; : > "$LOG_CHOWN"; : > "$LOG_CHMOD"; : > "$LOG_ID"; : > "$LOG_PS"
  INSTALL_BIN="$base/install"
  cat > "$INSTALL_BIN" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PROV_INSTALL_LOG"
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-g) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
exec /usr/bin/install "${args[@]}"
EOF
  chmod +x "$INSTALL_BIN"
  CHOWN_BIN="$base/chown"
  cat > "$CHOWN_BIN" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PROV_CHOWN_LOG"
[ ! -e "$PROV_CHOWN_FAIL_FILE" ] || [ "${!#}" != "$PROV_MONITORING_ENV_FILE" ] || exit 1
exit 0
EOF
  chmod +x "$CHOWN_BIN"
  CHMOD_BIN="$base/chmod"
  cat > "$CHMOD_BIN" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PROV_CHMOD_LOG"
[ ! -e "$PROV_CHMOD_FAIL_FILE" ] || [ "${!#}" != "$PROV_MONITORING_ENV_FILE" ] || exit 1
exec /bin/chmod "$@"
EOF
  chmod +x "$CHMOD_BIN"
  printf '4242\n' > "$base/hermes.uid"
  printf 'hermes\n' > "$base/hermes.reverse"
  printf 'user@4242.service init.scope\nuser@4242.service hermes-gateway.service\nuser@4242.service hermes-dashboard.service\n' > "$base/hermes.ps"
  ID_BIN="$base/id"
  cat > "$ID_BIN" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$LOG_ID"
case "\$1" in
  -u) [ -f "$base/hermes.uid" ] || exit 1; cat "$base/hermes.uid" ;;
  -nu) cat "$base/hermes.reverse" ;;
  *) exit 2 ;;
esac
EOF
  chmod +x "$ID_BIN"
  PS_BIN="$base/ps"
  cat > "$PS_BIN" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$LOG_PS"
cat "$base/hermes.ps"
EOF
  chmod +x "$PS_BIN"
  # caddy: espejo del Caddyfile en el repo falso, binario stub y destino
  # propio. El stub SÍ se ejecuta (`caddy validate` corre antes de instalar):
  # exit 0 = config válido; el test del Caddyfile roto lo pisa con exit 1.
  cp -R "$OPS_DIR/caddy" "$REPO/ops/caddy"
  CADDYF="$base/caddy-etc/Caddyfile"
  CADDY_BIN="$base/caddy-stub"
  printf '#!/bin/bash\nexit 0\n' > "$CADDY_BIN"
  chmod +x "$CADDY_BIN"
}

run_prov() {
  OUT=$(PROV_REPO_DIR="$REPO" PROV_SYSTEMD_DIR="$SYSD" PROV_SYSTEMCTL="$SYSCTL" \
        PROV_ENV_FILE="$ENVF" PROV_REQUIRED_USERS="${REQUIRED_USERS:-$(id -un)}" \
        PROV_ENV_OWNER="${ENV_OWNER:-$(id -un)}" PROV_ENV_GROUP="${ENV_GROUP:-$(id -gn)}" \
        PROV_MONITORING_DIR="$MON" PROV_CRON_DIR="$CRON" \
        PROV_MONITORING_ENV_FILE="$MON_ENV" PROV_MONITOR_STATE_DIR="$MON_STATE" \
        PROV_MONITOR_LOG_DIR="$MON_LOG" PROV_RESOURCE_CSV="$RESOURCE_CSV" \
        PROV_LOGROTATE_DEST="$LOGROTATE_DEST" \
        PROV_ID_BIN="$ID_BIN" PROV_PS_BIN="$PS_BIN" \
        PROV_INSTALL_BIN="$INSTALL_BIN" PROV_INSTALL_LOG="$LOG_INSTALL" \
        PROV_CHOWN_BIN="$CHOWN_BIN" PROV_CHOWN_LOG="$LOG_CHOWN" \
        PROV_CHOWN_FAIL_FILE="$REPO/../chown-monitor-env.fail" \
        PROV_CHMOD_BIN="$CHMOD_BIN" PROV_CHMOD_LOG="$LOG_CHMOD" \
        PROV_CHMOD_FAIL_FILE="$REPO/../chmod-monitor-env.fail" \
        PROV_ENABLE_PJUD_WORKER="${PROV_ENABLE_PJUD_WORKER:-0}" \
        PROV_SKIP_CADDY="${PROV_SKIP_CADDY:-0}" \
        PROV_CADDY_BIN="$CADDY_BIN" PROV_CADDYFILE_DEST="$CADDYF" bash "$PROV" 2>&1)
  RC=$?
}

reloads() { grep -c '^daemon-reload' "$LOG_SYSCTL" || true; }
caddy_reloads() { grep -c '^reload caddy' "$LOG_SYSCTL" || true; }
file_mode() {
  stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"
}
file_owner() {
  stat -f '%Su' "$1" 2>/dev/null || stat -c '%U' "$1"
}
file_group() {
  stat -f '%Sg' "$1" 2>/dev/null || stat -c '%G' "$1"
}
expect_not_group_other_writable() {
  local name="$1" mode
  if [ ! -e "$2" ]; then
    bad "$name (no existe $2)"
    return
  fi
  mode=$(file_mode "$2")
  if (( (8#$mode & 8#022) == 0 )); then
    ok "$name"
  else
    bad "$name (modo $mode permite escritura de grupo/otros)"
  fi
}
unit_property() { # archivo sección propiedad
  awk -v wanted_section="$2" -v wanted_key="$3" '
    /^[[:space:]]*\[[^]]+\][[:space:]]*$/ {
      section=$0; sub(/^[[:space:]]*\[/, "", section); sub(/\][[:space:]]*$/, "", section); next
    }
    section == wanted_section {
      line=$0; sub(/^[[:space:]]*/, "", line)
      key=line; sub(/[[:space:]]*=.*/, "", key)
      if (key == wanted_key) { sub(/^[^=]*=/, "", line); sub(/[[:space:]]*$/, "", line); print line }
    }
  ' "$1"
}

echo "== API interactiva: Playwright headed tiene display, browser y tmp escribible"
API_UNIT=$(cat "$OPS_DIR/systemd/estrado-pjud.service")
expect_contains "API corre dentro de Xvfb" "$API_UNIT" "/usr/bin/xvfb-run -a"
expect_contains "API usa Chromium instalado del VPS" "$API_UNIT" \
  "Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright"
expect_contains "API declara runtime privado escribible" "$API_UNIT" \
  "RuntimeDirectory=estrado-pjud-api"
expect_contains "Playwright usa ese runtime para temporales" "$API_UNIT" \
  "Environment=TMPDIR=/run/estrado-pjud-api"
expect_contains "xvfb-run conserva binarios del sistema en PATH" "$API_UNIT" \
  ":/usr/bin:/sbin:/bin"
expect_contains "Xvfb tiene un socket Unix escribible y aislado" "$API_UNIT" \
  "PrivateTmp=true"
expect_contains "API crea el directorio persistente aunque el worker esté apagado" "$API_UNIT" \
  "StateDirectory=estrado-pjud"
expect_contains "API escribe cookies con el grupo compartido" "$API_UNIT" \
  "Group=estrado"
WORKER_UNIT=$(cat "$OPS_DIR/systemd/estrado-pjud-worker.service")
expect_contains "worker conserva escritura del grupo compartido" "$WORKER_UNIT" \
  "StateDirectoryMode=0770"

echo "== primera corrida: instala todo, un daemon-reload, enable, exit 0"
setup fresh; run_prov
expect_eq "exit 0" "$RC" "0"
expect_eq "un daemon-reload" "$(reloads)" "1"
N_SRC=$(find "$OPS_DIR/systemd" -type f | wc -l | tr -d ' ')
N_DST=$(find "$SYSD" -type f | wc -l | tr -d ' ')
expect_eq "instaló units más el drop-in Hermes renderizado" "$N_DST" "$((N_SRC + 1))"
expect_contains "habilita API y timers; deja el worker bajo gate explícito" "$(cat "$LOG_SYSCTL")" \
  "enable estrado-pjud.service legaltech-monitor.timer legaltech-resource-tracker.timer"
expect_missing "no habilita los oneshot directamente" "$(cat "$LOG_SYSCTL")" \
  "enable estrado-pjud.service legaltech-monitor.service"
expect_missing "no habilita el worker por defecto" "$(cat "$LOG_SYSCTL")" \
  "enable estrado-pjud-worker.service"
expect_contains "lo dice" "$OUT" "OK:"
expect_eq ".env queda legible por API y worker, no por otros" "$(file_mode "$ENVF")" "640"
expect_eq ".env conserva el owner declarado" "$(file_owner "$ENVF")" "$(id -un)"
expect_eq ".env conserva el grupo declarado" "$(file_group "$ENVF")" "$(id -gn)"
expect_same_file "instaló el Caddyfile del repo" "$REPO/ops/caddy/Caddyfile" "$CADDYF"
expect_eq "una recarga de caddy" "$(caddy_reloads)" "1"

echo "== monitoreo: instala código, directorios, credenciales y logrotate seguros"
expect_same_file "instala monitor.py desde el repo" "$REPO/ops/monitoring/monitor.py" "$MON/monitor.py"
expect_same_file "instala imports sibling del monitor" "$REPO/ops/monitoring/alert_policy.py" "$MON/alert_policy.py"
expect_same_file "instala Python runtime recursivamente" "$REPO/ops/monitoring/runtime/helper.py" "$MON/runtime/helper.py"
if [ -e "$MON/tests/test_monitor_cli.py" ]; then
  bad "no instala tests Python en runtime"
else
  ok "no instala tests Python en runtime"
fi
expect_not_group_other_writable "monitor.py no es escribible por grupo/otros" "$MON/monitor.py"
expect_contains "pide ownership root:root para Python" "$(cat "$LOG_INSTALL")" "-o root -g root -m 0644"
expect_eq "StateDirectory provisionado con modo restrictivo" "$(file_mode "$MON_STATE")" "750"
expect_eq "LogsDirectory provisionado con modo restrictivo" "$(file_mode "$MON_LOG")" "750"
expect_eq "CSV precreado 0640" "$(file_mode "$RESOURCE_CSV")" "640"
expect_eq "CSV nuevo nace vacío" "$(wc -c < "$RESOURCE_CSV" | tr -d ' ')" "0"
expect_contains "CSV se instala root:root" "$(cat "$LOG_INSTALL")" \
  "-o root -g root -m 0640 /dev/null $RESOURCE_CSV"
expect_eq "CSV recibe ownership exacto" \
  "$(grep -cFx "root:root $RESOURCE_CSV" "$LOG_CHOWN" || true)" "1"
expect_eq "archivo de credenciales nace 0600" "$(file_mode "$MON_ENV")" "600"
expect_eq "archivo de credenciales nace vacío" "$(wc -c < "$MON_ENV" | tr -d ' ')" "0"
expect_contains "pide credenciales root:root" "$(cat "$LOG_INSTALL")" "-o root -g root -m 0600 /dev/null $MON_ENV"
expect_eq "logrotate queda 0644" "$(file_mode "$LOGROTATE_DEST")" "644"
LOGROTATE_RULE=$(cat "$LOGROTATE_DEST")
expect_contains "logrotate diario" "$LOGROTATE_RULE" "daily"
expect_contains "logrotate conserva 14" "$LOGROTATE_RULE" "rotate 14"
expect_contains "logrotate comprime" "$LOGROTATE_RULE" "compress"
expect_contains "logrotate evita dependencia de reopen" "$LOGROTATE_RULE" "copytruncate"

echo "== runtime anidado preexistente: converge owner y modo antes del código"
setup runtimemode
mkdir -p "$MON/runtime"
chmod 0777 "$MON/runtime"
run_prov
expect_eq "exit 0" "$RC" "0"
expect_eq "runtime anidado queda 0755" "$(file_mode "$MON/runtime")" "755"
expect_contains "instala runtime dir como root restrictivo" "$(cat "$LOG_INSTALL")" \
  "-d -o root -g root -m 0755 $MON/runtime"
expect_eq "converge ownership exacto del runtime dir" \
  "$(grep -cFx "root:root $MON/runtime" "$LOG_CHOWN" || true)" "1"
expect_same_file "recién entonces instala el helper" \
  "$REPO/ops/monitoring/runtime/helper.py" "$MON/runtime/helper.py"

echo "== runtime anidado symlink: aborta antes de cualquier mutación"
setup runtimesymlink
mkdir "$TMP/runtimesymlink/outside"
ln -s "$TMP/runtimesymlink/outside" "$MON/runtime"
run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica componente runtime inseguro" "$OUT" "runtime"
expect_eq "no ejecuta install" "$(wc -l < "$LOG_INSTALL" | tr -d ' ')" "0"
expect_eq "no instala systemd" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"
expect_eq "no escribe código tras el symlink" \
  "$(find "$TMP/runtimesymlink/outside" -type f | wc -l | tr -d ' ')" "0"
expect_missing "no hace reload ni enable" "$(cat "$LOG_SYSCTL")" "daemon-reload"

echo "== units y timers instalados: contrato observable completo"
setup unitcontract; run_prov
expect_eq "fixture sano para contrato instalado" "$RC" "0"
for monitor in legaltech-monitor legaltech-resource-tracker; do
  UNIT="$SYSD/$monitor.service"
  expect_eq "$monitor es oneshot" "$(unit_property "$UNIT" Service Type)" "oneshot"
  expect_eq "$monitor queda fuera del slice vigilado" "$(unit_property "$UNIT" Service Slice)" "system.slice"
  expect_eq "$monitor MemoryMax" "$(unit_property "$UNIT" Service MemoryMax)" "128M"
  expect_eq "$monitor CPUQuota" "$(unit_property "$UNIT" Service CPUQuota)" "20%"
  expect_eq "$monitor TasksMax" "$(unit_property "$UNIT" Service TasksMax)" "64"
  expect_contains "$monitor llama --once explícito" "$(unit_property "$UNIT" Service ExecStart)" "--once"
  expect_missing "$monitor no contiene credenciales" "$(cat "$UNIT")" "valor-secreto-falso"

  if [ "$monitor" = "legaltech-monitor" ]; then
    expect_eq "monitor usa env externo opcional" "$(unit_property "$UNIT" Service EnvironmentFile)" "-/etc/legaltech-monitoring.env"
    expect_eq "monitor crea StateDirectory" "$(unit_property "$UNIT" Service StateDirectory)" "legaltech-monitor"
    expect_eq "monitor restringe StateDirectory" "$(unit_property "$UNIT" Service StateDirectoryMode)" "0750"
    expect_eq "monitor crea LogsDirectory" "$(unit_property "$UNIT" Service LogsDirectory)" "legaltech"
    expect_eq "monitor restringe LogsDirectory" "$(unit_property "$UNIT" Service LogsDirectoryMode)" "0750"
    expect_eq "monitor permite sólo estado/logs" "$(unit_property "$UNIT" Service ReadWritePaths)" "/var/lib/legaltech-monitor /var/log/legaltech"
    expect_eq "monitor conserva red para Telegram" "$(unit_property "$UNIT" Service RestrictAddressFamilies)" ""
  else
    expect_eq "tracker no recibe credenciales" "$(unit_property "$UNIT" Service EnvironmentFile)" ""
    expect_eq "tracker no recibe StateDirectory" "$(unit_property "$UNIT" Service StateDirectory)" ""
    expect_eq "tracker no recibe LogsDirectory" "$(unit_property "$UNIT" Service LogsDirectory)" ""
    expect_eq "tracker escribe sólo el CSV" "$(unit_property "$UNIT" Service ReadWritePaths)" "/var/log/legaltech/resources.csv"
    expect_eq "tracker sólo puede abrir sockets AF_UNIX" "$(unit_property "$UNIT" Service RestrictAddressFamilies)" "AF_UNIX"
  fi

  TIMER="$SYSD/$monitor.timer"
  expect_eq "$monitor timer arranca tras 5min" "$(unit_property "$TIMER" Timer OnBootSec)" "5min"
  expect_eq "$monitor timer repite cada 5min" "$(unit_property "$TIMER" Timer OnUnitActiveSec)" "5min"
  expect_eq "$monitor timer es persistente" "$(unit_property "$TIMER" Timer Persistent)" "true"
  expect_eq "$monitor timer agrega jitter" "$(unit_property "$TIMER" Timer RandomizedDelaySec)" "60s"
  expect_eq "$monitor timer no declara Slice" "$(unit_property "$TIMER" Timer Slice)" ""
  expect_missing "$monitor timer no pertenece a legaltech.slice" "$(cat "$TIMER")" "legaltech.slice"
done

echo "== Hermes: UID dinámico validado y template renderizado"
HERMES_DROPIN="$SYSD/user-4242.slice.d/50-legaltech-resource-limits.conf"
expect_same_file "drop-in usa el template UID-free" "$REPO/ops/systemd-templates/hermes-user.slice.conf" "$HERMES_DROPIN"
expect_contains "resuelve id -u hermes" "$(cat "$LOG_ID")" "-u hermes"
expect_contains "valida reverse mapping del UID" "$(cat "$LOG_ID")" "-nu 4242"
expect_contains "inspecciona units/procesos del UID" "$(cat "$LOG_PS")" "-U 4242 -o unit=,uunit="
expect_contains "enumera units de sistema habilitadas" "$(cat "$LOG_SYSCTL")" "list-unit-files --type=service --state=enabled"
expect_contains "lee el User efectivo de cada unit de sistema" "$(cat "$LOG_SYSCTL")" "show ssh.service --property=User --value"
expect_contains "enumera units de usuario habilitadas aunque estén inactivas" "$(cat "$LOG_SYSCTL")" "--user --machine=hermes@.host list-unit-files --type=service --state=enabled"

echo "== segunda corrida: idempotente, sin daemon-reload ni recarga de caddy"
printf 'TOKEN_Y_CHAT_SE_CONFIGURAN_FUERA_DEL_REPO=preservar\n' > "$MON_ENV"
printf 'csv-existente-preservado\n' > "$RESOURCE_CSV"
run_prov
expect_eq "exit 0" "$RC" "0"
expect_eq "sigue habiendo UN daemon-reload (no re-instaló)" "$(reloads)" "1"
expect_eq "sigue habiendo UNA recarga de caddy" "$(caddy_reloads)" "1"
expect_contains "no truncó credenciales existentes" "$(cat "$MON_ENV")" "TOKEN_Y_CHAT_SE_CONFIGURAN_FUERA_DEL_REPO=preservar"
expect_contains "no truncó CSV existente" "$(cat "$RESOURCE_CSV")" "csv-existente-preservado"
expect_contains "lo dice" "$OUT" "units al día"

echo "== habilitación del worker: requiere flag explícito"
setup workeroptin
PROV_ENABLE_PJUD_WORKER=1 run_prov
expect_eq "exit 0" "$RC" "0"
expect_contains "habilita worker sólo con opt-in" "$(cat "$LOG_SYSCTL")" \
  "enable estrado-pjud-worker.service estrado-pjud.service legaltech-monitor.timer legaltech-resource-tracker.timer"

echo "== migración legacy: deshabilita oneshots antes de timers"
setup legacyenable
mkdir -p "$TMP/legacyenable/multi-user.target.wants"
ln -s "$SYSD/legaltech-monitor.service" \
  "$TMP/legacyenable/multi-user.target.wants/legaltech-monitor.service"
ln -s "$SYSD/legaltech-resource-tracker.service" \
  "$TMP/legacyenable/multi-user.target.wants/legaltech-resource-tracker.service"
run_prov
expect_eq "exit 0" "$RC" "0"
expect_contains "disable explícito" "$(cat "$LOG_SYSCTL")" \
  "disable legaltech-monitor.service legaltech-resource-tracker.service"
if [ ! -e "$TMP/legacyenable/multi-user.target.wants/legaltech-monitor.service" ] \
  && [ ! -L "$TMP/legacyenable/multi-user.target.wants/legaltech-monitor.service" ] \
  && [ ! -e "$TMP/legacyenable/multi-user.target.wants/legaltech-resource-tracker.service" ] \
  && [ ! -L "$TMP/legacyenable/multi-user.target.wants/legaltech-resource-tracker.service" ]; then
  ok "elimina ambos enables legacy"
else
  bad "elimina ambos enables legacy"
fi
DISABLE_LINE=$(grep -n '^disable legaltech-monitor.service' "$LOG_SYSCTL" | cut -d: -f1)
ENABLE_LINE=$(grep -n '^enable estrado-pjud.service' "$LOG_SYSCTL" | cut -d: -f1)
if [ -n "$DISABLE_LINE" ] && [ -n "$ENABLE_LINE" ] \
  && [ "$DISABLE_LINE" -lt "$ENABLE_LINE" ]; then
  ok "deshabilita legacy antes de habilitar timers"
else
  bad "deshabilita legacy antes de habilitar timers"
fi

echo "== Hermes ausente: falla antes de instalar o habilitar"
setup nohermes; rm "$TMP/nohermes/hermes.uid"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica ausencia de Hermes" "$OUT" "hermes"
expect_eq "no instala systemd" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"
expect_missing "no habilita nada" "$(cat "$LOG_SYSCTL")" "enable"

echo "== UID Hermes no numérico: falla cerrado"
setup baduid; printf 'no-es-uid\n' > "$TMP/baduid/hermes.uid"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica UID inválido" "$OUT" "UID"
expect_eq "no instala drop-in" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"

echo "== reverse lookup distinto de Hermes: falla cerrado"
setup reverse; printf 'otro-usuario\n' > "$TMP/reverse/hermes.reverse"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica reverse lookup" "$OUT" "reverse"
expect_eq "no instala drop-in" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"

echo "== unit persistente inesperada de Hermes: falla cerrado"
setup rogue; printf 'user@4242.service rogue-daemon.service\n' > "$TMP/rogue/hermes.ps"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "nombra sólo la unit inesperada" "$OUT" "rogue-daemon.service"
expect_eq "no instala drop-in" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"
expect_missing "no habilita nada" "$(cat "$LOG_SYSCTL")" "enable"

echo "== unit de usuario habilitada pero inactiva: falla cerrado"
setup inactiverogue; printf 'rogue-inactive.service enabled\n' >> "$USER_UNITS"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "nombra la unit inactiva inesperada" "$OUT" "rogue-inactive.service"
expect_eq "no instala systemd" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"
expect_missing "no habilita nada" "$(cat "$LOG_SYSCTL")" "enable estrado-pjud.service"

echo "== unit de sistema inactiva con User=hermes: falla cerrado"
setup systemrogue
printf 'rogue-system.service enabled\n' >> "$SYSTEM_UNITS"
printf 'rogue-system.service hermes\n' >> "$SYSTEM_UNIT_USERS"
run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "nombra la unit de sistema inesperada" "$OUT" "rogue-system.service"
expect_eq "no instala systemd" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"

echo "== unit de sistema inactiva con User=<UID Hermes>: falla cerrado"
setup numericuserrogue
printf 'rogue-numeric.service enabled\n' >> "$SYSTEM_UNITS"
printf 'rogue-numeric.service 4242\n' >> "$SYSTEM_UNIT_USERS"
run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "nombra la unit con User numérico" "$OUT" "rogue-numeric.service"
expect_eq "no instala systemd" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"
expect_missing "no habilita timers" "$(cat "$LOG_SYSCTL")" "enable estrado-pjud.service"

echo "== enumeración persistente falla: aborta cerrado"
setup enumfail; touch "$TMP/enumfail/user-units.fail"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica que no pudo enumerar" "$OUT" "enumerar"
expect_eq "no instala systemd" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"

echo "== env de monitoreo symlink: aborta antes de cualquier mutación"
setup monenvlink
touch "$TMP/monenvlink/credential-target"
ln -s "$TMP/monenvlink/credential-target" "$MON_ENV"
run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica path inseguro" "$OUT" "archivo regular"
expect_eq "no ejecuta install" "$(wc -l < "$LOG_INSTALL" | tr -d ' ')" "0"
expect_eq "no instala systemd" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"
expect_eq "no hace daemon-reload" "$(reloads)" "0"
expect_missing "no habilita timers" "$(cat "$LOG_SYSCTL")" "enable estrado-pjud.service"

echo "== env de monitoreo directorio: aborta antes de cualquier mutación"
setup monenvdir; mkdir "$MON_ENV"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica path inseguro" "$OUT" "archivo regular"
expect_eq "no ejecuta install" "$(wc -l < "$LOG_INSTALL" | tr -d ' ')" "0"
expect_eq "no instala systemd" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"
expect_eq "no hace daemon-reload" "$(reloads)" "0"
expect_missing "no habilita timers" "$(cat "$LOG_SYSCTL")" "enable estrado-pjud.service"

echo "== CSV de recursos symlink: aborta antes de cualquier mutación"
setup csvsymlink
mkdir -p "$MON_LOG"
touch "$TMP/csvsymlink/csv-target"
ln -s "$TMP/csvsymlink/csv-target" "$RESOURCE_CSV"
run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica CSV inseguro" "$OUT" "CSV"
expect_eq "no ejecuta install" "$(wc -l < "$LOG_INSTALL" | tr -d ' ')" "0"
expect_eq "no instala systemd" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"
expect_eq "cero daemon-reload" "$(reloads)" "0"
if [ -e "$CADDYF" ]; then bad "no muta Caddy"; else ok "no muta Caddy"; fi
expect_missing "no deshabilita servicios" "$(cat "$LOG_SYSCTL")" "disable legaltech-monitor.service"
expect_missing "no habilita timers" "$(cat "$LOG_SYSCTL")" "enable estrado-pjud.service"

echo "== chown de credenciales falla: no activa configuración parcial"
setup monenvchownfail; touch "$TMP/monenvchownfail/chown-monitor-env.fail"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica fallo de ownership" "$OUT" "owner root:root"
expect_eq "cero daemon-reload" "$(reloads)" "0"
if [ -e "$CADDYF" ]; then bad "no muta Caddy"; else ok "no muta Caddy"; fi
expect_missing "no deshabilita servicios" "$(cat "$LOG_SYSCTL")" "disable legaltech-monitor.service"
expect_missing "no habilita timers" "$(cat "$LOG_SYSCTL")" "enable estrado-pjud.service"

echo "== chmod de credenciales falla: no activa configuración parcial"
setup monenvchmodfail; touch "$TMP/monenvchmodfail/chmod-monitor-env.fail"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica fallo de modo" "$OUT" "modo 0600"
expect_eq "cero daemon-reload" "$(reloads)" "0"
if [ -e "$CADDYF" ]; then bad "no muta Caddy"; else ok "no muta Caddy"; fi
expect_missing "no deshabilita servicios" "$(cat "$LOG_SYSCTL")" "disable legaltech-monitor.service"
expect_missing "no habilita timers" "$(cat "$LOG_SYSCTL")" "enable estrado-pjud.service"

echo "== unit editada a mano en el destino: se pisa con la del repo"
setup drift; run_prov
echo "# drift manual" >> "$SYSD/estrado-pjud.service"
run_prov
expect_eq "exit 0" "$RC" "0"
expect_eq "segundo daemon-reload por el cambio" "$(reloads)" "2"
expect_same_file "el destino volvió a ser el del repo" "$REPO/ops/systemd/estrado-pjud.service" "$SYSD/estrado-pjud.service"

echo "== Caddyfile editado a mano en el destino: se pisa y recarga"
echo "# drift manual" >> "$CADDYF"
run_prov
expect_eq "exit 0" "$RC" "0"
expect_eq "segunda recarga de caddy por el drift" "$(caddy_reloads)" "2"
expect_same_file "el Caddyfile volvió a ser el del repo" "$REPO/ops/caddy/Caddyfile" "$CADDYF"

echo "== modo resource-guards omite Caddy sin omitir artefactos de recursos"
setup skipcaddy
rm "$REPO/ops/caddy/Caddyfile"
PROV_SKIP_CADDY=1 run_prov
expect_eq "skip Caddy sale 0 aunque la fuente Caddy no esté" "$RC" "0"
if [ -f "$SYSD/legaltech.slice" ] && [ -f "$SYSD/legaltech-monitor.timer" ]; then
  ok "skip Caddy igual instala slice y timers"
else
  bad "skip Caddy igual instala slice y timers"
fi
if [ ! -e "$CADDYF" ]; then ok "skip Caddy no instala Caddyfile"; else bad "skip Caddy no instala Caddyfile"; fi
expect_eq "skip Caddy no recarga, inicia ni reinicia Caddy" \
  "$(grep -cE '^(reload|start|restart) caddy' "$LOG_SYSCTL" || true)" "0"

setup invalidskip
PROV_SKIP_CADDY=2 run_prov
expect_eq "skip Caddy inválido falla cerrado" "$RC" "1"
expect_eq "skip Caddy inválido no instala recursos" "$(find "$SYSD" -type f | wc -l | tr -d ' ')" "0"

echo "== caddy: binario ausente = receta y exit 1"
setup nocaddy; rm "$CADDY_BIN"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "receta" "$OUT" "apt-get install caddy"
expect_eq "sin binario no se recarga nada" "$(caddy_reloads)" "0"

echo "== caddy: Caddyfile que no valida NO llega a /etc ni reinicia nada"
# El reload gracioso de Caddy rechaza un config inválido y sigue sirviendo el
# viejo; un restart de fallback contra el archivo roto mataría ese proceso
# sano. Por eso se valida ANTES de instalar y un config roto no toca nada.
setup caddyroto
printf '#!/bin/bash\nexit 1\n' > "$CADDY_BIN"   # `caddy validate` que rechaza
run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "lo dice" "$OUT" "no pasa"
if [ -e "$CADDYF" ]; then
  bad "el Caddyfile roto NO debe instalarse"
else
  ok "el Caddyfile roto no se instaló"
fi
expect_eq "y no se recarga ni reinicia caddy" "$(grep -cE '^(reload|restart|start) caddy' "$LOG_SYSCTL" || true)" "0"

echo "== caddy: con la unit parada se levanta en vez de recargar"
setup caddyparado
printf '#!/bin/bash\necho "$@" >> "%s"\n[ "$1" = is-active ] && exit 1\nexit 0\n' "$LOG_SYSCTL" > "$SYSCTL"
run_prov
expect_eq "exit 0" "$RC" "0"
expect_contains "la levanta" "$(cat "$LOG_SYSCTL")" "start caddy"
expect_eq "sin recarga sobre unit parada" "$(caddy_reloads)" "0"

echo "== caddy: Caddyfile ausente del repo = checkout a medias, aborta"
setup nocaddyfile; rm "$REPO/ops/caddy/Caddyfile"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "aborta con causa" "$OUT" "ABORTA"

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

echo "== chown del .env falla: igual restringe el modo y sale 1"
setup badenvowner; chmod 666 "$ENVF"
ENV_OWNER="usuario-que-no-existe-xyz" run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "explica el owner inválido" "$OUT" "NO SE PUDO"
expect_eq "no deja el secreto expuesto" "$(file_mode "$ENVF")" "640"

echo "== cookie store dentro del checkout: falla cerrado sin imprimir el valor"
setup cookiestore
sed -i.bak 's|^COOKIE_STORE_PATH=.*|COOKIE_STORE_PATH=./.cookies.json|' "$ENVF"
rm "$ENVF.bak"
run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "exige el StateDirectory compartido" "$OUT" "/var/lib/estrado-pjud/cookies.json"
expect_missing "no imprime el path inseguro" "$OUT" "./.cookies.json"

echo "== venv ausente: lo dice y exit 1"
setup novenv; rm "$REPO/estrado-pjud-service/.venv/bin/python"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "receta de la venv" "$OUT" "python3 -m venv"

echo "== fuente de monitoreo ausente: checkout a medias, aborta"
setup nomon; rm "$REPO/ops/monitoring/monitor.py"; run_prov
expect_eq "exit 1" "$RC" "1"
expect_contains "lo dice" "$OUT" "ABORTA"

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
