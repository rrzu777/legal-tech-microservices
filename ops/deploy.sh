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
  # endpoint se mueve, hay que tocar los dos (la fuente única es parte del
  # trabajo de ops/provision, no de este script).
  local health_url="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8000/api/v1/health}"
  local health_retries="${DEPLOY_HEALTH_RETRIES:-60}"
  local health_sleep="${DEPLOY_HEALTH_SLEEP:-1}"
  local systemctl_bin="${DEPLOY_SYSTEMCTL:-systemctl}"
  local services=(estrado-pjud.service estrado-pjud-worker.service)

  cd "$repo_dir"

  if ! git diff-index --quiet HEAD --; then
    echo "ABORTA: hay cambios sin commitear en $repo_dir — resolvelos antes de desplegar" >&2
    exit 1
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
  # nuevo. En el rollback las deps nuevas quedan instaladas: casi siempre son
  # compatibles hacia atrás y reinstalar acá alargaría la ventana caída; si un
  # rollback llegara a fallar por deps, es intervención manual igual.
  if ! git diff --quiet "$prev"..HEAD -- estrado-pjud-service/requirements.txt; then
    echo "==> requirements.txt cambió: pip install"
    "$service_dir/.venv/bin/pip" install -q -r "$service_dir/requirements.txt"
  fi

  echo "==> pytest en el VPS"
  if ! (cd "$service_dir" && .venv/bin/python -m pytest -q); then
    echo "TESTS ROJOS: código de vuelta a $prev; los servicios no se tocaron." >&2
    git reset --hard "$prev"
    exit 1
  fi

  "$systemctl_bin" restart "${services[@]}"

  if ! wait_health "$health_url" "$health_retries" "$health_sleep"; then
    echo "HEALTH FALLÓ tras el restart: rollback a $prev" >&2
    git reset --hard "$prev"
    "$systemctl_bin" restart "${services[@]}"
    if wait_health "$health_url" "$health_retries" "$health_sleep"; then
      echo "Rollback OK: el VPS quedó en $prev y sano. El deploy NO entró." >&2
    else
      echo "El rollback TAMPOCO sana — intervención manual YA (journalctl -u ${services[0]})" >&2
    fi
    exit 1
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

  echo "OK: desplegado $(git rev-parse --short HEAD) ($prev → HEAD), health contesta, units activas."
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
