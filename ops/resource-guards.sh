#!/usr/bin/env bash
# Fail-closed, host-local rollout and rollback for LegalTech resource guards.
# Test overrides are accepted only as one complete, explicit fake-host boundary.
set -euo pipefail

readonly EXIT_ERROR=1
readonly EXIT_USAGE=2
readonly MIN_DISK_BYTES=8589934592
readonly MIN_RAM_BYTES=6442450944
readonly ACTIVE_LEASE_SECONDS=14400
readonly HEARTBEAT_MAX_AGE_SECONDS=300

usage() {
  printf '%s\n' \
    'usage: resource-guards.sh preflight|apply --expected-sha <40-hex-sha>' \
    '       resource-guards.sh postflight' \
    '       resource-guards.sh rollback --backup-dir <timestamped-backup>' >&2
  exit "$EXIT_USAGE"
}

fail() {
  printf '%s\n' "ERROR: ${1:-resource guard state is unsafe or unknown}" >&2
  return "$EXIT_ERROR"
}

is_uint() {
  case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac
}

readonly -a OVERRIDE_NAMES=(
  RG_REPO_DIR RG_SYSTEMD_DIR RG_CREDENTIAL_FILE RG_BACKUP_ROOT RG_TMP_ROOT RG_DISK_PATH RG_NULL_FILE
  RG_MONITORING_DIR RG_MONITOR_ENV_FILE RG_FSTAB_FILE RG_SYSCTL_FILE
  RG_CADDYFILE RG_LOGROTATE_FILE RG_JURISTRACK_HEALTH_URL
  RG_ESTRADO_HEALTH_URL RG_GIT_BIN RG_DF_BIN RG_FREE_BIN RG_ID_BIN RG_PS_BIN
  RG_SYSTEMCTL_BIN RG_CURL_BIN RG_DATE_BIN RG_STAT_BIN RG_SHA256_BIN
  RG_FIND_BIN RG_CP_BIN RG_RM_BIN RG_MKDIR_BIN RG_CHMOD_BIN RG_CHOWN_BIN
  RG_MKTEMP_BIN RG_JQ_BIN RG_PROVISION_BIN RG_SWAP_BIN RG_PYTHON_BIN
  RG_TEST_ROOT_UID RG_TEST_ROOT_GID
)

test_mode=${RG_TEST_MODE:-0}
override_present=0
for variable_name in "${OVERRIDE_NAMES[@]}"; do
  if [ "${!variable_name+x}" = x ]; then override_present=1; fi
done
if [ "$test_mode" != 0 ] && [ "$test_mode" != 1 ]; then usage; fi
if [ "$test_mode" != 1 ] && [ "$override_present" -eq 1 ]; then usage; fi
if [ "$test_mode" = 1 ]; then
  for variable_name in "${OVERRIDE_NAMES[@]}"; do
    [ "${!variable_name+x}" = x ] || usage
  done
fi
for variable_name in $(compgen -A variable PROV_ || true) $(compgen -A variable SWAP_ || true); do
  case "$variable_name" in PROV_ENABLE_PJUD_WORKER) ;; *) usage ;; esac
done
if [ "${PROV_ENABLE_PJUD_WORKER:-0}" != 0 ]; then usage; fi

repo_dir=${RG_REPO_DIR:-/opt/legal-tech-microservices}
systemd_dir=${RG_SYSTEMD_DIR:-/etc/systemd/system}
credential_file=${RG_CREDENTIAL_FILE:-$repo_dir/estrado-pjud-service/.env}
backup_root=${RG_BACKUP_ROOT:-/var/backups/legaltech-resource-guards}
tmp_root=${RG_TMP_ROOT:-/tmp}
disk_path=${RG_DISK_PATH:-/}
null_file=${RG_NULL_FILE:-/dev/null}
monitoring_dir=${RG_MONITORING_DIR:-/opt/legaltech-monitoring}
monitor_env_file=${RG_MONITOR_ENV_FILE:-/etc/legaltech-monitoring.env}
fstab_file=${RG_FSTAB_FILE:-/etc/fstab}
sysctl_file=${RG_SYSCTL_FILE:-/etc/sysctl.d/60-legaltech-swap.conf}
caddyfile=${RG_CADDYFILE:-/etc/caddy/Caddyfile}
logrotate_file=${RG_LOGROTATE_FILE:-/etc/logrotate.d/legaltech-resources}
juristrack_health_url=${RG_JURISTRACK_HEALTH_URL:-https://juristrack.cl/}
estrado_health_url=${RG_ESTRADO_HEALTH_URL:-https://estrado.juristrack.cl/api/v1/health}

git_bin=${RG_GIT_BIN:-/usr/bin/git}
df_bin=${RG_DF_BIN:-/usr/bin/df}
free_bin=${RG_FREE_BIN:-/usr/bin/free}
id_bin=${RG_ID_BIN:-/usr/bin/id}
ps_bin=${RG_PS_BIN:-/usr/bin/ps}
systemctl_bin=${RG_SYSTEMCTL_BIN:-/usr/bin/systemctl}
curl_bin=${RG_CURL_BIN:-/usr/bin/curl}
date_bin=${RG_DATE_BIN:-/usr/bin/date}
stat_bin=${RG_STAT_BIN:-/usr/bin/stat}
sha256_bin=${RG_SHA256_BIN:-/usr/bin/sha256sum}
find_bin=${RG_FIND_BIN:-/usr/bin/find}
cp_bin=${RG_CP_BIN:-/usr/bin/cp}
rm_bin=${RG_RM_BIN:-/usr/bin/rm}
mkdir_bin=${RG_MKDIR_BIN:-/usr/bin/mkdir}
chmod_bin=${RG_CHMOD_BIN:-/usr/bin/chmod}
chown_bin=${RG_CHOWN_BIN:-/usr/bin/chown}
mktemp_bin=${RG_MKTEMP_BIN:-/usr/bin/mktemp}
jq_bin=${RG_JQ_BIN:-/usr/bin/jq}
provision_bin=${RG_PROVISION_BIN:-$repo_dir/ops/provision.sh}
swap_bin=${RG_SWAP_BIN:-$repo_dir/ops/swap/configure-swap.sh}
python_bin=${RG_PYTHON_BIN:-/usr/bin/python3}
root_uid=${RG_TEST_ROOT_UID:-0}
root_gid=${RG_TEST_ROOT_GID:-0}

for absolute_value in \
  "$repo_dir" "$systemd_dir" "$credential_file" "$backup_root" "$tmp_root" "$disk_path" "$null_file" \
  "$monitoring_dir" "$monitor_env_file" "$fstab_file" "$sysctl_file" \
  "$caddyfile" "$logrotate_file" "$git_bin" "$df_bin" "$free_bin" \
  "$id_bin" "$ps_bin" "$systemctl_bin" "$curl_bin" "$date_bin" \
  "$stat_bin" "$sha256_bin" "$find_bin" "$cp_bin" "$rm_bin" \
  "$mkdir_bin" "$chmod_bin" "$chown_bin" "$mktemp_bin" "$jq_bin" \
  "$provision_bin" "$swap_bin" "$python_bin"
do
  case "$absolute_value" in /*) ;; *) usage ;; esac
done
is_uint "$root_uid" || usage
is_uint "$root_gid" || usage

if [ "$test_mode" != 1 ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
  fail 'must run as root' || exit "$EXIT_ERROR"
fi

command_name=${1:-}
shift || true
expected_sha=''
requested_backup=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --expected-sha)
      [ "$#" -ge 2 ] && [ -z "$expected_sha" ] || usage
      expected_sha=$2
      shift 2
      ;;
    --backup-dir)
      [ "$#" -ge 2 ] && [ -z "$requested_backup" ] || usage
      requested_backup=$2
      shift 2
      ;;
    *) usage ;;
  esac
done

case "$command_name" in
  preflight|apply)
    [ -n "$expected_sha" ] && [ -z "$requested_backup" ] || usage
    [[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || usage
    ;;
  postflight) [ -z "$expected_sha" ] && [ -z "$requested_backup" ] || usage ;;
  rollback) [ -z "$expected_sha" ] && [ -n "$requested_backup" ] || usage ;;
  *) usage ;;
esac

temp_dir=''
curl_header_file=''
cleanup() {
  if [ -n "$temp_dir" ]; then
    case "$temp_dir" in "$tmp_root"/legaltech-resource-guards.*) "$rm_bin" -rf -- "$temp_dir" >"$null_file" 2>&1 || true ;; esac
  fi
}
trap cleanup EXIT

stat_fields() {
  "$stat_bin" --format='%a|%u|%g|%h' -- "$1" 2>"$null_file"
}

path_has_symlink_component() {
  local current=$1 parent
  while [ "$current" != / ] && [ -n "$current" ]; do
    [ ! -L "$current" ] || return 0
    parent=${current%/*}
    [ -n "$parent" ] || parent=/
    [ "$parent" != "$current" ] || break
    current=$parent
  done
  return 1
}

safe_existing_path() {
  local path=$1 fields links
  [ -e "$path" ] && [ ! -L "$path" ] || return 1
  path_has_symlink_component "$path" && return 1
  if [ -f "$path" ]; then
    fields=$(stat_fields "$path") || return 1
    links=${fields##*|}
    [ "$links" = 1 ] || return 1
  elif [ -d "$path" ]; then
    [ -z "$("$find_bin" "$path" -type l -print -quit 2>"$null_file")" ] || return 1
    [ -z "$("$find_bin" "$path" -type f -links +1 -print -quit 2>"$null_file")" ] || return 1
  else
    return 1
  fi
}

prepare_temp_credentials() {
  local url_count=0 key_count=0 line value credential_fields credential_mode credential_owner _credential_group _credential_links
  [ -d "$tmp_root" ] && [ ! -L "$tmp_root" ] || return 1
  path_has_symlink_component "$tmp_root" && return 1
  safe_existing_path "$credential_file" || return 1
  credential_fields=$(stat_fields "$credential_file") || return 1
  IFS='|' read -r credential_mode credential_owner _credential_group _credential_links <<< "$credential_fields"
  [ "$credential_owner" = "$root_uid" ] || return 1
  if [ "$test_mode" = 1 ]; then
    [ "$credential_mode" = 600 ] || [ "$credential_mode" = 640 ] || return 1
  else
    [ "$credential_mode" = 640 ] || return 1
  fi
  SUPABASE_URL=''
  SUPABASE_SERVICE_KEY=''
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    case "$line" in
      SUPABASE_URL=*)
        url_count=$((url_count + 1))
        value=${line#SUPABASE_URL=}
        SUPABASE_URL=$value
        ;;
      SUPABASE_SERVICE_KEY=*)
        key_count=$((key_count + 1))
        value=${line#SUPABASE_SERVICE_KEY=}
        SUPABASE_SERVICE_KEY=$value
        ;;
    esac
  done < "$credential_file"
  [ "$url_count" -eq 1 ] && [ "$key_count" -eq 1 ] \
    && [ -n "$SUPABASE_URL" ] && [ -n "$SUPABASE_SERVICE_KEY" ] || return 1
  if [ "$test_mode" = 1 ]; then
    [[ "$SUPABASE_URL" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[^\"[:space:]]*)?$ ]] || return 1
  else
    [[ "$SUPABASE_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[^\"[:space:]]*)?$ ]] || return 1
  fi
  case "$SUPABASE_URL$SUPABASE_SERVICE_KEY" in *$'\n'*|*$'\r'*) return 1 ;; esac

  if [ -z "$temp_dir" ]; then
    temp_dir=$("$mktemp_bin" -d "$tmp_root/legaltech-resource-guards.XXXXXX") || return 1
    "$chmod_bin" 0700 "$temp_dir" || return 1
    "$chown_bin" "$root_uid:$root_gid" "$temp_dir" || return 1
    validate_root_path "$temp_dir" 700 || return 1
  fi
  curl_header_file="$temp_dir/supabase.headers"
  umask 077
  {
    printf 'apikey: %s\n' "$SUPABASE_SERVICE_KEY"
    printf 'Authorization: Bearer %s\n' "$SUPABASE_SERVICE_KEY"
  } > "$curl_header_file" || return 1
  "$chmod_bin" 0600 "$curl_header_file" || return 1
  "$chown_bin" "$root_uid:$root_gid" "$curl_header_file" || return 1
  unset SUPABASE_SERVICE_KEY
}

write_curl_config() { # config, url, output, optional request, optional dump header
  local config=$1 url=$2 output=$3 request=${4:-} dump_header=${5:-}
  umask 077
  {
    printf 'silent\nshow-error\nmax-time = 20\nconnect-timeout = 5\n'
    printf 'header = "@%s"\n' "$curl_header_file"
    printf 'url = "%s"\n' "$url"
    printf 'output = "%s"\n' "$output"
    [ -z "$request" ] || printf 'request = "%s"\n' "$request"
    if [ -n "$dump_header" ]; then
      printf 'dump-header = "%s"\n' "$dump_header"
      printf 'header = "Prefer: count=exact"\nheader = "Range: 0-0"\n'
    fi
  } > "$config" || return 1
  "$chmod_bin" 0600 "$config" || return 1
  "$chown_bin" "$root_uid:$root_gid" "$config" || return 1
}

heartbeat_is_fresh() {
  local config="$temp_dir/heartbeat.curl" body="$temp_dir/heartbeat.body"
  local http timestamp now_epoch heartbeat_epoch age
  write_curl_config "$config" \
    "${SUPABASE_URL%/}/rest/v1/sync_worker_heartbeats?select=status,last_heartbeat_at&order=last_heartbeat_at.desc&limit=1" \
    "$body" || return 1
  if ! http=$("$curl_bin" --config "$config" --write-out '%{http_code}' 2>"$null_file"); then return 1; fi
  [ "$http" = 200 ] || return 1
  if ! timestamp=$("$jq_bin" -er '
    if type == "array" and length == 1 and
       (.[0] | type == "object") and
       (.[0] | keys | sort) == ["last_heartbeat_at", "status"] and
       (.[0].status | IN("starting", "paused", "running", "backoff", "idle_off_hours", "stopped")) and
       (.[0].last_heartbeat_at | type == "string")
    then .[0].last_heartbeat_at else empty end
  ' "$body" 2>"$null_file"); then return 1; fi
  now_epoch=$(LC_ALL=C "$date_bin" -u +%s 2>"$null_file") || return 1
  heartbeat_epoch=$(LC_ALL=C "$date_bin" -u -d "$timestamp" +%s 2>"$null_file") || return 1
  is_uint "$now_epoch" && is_uint "$heartbeat_epoch" || return 1
  [ "$now_epoch" -ge "$heartbeat_epoch" ] || return 1
  age=$((now_epoch - heartbeat_epoch))
  [ "$age" -le "$HEARTBEAT_MAX_AGE_SECONDS" ]
}

active_claim_count() {
  local now_epoch cutoff_epoch cutoff config headers http line normalized value
  local found=0 count=''
  now_epoch=$(LC_ALL=C "$date_bin" -u +%s 2>"$null_file") || return 1
  is_uint "$now_epoch" && [ "$now_epoch" -ge "$ACTIVE_LEASE_SECONDS" ] || return 1
  cutoff_epoch=$((now_epoch - ACTIVE_LEASE_SECONDS))
  cutoff=$(LC_ALL=C "$date_bin" -u -d "@$cutoff_epoch" +%Y-%m-%dT%H:%M:%SZ 2>"$null_file") || return 1
  [ -n "$cutoff" ] || return 1
  config="$temp_dir/claims.curl"
  headers="$temp_dir/claims.headers"
  write_curl_config "$config" \
    "${SUPABASE_URL%/}/rest/v1/cases?select=id&sync_worker_id=not.is.null&sync_claimed_at=gte.$cutoff" \
    "$null_file" HEAD "$headers" || return 1
  if ! http=$("$curl_bin" --config "$config" --write-out '%{http_code}' 2>"$null_file"); then return 1; fi
  [ "$http" = 200 ] && [ -f "$headers" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    normalized=${line,,}
    case "$normalized" in
      content-range:*)
        found=$((found + 1))
        value=${normalized#content-range:}
        value=${value#${value%%[![:space:]]*}}
        case "$value" in
          '*/0') count=0 ;;
          0-0/*)
            count=${value#0-0/}
            is_uint "$count" || return 1
            ;;
          *) return 1 ;;
        esac
        ;;
    esac
  done < "$headers"
  [ "$found" -eq 1 ] && is_uint "$count" || return 1
  printf '%s\n' "$count"
}

check_exact_zero_and_fresh_heartbeat() {
  local count
  heartbeat_is_fresh || return 1
  count=$(active_claim_count) || return 1
  [ "$count" = 0 ]
}

check_git() {
  local status actual
  status=$(cd "$repo_dir" && "$git_bin" status --porcelain --untracked-files=all) || return 1
  [ -z "$status" ] || return 1
  actual=$(cd "$repo_dir" && "$git_bin" rev-parse HEAD) || return 1
  [ "$actual" = "$expected_sha" ]
}

check_disk() {
  local _filesystem _blocks _used available _capacity _mount last=''
  while read -r _filesystem _blocks _used available _capacity _mount; do
    last=$available
  done < <(LC_ALL=C "$df_bin" -Pk "$disk_path" 2>"$null_file") || return 1
  is_uint "$last" || return 1
  [ $((last * 1024)) -ge "$MIN_DISK_BYTES" ]
}

check_ram() {
  local label _total _used _free _shared _cache available extra
  available=''
  while read -r label _total _used _free _shared _cache available extra; do
    [ "$label" = Mem: ] || continue
    [ -z "${extra:-}" ] || return 1
    break
  done < <(LC_ALL=C "$free_bin" -b 2>"$null_file") || return 1
  is_uint "$available" || return 1
  [ "$available" -ge "$MIN_RAM_BYTES" ]
}

validate_hermes_inventory() {
  local uid reverse ownership system_units user_units system_unit user_unit extra
  local unit_name _unit_state unit_user
  uid=$("$id_bin" -u hermes 2>"$null_file") || return 1
  is_uint "$uid" || return 1
  reverse=$("$id_bin" -nu "$uid" 2>"$null_file") || return 1
  [ "$reverse" = hermes ] || return 1
  ownership=$("$ps_bin" -U "$uid" -o unit=,uunit= 2>"$null_file") || return 1
  while read -r system_unit user_unit extra; do
    [ -n "${system_unit:-}" ] || continue
    [ "$system_unit" = "user@$uid.service" ] && [ -z "${extra:-}" ] || return 1
    case "$user_unit" in init.scope|hermes-gateway.service|hermes-dashboard.service) ;; *) return 1 ;; esac
  done <<< "$ownership"
  system_units=$("$systemctl_bin" list-unit-files --type=service --state=enabled --no-legend --no-pager 2>"$null_file") || return 1
  while read -r unit_name _unit_state extra; do
    [ -n "${unit_name:-}" ] || continue
    [ -z "${extra:-}" ] || return 1
    unit_user=$("$systemctl_bin" show "$unit_name" --property User --value 2>"$null_file") || return 1
    if [ "$unit_user" = hermes ] || [ "$unit_user" = "$uid" ]; then
      case "$unit_name" in hermes-gateway.service|hermes-dashboard.service|"user@$uid.service") ;; *) return 1 ;; esac
    fi
  done <<< "$system_units"
  user_units=$("$systemctl_bin" --user --machine=hermes@.host list-unit-files --type=service --state=enabled --no-legend --no-pager 2>"$null_file") || return 1
  while read -r unit_name _unit_state extra; do
    [ -n "${unit_name:-}" ] || continue
    [ -z "${extra:-}" ] || return 1
    case "$unit_name" in hermes-gateway.service|hermes-dashboard.service) ;; *) return 1 ;; esac
  done <<< "$user_units"
  printf '%s\n' "$uid"
}

check_public_health() {
  local url code
  for url in "$juristrack_health_url" "$estrado_health_url"; do
    if ! code=$("$curl_bin" --silent --show-error --output "$null_file" \
      --write-out '%{http_code}' --connect-timeout 5 --max-time 10 "$url" 2>"$null_file"); then return 1; fi
    [ "$code" = 200 ] || return 1
  done
}

run_preflight() {
  local uid
  check_git || { fail 'Git tree or deployed SHA is not exact'; return 1; }
  safe_existing_path "$provision_bin" && [ -x "$provision_bin" ] \
    && safe_existing_path "$swap_bin" && [ -x "$swap_bin" ] \
    || { fail 'reviewed provision or swap executable is unsafe'; return 1; }
  check_disk || { fail 'free disk is unavailable or below 8 GiB'; return 1; }
  check_ram || { fail 'MemAvailable is unavailable or below 6 GiB'; return 1; }
  uid=$(validate_hermes_inventory) || { fail 'Hermes UID or persistent inventory is unknown'; return 1; }
  [ -n "$uid" ] || { fail 'Hermes UID is unknown'; return 1; }
  check_public_health || { fail 'a public health endpoint is not exactly HTTP 200'; return 1; }
  prepare_temp_credentials || { fail 'protected Supabase configuration is invalid'; return 1; }
  check_exact_zero_and_fresh_heartbeat || { fail 'worker heartbeat or exact active-claim count is unsafe'; return 1; }
  "$swap_bin" preflight >"$null_file" 2>&1 || { fail 'swap preflight is unsafe'; return 1; }
  swap_preexisting=0
  if "$swap_bin" verify >"$null_file" 2>&1; then swap_preexisting=1; fi
  printf '%s\n' 'PREFLIGHT OK'
}

managed_paths=()
build_managed_paths() {
  local uid=$1
  managed_paths=(
    "$systemd_dir/legaltech.slice"
    "$systemd_dir/estrado-pjud.service"
    "$systemd_dir/estrado-pjud-worker.service"
    "$systemd_dir/estrado-pjud-worker.service.d/xvfb.conf"
    "$systemd_dir/legaltech-monitor.service"
    "$systemd_dir/legaltech-resource-tracker.service"
    "$systemd_dir/legaltech-monitor.timer"
    "$systemd_dir/legaltech-resource-tracker.timer"
    "$systemd_dir/user-$uid.slice.d/50-legaltech-resource-limits.conf"
    "$credential_file"
    "$monitor_env_file"
    "$fstab_file"
    "$sysctl_file"
    "$caddyfile"
    "$logrotate_file"
    "$monitoring_dir"
  )
}

is_managed_path() {
  local candidate=$1 path
  for path in "${managed_paths[@]}"; do [ "$candidate" != "$path" ] || return 0; done
  return 1
}

validate_root_path() { # path mode
  local fields mode uid gid links expected_mode=$2
  fields=$(stat_fields "$1") || return 1
  IFS='|' read -r mode uid gid links <<< "$fields"
  [ "$mode" = "$expected_mode" ] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ]
}

create_backup() {
  local uid=$1 timestamp path index=0 rel fields mode owner group links existed
  path_has_symlink_component "$backup_root" && return 1
  if [ ! -e "$backup_root" ]; then
    "$mkdir_bin" -p -- "$backup_root" || return 1
    "$chmod_bin" 0700 "$backup_root" || return 1
    "$chown_bin" "$root_uid:$root_gid" "$backup_root" || return 1
  else
    safe_existing_path "$backup_root" || return 1
  fi
  validate_root_path "$backup_root" 700 || return 1
  timestamp=$(LC_ALL=C "$date_bin" -u +%Y%m%dT%H%M%SZ 2>"$null_file") || return 1
  [[ "$timestamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || return 1
  backup_dir="$backup_root/$timestamp"
  [ ! -e "$backup_dir" ] && [ ! -L "$backup_dir" ] || return 1
  "$mkdir_bin" -- "$backup_dir" "$backup_dir/entries" || return 1
  "$chmod_bin" 0700 "$backup_dir" "$backup_dir/entries" || return 1
  "$chown_bin" "$root_uid:$root_gid" "$backup_dir" "$backup_dir/entries" || return 1
  validate_root_path "$backup_dir" 700 && validate_root_path "$backup_dir/entries" 700 || return 1
  manifest="$backup_dir/manifest.tsv"
  umask 077
  : > "$manifest" || return 1
  for path in "${managed_paths[@]}"; do
    index=$((index + 1))
    rel=$(printf 'entries/%04d' "$index")
    if [ -e "$path" ] || [ -L "$path" ]; then
      safe_existing_path "$path" || return 1
      fields=$(stat_fields "$path") || return 1
      IFS='|' read -r mode owner group links <<< "$fields"
      [[ "$mode" =~ ^[0-7]{3,4}$ ]] && is_uint "$owner" && is_uint "$group" && is_uint "$links" || return 1
      "$cp_bin" -a -- "$path" "$backup_dir/$rel" || return 1
      if [ "$path" = "$credential_file" ] || [ "$path" = "$monitor_env_file" ]; then
        "$chmod_bin" 0600 "$backup_dir/$rel" || return 1
        "$chown_bin" "$root_uid:$root_gid" "$backup_dir/$rel" || return 1
      fi
      existed=1
    else
      path_has_symlink_component "${path%/*}" && return 1
      rel=- mode=- owner=- group=- existed=0
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$path" "$existed" "$rel" "$mode" "$owner" "$group" >> "$manifest" || return 1
  done
  "$chmod_bin" 0600 "$manifest" || return 1
  "$chown_bin" "$root_uid:$root_gid" "$manifest" || return 1
  validate_root_path "$manifest" 600 || return 1
  printf '%s\n' "$expected_sha" > "$backup_dir/expected-sha" || return 1
  : > "$backup_dir/changes" || return 1
  if [ "${swap_preexisting:-0}" -eq 1 ]; then
    printf '%s\n' preexisting > "$backup_dir/swap-state" || return 1
  else
    printf '%s\n' 'not-attempted' > "$backup_dir/swap-state" || return 1
  fi
  "$chmod_bin" 0600 "$backup_dir/expected-sha" "$backup_dir/changes" "$backup_dir/swap-state" || return 1
  "$chown_bin" "$root_uid:$root_gid" "$backup_dir/expected-sha" "$backup_dir/changes" "$backup_dir/swap-state" || return 1
  printf '%s\n' "$backup_dir"
}

digest_path() {
  local digest
  if [ -f "$1" ] && [ ! -L "$1" ]; then
    digest=$("$sha256_bin" "$1" 2>"$null_file") || return 1
    [[ "$digest" =~ ^[0-9a-fA-F]{64}$ ]] || return 1
    printf '%s\n' "${digest,,}"
  elif [ ! -e "$1" ] && [ ! -L "$1" ]; then
    printf '%s\n' absent
  else
    return 1
  fi
}

record_change() { printf '%s\n' "$1" >> "$backup_dir/changes"; }

show_contract() { # unit property expected [property expected ...]
  local unit=$1 output property expected line key value count index
  local -a arguments=() properties=() expected_values=()
  shift
  [ $(( $# % 2 )) -eq 0 ] || return 1
  while [ "$#" -gt 0 ]; do
    arguments+=("--property=$1")
    properties+=("$1")
    expected_values+=("$2")
    shift 2
  done
  output=$("$systemctl_bin" show "$unit" "${arguments[@]}" 2>"$null_file") || return 1
  for ((index = 0; index < ${#properties[@]}; index++)); do
    property=${properties[$index]}
    expected=${expected_values[$index]}
    count=0
    while IFS= read -r line || [ -n "$line" ]; do
      key=${line%%=*}
      value=${line#*=}
      if [ "$key" = "$property" ]; then
        count=$((count + 1))
        [ "$value" = "$expected" ] || return 1
      fi
    done <<< "$output"
    [ "$count" -eq 1 ] || return 1
  done
}

run_postflight() {
  local uid timer
  uid=$(validate_hermes_inventory) || { fail 'postflight Hermes inventory is unknown'; return 1; }
  show_contract legaltech.slice CPUWeight 1000 MemoryLow 3221225472 MemoryHigh 6442450944 MemoryMax 8589934592 || return 1
  show_contract estrado-pjud.service Slice legaltech.slice TasksMax 512 || return 1
  show_contract estrado-pjud-worker.service Slice legaltech.slice MemoryHigh 2147483648 MemoryMax 3221225472 CPUQuotaPerSecUSec 2s CPUWeight 800 TasksMax 512 || return 1
  show_contract "user-$uid.slice" MemoryHigh 2147483648 MemoryMax 2621440000 TasksMax 1024 CPUWeight 200 || return 1
  for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
    show_contract "$unit" Slice system.slice MemoryMax 134217728 CPUQuotaPerSecUSec 200ms TasksMax 64 || return 1
  done
  for timer in legaltech-monitor.timer legaltech-resource-tracker.timer; do
    [ "$("$systemctl_bin" is-enabled "$timer" 2>"$null_file")" = enabled ] || return 1
    [ "$("$systemctl_bin" is-active "$timer" 2>"$null_file")" = active ] || return 1
  done
  "$swap_bin" verify >"$null_file" 2>&1 || return 1
  check_public_health || return 1
  printf '%s\n' 'POSTFLIGHT OK'
}

validate_backup_dir() {
  local leaf=${1##*/}
  case "$1" in "$backup_root"/*) ;; *) return 1 ;; esac
  [ "${1%/*}" = "$backup_root" ] || return 1
  [[ "$leaf" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || return 1
  safe_existing_path "$1" && validate_root_path "$1" 700 || return 1
  [ -f "$1/manifest.tsv" ] && [ ! -L "$1/manifest.tsv" ] || return 1
  validate_root_path "$1/manifest.tsv" 600 || return 1
  [ -f "$1/changes" ] && [ ! -L "$1/changes" ] || return 1
  validate_root_path "$1/changes" 600 || return 1
  [ -f "$1/swap-state" ] && [ ! -L "$1/swap-state" ] || return 1
  validate_root_path "$1/swap-state" 600 || return 1
}

validate_manifest() {
  local path existed rel mode owner group extra backup_path fields bmode bowner bgroup _blinks
  local seen='|' seen_rel='|' count=0 managed
  while IFS=$'\t' read -r path existed rel mode owner group extra; do
    [ -n "$path" ] && [ -z "${extra:-}" ] && is_managed_path "$path" || return 1
    case "$seen" in *"|$path|"*) return 1 ;; esac
    seen="$seen$path|"
    count=$((count + 1))
    case "$existed" in
      1)
        [[ "$mode" =~ ^[0-7]{3,4}$ ]] && is_uint "$owner" && is_uint "$group" || return 1
        case "$rel" in entries/[0-9][0-9][0-9][0-9]) ;; *) return 1 ;; esac
        case "$seen_rel" in *"|$rel|"*) return 1 ;; esac
        seen_rel="$seen_rel$rel|"
        backup_path="$backup_dir/$rel"
        safe_existing_path "$backup_path" || return 1
        fields=$(stat_fields "$backup_path") || return 1
        IFS='|' read -r bmode bowner bgroup _blinks <<< "$fields"
        if [ "$path" = "$credential_file" ] || [ "$path" = "$monitor_env_file" ]; then
          [ "$bmode" = 600 ] && [ "$bowner" = "$root_uid" ] && [ "$bgroup" = "$root_gid" ] || return 1
        else
          [ "$bmode" = "$mode" ] && [ "$bowner" = "$owner" ] && [ "$bgroup" = "$group" ] || return 1
        fi
        ;;
      0) [ "$rel" = - ] && [ "$mode" = - ] && [ "$owner" = - ] && [ "$group" = - ] || return 1 ;;
      *) return 1 ;;
    esac
  done < "$backup_dir/manifest.tsv"
  [ "$count" -eq "${#managed_paths[@]}" ] || return 1
  for managed in "${managed_paths[@]}"; do case "$seen" in *"|$managed|"*) ;; *) return 1 ;; esac; done
}

load_and_validate_changes() {
  local change
  changed_api=0 changed_worker=0 changed_hermes=0
  while IFS= read -r change || [ -n "$change" ]; do
    case "$change" in
      api) [ "$changed_api" -eq 0 ] || return 1; changed_api=1 ;;
      worker) [ "$changed_worker" -eq 0 ] || return 1; changed_worker=1 ;;
      hermes) [ "$changed_hermes" -eq 0 ] || return 1; changed_hermes=1 ;;
      '') ;;
      *) return 1 ;;
    esac
  done < "$backup_dir/changes"
}

restore_manifest() { # skip swap-owned fstab/sysctl when swap rollback was unsafe
  local skip_swap=$1 path existed rel mode owner group extra backup_path fields bmode bowner bgroup _blinks
  local rc=0
  while IFS=$'\t' read -r path existed rel mode owner group extra; do
    [ -n "$path" ] && [ -z "${extra:-}" ] && is_managed_path "$path" || { rc=1; continue; }
    if [ "$skip_swap" -eq 1 ] && { [ "$path" = "$fstab_file" ] || [ "$path" = "$sysctl_file" ]; }; then continue; fi
    case "$existed" in
      1)
        case "$rel" in entries/[0-9][0-9][0-9][0-9]) ;; *) rc=1; continue ;; esac
        backup_path="$backup_dir/$rel"
        safe_existing_path "$backup_path" || { rc=1; continue; }
        fields=$(stat_fields "$backup_path") || { rc=1; continue; }
        IFS='|' read -r bmode bowner bgroup _blinks <<< "$fields"
        if [ "$path" = "$credential_file" ] || [ "$path" = "$monitor_env_file" ]; then
          [ "$bmode" = 600 ] && [ "$bowner" = "$root_uid" ] && [ "$bgroup" = "$root_gid" ] || { rc=1; continue; }
        else
          [ "$bmode" = "$mode" ] && [ "$bowner" = "$owner" ] && [ "$bgroup" = "$group" ] || { rc=1; continue; }
        fi
        if [ -e "$path" ] || [ -L "$path" ]; then
          safe_existing_path "$path" || { rc=1; continue; }
          "$rm_bin" -rf -- "$path" || { rc=1; continue; }
        fi
        "$mkdir_bin" -p -- "${path%/*}" || { rc=1; continue; }
        if "$cp_bin" -a -- "$backup_path" "$path"; then
          "$chmod_bin" "$mode" "$path" || rc=1
          "$chown_bin" "$owner:$group" "$path" || rc=1
        else
          rc=1
        fi
        ;;
      0)
        [ "$rel" = - ] && [ "$mode" = - ] && [ "$owner" = - ] && [ "$group" = - ] || { rc=1; continue; }
        if [ -e "$path" ] || [ -L "$path" ]; then
          safe_existing_path "$path" || { rc=1; continue; }
          "$rm_bin" -rf -- "$path" || rc=1
        fi
        ;;
      *) rc=1 ;;
    esac
  done < "$backup_dir/manifest.tsv"
  return "$rc"
}

do_rollback() {
  local uid rollback_rc=0 swap_rc=0 swap_state
  backup_dir=$1
  uid=$(validate_hermes_inventory) || { fail 'rollback cannot validate Hermes inventory'; return 1; }
  build_managed_paths "$uid"
  validate_backup_dir "$backup_dir" || { fail 'rollback backup is unsafe or invalid'; return 1; }
  validate_manifest || { fail 'rollback manifest is unsafe or invalid'; return 1; }
  load_and_validate_changes || { fail 'rollback affected-unit metadata is invalid'; return 1; }
  swap_state=$(<"$backup_dir/swap-state")
  case "$swap_state" in
    attempted)
      if ! "$swap_bin" rollback >"$null_file" 2>&1; then swap_rc=1; rollback_rc=1; fi
      ;;
    not-attempted|preexisting) ;;
    *) fail 'rollback swap metadata is invalid'; return 1 ;;
  esac
  restore_manifest "$swap_rc" || rollback_rc=1
  "$systemctl_bin" daemon-reload >"$null_file" 2>&1 || rollback_rc=1
  if [ "$changed_api" -eq 1 ]; then "$systemctl_bin" restart estrado-pjud.service >"$null_file" 2>&1 || rollback_rc=1; fi
  if [ "$changed_hermes" -eq 1 ]; then
    "$systemctl_bin" --user --machine=hermes@.host restart \
      hermes-gateway.service hermes-dashboard.service >"$null_file" 2>&1 || rollback_rc=1
  fi
  if [ "$changed_worker" -eq 1 ]; then
    if prepare_temp_credentials && check_exact_zero_and_fresh_heartbeat; then
      "$systemctl_bin" restart estrado-pjud-worker.service >"$null_file" 2>&1 || rollback_rc=1
    else
      rollback_rc=1
    fi
  fi
  if [ "$rollback_rc" -ne 0 ]; then
    printf '%s\n' 'ROLLBACK INCOMPLETO: intervención manual requerida; no se hizo borrado amplio.' >&2
    return 1
  fi
  printf '%s\n' "ROLLBACK OK: $backup_dir"
}

rollback_once=0
automatic_rollback() {
  [ "$rollback_once" -eq 0 ] || return 1
  rollback_once=1
  do_rollback "$backup_dir"
}

run_apply_steps() {
  local uid=$1 api_path worker_path hermes_path before_api before_worker before_hermes
  local after_api after_worker after_hermes provision_rc=0
  api_path="$systemd_dir/estrado-pjud.service"
  worker_path="$systemd_dir/estrado-pjud-worker.service"
  hermes_path="$systemd_dir/user-$uid.slice.d/50-legaltech-resource-limits.conf"
  before_api=$(digest_path "$api_path") || return 1
  before_worker=$(digest_path "$worker_path") || return 1
  before_hermes=$(digest_path "$hermes_path") || return 1
  create_backup "$uid" >"$null_file" || return 1
  check_git || return 1
  mutation_started=1
  PROV_ENABLE_PJUD_WORKER=0 "$provision_bin" >"$null_file" 2>&1 || provision_rc=1
  after_api=$(digest_path "$api_path") || return 1
  after_worker=$(digest_path "$worker_path") || return 1
  after_hermes=$(digest_path "$hermes_path") || return 1
  if [ "$before_api" != "$after_api" ]; then record_change api || return 1; fi
  if [ "$before_worker" != "$after_worker" ]; then record_change worker || return 1; fi
  if [ "$before_hermes" != "$after_hermes" ]; then record_change hermes || return 1; fi
  [ "$provision_rc" -eq 0 ] || return 1
  if [ "${swap_preexisting:-0}" -eq 0 ]; then
    printf '%s\n' attempted > "$backup_dir/swap-state" || return 1
  fi
  "$swap_bin" apply >"$null_file" 2>&1 || return 1
  "$swap_bin" verify >"$null_file" 2>&1 || return 1
  "$systemctl_bin" daemon-reload >"$null_file" 2>&1 || return 1
  if [ "$before_api" != "$after_api" ]; then "$systemctl_bin" restart estrado-pjud.service >"$null_file" 2>&1 || return 1; fi
  if [ "$before_hermes" != "$after_hermes" ]; then
    "$systemctl_bin" --user --machine=hermes@.host restart \
      hermes-gateway.service hermes-dashboard.service >"$null_file" 2>&1 || return 1
  fi
  if [ "$before_worker" != "$after_worker" ]; then
    check_exact_zero_and_fresh_heartbeat || return 1
    "$systemctl_bin" restart estrado-pjud-worker.service >"$null_file" 2>&1 || return 1
  fi
  "$systemctl_bin" start legaltech-monitor.timer legaltech-resource-tracker.timer >"$null_file" 2>&1 || return 1
  "$python_bin" "$monitoring_dir/resource-tracker.py" --once >"$null_file" 2>&1 || return 1
  "$python_bin" "$monitoring_dir/monitor.py" --once --dry-run >"$null_file" 2>&1 || return 1
  run_postflight || return 1
}

run_apply() {
  local uid
  run_preflight || return 1
  uid=$(validate_hermes_inventory) || return 1
  build_managed_paths "$uid"
  backup_dir=''
  mutation_started=0
  if ! run_apply_steps "$uid"; then
    if [ -n "$backup_dir" ] && [ "$mutation_started" -eq 1 ]; then automatic_rollback || true; fi
    return 1
  fi
  printf '%s\n' "APPLY OK; backup: $backup_dir"
}

case "$command_name" in
  preflight) run_preflight ;;
  apply) run_apply ;;
  postflight) run_postflight ;;
  rollback) do_rollback "$requested_backup" ;;
esac
