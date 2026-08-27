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
#     cambió; si el paquete caddy falta, receta y exit 1 — ops/caddy/README.md;
#     PROV_SKIP_CADDY=1 omite por completo esta superficie para rollouts que
#     administran exclusivamente los recursos declarados)
#   - .env presente, 0640 root:estrado y con TODAS las variables de
#     ops/env.inventory (compara NOMBRES; los valores nunca se imprimen)
#   - venv, usuarios de las units y código de ops/monitoring instalado en
#     /opt/legaltech-monitoring
#   - estado/logs, credenciales vacías si faltan y logrotate del monitoreo
#   - drop-in de user-<uid>.slice sólo tras validar UID/ownership de Hermes
#   - systemctl enable de API/timers; el worker requiere opt-in explícito
#
# Qué NO cubre: el crontab y /opt/estrado-cron (eso es ops/cron/deploy-cron.sh
# + el procedimiento del crontab.snapshot), el paquete caddy y las reglas de
# ufw (manuales: ops/caddy/README.md, pasos 1 y 6), y los VALORES de los
# secretos.
#
# Sale 0 solo si el VPS quedó completo. Inyectable por entorno para probarlo
# en el laptop (ver ops/tests/test-provision.sh): PROV_REPO_DIR,
# PROV_SYSTEMD_DIR, PROV_SYSTEMCTL, PROV_ENV_FILE, PROV_REQUIRED_USERS,
# PROV_ENV_OWNER, PROV_ENV_GROUP, PROV_MONITORING_DIR, PROV_CADDY_BIN,
# PROV_CADDYFILE_DEST, PROV_SKIP_CADDY y los destinos/binarios PROV_* usados
# por el harness.
set -euo pipefail

main() {
  local repo_dir="${PROV_REPO_DIR:-/opt/legal-tech-microservices}"
  local systemd_dir="${PROV_SYSTEMD_DIR:-/etc/systemd/system}"
  local systemctl_bin="${PROV_SYSTEMCTL:-systemctl}"
  local env_file="${PROV_ENV_FILE:-$repo_dir/estrado-pjud-service/.env}"
  local env_owner="${PROV_ENV_OWNER:-root}"
  local env_group="${PROV_ENV_GROUP:-estrado}"
  local required_users="${PROV_REQUIRED_USERS:-estrado www-data}"
  local monitoring_dir="${PROV_MONITORING_DIR:-/opt/legaltech-monitoring}"
  local monitoring_env_file="${PROV_MONITORING_ENV_FILE:-/etc/legaltech-monitoring.env}"
  local monitor_state_dir="${PROV_MONITOR_STATE_DIR:-/var/lib/legaltech-monitor}"
  local monitor_log_dir="${PROV_MONITOR_LOG_DIR:-/var/log/legaltech}"
  local resource_csv="${PROV_RESOURCE_CSV:-$monitor_log_dir/resources.csv}"
  local logrotate_dest="${PROV_LOGROTATE_DEST:-/etc/logrotate.d/legaltech-resources}"
  local cron_dir="${PROV_CRON_DIR:-/opt/estrado-cron}"
  local caddy_bin="${PROV_CADDY_BIN:-caddy}"
  local id_bin="${PROV_ID_BIN:-id}"
  local ps_bin="${PROV_PS_BIN:-ps}"
  local install_bin="${PROV_INSTALL_BIN:-install}"
  local chown_bin="${PROV_CHOWN_BIN:-chown}"
  local chmod_bin="${PROV_CHMOD_BIN:-chmod}"
  local caddyfile_src="$repo_dir/ops/caddy/Caddyfile"
  local caddyfile_dest="${PROV_CADDYFILE_DEST:-/etc/caddy/Caddyfile}"
  local enable_pjud_worker="${PROV_ENABLE_PJUD_WORKER:-0}"
  local skip_caddy="${PROV_SKIP_CADDY:-0}"
  local src="$repo_dir/ops/systemd"
  local template_src="$repo_dir/ops/systemd-templates/hermes-user.slice.conf"
  local monitoring_src="$repo_dir/ops/monitoring"
  local logrotate_src="$repo_dir/ops/logrotate/legaltech-resources"
  local changed=0 rc=0 rel dest

  # Las fuentes se validan ANTES de usarlas: un `while read` alimentado por un
  # `cd` que falla itera cero veces y sale 0, y un `comm` con un lado vacío
  # dice "no falta nada" — o sea que un PROV_REPO_DIR malo o un checkout a
  # medias (el escenario exacto de una reconstrucción) imprimiría "todo OK"
  # sin haber verificado NADA. La ausencia no es señal.
  case "$skip_caddy" in 0|1) ;; *) echo "ABORTA: PROV_SKIP_CADDY debe ser 0 o 1." >&2; exit 1 ;; esac

  if [ ! -d "$src" ] || [ ! -r "$repo_dir/ops/env.inventory" ] \
    || { [ "$skip_caddy" = 0 ] && [ ! -r "$caddyfile_src" ]; } || [ ! -r "$template_src" ] \
    || [ ! -r "$logrotate_src" ] || [ ! -r "$monitoring_src/monitor.py" ] \
    || [ ! -r "$monitoring_src/resource-tracker.py" ] \
    || [ ! -r "$monitoring_src/alert_policy.py" ] \
    || [ ! -r "$monitoring_src/resource_metrics.py" ]; then
    echo "ABORTA: checkout incompleto; faltan fuentes declaradas de systemd, monitoreo, logrotate, Caddy o env.inventory." >&2
    exit 1
  fi

  # Un path existente debe ser regular y nunca symlink. Este gate ocurre
  # antes de consultar/instalar units para que un fallo no pueda activar los
  # timers con credenciales apuntando a un destino inseguro.
  if { [ -e "$monitoring_env_file" ] || [ -L "$monitoring_env_file" ]; } \
    && { [ ! -f "$monitoring_env_file" ] || [ -L "$monitoring_env_file" ]; }; then
    echo "ABORTA: $monitoring_env_file debe ser un archivo regular root-only, nunca symlink." >&2
    exit 1
  fi
  if [ -L "$monitor_log_dir" ] \
    || { [ -e "$monitor_log_dir" ] && [ ! -d "$monitor_log_dir" ]; }; then
    echo "ABORTA: el directorio del CSV de recursos es inseguro." >&2
    exit 1
  fi
  if { [ -e "$resource_csv" ] || [ -L "$resource_csv" ]; } \
    && { [ ! -f "$resource_csv" ] || [ -L "$resource_csv" ]; }; then
    echo "ABORTA: el CSV de recursos debe ser un archivo regular, nunca symlink." >&2
    exit 1
  fi

  # Preflight completo de los directorios donde terminará Python. Se hace
  # antes de instalar units: seguir un symlink aquí permitiría reemplazar
  # código que luego ejecuta root. Directorios reales, aunque hoy tengan un
  # modo débil, se aceptan para convergerlos en la fase de instalación.
  local runtime_rel runtime_parent runtime_current runtime_part
  local -a runtime_parts=()
  if [ -L "$monitoring_dir" ] \
    || { [ -e "$monitoring_dir" ] && [ ! -d "$monitoring_dir" ]; }; then
    echo "ABORTA: el directorio runtime de monitoreo es symlink o no es directorio." >&2
    exit 1
  fi
  while IFS= read -r runtime_rel; do
    runtime_parent=$(dirname "$runtime_rel")
    [ "$runtime_parent" != "." ] || continue
    runtime_current="$monitoring_dir"
    IFS='/' read -r -a runtime_parts <<< "$runtime_parent"
    for runtime_part in "${runtime_parts[@]}"; do
      [ -n "$runtime_part" ] || continue
      runtime_current="$runtime_current/$runtime_part"
      if [ -L "$runtime_current" ] \
        || { [ -e "$runtime_current" ] && [ ! -d "$runtime_current" ]; }; then
        echo "ABORTA: un componente del runtime de monitoreo es symlink o no es directorio." >&2
        exit 1
      fi
    done
  done < <(cd "$monitoring_src" && find . -type f -name '*.py' \
    ! -path './tests/*' ! -path '*/__pycache__/*' | sed 's|^\./||' | sort)

  # --- Hermes: validar identidad y ownership ANTES de mutar el host -------
  # `unit` es la unit de sistema y `uunit` la unit del user manager. Mirar
  # sólo nombres de cgroup evita inspeccionar argumentos o payloads del
  # proceso. Cualquier owner persistente fuera de este set explícito aborta.
  local hermes_uid hermes_reverse hermes_ownership system_unit user_unit extra
  local enabled_system_units enabled_user_units unit_name unit_user
  local dbus_seen=0 session_migration_seen=0
  if ! hermes_uid=$("$id_bin" -u hermes 2>/dev/null); then
    echo "FALTA el usuario hermes; no se instala el límite de su user slice." >&2
    exit 1
  fi
  if [[ ! "$hermes_uid" =~ ^[0-9]+$ ]]; then
    echo "UID inválido para hermes; se requiere un UID numérico." >&2
    exit 1
  fi
  if ! hermes_reverse=$("$id_bin" -nu "$hermes_uid" 2>/dev/null) \
    || [ "$hermes_reverse" != "hermes" ]; then
    echo "El reverse lookup del UID de hermes no devuelve exactamente hermes." >&2
    exit 1
  fi
  if ! hermes_ownership=$("$ps_bin" -U "$hermes_uid" -o unit=,uunit= 2>/dev/null); then
    echo "No se pudo verificar el ownership persistente de hermes; se aborta cerrado." >&2
    exit 1
  fi
  while read -r system_unit user_unit extra; do
    [ -z "${system_unit:-}" ] && continue
    if [ "$system_unit" != "user@$hermes_uid.service" ] \
      || [ -n "${extra:-}" ]; then
      echo "Ownership persistente inesperado para hermes; revisar units antes de provisionar." >&2
      exit 1
    fi
    case "$user_unit" in
      init.scope|hermes-gateway.service|hermes-dashboard.service) ;;
      dbus.service)
        dbus_seen=$((dbus_seen + 1))
        if [ "$dbus_seen" -ne 1 ]; then
          echo "Unit persistente duplicada para hermes: dbus.service" >&2
          exit 1
        fi
        ;;
      *)
        if [[ "$user_unit" =~ ^[A-Za-z0-9_.@:-]+$ ]]; then
          echo "Unit persistente inesperada para hermes: $user_unit" >&2
        else
          echo "Unit persistente inesperada para hermes." >&2
        fi
        exit 1
        ;;
    esac
  done <<< "$hermes_ownership"

  # `ps` sólo cubre procesos vivos. Una unit habilitada pero hoy inactiva
  # también puede arrancar mañana dentro del user slice, por lo que el
  # inventario persistente es un gate separado y obligatorio.
  if ! enabled_system_units=$("$systemctl_bin" list-unit-files \
    --type=service --state=enabled --no-legend --no-pager); then
    echo "No se pudieron enumerar las units de sistema habilitadas; se aborta cerrado." >&2
    exit 1
  fi
  while read -r unit_name _ extra; do
    [ -z "${unit_name:-}" ] && continue
    if ! unit_user=$("$systemctl_bin" show "$unit_name" \
      --property=User --value); then
      echo "No se pudo leer el User efectivo de una unit habilitada; se aborta cerrado." >&2
      exit 1
    fi
    if [ "$unit_user" != "hermes" ] && [ "$unit_user" != "$hermes_uid" ]; then
      continue
    fi
    case "$unit_name" in
      hermes-gateway.service|hermes-dashboard.service|"user@$hermes_uid.service") ;;
      *)
        if [[ "$unit_name" =~ ^[A-Za-z0-9_.@:-]+$ ]]; then
          echo "Unit de sistema habilitada inesperada para hermes: $unit_name" >&2
        else
          echo "Unit de sistema habilitada inesperada para hermes." >&2
        fi
        exit 1
        ;;
    esac
  done <<< "$enabled_system_units"

  if ! enabled_user_units=$("$systemctl_bin" --user \
    --machine=hermes@.host list-unit-files --type=service --state=enabled \
    --no-legend --no-pager); then
    echo "No se pudieron enumerar las units de usuario habilitadas de hermes; se aborta cerrado." >&2
    exit 1
  fi
  while read -r unit_name _ extra; do
    [ -z "${unit_name:-}" ] && continue
    case "$unit_name" in
      hermes-gateway.service|hermes-dashboard.service) ;;
      session-migration.service)
        session_migration_seen=$((session_migration_seen + 1))
        if [ "$session_migration_seen" -ne 1 ]; then
          echo "Unit de usuario habilitada duplicada para hermes: session-migration.service" >&2
          exit 1
        fi
        ;;
      *)
        if [[ "$unit_name" =~ ^[A-Za-z0-9_.@:-]+$ ]]; then
          echo "Unit de usuario habilitada inesperada para hermes: $unit_name" >&2
        else
          echo "Unit de usuario habilitada inesperada para hermes." >&2
        fi
        exit 1
        ;;
    esac
  done <<< "$enabled_user_units"

  # --- units: instalar solo lo que difiere -------------------------------
  while IFS= read -r rel; do
    dest="$systemd_dir/$rel"
    if [ -f "$dest" ] && cmp -s "$src/$rel" "$dest"; then
      continue
    fi
    echo "==> instala $rel"
    # Sin `install -D`: es GNU-only y los tests corren en el laptop (BSD).
    mkdir -p "$(dirname "$dest")"
    "$install_bin" -o root -g root -m 0644 "$src/$rel" "$dest"
    changed=1
  done < <(cd "$src" && find . -type f | sed 's|^\./||' | sort)

  # El template queda libre de UID en Git; sólo este render runtime crea el
  # path numérico. No hay sustituciones de contenido que puedan inyectar
  # propiedades: el UID validado participa únicamente en el nombre.
  dest="$systemd_dir/user-$hermes_uid.slice.d/50-legaltech-resource-limits.conf"
  if [ ! -f "$dest" ] || ! cmp -s "$template_src" "$dest"; then
    echo "==> instala límite de user-$hermes_uid.slice"
    mkdir -p "$(dirname "$dest")"
    "$install_bin" -o root -g root -m 0644 "$template_src" "$dest"
    changed=1
  fi

  # --- runtime de monitoreo -----------------------------------------------
  # Se conserva el layout relativo de Python; así funcionan tanto imports
  # sibling planos como módulos runtime anidados, sin desplegar tests/caches.
  ensure_runtime_dir() {
    local runtime_dir="$1"
    "$install_bin" -d -o root -g root -m 0755 "$runtime_dir"
    "$chmod_bin" 0755 "$runtime_dir"
    "$chown_bin" root:root "$runtime_dir"
  }
  ensure_runtime_dir "$monitoring_dir"
  while IFS= read -r rel; do
    runtime_parent=$(dirname "$rel")
    runtime_current="$monitoring_dir"
    if [ "$runtime_parent" != "." ]; then
      IFS='/' read -r -a runtime_parts <<< "$runtime_parent"
      for runtime_part in "${runtime_parts[@]}"; do
        [ -n "$runtime_part" ] || continue
        runtime_current="$runtime_current/$runtime_part"
        ensure_runtime_dir "$runtime_current"
      done
    fi
    dest="$monitoring_dir/$rel"
    if [ ! -f "$dest" ] || ! cmp -s "$monitoring_src/$rel" "$dest"; then
      "$install_bin" -o root -g root -m 0644 "$monitoring_src/$rel" "$dest"
    fi
    "$chmod_bin" 0644 "$dest"
    "$chown_bin" root:root "$dest"
  done < <(cd "$monitoring_src" && find . -type f -name '*.py' ! -path './tests/*' ! -path '*/__pycache__/*' | sed 's|^\./||' | sort)

  "$install_bin" -d -o root -g root -m 0750 "$monitor_state_dir"
  "$install_bin" -d -o root -g root -m 0750 "$monitor_log_dir"

  # El tracker recibe write access sólo a este inode, no al directorio. Crear
  # cuando falta y nunca truncar un CSV existente.
  if [ ! -e "$resource_csv" ] && [ ! -L "$resource_csv" ]; then
    "$install_bin" -o root -g root -m 0640 /dev/null "$resource_csv"
  elif [ ! -f "$resource_csv" ] || [ -L "$resource_csv" ]; then
    echo "NO SE PUDO asegurar el CSV de recursos como archivo regular." >&2
    rc=1
  fi
  if [ -f "$resource_csv" ] && [ ! -L "$resource_csv" ]; then
    if ! "$chmod_bin" 0640 "$resource_csv"; then
      echo "NO SE PUDO dejar el CSV de recursos en modo 0640." >&2
      rc=1
    fi
    if ! "$chown_bin" root:root "$resource_csv"; then
      echo "NO SE PUDO dejar el CSV de recursos con owner root:root." >&2
      rc=1
    fi
  fi

  # Crear el archivo vacío sólo si no existe. Un symlink o tipo inesperado no
  # se sigue: credenciales futuras deben vivir en un regular root-only.
  if [ ! -e "$monitoring_env_file" ] && [ ! -L "$monitoring_env_file" ]; then
    mkdir -p "$(dirname "$monitoring_env_file")"
    "$install_bin" -o root -g root -m 0600 /dev/null "$monitoring_env_file"
  elif [ ! -f "$monitoring_env_file" ] || [ -L "$monitoring_env_file" ]; then
    echo "NO SE PUDO asegurar $monitoring_env_file como archivo regular root-only." >&2
    exit 1
  fi
  if [ -f "$monitoring_env_file" ] && [ ! -L "$monitoring_env_file" ]; then
    if ! "$chmod_bin" 0600 "$monitoring_env_file"; then
      echo "NO SE PUDO dejar $monitoring_env_file en modo 0600." >&2
      rc=1
    fi
    if ! "$chown_bin" root:root "$monitoring_env_file"; then
      echo "NO SE PUDO dejar $monitoring_env_file con owner root:root." >&2
      rc=1
    fi
  fi

  mkdir -p "$(dirname "$logrotate_dest")"
  if [ ! -f "$logrotate_dest" ] || ! cmp -s "$logrotate_src" "$logrotate_dest"; then
    "$install_bin" -o root -g root -m 0644 "$logrotate_src" "$logrotate_dest"
  fi
  "$chmod_bin" 0644 "$logrotate_dest"
  "$chown_bin" root:root "$logrotate_dest"

  # Ninguna configuración parcial se activa. Los archivos systemd pueden
  # haberse copiado, pero un fallo de modo/ownership previo bloquea reload,
  # Caddy, migración de enables legacy y timers.
  if [ "$rc" -ne 0 ]; then
    echo "INCOMPLETO: monitoreo no quedó con permisos seguros; no se activa ninguna configuración." >&2
    exit "$rc"
  fi

  if [ "$changed" -eq 1 ]; then
    "$systemctl_bin" daemon-reload
  else
    echo "units al día"
  fi

  # --- caddy: TLS delante de la API (ops/caddy/README.md) ------------------
  if [ "$skip_caddy" = 0 ] && ! command -v "$caddy_bin" >/dev/null 2>&1; then
    echo "FALTA caddy (apt-get install caddy) — sin él la API queda sin TLS delante." >&2
    rc=1
  elif [ "$skip_caddy" = 0 ] && ! cmp -s "$caddyfile_src" "$caddyfile_dest"; then
    # Validar ANTES de instalar. Sin esto, un Caddyfile roto llegaba a /etc y
    # un `reload || restart` ciego hacía lo peor posible: el reload gracioso
    # de Caddy rechaza el config inválido y SIGUE sirviendo el viejo (diseño,
    # zero-downtime), y el restart de "fallback" mataba ese proceso sano
    # contra el mismo archivo roto — Caddy caído por decisión nuestra.
    if ! "$caddy_bin" validate --config "$caddyfile_src" --adapter caddyfile >/dev/null 2>&1; then
      echo "INVÁLIDO: $caddyfile_src no pasa \`caddy validate\` — no se toca $caddyfile_dest." >&2
      rc=1
    else
      echo "==> instala Caddyfile"
      mkdir -p "$(dirname "$caddyfile_dest")"
      install -m 644 "$caddyfile_src" "$caddyfile_dest"
      # reload solo tiene sentido con la unit andando; parada, se levanta.
      if "$systemctl_bin" is-active --quiet caddy; then
        "$systemctl_bin" reload caddy
      else
        "$systemctl_bin" start caddy
      fi
    fi
  fi

  # --- .env: acceso compartido API/worker; nombres, jamás valores --------
  # API corre como www-data:estrado y worker como estrado. 0600 para el
  # owner de la API deja al worker en crash-loop; 0640 comparte sólo con el
  # grupo de servicio y mantiene fuera a otros usuarios.
  if [ ! -f "$env_file" ] || [ -L "$env_file" ]; then
    echo "FALTA $env_file como archivo regular (0640 $env_owner:$env_group). Variables requeridas en ops/env.inventory; los valores se reponen desde el gestor de secretos." >&2
    rc=1
  else
    # Restringir primero y SIEMPRE intentar ambas operaciones. Un chown que
    # falle durante una reconstrucción no puede dejar un secreto 0644/0666.
    if ! chmod 640 "$env_file"; then
      echo "NO SE PUDO dejar $env_file en modo 0640." >&2
      rc=1
    fi
    if ! chown "$env_owner:$env_group" "$env_file"; then
      echo "NO SE PUDO dejar $env_file con owner $env_owner:$env_group." >&2
      rc=1
    fi
    local missing extra cookie_store_value
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
    cookie_store_value=$(awk -F= '$1 == "COOKIE_STORE_PATH" {print substr($0, index($0, "=") + 1); exit}' "$env_file")
    if [ "$cookie_store_value" != "/var/lib/estrado-pjud/cookies.json" ]; then
      echo "COOKIE_STORE_PATH debe ser /var/lib/estrado-pjud/cookies.json para usar el StateDirectory privado compartido; no se muestra el valor actual." >&2
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

  # --- cron instalado (el contenido del crontab lo vigila el watchdog #10) --
  if [ ! -f "$cron_dir/run-cron.sh" ]; then
    echo "FALTA $cron_dir/run-cron.sh — un VPS reconstruido sin los crons revive el silencio de los 120 días. Correr ops/cron/deploy-cron.sh desde el laptop e instalar crontab.snapshot a mano (ver ops/cron/README.md)." >&2
    rc=1
  fi

  local enable_units=()
  # Versiones previas habilitaban ambos monitores como servicios permanentes.
  # Quitar esos symlinks legacy antes de activar los timers evita dos caminos
  # de scheduling sin interrumpir un oneshot que el timer esté ejecutando.
  "$systemctl_bin" disable \
    legaltech-monitor.service legaltech-resource-tracker.service
  if [ "$enable_pjud_worker" = "1" ]; then
    enable_units+=(estrado-pjud-worker.service)
  fi
  enable_units+=(
    estrado-pjud.service
    legaltech-monitor.timer
    legaltech-resource-tracker.timer
  )
  "$systemctl_bin" enable "${enable_units[@]}"

  if [ "$rc" -eq 0 ]; then
    echo "OK: units y timers instalados; worker sólo habilitado con PROV_ENABLE_PJUD_WORKER=1; monitoreo, .env, venv y usuarios presentes."
  else
    echo "INCOMPLETO: resolver lo listado arriba y volver a correr (es idempotente)." >&2
  fi
  exit "$rc"
}

main "$@"
