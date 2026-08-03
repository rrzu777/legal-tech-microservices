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
#   - ops/caddy/Caddyfile → /etc/caddy/Caddyfile (reload de caddy solo si
#     cambió; si el paquete caddy falta, receta y exit 1 — ops/caddy/README.md)
#   - .env presente y con TODAS las variables de ops/env.inventory (compara
#     NOMBRES; los valores nunca se leen ni se imprimen)
#   - venv, usuarios de las units y /opt/legaltech-monitoring (los scripts de
#     monitoreo NO viven en este repo — solo se avisa si faltan)
#   - systemctl enable de las cuatro units
#
# Qué NO cubre: el crontab y /opt/estrado-cron (eso es ops/cron/deploy-cron.sh
# + el procedimiento del crontab.snapshot), las reglas de ufw (ops/caddy/
# README.md, paso 5), y los VALORES de los secretos.
#
# Sale 0 solo si el VPS quedó completo. Inyectable por entorno para probarlo
# en el laptop (ver ops/tests/test-provision.sh): PROV_REPO_DIR,
# PROV_SYSTEMD_DIR, PROV_SYSTEMCTL, PROV_ENV_FILE, PROV_REQUIRED_USERS,
# PROV_MONITORING_DIR, PROV_CADDY_BIN, PROV_CADDYFILE_DEST.
set -euo pipefail

main() {
  local repo_dir="${PROV_REPO_DIR:-/opt/legal-tech-microservices}"
  local systemd_dir="${PROV_SYSTEMD_DIR:-/etc/systemd/system}"
  local systemctl_bin="${PROV_SYSTEMCTL:-systemctl}"
  local env_file="${PROV_ENV_FILE:-$repo_dir/estrado-pjud-service/.env}"
  local required_users="${PROV_REQUIRED_USERS:-estrado www-data}"
  local monitoring_dir="${PROV_MONITORING_DIR:-/opt/legaltech-monitoring}"
  local cron_dir="${PROV_CRON_DIR:-/opt/estrado-cron}"
  local caddy_bin="${PROV_CADDY_BIN:-caddy}"
  local caddyfile_src="$repo_dir/ops/caddy/Caddyfile"
  local caddyfile_dest="${PROV_CADDYFILE_DEST:-/etc/caddy/Caddyfile}"
  local src="$repo_dir/ops/systemd"
  local changed=0 rc=0 rel dest

  # Las fuentes se validan ANTES de usarlas: un `while read` alimentado por un
  # `cd` que falla itera cero veces y sale 0, y un `comm` con un lado vacío
  # dice "no falta nada" — o sea que un PROV_REPO_DIR malo o un checkout a
  # medias (el escenario exacto de una reconstrucción) imprimiría "todo OK"
  # sin haber verificado NADA. La ausencia no es señal.
  if [ ! -d "$src" ] || [ ! -r "$repo_dir/ops/env.inventory" ] || [ ! -r "$caddyfile_src" ]; then
    echo "ABORTA: falta $src, $repo_dir/ops/env.inventory o $caddyfile_src — ¿PROV_REPO_DIR o checkout incompletos?" >&2
    exit 1
  fi

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

  # --- caddy: TLS delante de la API (ops/caddy/README.md) ------------------
  if ! command -v "$caddy_bin" >/dev/null 2>&1; then
    echo "FALTA caddy (apt-get install caddy) — sin él la API queda sin TLS delante." >&2
    rc=1
  elif ! cmp -s "$caddyfile_src" "$caddyfile_dest"; then
    echo "==> instala Caddyfile"
    mkdir -p "$(dirname "$caddyfile_dest")"
    install -m 644 "$caddyfile_src" "$caddyfile_dest"
    # reload de una unit parada falla; en ese caso se levanta.
    "$systemctl_bin" reload caddy || "$systemctl_bin" restart caddy
  fi

  # --- .env: nombres, jamás valores --------------------------------------
  if [ ! -r "$env_file" ]; then
    echo "FALTA $env_file (modo 600). Variables requeridas en ops/env.inventory; los valores se reponen desde el gestor de secretos." >&2
    rc=1
  else
    local missing extra
    missing=$(comm -23 \
      <(grep -vE '^#|^$' "$repo_dir/ops/env.inventory" | sort) \
      <(cut -d= -f1 "$env_file" | grep -vE '^#|^$' | sort))
    if [ -n "$missing" ]; then
      echo "Variables FALTANTES en $env_file (solo nombres):" >&2
      echo "$missing" >&2
      rc=1
    fi
    # El reverso, como WARNING sin tocar rc: variables que el .env real tiene
    # y el inventario no. Es el modo de falla clásico de un snapshot — alguien
    # agrega una var a mano y la próxima reconstrucción la pierde en silencio
    # mientras provision dice "completo".
    extra=$(comm -13 \
      <(grep -vE '^#|^$' "$repo_dir/ops/env.inventory" | sort) \
      <(cut -d= -f1 "$env_file" | grep -vE '^#|^$' | sort))
    if [ -n "$extra" ]; then
      echo "OJO: el .env tiene variables que ops/env.inventory no lista (agregalas al inventario o una reconstrucción las pierde):"
      echo "$extra"
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

  # --- cron instalado (el contenido del crontab lo vigila el watchdog #10) --
  if [ ! -f "$cron_dir/run-cron.sh" ]; then
    echo "FALTA $cron_dir/run-cron.sh — un VPS reconstruido sin los crons revive el silencio de los 120 días. Correr ops/cron/deploy-cron.sh desde el laptop e instalar crontab.snapshot a mano (ver ops/cron/README.md)." >&2
    rc=1
  fi

  # La lista de units a habilitar se deriva de los archivos del espejo (los
  # .service de primer nivel; el slice no es enable-able y el drop-in no es
  # unit): agregar una unit al espejo no exige acordarse de una segunda lista.
  local enable_units=()
  while IFS= read -r rel; do
    enable_units+=("$rel")
  done < <(cd "$src" && find . -maxdepth 1 -name '*.service' -type f | sed 's|^\./||' | sort)
  "$systemctl_bin" enable "${enable_units[@]}"

  if [ "$rc" -eq 0 ]; then
    echo "OK: units instaladas y habilitadas, .env completo, venv y usuarios presentes."
  else
    echo "INCOMPLETO: resolver lo listado arriba y volver a correr (es idempotente)." >&2
  fi
  exit "$rc"
}

main "$@"
