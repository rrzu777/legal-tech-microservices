#!/usr/bin/env bash
# Tests de ops/deploy.sh. Corren EN EL LAPTOP: todo lo que en el VPS es real
# (git origin, .venv, systemctl, health HTTP) acá se inyecta como stub, que es
# exactamente para lo que el script expone DEPLOY_REPO_DIR/DEPLOY_SYSTEMCTL/
# DEPLOY_HEALTH_URL/etc.
#
#   ./ops/tests/test-deploy.sh [ruta-a-deploy.sh]
#
# El bootstrap (mktemp, contadores, http.server) es el mismo de
# ops/cron/tests/test-watchdog.sh a propósito y SIN extraer a una lib: ese test
# viaja por scp al VPS como archivo único y una lib compartida le rompería el
# modo de uso. A la tercera copia, extraer.
#
# Sale 0 si pasa todo. Nunca escribe fuera de un mktemp -d.
set -uo pipefail

DEPLOY="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy.sh}"
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=maintenance-fixture.sh
source "$OPS_DIR/tests/maintenance-fixture.sh"
TMP=$(mktemp -d)
PASS=0; FAIL=0

# Health por HTTP de verdad (mismo razonamiento que test-watchdog.sh): con
# file:// curl sale 000 y no distingue "contestó mal" de "no contestó".
PORT=$((18000 + RANDOM % 1000))
mkdir -p "$TMP/www"
( cd "$TMP/www" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 ) &
HTTPD=$!
trap 'kill "$HTTPD" 2>/dev/null; rm -rf "$TMP"' EXIT
for _ in $(seq 1 50); do curl -sf -m 1 "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break; sleep 0.1; done

health_ok()  { echo '{"status":"ok"}' > "$TMP/www/health.json"; }
health_bad() { rm -f "$TMP/www/health.json"; }
HEALTH="http://127.0.0.1:$PORT/health.json"

ok()  { echo "  ok   $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL $1"; FAIL=$((FAIL+1)); }
expect_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (esperaba '$3', vino '$2')"; }
expect_contains() {
  printf '%s' "$2" | grep -qF -- "$3" && ok "$1" || bad "$1 (no contiene '$3'; salida: ${2:-<vacía>})"
}
expect_missing() {
  printf '%s' "$2" | grep -qF -- "$3" && bad "$1 (contiene '$3' y no debería)" || ok "$1"
}
file_mode() {
  if stat --version >/dev/null 2>&1; then stat -c '%a' "$1"; else stat -f '%Lp' "$1"; fi
}

# Cada test estrena un par origin/clone. El clone es DEPLOY_REPO_DIR; los stubs
# viven como untracked adentro (sobreviven al reset --hard, igual que el .venv
# real).
setup() { # setup <nombre> — deja stubs y logs del deploy
  local base="$TMP/$1"
  rm -rf "$TMP/ms-playwright"
  mkdir -p "$TMP/ms-playwright"
  ORIGIN="$base/origin"; REPO="$base/repo"
  LOG_SYSCTL="$base/systemctl.log"; LOG_PIP="$base/pip.log"; LOG_PYTHON="$base/python.log"
  LOG_XVFB="$base/xvfb.log"; LOG_RUNUSER="$base/runuser.log"
  mkdir -p "$ORIGIN/estrado-pjud-service"
  mkdir -p "$ORIGIN/estrado-pjud-service/worker" "$ORIGIN/estrado-pjud-service/app" "$ORIGIN/ops"
  cp "$OPS_DIR/../estrado-pjud-service/worker/maintenance.py" \
    "$OPS_DIR/../estrado-pjud-service/worker/maintenance_store.py" \
    "$OPS_DIR/../estrado-pjud-service/worker/__main__.py" \
    "$OPS_DIR/../estrado-pjud-service/worker/sd_notify.py" \
    "$OPS_DIR/../estrado-pjud-service/worker/metrics.py" "$ORIGIN/estrado-pjud-service/worker/"
  cp "$OPS_DIR/../estrado-pjud-service/worker/__init__.py" \
    "$OPS_DIR/../estrado-pjud-service/worker/config.py" \
    "$OPS_DIR/../estrado-pjud-service/worker/session_pool.py" "$ORIGIN/estrado-pjud-service/worker/"
  cp "$OPS_DIR/../estrado-pjud-service/app/__init__.py" \
    "$OPS_DIR/../estrado-pjud-service/app/r2.py" \
    "$OPS_DIR/../estrado-pjud-service/app/minter.py" "$ORIGIN/estrado-pjud-service/app/"
  mkdir -p "$ORIGIN/estrado-pjud-service/app/ojv"
  cp "$OPS_DIR/../estrado-pjud-service/app/ojv/__init__.py" \
    "$OPS_DIR/../estrado-pjud-service/app/ojv/session.py" \
    "$OPS_DIR/../estrado-pjud-service/app/ojv/browser_login.py" "$ORIGIN/estrado-pjud-service/app/ojv/"
  cp "$OPS_DIR/../estrado-pjud-service/app/playwright_runtime.py" "$ORIGIN/estrado-pjud-service/app/"
  cp "$OPS_DIR/../estrado-pjud-service/worker/maintenance_heartbeat.py" \
    "$OPS_DIR/../estrado-pjud-service/worker/proxy_control.py" "$ORIGIN/estrado-pjud-service/worker/"
  cp "$OPS_DIR/worker-maintenance.py" "$OPS_DIR/worker-maintenance.sh" "$ORIGIN/ops/"
  git -C "$ORIGIN" init -q -b main
  git -C "$ORIGIN" config user.email t@t
  git -C "$ORIGIN" config user.name t
  printf 'httpx==0.27\nplaywright==1.61.0\n' > "$ORIGIN/estrado-pjud-service/requirements.txt"
  printf '.venv/\n' > "$ORIGIN/estrado-pjud-service/.gitignore"
  git -C "$ORIGIN" add estrado-pjud-service ops
  git -C "$ORIGIN" commit -q -m base
  git clone -q "$ORIGIN" "$REPO"

  local venv="$REPO/estrado-pjud-service/.venv/bin"
  mkdir -p "$venv"
  printf '#!/bin/bash\necho "$@" >> "%s"\nif [ "$1" = -m ] && [ "$2" = playwright ]; then exit "${FAKE_PLAYWRIGHT_INSTALL_EXIT:-0}"; fi\nif [ "$1" = -c ]; then exit "${FAKE_PLAYWRIGHT_VERIFY_EXIT:-0}"; fi\nexit "${FAKE_PYTEST_EXIT:-0}"\n' "$LOG_PYTHON" > "$venv/python"
  printf '#!/bin/bash\necho "$@" >> "%s"\nexit "${FAKE_PIP_EXIT:-0}"\n' "$LOG_PIP" > "$venv/pip"
  printf '#!/bin/bash\necho "$@" >> "%s"\n[ "${FAKE_XVFB_EXIT:-0}" = 0 ] || exit "$FAKE_XVFB_EXIT"\n[ "$1" = -a ] && shift\nexec "$@"\n' "$LOG_XVFB" > "$base/xvfb-run"
  printf '#!/bin/bash\necho "$@" >> "%s"\n[ "$1" = -u ] || exit 64\nshift 2\n[ "$1" = -- ] || exit 64\nshift\nexec "$@"\n' "$LOG_RUNUSER" > "$base/runuser"
  printf '#!/bin/bash\n[ "${FAKE_FIND_EXIT:-0}" = 0 ] || exit "$FAKE_FIND_EXIT"\nexec /usr/bin/find "$@"\n' > "$base/find"
  printf '#!/bin/bash\necho "$@" >> "%s"\nSTATE="%s/worker-disabled"\nFORCE_ACTIVE="%s/worker-force-active"\nif [ "${FAKE_SYSTEMCTL_STATE_UNKNOWN:-0}" = 1 ] && { [ "$1" = is-enabled ] || [ "$1" = is-active ]; }; then exit 4; fi\nif [ "$1" = disable ]; then touch "$STATE"; rm -f "$FORCE_ACTIVE"; exit "${FAKE_SYSTEMCTL_EXIT:-0}"; fi\nif [ "$1" = stop ] && [ "$2" = estrado-pjud-worker.service ]; then rm -f "$FORCE_ACTIVE"; exit 0; fi\nif [ "$1" = is-active ] && { [ "$2" = estrado-pjud-worker.service ] || [ "${3:-}" = estrado-pjud-worker.service ]; }; then [ -f "$FORCE_ACTIVE" ] && { echo active; exit 0; }; [ -f "$STATE" ] && { echo inactive; exit 3; }; echo active; exit 0; fi\nif [ "$1" = is-enabled ]; then [ -f "$STATE" ] && { echo disabled; exit 1; }; echo enabled; exit 0; fi\nif [ "$1" = restart ]; then exit "${FAKE_SYSTEMCTL_EXIT:-0}"; fi\nif [ "$1" = is-active ]; then echo active; exit 0; fi\nexit 0\n' "$LOG_SYSCTL" "$base" "$base" > "$base/systemctl"
  chmod +x "$venv/python" "$venv/pip" "$base/systemctl" "$base/xvfb-run" "$base/runuser" "$base/find"
  : > "$LOG_SYSCTL"; : > "$LOG_PIP"; : > "$LOG_PYTHON"; : > "$LOG_XVFB"; : > "$LOG_RUNUSER"
  SYSCTL="$base/systemctl"; XVFB="$base/xvfb-run"; RUNUSER="$base/runuser"; FIND="$base/find"
}

avanza_origin() { # avanza_origin [archivo] — commit B en origin
  local f="${1:-cambio.txt}"
  mkdir -p "$ORIGIN/$(dirname "$f")"
  echo x >> "$ORIGIN/$f"
  git -C "$ORIGIN" add "$f"
  git -C "$ORIGIN" commit -q -m B
}

avanza_playwright() {
  sed -i.bak 's/playwright==1.61.0/playwright==1.62.0/' \
    "$ORIGIN/estrado-pjud-service/requirements.txt"
  rm "$ORIGIN/estrado-pjud-service/requirements.txt.bak"
  git -C "$ORIGIN" add estrado-pjud-service/requirements.txt
  git -C "$ORIGIN" commit -q -m playwright
}

# Sin set -e en este archivo: RC se captura a mano y los asserts guardan solos.
run_deploy() { # run_deploy — usa REPO/SYSCTL/HEALTH; deja RC y OUT
  maintenance_fixture "$(dirname "$LOG_SYSCTL")" "$OPS_DIR"
  mkdir -p "$TMP/state" "$TMP/ms-playwright"
  OUT=$(DEPLOY_REPO_DIR="$REPO" DEPLOY_SYSTEMCTL="$SYSCTL" DEPLOY_HEALTH_URL="$HEALTH" \
        DEPLOY_STATE_DIR="$TMP/state" \
        DEPLOY_PLAYWRIGHT_BROWSERS_PATH="${BROWSER_PATH:-$TMP/ms-playwright}" \
        DEPLOY_ALLOW_TEST_BROWSER_PATH="${ALLOW_TEST_BROWSER_PATH:-1}" \
        DEPLOY_BROWSER_OWNER_UID="$(id -u)" DEPLOY_BROWSER_OWNER_GID="$(id -g)" \
        DEPLOY_XVFB_RUN="$XVFB" DEPLOY_RUNUSER="$RUNUSER" DEPLOY_FIND="$FIND" \
        DEPLOY_KEEP_WORKER_STOPPED="${KEEP_WORKER_STOPPED:-0}" \
        DEPLOY_HEALTH_RETRIES=2 DEPLOY_HEALTH_SLEEP=0 bash "$DEPLOY" 2>&1)
  RC=$?
}

sha_origin() { git -C "$ORIGIN" rev-parse main; }
sha_repo()   { git -C "$REPO" rev-parse HEAD; }
# Las dos units van en UNA invocación (systemd encola ambos jobs): una línea
# "restart a b" por ronda.
restarts()   { grep -c '^restart ' "$LOG_SYSCTL" || true; }

run_maintenance_review_regressions() {
  local scenario new_identity path change
  setup api-only-maintenance; avanza_origin estrado-pjud-service/app/api_only.py; health_ok
  run_deploy
  expect_eq 'unrelated API-only change remains deployable' "$RC" 0
  expect_eq 'API-only change merges expected revision' "$(sha_repo)" "$(sha_origin)"
  expect_eq 'API-only compatible change restarts normally' "$(restarts)" 1
  for scenario in pid nonce; do
    setup "drift-$scenario"; avanza_origin; health_ok
    case "$scenario" in
      pid) new_identity='f784c8bd-67c3-448e-ae1c-55ac6feab947:514:9014:bf763d76-b99c-464d-80d8-bcbd9520b923' ;;
      nonce) new_identity='f784c8bd-67c3-448e-ae1c-55ac6feab947:512:9012:ab763d76-b99c-464d-80d8-bcbd9520b923' ;;
    esac
    printf '#!/bin/bash\nif [ "$1" = -m ] && [ "$2" = pytest ]; then printf "%%s\\n" "%s" > "%s/maintenance-identity"; fi\nexit 0\n' \
      "$new_identity" "$(dirname "$LOG_SYSCTL")" > "$REPO/estrado-pjud-service/.venv/bin/python"
    run_deploy
    expect_eq "$scenario drift during tests rejects deploy" "$RC" 1
    expect_eq "$scenario drift never stops replacement via restart" "$(restarts)" 0
    expect_eq "$scenario drift retains hold" "$(cat "$WM_FIXTURE_ROOT/maintenance-state")" hold
  done
  for path in worker/__init__.py worker/config.py worker/session_pool.py app/__init__.py app/r2.py app/minter.py; do
    setup "hook-${path//\//-}"; avanza_origin "estrado-pjud-service/$path"; health_ok
    PREV=$(sha_repo)
    run_deploy
    expect_eq "$path target hook changed rejects deploy" "$RC" 1
    expect_eq "$path target hook rejected before merge" "$(sha_repo)" "$PREV"
    expect_eq "$path target hook never restarts worker" "$(restarts)" 0
    expect_eq "$path target hook retains hold" "$(cat "$WM_FIXTURE_ROOT/maintenance-state")" hold
  done
  for path in app/ojv/__init__.py app/ojv/session.py app/ojv/browser_login.py \
    app/playwright_runtime.py worker/maintenance_heartbeat.py worker/proxy_control.py; do
    for change in edit remove; do
      setup "ownership-$change-${path//\//-}"; health_ok
      if [ "$change" = edit ]; then
        avanza_origin "estrado-pjud-service/$path"
      else
        git -C "$ORIGIN" rm -q -- "estrado-pjud-service/$path"
        git -C "$ORIGIN" commit -q -m 'fixture removes ownership boundary'
      fi
      PREV=$(sha_repo)
      run_deploy
      expect_eq "$path $change rejected" "$RC" 1
      expect_eq "$path $change rejected before merge" "$(sha_repo)" "$PREV"
      expect_eq "$path $change never restarts worker" "$(restarts)" 0
      expect_eq "$path $change retains hold" "$(cat "$WM_FIXTURE_ROOT/maintenance-state")" hold
    done
  done
}

if [ "${DEPLOY_TEST_FOCUS:-}" = maintenance-review ]; then
  run_maintenance_review_regressions
  printf '\n%s ok, %s fail\n' "$PASS" "$FAIL"
  [ "$FAIL" -eq 0 ]
  exit $?
fi

echo "== deploy feliz: avanza HEAD, una ronda de restart, exit 0"
setup feliz; avanza_origin; health_ok
mkdir -p "$TMP/state"
touch "$TMP/state/alert-cooldowns.json" "$TMP/state/alert-cooldowns.json.lock"
chmod 640 "$TMP/state/alert-cooldowns.json" "$TMP/state/alert-cooldowns.json.lock"
run_deploy
expect_eq "exit 0" "$RC" "0"
expect_eq "HEAD avanzó al de origin" "$(sha_repo)" "$(sha_origin)"
expect_eq "una ronda de restart" "$(restarts)" "1"
expect_contains "las dos units en la misma invocación" "$(cat "$LOG_SYSCTL")" "restart estrado-pjud.service estrado-pjud-worker.service"
expect_contains "verifica is-active" "$(cat "$LOG_SYSCTL")" "is-active"
expect_eq "no instala deps sin cambio de requirements" "$(cat "$LOG_PIP")" ""
expect_missing "no avisa de ops/cron si no cambió" "$OUT" "ops/cron"
expect_eq "repara permisos del estado compartido" "$(file_mode "$TMP/state/alert-cooldowns.json")" "660"
expect_eq "repara permisos del lock compartido" "$(file_mode "$TMP/state/alert-cooldowns.json.lock")" "660"

echo "== deploy seguro: actualiza API y mantiene worker detenido"
setup workerstop; avanza_origin; health_ok
KEEP_WORKER_STOPPED=1 run_deploy
expect_eq "exit 0" "$RC" "0"
expect_contains "deshabilita y detiene el worker" "$(cat "$LOG_SYSCTL")" "disable --now estrado-pjud-worker.service"
expect_contains "verifica que quedó inactivo" "$(cat "$LOG_SYSCTL")" "is-active estrado-pjud-worker.service"
expect_contains "verifica que no arranque tras reboot" "$(cat "$LOG_SYSCTL")" "is-enabled estrado-pjud-worker.service"
expect_contains "reinicia sólo API" "$(cat "$LOG_SYSCTL")" "restart estrado-pjud.service"
expect_missing "nunca reinicia worker" "$(cat "$LOG_SYSCTL")" "restart estrado-pjud.service estrado-pjud-worker.service"
expect_contains "reporta el gate" "$OUT" "worker permanece detenido"
unset KEEP_WORKER_STOPPED

: > "$LOG_SYSCTL"
touch "$(dirname "$LOG_SYSCTL")/worker-force-active"
avanza_origin cambio-posterior.txt
run_deploy
expect_eq "deploy posterior sin flag pasa" "$RC" "0"
expect_contains "deploy posterior reinicia API" "$(cat "$LOG_SYSCTL")" "restart estrado-pjud.service"
expect_contains "detiene una activación manual aunque siga disabled" "$(cat "$LOG_SYSCTL")" "stop estrado-pjud-worker.service"
expect_missing "deploy posterior preserva worker disabled" "$(cat "$LOG_SYSCTL")" "restart estrado-pjud.service estrado-pjud-worker.service"
expect_contains "explica el gate persistente" "$OUT" "worker disabled"

echo "== tests rojos: código vuelve, servicios NI SE TOCAN, exit 1"
setup rojos; avanza_origin; health_ok
PREV=$(sha_repo)
FAKE_PYTEST_EXIT=1 run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "lo dice" "$OUT" "TESTS ROJOS"

echo "== health no contesta: rollback con re-restart, exit 1"
setup salud; avanza_origin; health_bad
PREV=$(sha_repo)
run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "ronda de ida y ronda de rollback" "$(restarts)" "2"
expect_contains "avisa intervención manual" "$OUT" "TAMPOCO sana"

echo "== health falla pero el rollback sana: lo dice"
setup sana; avanza_origin; health_bad
# Stub de systemctl que "sana" el health recién en la ronda de rollback (la
# segunda invocación de restart): determinista, sin sleeps ni races.
printf '#!/bin/bash\necho "$@" >> "%s"\nif [ "$1" = is-enabled ]; then echo enabled; exit 0; fi\nif [ "$1" = is-active ]; then echo active; exit 0; fi\nif [ "$(grep -c "^restart" "%s")" -ge 2 ]; then echo ok > "%s"; fi\nexit 0\n' \
  "$LOG_SYSCTL" "$LOG_SYSCTL" "$TMP/www/health.json" > "$SYSCTL"
chmod +x "$SYSCTL"
run_deploy
expect_eq "exit 1 igual: el deploy NO entró" "$RC" "1"
expect_contains "reporta rollback OK" "$OUT" "Rollback OK"

echo "== rollback health sano sin ACK no acredita recuperación"
setup rollbackack; avanza_origin; health_bad
printf '#!/bin/bash\necho "$@" >> "%s"\nif [ "$1" = is-enabled ]; then echo enabled; exit 0; fi\nif [ "$1" = is-active ]; then echo active; exit 0; fi\nif [ "$(grep -c "^restart" "%s")" -ge 2 ]; then echo ok > "%s"; fi\nexit 0\n' \
  "$LOG_SYSCTL" "$LOG_SYSCTL" "$TMP/www/health.json" > "$SYSCTL"
chmod +x "$SYSCTL"
touch "$(dirname "$LOG_SYSCTL")/maintenance-fail-after-initial-drain"
printf '3\n' > "$(dirname "$LOG_SYSCTL")/maintenance-fail-after-drain-count"
run_deploy
expect_eq "rollback sin ACK falla" "$RC" 1
expect_missing "rollback sin ACK jamás dice OK" "$OUT" 'Rollback OK'
expect_contains "rollback sin ACK requiere intervención" "$OUT" 'TAMPOCO sana'
expect_eq "rollback sin ACK conserva hold" "$(cat "$WM_FIXTURE_ROOT/maintenance-state")" hold
expect_eq "ACK restaurado se exige después del restart rollback" "$(restarts)" 2

echo "== árbol sucio: aborta sin tocar nada"
setup sucio; avanza_origin; health_ok
echo hack >> "$REPO/estrado-pjud-service/requirements.txt"
PREV=$(sha_repo)
run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD no se movió" "$(sha_repo)" "$PREV"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "lo dice" "$OUT" "ABORTA"

echo "== systemctl sin estado confiable: aborta y no inventa un gate"
setup stateunknown; avanza_origin; health_ok
FAKE_SYSTEMCTL_STATE_UNKNOWN=1 run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "lo dice" "$OUT" "estado enabled/disabled"

echo "== archivo no trackeado: aborta antes de dejar un secreto fuera del guard"
setup untracked; avanza_origin; health_ok
echo cookie > "$REPO/estrado-pjud-service/credential-leak.json"
run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "lo dice" "$OUT" "ABORTA"

echo "== ya al día: exit 0 y no reinicia nada"
setup aldia; health_ok
run_deploy
expect_eq "exit 0" "$RC" "0"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "lo dice" "$OUT" "Ya al día"

echo "== requirements cambió: pip install antes de los tests"
setup deps; avanza_playwright; health_ok
run_deploy
expect_eq "exit 0" "$RC" "0"
expect_contains "instaló deps" "$(cat "$LOG_PIP")" "install"
expect_contains "instaló Chromium compatible" "$(cat "$LOG_PYTHON")" "-m playwright install chromium"
expect_contains "verificó que Chromium abre" "$(cat "$LOG_PYTHON")" "from playwright.sync_api import sync_playwright"
expect_contains "smoke usa headed y flags productivos" "$(cat "$LOG_PYTHON")" "headless=False"
expect_contains "smoke corre dentro de Xvfb" "$(cat "$LOG_XVFB")" "-a env"
expect_contains "smoke valida usuario API" "$(cat "$LOG_RUNUSER")" "-u www-data --"
expect_contains "smoke valida usuario worker" "$(cat "$LOG_RUNUSER")" "-u estrado --"
expect_eq "normaliza sólo la raíz del cache a 0755" \
  "$(file_mode "$TMP/ms-playwright")" "755"

echo "== path de browsers demasiado amplio: falla antes de instalar"
setup browserpath; avanza_playwright; health_ok
PREV=$(sha_repo)
BROWSER_PATH=/opt ALLOW_TEST_BROWSER_PATH=0 run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "no intenta instalar" "$(grep -c -- '-m playwright install chromium' "$LOG_PYTHON" || true)" "0"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "rechaza el path" "$OUT" "path canónico"
unset BROWSER_PATH ALLOW_TEST_BROWSER_PATH

echo "== symlink de browsers: falla antes de instalar"
setup browsersymlink; avanza_playwright; health_ok
PREV=$(sha_repo)
mkdir -p "$TMP/browser-target"
rm -rf "$TMP/ms-playwright"
ln -s "$TMP/browser-target" "$TMP/ms-playwright"
run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "no instala siguiendo symlink" "$(grep -c -- '-m playwright install chromium' "$LOG_PYTHON" || true)" "0"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "rechaza el symlink" "$OUT" "symlink"

echo "== contenido del cache escribible por grupo: falla cerrado"
setup browserpermissions; avanza_playwright; health_ok
PREV=$(sha_repo)
touch "$TMP/ms-playwright/browser-mutable"
chmod 0664 "$TMP/ms-playwright/browser-mutable"
run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "rechaza contenido reemplazable" "$OUT" "permisos inseguros"

echo "== inspección del cache falla: no asume que sea seguro"
setup browserfindfail; avanza_playwright; health_ok
PREV=$(sha_repo)
FAKE_FIND_EXIT=74 run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "falla cerrado" "$OUT" "no se pudo inspeccionar"
unset FAKE_FIND_EXIT

echo "== Chromium no se instala: revierte código, no reinicia y falla cerrado"
setup browserfail; avanza_playwright; health_ok
PREV=$(sha_repo)
FAKE_PLAYWRIGHT_INSTALL_EXIT=1 run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "nombra la causa" "$OUT" "CHROMIUM FALLÓ"
unset FAKE_PLAYWRIGHT_INSTALL_EXIT

echo "== Chromium no abre: revierte código, no reinicia y falla cerrado"
setup browsersmokefail; avanza_playwright; health_ok
PREV=$(sha_repo)
FAKE_PLAYWRIGHT_VERIFY_EXIT=1 run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "nombra la causa" "$OUT" "CHROMIUM NO ABRE"
unset FAKE_PLAYWRIGHT_VERIFY_EXIT

echo "== tests rojos tras cambiar deps: restaura requirements y browser previos"
setup depsrollback; avanza_playwright; health_ok
PREV=$(sha_repo)
FAKE_PYTEST_EXIT=1 run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "reinstala requirements previos" "$(grep -c '^install ' "$LOG_PIP" || true)" "2"
expect_eq "reinstala Chromium compatible con el rollback" "$(grep -c -- '-m playwright install chromium' "$LOG_PYTHON" || true)" "2"
expect_eq "cero rondas de restart" "$(restarts)" "0"
unset FAKE_PYTEST_EXIT

echo "== pip falla: código vuelve, sin restarts; hold bloquea reintento automático"
setup pipfail; avanza_origin estrado-pjud-service/requirements.txt; health_ok
PREV=$(sha_repo)
FAKE_PIP_EXIT=1 run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo (sin esto la corrida siguiente diría Ya al día sin desplegar)" "$(sha_repo)" "$PREV"
expect_eq "cero rondas de restart" "$(restarts)" "0"
expect_contains "lo dice" "$OUT" "PIP FALLÓ"
run_deploy
expect_eq "el reintento queda bloqueado por hold (exit 1)" "$RC" "1"
expect_eq "sin finalización explícita HEAD permanece previo" "$(sha_repo)" "$PREV"

echo "== systemctl restart falla: rollback igual, con diagnóstico y no un stacktrace de set -e"
setup restartfail; avanza_origin; health_ok
PREV=$(sha_repo)
FAKE_SYSTEMCTL_EXIT=1 run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "intentó la ronda de ida y la de rollback" "$(restarts)" "2"
expect_contains "nombra la causa" "$OUT" "systemctl restart FALLÓ"
expect_contains "y pide manos porque el rollback tampoco levantó" "$OUT" "TAMPOCO sana"

echo "== ops/cron cambió: el deploy avisa que lo instalado no se actualiza solo"
setup cron; avanza_origin ops/cron/estrado-watchdog.sh; health_ok
run_deploy
expect_eq "exit 0 (es aviso, no error)" "$RC" "0"
expect_contains "avisa correr deploy-cron.sh" "$OUT" "deploy-cron.sh"

echo "== target con contrato worker diferente se rechaza antes de merge/restart"
setup legacytarget; avanza_origin estrado-pjud-service/worker/__main__.py; health_ok
PREV=$(sha_repo)
run_deploy
expect_eq "contrato target incompatible falla" "$RC" "1"
expect_eq "target rechazado no mueve HEAD" "$(sha_repo)" "$PREV"
expect_eq "target rechazado nunca reinicia worker" "$(restarts)" "0"
expect_eq "target rechazado conserva hold" "$(cat "$WM_FIXTURE_ROOT/maintenance-state")" hold

echo
run_maintenance_review_regressions
echo "$PASS ok, $FAIL fail"
[ "$FAIL" -eq 0 ]
