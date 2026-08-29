#!/usr/bin/env bash
# ops/deploy.sh — despliegue del microservicio EN el VPS, con verificación y rollback.
#
#   ssh legaltech-vps /opt/legal-tech-microservices/ops/deploy.sh
#
# Reemplaza el procedimiento manual (fetch → ff → pytest → restart → health) que
# hasta agosto 2026 se ejecutaba comando por comando. El orden importa y es el
# mismo que el manual:
#
#   1. árbol limpio        — desplegar sobre ediciones a mano es la receta del drift
#   2. ff-only a origin/main — si divergió, alguien tocó el checkout: abortar
#   3. pytest EN el VPS    — el laptop no prueba el .env ni el Python del VPS
#   4. restart de las dos units en UNA invocación: systemd encola ambos jobs a
#      la vez (ventana de corte = max, no suma) y una unit que falla no impide
#      el intento de la otra
#      Con DEPLOY_KEEP_WORKER_STOPPED=1, detiene el worker y reinicia sólo API.
#   5. health con reintentos — la API tarda en levantar; un curl inmediato da
#      falso negativo
#
# Si 3 falla: el código vuelve al SHA anterior y los servicios NI SE TOCAN
# (siguen corriendo el binario viejo, que es exactamente lo que queremos).
# Si 5 falla: código al SHA anterior + restart de nuevo + health de nuevo.
#
# Todo es inyectable por entorno para poder probarlo en un laptop con stubs
# (ver ops/tests/test-deploy.sh). Prefijo DEPLOY_ a propósito: un HEALTH_URL o
# SYSTEMCTL genérico exportado en el entorno de root redirigiría la verificación
# sin que nadie lo pida.
set -euo pipefail

# Envuelto en main(): bash lee los scripts por pedazos mientras los ejecuta, y
# este archivo se PISA A SÍ MISMO en el paso 2. Con main() el archivo entero ya
# está parseado antes de ejecutar nada; la versión nueva recién rige en la
# próxima corrida.
main() {
  local repo_dir="${DEPLOY_REPO_DIR:-/opt/legal-tech-microservices}"
  local service_dir="$repo_dir/estrado-pjud-service"
  # Mismo default que API_HEALTH_URL en ops/cron/estrado-watchdog.sh; si el
  # endpoint se mueve, hay que tocar los dos. Fuente única evaluada al armar
  # provision y descartada: contextos distintos (deploy on-box vs cron) y un
  # archivo compartido sería una pieza más que puede faltar.
  local health_url="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8000/api/v1/health}"
  local health_retries="${DEPLOY_HEALTH_RETRIES:-60}"
  local health_sleep="${DEPLOY_HEALTH_SLEEP:-1}"
  local systemctl_bin="${DEPLOY_SYSTEMCTL:-systemctl}"
  local shared_state_dir="${DEPLOY_STATE_DIR:-/var/lib/estrado-pjud}"
  local playwright_browsers_path="${DEPLOY_PLAYWRIGHT_BROWSERS_PATH:-/opt/ms-playwright}"
  local allow_test_browser_path="${DEPLOY_ALLOW_TEST_BROWSER_PATH:-0}"
  local browser_owner_uid="${DEPLOY_BROWSER_OWNER_UID:-0}"
  local browser_owner_gid="${DEPLOY_BROWSER_OWNER_GID:-0}"
  local xvfb_run_bin="${DEPLOY_XVFB_RUN:-/usr/bin/xvfb-run}"
  local runuser_bin="${DEPLOY_RUNUSER:-/usr/sbin/runuser}"
  local find_bin="${DEPLOY_FIND:-find}"
  local keep_worker_stopped="${DEPLOY_KEEP_WORKER_STOPPED:-0}"
  local deps_changed=0
  local playwright_changed=0
  local services=(estrado-pjud.service)
  local worker_enabled=0
  local worker_enabled_state worker_active_state
  if [ "$keep_worker_stopped" != "1" ]; then
    worker_enabled_state=$("$systemctl_bin" is-enabled estrado-pjud-worker.service 2>/dev/null || true)
    case "$worker_enabled_state" in
      enabled)
        worker_enabled=1
        services+=(estrado-pjud-worker.service)
        ;;
      disabled) ;;
      *)
        echo "ABORTA: estado enabled/disabled del worker desconocido (${worker_enabled_state:-sin respuesta})" >&2
        exit 1
        ;;
    esac
  fi

  cd "$repo_dir"

  if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
    echo "ABORTA: hay cambios sin commitear en $repo_dir — resolvelos antes de desplegar" >&2
    exit 1
  fi

  # API (www-data:estrado) y worker (estrado:estrado) comparten estos dos
  # archivos. Una versión anterior los dejó 0640: el creador podía escribir,
  # pero el otro proceso sólo leer, convirtiendo una alerta en un 500. Sanar
  # los archivos existentes además del modo de creación evita depender de que
  # sean borrados o recreados durante este despliegue.
  local shared_state_file
  for shared_state_file in alert-cooldowns.json alert-cooldowns.json.lock; do
    if [ -e "$shared_state_dir/$shared_state_file" ] \
      && ! chmod 0660 "$shared_state_dir/$shared_state_file"; then
      echo "ABORTA: no se pudieron reparar los permisos del estado compartido de alertas" >&2
      exit 1
    fi
  done

  if [ "$keep_worker_stopped" = "1" ]; then
    if ! "$systemctl_bin" disable --now estrado-pjud-worker.service; then
      echo "ABORTA: no se pudo deshabilitar/detener estrado-pjud-worker.service" >&2
      exit 1
    fi
    worker_enabled_state=$("$systemctl_bin" is-enabled estrado-pjud-worker.service 2>/dev/null || true)
    worker_active_state=$("$systemctl_bin" is-active estrado-pjud-worker.service 2>/dev/null || true)
    if [ "$worker_enabled_state" != "disabled" ] \
      || [ "$worker_active_state" != "inactive" ]; then
      echo "ABORTA: el worker no quedó inequívocamente disabled+inactive después del gate" >&2
      exit 1
    fi
    echo "Gate activo: el worker permanece detenido; se desplegará sólo la API."
  elif [ "$worker_enabled" = "0" ]; then
    worker_active_state=$("$systemctl_bin" is-active estrado-pjud-worker.service 2>/dev/null || true)
    if [ "$worker_active_state" = "active" ]; then
      if ! "$systemctl_bin" stop estrado-pjud-worker.service \
        || [ "$("$systemctl_bin" is-active estrado-pjud-worker.service 2>/dev/null || true)" != "inactive" ]; then
        echo "ABORTA: worker disabled pero no se pudo detener" >&2
        exit 1
      fi
    elif [ "$worker_active_state" != "inactive" ]; then
      echo "ABORTA: estado active/inactive del worker desconocido (${worker_active_state:-sin respuesta})" >&2
      exit 1
    fi
    echo "Gate persistente: worker disabled; el deploy normal no lo reiniciará."
  fi

  local prev
  prev=$(git rev-parse HEAD)

  git fetch origin main
  if [ "$(git rev-parse origin/main)" = "$prev" ]; then
    echo "Ya al día ($(git rev-parse --short HEAD)); nada que desplegar."
    exit 0
  fi

  git merge --ff-only origin/main

  # requirements.txt cambió → instalar ANTES de los tests, que ya importan lo
  # nuevo. Si cambia Playwright, instalar y abrir además su Chromium exacto.
  # Cualquier rollback restaura requirements y browser desde $prev antes de
  # volver a levantar servicios, para no mezclar código y runtime incompatibles.
  if ! git diff --quiet "$prev"..HEAD -- estrado-pjud-service/requirements.txt; then
    deps_changed=1
    echo "==> requirements.txt cambió: pip install"
    # Guardado: sin esto, un pip fallido (red, conflicto de versiones) moría
    # por set -e con HEAD ya avanzado, y la corrida siguiente veía
    # HEAD == origin/main y decía "Ya al día" sin haber desplegado nada.
    # Con el reset, reintentar el deploy es simplemente volver a correrlo.
    if ! "$service_dir/.venv/bin/pip" install -q -r "$service_dir/requirements.txt"; then
      echo "PIP FALLÓ: restaurando código y dependencias de $prev; los servicios no se tocaron." >&2
      rollback_code_and_dependencies || true
      exit 1
    fi

    if ! git diff "$prev"..HEAD -- estrado-pjud-service/requirements.txt \
      | grep -qE '^[+-]playwright(==|>=|<=|~=|>|<)'; then
      playwright_changed=0
    else
      playwright_changed=1
      if ! install_and_verify_playwright; then
        rollback_code_and_dependencies || true
        exit 1
      fi
    fi
  fi

  echo "==> pytest en el VPS"
  if ! (cd "$service_dir" && .venv/bin/python -m pytest -q); then
    echo "TESTS ROJOS: restaurando código y dependencias de $prev; los servicios no se tocaron." >&2
    rollback_code_and_dependencies || true
    exit 1
  fi

  # El restart también va guardado: puede fallar él mismo (ExecStartPre, env
  # que falta, StartLimitBurst ya gastado) y con set -e eso se salteaba el
  # rollback entero — justo la red de seguridad que este script promete.
  if ! "$systemctl_bin" restart "${services[@]}"; then
    rollback_and_exit "systemctl restart FALLÓ"
  fi

  if ! wait_health "$health_url" "$health_retries" "$health_sleep"; then
    rollback_and_exit "HEALTH FALLÓ tras el restart"
  fi

  local unit
  for unit in "${services[@]}"; do
    if ! "$systemctl_bin" is-active --quiet "$unit"; then
      echo "OJO: $unit no está activa aunque el health contestó — mirar journalctl -u $unit" >&2
      exit 1
    fi
  done

  # El trap que ya mordió una vez ("mergear crontab.snapshot no cambia nada en
  # el VPS"): este deploy avanza las FUENTES de ops/cron/ pero los instalados
  # en /opt/estrado-cron no se actualizan solos. Sin este aviso, un fix del
  # watchdog "desplegado" puede no estar corriendo — la clase de falla de los
  # 120 días que ops/ existe para impedir.
  if ! git diff --quiet "$prev"..HEAD -- ops/cron; then
    echo "OJO: ops/cron cambió en este rango; lo instalado en /opt/estrado-cron NO se actualiza solo — corré ops/cron/deploy-cron.sh desde el laptop."
  fi

  if [ "$keep_worker_stopped" = "1" ] || [ "$worker_enabled" = "0" ]; then
    echo "OK: desplegado $(git rev-parse --short HEAD) ($prev → HEAD), health contesta y el worker permanece detenido."
  else
    echo "OK: desplegado $(git rev-parse --short HEAD) ($prev → HEAD), health contesta, units activas."
  fi
}

# Lee los locals de main() (scoping dinámico de bash): prev, systemctl_bin,
# services, health_*. Deja el código en $prev e intenta revivir los servicios;
# si ni el restart ni el health del rollback sanan, lo dice y pide manos.
rollback_and_exit() { # rollback_and_exit <motivo>
  echo "$1: rollback a $prev" >&2
  local rollback_ready=1
  if ! rollback_code_and_dependencies; then
    rollback_ready=0
  fi
  if [ "$rollback_ready" = "1" ] \
    && "$systemctl_bin" restart "${services[@]}" \
    && wait_health "$health_url" "$health_retries" "$health_sleep"; then
    echo "Rollback OK: el VPS quedó en $prev y sano. El deploy NO entró." >&2
  else
    echo "El rollback TAMPOCO sana — intervención manual YA (journalctl -u ${services[0]})" >&2
  fi
  exit 1
}

install_and_verify_playwright() {
  if [ "$allow_test_browser_path" != "1" ] \
    && [ "$playwright_browsers_path" != "/opt/ms-playwright" ]; then
    echo "CHROMIUM FALLÓ: producción exige el path canónico /opt/ms-playwright." >&2
    return 1
  fi
  if [ -L "$playwright_browsers_path" ]; then
    echo "CHROMIUM FALLÓ: el directorio de browsers no puede ser un symlink." >&2
    return 1
  fi
  if ! (umask 022 && mkdir -p "$playwright_browsers_path"); then
    echo "CHROMIUM FALLÓ: no se pudo preparar el directorio canónico de browsers." >&2
    return 1
  fi
  if ! chown "$browser_owner_uid:$browser_owner_gid" "$playwright_browsers_path" \
    || ! chmod 0755 "$playwright_browsers_path"; then
    echo "CHROMIUM FALLÓ: no se pudo fijar owner/mode seguros en el cache." >&2
    return 1
  fi
  if ! validate_browser_cache_permissions; then
    return 1
  fi

  if ! (umask 022 && env PLAYWRIGHT_BROWSERS_PATH="$playwright_browsers_path" \
    "$service_dir/.venv/bin/python" -m playwright install chromium); then
    echo "CHROMIUM FALLÓ: no se pudo instalar la revisión compatible; no se reinició ningún servicio." >&2
    return 1
  fi
  if ! validate_browser_cache_permissions; then
    return 1
  fi

  local smoke_user
  for smoke_user in www-data estrado; do
    if ! "$runuser_bin" -u "$smoke_user" -- "$xvfb_run_bin" -a \
      env PLAYWRIGHT_BROWSERS_PATH="$playwright_browsers_path" \
      HOME=/tmp TMPDIR=/tmp PYTHONPATH="$service_dir" \
      "$service_dir/.venv/bin/python" -c \
        'from app.minter import _ANTIBOT_ARGS; from playwright.sync_api import sync_playwright; playwright = sync_playwright().start(); browser = playwright.chromium.launch(headless=False, args=_ANTIBOT_ARGS); page = browser.new_page(); page.set_content("<title>juristrack-playwright-smoke</title>"); assert page.title() == "juristrack-playwright-smoke"; browser.close(); playwright.stop()'; then
      echo "CHROMIUM NO ABRE: el smoke headed falló como $smoke_user bajo Xvfb; no se reinició ningún servicio." >&2
      return 1
    fi
  done
}

validate_browser_cache_permissions() {
  local unsafe_entry
  if ! unsafe_entry=$("$find_bin" "$playwright_browsers_path" -mindepth 1 \
    \( ! -user "$browser_owner_uid" -o -perm -0020 -o -perm -0002 \) -print -quit); then
    echo "CHROMIUM FALLÓ: no se pudo inspeccionar owner/permisos del cache." >&2
    return 1
  fi
  if [ -n "$unsafe_entry" ]; then
    echo "CHROMIUM FALLÓ: el cache tiene owner o permisos inseguros." >&2
    return 1
  fi
}

rollback_code_and_dependencies() {
  git reset --hard "$prev"
  if [ "$deps_changed" != "1" ]; then
    return 0
  fi

  echo "==> restaurando dependencias de $prev"
  if ! "$service_dir/.venv/bin/pip" install -q -r "$service_dir/requirements.txt"; then
    echo "ROLLBACK DE DEPS FALLÓ: el código volvió a $prev pero la venv requiere intervención manual." >&2
    return 1
  fi
  if [ "$playwright_changed" = "1" ] && ! install_and_verify_playwright; then
    echo "ROLLBACK DE CHROMIUM FALLÓ: el código volvió a $prev pero browser/venv requieren intervención manual." >&2
    return 1
  fi
}

wait_health() {
  local url="$1" retries="$2" sleep_s="$3" i
  for i in $(seq 1 "$retries"); do
    if curl -fsS -m 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$sleep_s"
  done
  return 1
}

main "$@"
