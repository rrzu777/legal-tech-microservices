#!/usr/bin/env bash
# ops/provision.sh — deja el VPS en el estado que este repo declara, o dice
# exactamente qué falta. Idempotente: correrlo dos veces seguidas no hace nada
# la segunda.
#
#   ssh legaltech-vps /opt/legal-tech-microservices/ops/provision.sh
#
# Qué cubre:
#   - ops/systemd/** → /etc/systemd/system/** (instala solo lo que difiere;
#     daemon-reload únicamente si algo cambió)
#   - .env presente y con TODAS las variables de ops/env.inventory (compara
#     NOMBRES; los valores nunca se leen ni se imprimen)
#   - venv, usuarios de las units y /opt/legaltech-monitoring (los scripts de
#     monitoreo NO viven en este repo — solo se avisa si faltan)
#   - systemctl enable de las cuatro units
#
# Qué NO cubre: el crontab y /opt/estrado-cron (eso es ops/cron/deploy-cron.sh
# + el procedimiento del crontab.snapshot), y los VALORES de los secretos.
#
# Sale 0 solo si el VPS quedó completo. Inyectable por entorno para probarlo
# en el laptop (ver ops/tests/test-provision.sh): PROV_REPO_DIR,
# PROV_SYSTEMD_DIR, PROV_SYSTEMCTL, PROV_ENV_FILE, PROV_REQUIRED_USERS,
# PROV_MONITORING_DIR.
set -euo pipefail

main() {
  local repo_dir="${PROV_REPO_DIR:-/opt/legal-tech-microservices}"
  local systemd_dir="${PROV_SYSTEMD_DIR:-/etc/systemd/system}"
  local systemctl_bin="${PROV_SYSTEMCTL:-systemctl}"
  local env_file="${PROV_ENV_FILE:-$repo_dir/estrado-pjud-service/.env}"
  local required_users="${PROV_REQUIRED_USERS:-estrado www-data}"
  local monitoring_dir="${PROV_MONITORING_DIR:-/opt/legaltech-monitoring}"
  local src="$repo_dir/ops/systemd"
  local changed=0 rc=0 rel dest

  # --- units: instalar solo lo que difiere -------------------------------
  while IFS= read -r rel; do
    dest="$systemd_dir/$rel"
    if [ -f "$dest" ] && cmp -s "$src/$rel" "$dest"; then
      continue
    fi
    echo "==> instala $rel"
    # Sin `install -D`: es GNU-only y los tests corren en el laptop (BSD).
    mkdir -p "$(dirname "$dest")"
    install -m 644 "$src/$rel" "$dest"
    changed=1
  done < <(cd "$src" && find . -type f | sed 's|^\./||' | sort)

  if [ "$changed" -eq 1 ]; then
    "$systemctl_bin" daemon-reload
  else
    echo "units al día"
  fi

  # --- .env: nombres, jamás valores --------------------------------------
  if [ ! -r "$env_file" ]; then
    echo "FALTA $env_file (modo 600). Variables requeridas en ops/env.inventory; los valores se reponen desde el gestor de secretos." >&2
    rc=1
  else
    local missing
    missing=$(comm -23 \
      <(grep -vE '^#|^$' "$repo_dir/ops/env.inventory" | sort) \
      <(cut -d= -f1 "$env_file" | grep -vE '^#|^$' | sort))
    if [ -n "$missing" ]; then
      echo "Variables FALTANTES en $env_file (solo nombres):" >&2
      echo "$missing" >&2
      rc=1
    fi
  fi

  # --- venv ----------------------------------------------------------------
  if [ ! -x "$repo_dir/estrado-pjud-service/.venv/bin/python" ]; then
    echo "FALTA la venv: cd estrado-pjud-service && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    rc=1
  fi

  # --- usuarios de las units ----------------------------------------------
  local u
  for u in $required_users; do
    if ! id "$u" >/dev/null 2>&1; then
      echo "FALTA el usuario $u (useradd --system $u)" >&2
      rc=1
    fi
  done

  # --- scripts de monitoreo (viven FUERA de este repo) --------------------
  if [ ! -f "$monitoring_dir/monitor.py" ] || [ ! -f "$monitoring_dir/resource-tracker.py" ]; then
    echo "FALTA $monitoring_dir/{monitor.py,resource-tracker.py} — no viven en este repo; sin ellos esas dos units quedan en crash-loop." >&2
    rc=1
  fi

  "$systemctl_bin" enable estrado-pjud.service estrado-pjud-worker.service \
    legaltech-monitor.service legaltech-resource-tracker.service

  if [ "$rc" -eq 0 ]; then
    echo "OK: units instaladas y habilitadas, .env completo, venv y usuarios presentes."
  else
    echo "INCOMPLETO: resolver lo listado arriba y volver a correr (es idempotente)." >&2
  fi
  exit "$rc"
}

main "$@"
