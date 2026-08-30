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
readonly WORKER_DRAIN_ATTEMPTS=5
readonly WORKER_POST_START_HEARTBEAT_ATTEMPTS=16
readonly WORKER_CGROUP=/legaltech.slice/estrado-pjud-worker.service

usage() {
  printf '%s\n' \
    'usage: resource-guards.sh preflight|apply --expected-sha <40-hex-sha>' \
    '       resource-guards.sh apply --expected-sha <40-hex-sha> --allow-daytime-maintenance' \
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
  RG_REPO_DIR RG_SYSTEMD_DIR RG_SYSTEMD_VENDOR_DIR RG_CREDENTIAL_FILE RG_BACKUP_ROOT RG_TMP_ROOT RG_DISK_PATH RG_NULL_FILE
  RG_MONITORING_DIR RG_MONITOR_ENV_FILE RG_FSTAB_FILE RG_SYSCTL_FILE RG_SWAPPINESS_METADATA_FILE
  RG_CADDYFILE RG_LOGROTATE_FILE RG_JURISTRACK_HEALTH_URL
  RG_ESTRADO_HEALTH_URL RG_GIT_BIN RG_DF_BIN RG_FREE_BIN RG_ID_BIN RG_PS_BIN
  RG_SYSTEMCTL_BIN RG_BUSCTL_BIN RG_CURL_BIN RG_DATE_BIN RG_SLEEP_BIN RG_STAT_BIN RG_SHA256_BIN
  RG_FLOCK_BIN RG_READLINK_BIN RG_LOCK_FILE RG_FD_ROOT
  RG_FIND_BIN RG_CP_BIN RG_RM_BIN RG_MKDIR_BIN RG_CHMOD_BIN RG_CHOWN_BIN
  RG_MKTEMP_BIN RG_JQ_BIN RG_PROVISION_BIN RG_SWAP_BIN RG_PYTHON_BIN
  RG_TEST_ROOT_UID RG_TEST_ROOT_GID RG_WORKER_FENCE_POLL_DELAY_SECONDS
  RG_WORKER_HEARTBEAT_POLL_DELAY_SECONDS
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
systemd_vendor_dir=${RG_SYSTEMD_VENDOR_DIR:-/usr/lib/systemd/system}
credential_file=${RG_CREDENTIAL_FILE:-$repo_dir/estrado-pjud-service/.env}
backup_root=${RG_BACKUP_ROOT:-/var/backups/legaltech-resource-guards}
tmp_root=${RG_TMP_ROOT:-/tmp}
disk_path=${RG_DISK_PATH:-/}
null_file=${RG_NULL_FILE:-/dev/null}
monitoring_dir=${RG_MONITORING_DIR:-/opt/legaltech-monitoring}
monitor_env_file=${RG_MONITOR_ENV_FILE:-/etc/legaltech-monitoring.env}
fstab_file=${RG_FSTAB_FILE:-/etc/fstab}
sysctl_file=${RG_SYSCTL_FILE:-/etc/sysctl.d/60-legaltech-swap.conf}
swappiness_metadata_file=${RG_SWAPPINESS_METADATA_FILE:-/etc/sysctl.d/60-legaltech-swap.previous}
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
busctl_bin=${RG_BUSCTL_BIN:-/usr/bin/busctl}
curl_bin=${RG_CURL_BIN:-/usr/bin/curl}
date_bin=${RG_DATE_BIN:-/usr/bin/date}
sleep_bin=${RG_SLEEP_BIN:-/usr/bin/sleep}
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
flock_bin=${RG_FLOCK_BIN:-/usr/bin/flock}
readlink_bin=${RG_READLINK_BIN:-/usr/bin/readlink}
lock_file=${RG_LOCK_FILE:-/run/lock/legaltech-resource-guards.lock}
fd_root=${RG_FD_ROOT:-/proc/self/fd}
root_uid=${RG_TEST_ROOT_UID:-0}
root_gid=${RG_TEST_ROOT_GID:-0}
worker_fence_poll_delay_seconds=${RG_WORKER_FENCE_POLL_DELAY_SECONDS:-1}
worker_heartbeat_poll_delay_seconds=${RG_WORKER_HEARTBEAT_POLL_DELAY_SECONDS:-5}

readonly durable_metadata_writer='import os
import stat
import sys
import tempfile

OK = "DURABLE_WRITE_OK"

def inject(name, enabled):
    if not enabled:
        return
    boundary = os.environ.get("RG_DURABLE_TEST_BOUNDARY")
    if boundary == "crash-" + name:
        os._exit(91)
    if boundary == name:
        raise OSError("injected durable metadata boundary")

def write_all(fd, payload):
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError("short durable metadata write")
        offset += written

if len(sys.argv) != 7 or sys.argv[1] != "--resource-guards-atomic-write":
    raise SystemExit(2)

target, uid_text, gid_text, mode_text, test_text = sys.argv[2:]
if not target.startswith("/") or "/" not in target[1:]:
    raise SystemExit(2)
if not uid_text.isdecimal() or not gid_text.isdecimal() or mode_text != "600":
    raise SystemExit(2)
if test_text not in ("0", "1"):
    raise SystemExit(2)

parent = os.path.dirname(target)
name = os.path.basename(target)
if not name or name in (".", ".."):
    raise SystemExit(2)
payload = sys.stdin.buffer.read(1048577)
if len(payload) > 1048576:
    raise SystemExit(1)

fd = -1
dir_fd = -1
temporary = ""
renamed = False
try:
    prefix = "." + name + "."
    for candidate in os.scandir(parent):
        if not candidate.name.startswith(prefix):
            continue
        metadata = candidate.stat(follow_symlinks=False)
        if (not candidate.is_file(follow_symlinks=False) or metadata.st_nlink != 1
                or metadata.st_uid != int(uid_text) or metadata.st_gid != int(gid_text)
                or (metadata.st_mode & 0o7777) != 0o600):
            raise OSError("unsafe stale durable metadata temporary")
        os.unlink(candidate.path)
    fd, temporary = tempfile.mkstemp(prefix="." + name + ".", dir=parent)
    os.fchmod(fd, 0o600)
    os.fchown(fd, int(uid_text), int(gid_text))
    write_all(fd, payload)
    inject("before-file-fsync", test_text == "1")
    os.fsync(fd)
    inject("after-file-fsync", test_text == "1")
    os.close(fd)
    fd = -1
    inject("before-rename", test_text == "1")
    os.replace(temporary, target)
    renamed = True
    inject("after-rename", test_text == "1")
    dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    inject("before-dir-fsync", test_text == "1")
    os.fsync(dir_fd)
    inject("after-dir-fsync", test_text == "1")
    metadata = os.lstat(target)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_uid != int(uid_text) or metadata.st_gid != int(gid_text)
            or (metadata.st_mode & 0o7777) != 0o600):
        raise OSError("durable metadata identity changed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    verify_fd = os.open(target, flags)
    try:
        chunks = []
        remaining = len(payload) + 1
        while remaining > 0:
            chunk = os.read(verify_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if b"".join(chunks) != payload:
            raise OSError("durable metadata content changed")
    finally:
        os.close(verify_fd)
except BaseException:
    raise SystemExit(1)
finally:
    if fd >= 0:
        try: os.close(fd)
        except OSError: pass
    if dir_fd >= 0:
        try: os.close(dir_fd)
        except OSError: pass
    if temporary and not renamed:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        except OSError: pass

sys.stdout.write(OK + "\n")'

readonly durable_backup_syncer='import os
import stat
import sys

if len(sys.argv) != 6 or sys.argv[1] != "--resource-guards-fsync-tree":
    raise SystemExit(2)
root, uid_text, gid_text, test_text = sys.argv[2:]
if not root.startswith("/") or not uid_text.isdecimal() or not gid_text.isdecimal():
    raise SystemExit(2)
if test_text not in ("0", "1"):
    raise SystemExit(2)
uid, gid = int(uid_text), int(gid_text)

def inject(name):
    if test_text != "1":
        return
    boundary = os.environ.get("RG_DURABLE_TEST_BOUNDARY")
    if boundary == "crash-" + name:
        os._exit(91)
    if boundary == name:
        raise OSError("injected durable backup boundary")

directories = []
try:
    backup_root = os.path.dirname(root)
    namespace_parent = os.path.dirname(backup_root)
    for directory, exact_mode in ((root, 0o700), (backup_root, 0o700)):
        metadata = os.lstat(directory)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid
                or metadata.st_gid != gid or (metadata.st_mode & 0o7777) != exact_mode):
            raise OSError("unsafe durable backup directory")
    namespace_metadata = os.lstat(namespace_parent)
    namespace_mode = namespace_metadata.st_mode & 0o7777
    if (not stat.S_ISDIR(namespace_metadata.st_mode) or namespace_metadata.st_uid != uid
            or namespace_metadata.st_gid != gid or (namespace_mode & 0o700) != 0o700
            or (namespace_mode & 0o022) != 0):
        raise OSError("unsafe durable backup namespace parent")
    for current, names, files in os.walk(root, topdown=True, followlinks=False):
        current_stat = os.lstat(current)
        if not stat.S_ISDIR(current_stat.st_mode) or current_stat.st_uid != uid or current_stat.st_gid != gid:
            raise OSError("unsafe durable backup directory")
        directories.append(current)
        for name in names:
            entry = os.path.join(current, name)
            entry_stat = os.lstat(entry)
            if not stat.S_ISDIR(entry_stat.st_mode) or entry_stat.st_uid != uid or entry_stat.st_gid != gid:
                raise OSError("unsafe durable backup subtree")
        for name in files:
            entry = os.path.join(current, name)
            entry_stat = os.lstat(entry)
            if (not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_nlink != 1
                    or entry_stat.st_uid != uid or entry_stat.st_gid != gid):
                raise OSError("unsafe durable backup file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(entry, flags)
            try:
                opened = os.fstat(fd)
                if opened.st_dev != entry_stat.st_dev or opened.st_ino != entry_stat.st_ino:
                    raise OSError("durable backup file identity changed")
                inject("before-tree-file-fsync")
                os.fsync(fd)
                inject("after-tree-file-fsync")
            finally:
                os.close(fd)
    for directory in reversed(directories):
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            inject("before-tree-dir-fsync")
            os.fsync(fd)
            inject("after-tree-dir-fsync")
        finally:
            os.close(fd)
    fd = os.open(backup_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(namespace_parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        inject("before-root-parent-fsync")
        os.fsync(fd)
        inject("after-root-parent-fsync")
    finally:
        os.close(fd)
except BaseException:
    raise SystemExit(1)
sys.stdout.write("DURABLE_SYNC_OK\n")'

for absolute_value in \
  "$repo_dir" "$systemd_dir" "$systemd_vendor_dir" "$credential_file" "$backup_root" "$tmp_root" "$disk_path" "$null_file" \
  "$monitoring_dir" "$monitor_env_file" "$fstab_file" "$sysctl_file" "$swappiness_metadata_file" \
  "$caddyfile" "$logrotate_file" "$git_bin" "$df_bin" "$free_bin" \
  "$id_bin" "$ps_bin" "$systemctl_bin" "$busctl_bin" "$curl_bin" "$date_bin" "$sleep_bin" \
  "$stat_bin" "$sha256_bin" "$find_bin" "$cp_bin" "$rm_bin" \
  "$mkdir_bin" "$chmod_bin" "$chown_bin" "$mktemp_bin" "$jq_bin" \
  "$provision_bin" "$swap_bin" "$python_bin" \
  "$flock_bin" "$readlink_bin" "$lock_file" "$fd_root"
do
  case "$absolute_value" in /*) ;; *) usage ;; esac
done
is_uint "$root_uid" || usage
is_uint "$root_gid" || usage
is_uint "$worker_fence_poll_delay_seconds" || usage
[ "$worker_fence_poll_delay_seconds" -le 30 ] || usage
is_uint "$worker_heartbeat_poll_delay_seconds" || usage
[ "$worker_heartbeat_poll_delay_seconds" -le 30 ] || usage

if [ "$test_mode" != 1 ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
  fail 'must run as root' || exit "$EXIT_ERROR"
fi

command_name=${1:-}
shift || true
expected_sha=''
requested_backup=''
allow_daytime_maintenance=0
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
    --allow-daytime-maintenance)
      [ "$command_name" = apply ] && [ "$allow_daytime_maintenance" -eq 0 ] || usage
      allow_daytime_maintenance=1
      shift
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
resource_lock_fd=''
cleanup() {
  if [ -n "$temp_dir" ]; then
    case "$temp_dir" in "$tmp_root"/legaltech-resource-guards.*) "$rm_bin" -rf -- "$temp_dir" >"$null_file" 2>&1 || true ;; esac
  fi
  if [ -n "$resource_lock_fd" ]; then
    "$flock_bin" -u "$resource_lock_fd" >"$null_file" 2>&1 || true
    exec {resource_lock_fd}>&-
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
  local path=$1 fields links output
  [ -e "$path" ] && [ ! -L "$path" ] || return 1
  path_has_symlink_component "$path" && return 1
  if [ -f "$path" ]; then
    fields=$(stat_fields "$path") || return 1
    links=${fields##*|}
    [ "$links" = 1 ] || return 1
  elif [ -d "$path" ]; then
    output=$("$find_bin" "$path" -type l -print -quit 2>"$null_file") || return 1
    [ -z "$output" ] || return 1
    output=$("$find_bin" "$path" -type f -links +1 -print -quit 2>"$null_file") || return 1
    [ -z "$output" ] || return 1
  else
    return 1
  fi
}

acquire_resource_mutation_lock() {
  local fd_target
  if [ ! -e "$lock_file" ] && [ ! -L "$lock_file" ]; then
    path_has_symlink_component "${lock_file%/*}" && return 1
    ( umask 077; set -o noclobber; : > "$lock_file" ) 2>"$null_file" || true
  fi
  safe_existing_path "$lock_file" && validate_root_path "$lock_file" 600 || return 1
  exec {resource_lock_fd}>"$lock_file" || return 1
  fd_target=$("$readlink_bin" "$fd_root/$resource_lock_fd" 2>"$null_file") || return 1
  [ "$fd_target" = "$lock_file" ] || return 1
  safe_existing_path "$lock_file" && validate_root_path "$lock_file" 600 || return 1
  if ! "$flock_bin" -n "$resource_lock_fd" >"$null_file" 2>&1; then
    fail 'another resource mutation is already in progress'
    return 1
  fi
}

find_no_match() {
  local output
  output=$("$find_bin" "$@" -print -quit 2>"$null_file") || return 1
  [ -z "$output" ]
}

safe_read_mode() {
  local mode=$1 value
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || return 1
  value=$((8#$mode))
  [ $((value & 8#0400)) -ne 0 ] && [ $((value & 8#0133)) -eq 0 ]
}

validate_runtime_tree() {
  local path=$1 fields mode uid gid _links
  safe_existing_path "$path" && [ -d "$path" ] || return 1
  fields=$(stat_fields "$path") || return 1
  IFS='|' read -r mode uid gid _links <<< "$fields"
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] || return 1
  [ $((8#$mode & 8#0022)) -eq 0 ] || return 1
  find_no_match "$path" ! -type f ! -type d || return 1
  find_no_match "$path" ! -user "$root_uid" || return 1
  find_no_match "$path" ! -group "$root_gid" || return 1
  find_no_match "$path" -perm -0022 || return 1
}

validate_managed_source() {
  local path=$1 fields mode uid gid links expected_gid
  safe_existing_path "$path" || return 1
  if [ "$path" = "$monitoring_dir" ]; then
    validate_runtime_tree "$path"
    return
  fi
  [ -f "$path" ] && [ ! -L "$path" ] || return 1
  fields=$(stat_fields "$path") || return 1
  IFS='|' read -r mode uid gid links <<< "$fields"
  [ "$links" = 1 ] || return 1
  if [ "$path" = "$credential_file" ]; then
    expected_gid=$("$id_bin" -g estrado 2>"$null_file") || return 1
    is_uint "$expected_gid" || return 1
    [ "$mode" = 640 ] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$expected_gid" ]
  elif [ "$path" = "$monitor_env_file" ] || [ "$path" = "$swappiness_metadata_file" ]; then
    [ "$mode" = 600 ] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ]
  else
    [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] && safe_read_mode "$mode"
  fi
}

worker_id=''
worker_id_encoded=''
worker_proxy_mode=0
worker_last_heartbeat_order=''
worker_pre_stop_heartbeat_order=''
worker_restore_allowed=1

load_worker_fence_config() {
  local url_count=0 key_count=0 worker_count=0 outside_count=0 validation_count=0 proxy_count=0
  local line value proxy_url='' credential_fields credential_mode credential_owner credential_group _credential_links expected_gid
  [ -d "$tmp_root" ] && [ ! -L "$tmp_root" ] || return 1
  path_has_symlink_component "$tmp_root" && return 1
  safe_existing_path "$credential_file" || return 1
  credential_fields=$(stat_fields "$credential_file") || return 1
  IFS='|' read -r credential_mode credential_owner credential_group _credential_links <<< "$credential_fields"
  expected_gid=$("$id_bin" -g estrado 2>"$null_file") || return 1
  is_uint "$expected_gid" || return 1
  [ "$credential_owner" = "$root_uid" ] || return 1
  [ "$credential_group" = "$expected_gid" ] && [ "$credential_mode" = 640 ] || return 1
  SUPABASE_URL=''
  SUPABASE_SERVICE_KEY=''
  worker_id=''
  worker_id_encoded=''
  worker_proxy_mode=0
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
      WORKER_ID=*)
        worker_count=$((worker_count + 1))
        worker_id=${line#WORKER_ID=}
        ;;
      PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=*)
        outside_count=$((outside_count + 1))
        value=${line#PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=}
        [ "$value" = false ] || return 1
        ;;
      PJUD_OFF_HOURS_VALIDATION_ONCE=*)
        validation_count=$((validation_count + 1))
        value=${line#PJUD_OFF_HOURS_VALIDATION_ONCE=}
        [ "$value" = false ] || return 1
        ;;
      OJV_PROXY_URL=*)
        proxy_count=$((proxy_count + 1))
        proxy_url=${line#OJV_PROXY_URL=}
        ;;
    esac
  done < "$credential_file"
  [ "$url_count" -eq 1 ] && [ "$key_count" -eq 1 ] \
    && [ "$worker_count" -eq 1 ] && [ "$outside_count" -eq 1 ] \
    && [ "$validation_count" -le 1 ] && [ "$proxy_count" -le 1 ] \
    && [ -n "$SUPABASE_URL" ] && [ -n "$SUPABASE_SERVICE_KEY" ] || return 1
  [[ "$worker_id" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$ ]] || return 1
  if [ "$test_mode" = 1 ]; then
    [[ "$SUPABASE_URL" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[^\"[:space:]]*)?$ ]] || return 1
  else
    [[ "$SUPABASE_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[^\"[:space:]]*)?$ ]] || return 1
  fi
  case "$SUPABASE_URL$SUPABASE_SERVICE_KEY$worker_id$proxy_url" in *$'\n'*|*$'\r'*) return 1 ;; esac
  if [ -n "$proxy_url" ]; then
    case "$proxy_url" in http://?*|https://?*) ;; *) return 1 ;; esac
    case "$proxy_url" in *' '*|*$'\t'*|*'"'*) return 1 ;; esac
    worker_proxy_mode=1
  fi
  if ! worker_id_encoded=$("$jq_bin" -nr --arg value "$worker_id" '$value | @uri' 2>"$null_file"); then
    return 1
  fi
  [ -n "$worker_id_encoded" ] && [[ "$worker_id_encoded" != *$'\n'* ]] || return 1
  unset proxy_url

  umask 077
  if [ -z "$temp_dir" ]; then
    temp_dir=$("$mktemp_bin" -d "$tmp_root/legaltech-resource-guards.XXXXXX") || return 1
    "$chmod_bin" 0700 "$temp_dir" || return 1
    "$chown_bin" "$root_uid:$root_gid" "$temp_dir" || return 1
    validate_root_path "$temp_dir" 700 || return 1
  fi
  curl_header_file="$temp_dir/supabase.headers"
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

maintenance_window_is_open() {
  local hour
  if ! hour=$(TZ=America/Santiago LC_ALL=C "$date_bin" +%H 2>"$null_file"); then
    return 1
  fi
  [[ "$hour" =~ ^[0-9]{2}$ ]] || return 1
  hour=$((10#$hour))
  { [ "$hour" -ge 20 ] && [ "$hour" -le 23 ]; } \
    || { [ "$hour" -ge 0 ] && [ "$hour" -le 3 ]; }
}

apply_maintenance_window_is_open() {
  if [ "$allow_daytime_maintenance" -eq 0 ]; then
    maintenance_window_is_open
    return
  fi
  # Explicit operator consent relaxes only the hour, never a broken clock or
  # idle/claims fences. It is not persisted or accepted by manual rollback.
  local hour
  hour=$(TZ=America/Santiago LC_ALL=C "$date_bin" +%H 2>"$null_file") || return 1
  [[ "$hour" =~ ^[0-9]{2}$ ]] && [ "$((10#$hour))" -le 23 ] || return 1
  printf '%s\n' 'DAYTIME MAINTENANCE AUTHORIZED; all worker safety fences still required'
}

worker_heartbeat_is_idle() { # require-zero-mint minimum-exclusive-order
  local require_zero_mint=${1:-0} minimum_order=${2:-}
  local config="$temp_dir/heartbeat.curl" body="$temp_dir/heartbeat.body"
  local http timestamp now_epoch heartbeat_epoch heartbeat_order age
  write_curl_config "$config" \
    "${SUPABASE_URL%/}/rest/v1/sync_worker_heartbeats?worker_id=eq.${worker_id_encoded}&select=status,last_heartbeat_at,metadata" \
    "$body" || return 1
  if ! http=$("$curl_bin" --config "$config" --write-out '%{http_code}' 2>"$null_file"); then return 1; fi
  [ "$http" = 200 ] || return 1
  if ! timestamp=$("$jq_bin" -er \
    --argjson proxy_mode "$worker_proxy_mode" \
    --argjson require_zero_mint "$require_zero_mint" '
    if type == "array" and length == 1 and
       (.[0] | type == "object") and
       (.[0] | keys | sort) == ["last_heartbeat_at", "metadata", "status"] and
       .[0].status == "idle_off_hours" and
       (.[0].last_heartbeat_at | type == "string") and
       (.[0].metadata | type == "object") and
       .[0].metadata.process_outside_office_hours_enabled == false and
       (.[0].metadata.mint_attempts | type == "number" and . >= 0 and . == floor) and
       ($require_zero_mint == 0 or .[0].metadata.mint_attempts == 0) and
       ($proxy_mode == 0 or (
         .[0].metadata.proxy_control_status == "enabled" and
         .[0].metadata.proxy_control_reason == null
       ))
    then .[0].last_heartbeat_at else empty end
  ' "$body" 2>"$null_file"); then return 1; fi
  now_epoch=$(LC_ALL=C "$date_bin" -u +%s 2>"$null_file") || return 1
  heartbeat_epoch=$(LC_ALL=C "$date_bin" -u -d "$timestamp" +%s 2>"$null_file") || return 1
  heartbeat_order=$(LC_ALL=C "$date_bin" -u -d "$timestamp" +%s%N 2>"$null_file") || return 1
  is_uint "$now_epoch" && is_uint "$heartbeat_epoch" && is_uint "$heartbeat_order" || return 1
  [ "$now_epoch" -ge "$heartbeat_epoch" ] || return 1
  age=$((now_epoch - heartbeat_epoch))
  [ "$age" -le "$HEARTBEAT_MAX_AGE_SECONDS" ] || return 1
  if [ -n "$minimum_order" ]; then
    is_uint "$minimum_order" && [ "$heartbeat_order" -gt "$minimum_order" ] || return 1
  fi
  worker_last_heartbeat_order=$heartbeat_order
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

wait_for_zero_claims() {
  local attempt count
  for ((attempt = 1; attempt <= WORKER_DRAIN_ATTEMPTS; attempt++)); do
    count=$(active_claim_count) || return 1
    [ "$count" = 0 ] && return 0
    [ "$attempt" -lt "$WORKER_DRAIN_ATTEMPTS" ] || return 1
    "$sleep_bin" "$worker_fence_poll_delay_seconds" >"$null_file" 2>&1 || return 1
  done
  return 1
}

check_git() {
  local status actual
  status=$(cd "$repo_dir" && "$git_bin" status --porcelain --untracked-files=all) || return 1
  [ -z "$status" ] || return 1
  actual=$(cd "$repo_dir" && "$git_bin" rev-parse HEAD) || return 1
  [ "$actual" = "$expected_sha" ]
}

check_disk() {
  local output _filesystem _blocks _used available _capacity _mount last=''
  output=$(LC_ALL=C "$df_bin" -Pk "$disk_path" 2>"$null_file") || return 1
  while read -r _filesystem _blocks _used available _capacity _mount; do
    last=$available
  done <<< "$output"
  is_uint "$last" || return 1
  [ $((last * 1024)) -ge "$MIN_DISK_BYTES" ]
}

check_ram() {
  local output label _total _used _free _shared _cache available extra
  available=''
  output=$(LC_ALL=C "$free_bin" -b 2>"$null_file") || return 1
  while read -r label _total _used _free _shared _cache available extra; do
    [ "$label" = Mem: ] || continue
    [ -z "${extra:-}" ] || return 1
    break
  done <<< "$output"
  is_uint "$available" || return 1
  [ "$available" -ge "$MIN_RAM_BYTES" ]
}

validate_hermes_os_auxiliary() { # uid dbus.service|session-migration.service
  local uid=$1 unit=$2 output line key value line_count=0
  local load_state='' unit_file_state='' type='' remain_after_exit=''
  local active_state='' sub_state='' main_pid='' control_group='' fragment_path=''
  local load_count=0 unit_file_count=0 type_count=0 remain_count=0
  local active_count=0 sub_count=0 pid_count=0 control_count=0 fragment_count=0
  local expected_unit_file expected_type expected_active expected_sub expected_fragment
  case "$unit" in
    dbus.service)
      expected_unit_file=static
      expected_type=notify
      expected_active=active
      expected_sub=running
      expected_fragment=/usr/lib/systemd/user/dbus.service
      ;;
    session-migration.service)
      expected_unit_file=enabled
      expected_type=oneshot
      expected_active=inactive
      expected_sub=dead
      expected_fragment=/usr/lib/systemd/user/session-migration.service
      ;;
    *) return 1 ;;
  esac
  output=$("$systemctl_bin" --user --machine=hermes@.host show "$unit" \
    --property=LoadState --property=UnitFileState --property=Type \
    --property=RemainAfterExit --property=ActiveState --property=SubState \
    --property=MainPID --property=ControlGroup --property=FragmentPath \
    2>"$null_file") || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line_count=$((line_count + 1))
    case "$line" in *$'\r'*|*$'\t'*|*' '*) return 1 ;; esac
    key=${line%%=*}
    value=${line#*=}
    [ "$key" != "$line" ] || return 1
    case "$key" in
      LoadState) load_count=$((load_count + 1)); load_state=$value ;;
      UnitFileState) unit_file_count=$((unit_file_count + 1)); unit_file_state=$value ;;
      Type) type_count=$((type_count + 1)); type=$value ;;
      RemainAfterExit) remain_count=$((remain_count + 1)); remain_after_exit=$value ;;
      ActiveState) active_count=$((active_count + 1)); active_state=$value ;;
      SubState) sub_count=$((sub_count + 1)); sub_state=$value ;;
      MainPID) pid_count=$((pid_count + 1)); main_pid=$value ;;
      ControlGroup) control_count=$((control_count + 1)); control_group=$value ;;
      FragmentPath) fragment_count=$((fragment_count + 1)); fragment_path=$value ;;
      *) return 1 ;;
    esac
  done <<< "$output"
  [ "$line_count" -eq 9 ] && [ "$load_count" -eq 1 ] \
    && [ "$unit_file_count" -eq 1 ] && [ "$type_count" -eq 1 ] \
    && [ "$remain_count" -eq 1 ] && [ "$active_count" -eq 1 ] \
    && [ "$sub_count" -eq 1 ] && [ "$pid_count" -eq 1 ] \
    && [ "$control_count" -eq 1 ] && [ "$fragment_count" -eq 1 ] || return 1
  [ "$load_state" = loaded ] && [ "$unit_file_state" = "$expected_unit_file" ] \
    && [ "$type" = "$expected_type" ] && [ "$remain_after_exit" = no ] \
    && [ "$active_state" = "$expected_active" ] && [ "$sub_state" = "$expected_sub" ] \
    && [ "$fragment_path" = "$expected_fragment" ] || return 1
  case "$unit" in
    dbus.service)
      is_uint "$main_pid" && [ "$main_pid" -gt 0 ] || return 1
      [ "$control_group" \
        = "/user.slice/user-$uid.slice/user@$uid.service/session.slice/dbus.service" ]
      ;;
    session-migration.service)
      [ "$main_pid" = 0 ] && [ -z "$control_group" ]
      ;;
  esac
}

validate_getty_system_template() { # Hermes uid
  local uid=$1 path="$systemd_vendor_dir/getty@.service" fields fields_after
  local mode owner group links extra line section='' key service_sections=0
  local vendor_content vendor_content_after output body='' line_number=0 header_count=0
  safe_existing_path "$path" && [ -f "$path" ] || return 1
  fields=$(stat_fields "$path") || return 1
  IFS='|' read -r mode owner group links extra <<< "$fields"
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] && [ "$owner" = "$root_uid" ] \
    && [ "$group" = "$root_gid" ] && [ "$links" = 1 ] \
    && [ -z "${extra:-}" ] || return 1
  [ $((8#$mode & 022)) -eq 0 ] || return 1
  vendor_content=$(<"$path") || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in *$'\r'*) return 1 ;; esac
    case "$line" in *\\) return 1 ;; esac
    line=${line#"${line%%[![:space:]]*}"}
    line=${line%"${line##*[![:space:]]}"}
    case "$line" in ''|'#'*|';'*) continue ;; esac
    case "$line" in
      '['*']')
        if [ "$line" = '[Service]' ]; then
          service_sections=$((service_sections + 1))
          section=Service
        elif [[ "$line" =~ ^\[[A-Za-z0-9_.-]+\]$ ]]; then
          section=${line#'['}
          section=${section%']'}
        else
          return 1
        fi
        ;;
      *)
        [ -n "$section" ] || return 1
        [ "$section" = Service ] || continue
        key=${line%%=*}
        [ "$key" != "$line" ] || continue
        key=${key#"${key%%[![:space:]]*}"}
        key=${key%"${key##*[![:space:]]}"}
        [ "$key" != User ] || return 1
        ;;
    esac
  done < "$path"
  [ "$service_sections" -eq 1 ] || return 1

  output=$("$systemctl_bin" cat getty@.service --no-pager 2>"$null_file") || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line_number=$((line_number + 1))
    case "$line" in *$'\r'*) return 1 ;; esac
    case "$line" in
      '# /'*)
        header_count=$((header_count + 1))
        [ "$header_count" -eq 1 ] && [ "$line_number" -eq 1 ] \
          && [ "$line" = "# $path" ] || return 1
        ;;
      *)
        [ "$header_count" -eq 1 ] || return 1
        if [ -n "$body" ]; then body+=$'\n'; fi
        body+=$line
        ;;
    esac
  done <<< "$output"
  [ "$header_count" -eq 1 ] && [ "$body" = "$vendor_content" ] || return 1

  safe_existing_path "$path" && [ -f "$path" ] || return 1
  fields_after=$(stat_fields "$path") || return 1
  [ "$fields_after" = "$fields" ] || return 1
  vendor_content_after=$(<"$path") || return 1
  [ "$vendor_content_after" = "$vendor_content" ]
}

validate_hermes_inventory() {
  local uid reverse ownership system_units user_units system_unit user_unit extra
  local unit_name unit_state preset unit_user dbus_seen=0 session_migration_seen=0
  local getty_template_seen=0
  uid=$("$id_bin" -u hermes 2>"$null_file") || return 1
  is_uint "$uid" || return 1
  reverse=$("$id_bin" -nu "$uid" 2>"$null_file") || return 1
  [ "$reverse" = hermes ] || return 1
  ownership=$("$ps_bin" -U "$uid" -o unit=,uunit= 2>"$null_file") || return 1
  while read -r system_unit user_unit extra; do
    [ -n "${system_unit:-}" ] || continue
    [ "$system_unit" = "user@$uid.service" ] && [ -z "${extra:-}" ] || return 1
    case "$user_unit" in
      init.scope|hermes-gateway.service|hermes-dashboard.service) ;;
      dbus.service) dbus_seen=$((dbus_seen + 1)); [ "$dbus_seen" -eq 1 ] || return 1 ;;
      *) return 1 ;;
    esac
  done <<< "$ownership"
  [ "$dbus_seen" -eq 1 ] || return 1
  validate_hermes_os_auxiliary "$uid" dbus.service || return 1
  system_units=$("$systemctl_bin" list-unit-files --type=service --state=enabled --no-legend --no-pager 2>"$null_file") || return 1
  while read -r unit_name unit_state preset extra; do
    [ -n "${unit_name:-}" ] || continue
    [[ "$unit_name" =~ ^[A-Za-z0-9_.@:-]+$ ]] && [ "$unit_state" = enabled ] && [ -z "${extra:-}" ] || return 1
    case "${preset:-}" in ''|enabled|disabled|ignored|-) ;; *) return 1 ;; esac
    case "$unit_name" in
      getty@.service)
        getty_template_seen=$((getty_template_seen + 1))
        [ "$getty_template_seen" -eq 1 ] || return 1
        validate_getty_system_template "$uid" || return 1
        continue
        ;;
      *@.service) return 1 ;;
    esac
    unit_user=$("$systemctl_bin" show "$unit_name" --property User --value 2>"$null_file") || return 1
    if [ "$unit_user" = hermes ] || [ "$unit_user" = "$uid" ]; then
      case "$unit_name" in hermes-gateway.service|hermes-dashboard.service|"user@$uid.service") ;; *) return 1 ;; esac
    fi
  done <<< "$system_units"
  [ "$getty_template_seen" -eq 1 ] || return 1
  user_units=$("$systemctl_bin" --user --machine=hermes@.host list-unit-files --type=service --state=enabled --no-legend --no-pager 2>"$null_file") || return 1
  while read -r unit_name unit_state preset extra; do
    [ -n "${unit_name:-}" ] || continue
    [[ "$unit_name" =~ ^[A-Za-z0-9_.@:-]+$ ]] && [ "$unit_state" = enabled ] && [ -z "${extra:-}" ] || return 1
    case "${preset:-}" in ''|enabled|disabled|ignored|-) ;; *) return 1 ;; esac
    case "$unit_name" in
      hermes-gateway.service|hermes-dashboard.service) ;;
      session-migration.service)
        session_migration_seen=$((session_migration_seen + 1))
        [ "$session_migration_seen" -eq 1 ] || return 1
        validate_hermes_os_auxiliary "$uid" "$unit_name" || return 1
        ;;
      *) return 1 ;;
    esac
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
  local uid swap_state worker_activity
  check_git || { fail 'Git tree or deployed SHA is not exact'; return 1; }
  safe_existing_path "$provision_bin" && [ -x "$provision_bin" ] \
    && safe_existing_path "$swap_bin" && [ -x "$swap_bin" ] \
    || { fail 'reviewed provision or swap executable is unsafe'; return 1; }
  [ -x "$busctl_bin" ] || { fail 'typed systemd property client is unavailable'; return 1; }
  verify_monitor_configuration 0 || return 1
  check_disk || { fail 'free disk is unavailable or below 8 GiB'; return 1; }
  check_ram || { fail 'MemAvailable is unavailable or below 6 GiB'; return 1; }
  uid=$(validate_hermes_inventory) || { fail 'Hermes UID or persistent inventory is unknown'; return 1; }
  [ -n "$uid" ] || { fail 'Hermes UID is unknown'; return 1; }
  check_public_health || { fail 'a public health endpoint is not exactly HTTP 200'; return 1; }
  worker_activity=$(read_unit_state system is-active estrado-pjud-worker.service) \
    || { fail 'worker activity is unknown'; return 1; }
  case "$worker_activity" in active|inactive) ;; *) return 1 ;; esac
  swap_state=$("$swap_bin" preflight 2>"$null_file") || { fail 'swap preflight is unsafe'; return 1; }
  case "$swap_state" in
    clean) swap_initial_state=clean ;;
    managed)
      "$swap_bin" verify >"$null_file" 2>&1 || { fail 'managed swap verification is unsafe'; return 1; }
      swap_initial_state=managed
      ;;
    *) fail 'swap initial state is unknown'; return 1 ;;
  esac
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
    "$swappiness_metadata_file"
    "$logrotate_file"
    "$monitoring_dir"
  )
}

is_managed_path() {
  local candidate=$1 path
  for path in "${managed_paths[@]}"; do [ "$candidate" != "$path" ] || return 0; done
  return 1
}

tracked_units=(
  estrado-pjud.service
  estrado-pjud-worker.service
  legaltech-monitor.service
  legaltech-resource-tracker.service
  legaltech-monitor.timer
  legaltech-resource-tracker.timer
  hermes-gateway.service
  hermes-dashboard.service
)
tracked_unit_scopes=(system system system system system system user user)
readonly system_unit_count=6
desired_enabled_states=()
desired_active_states=()
monitor_runtime_units=(legaltech-monitor.service legaltech-resource-tracker.service)
previous_monitor_pids=()
previous_monitor_control_groups=()
previous_monitor_slices=()

scoped_systemctl() { # system|user, arguments...
  local scope=$1
  shift
  case "$scope" in
    system) "$systemctl_bin" "$@" ;;
    user) "$systemctl_bin" --user --machine=hermes@.host "$@" ;;
    *) return 1 ;;
  esac
}

read_unit_state() { # system|user is-enabled|is-active unit
  local scope=$1 query=$2 unit=$3 output rc=0
  if output=$(scoped_systemctl "$scope" "$query" "$unit" 2>"$null_file"); then
    rc=0
  else
    rc=$?
  fi
  case "$query:$output:$rc" in
    is-enabled:enabled:0) printf '%s\n' enabled ;;
    is-enabled:disabled:1) printf '%s\n' disabled ;;
    is-enabled:static:0) printf '%s\n' static ;;
    is-enabled:not-found:1|is-enabled:not-found:4) printf '%s\n' absent ;;
    is-active:active:0) printf '%s\n' active ;;
    is-active:inactive:3) printf '%s\n' inactive ;;
    is-active:inactive:4) printf '%s\n' unknown-inactive ;;
    *) return 1 ;;
  esac
}

read_correlated_unit_activity() { # scope unit enabled-state
  local scope=$1 unit=$2 enabled=$3 active
  active=$(read_unit_state "$scope" is-active "$unit") || return 1
  case "$active" in
    active|inactive) printf '%s\n' "$active" ;;
    unknown-inactive)
      [ "$enabled" = absent ] || return 1
      case "$scope:$unit" in
        system:legaltech-monitor.timer|system:legaltech-resource-tracker.timer)
          printf '%s\n' inactive
          ;;
        *) return 1 ;;
      esac
      ;;
    *) return 1 ;;
  esac
}

capture_unit_states() {
  local index unit scope enabled active
  for ((index = 0; index < ${#tracked_units[@]}; index++)); do
    unit=${tracked_units[$index]}
    scope=${tracked_unit_scopes[$index]}
    enabled=$(read_unit_state "$scope" is-enabled "$unit") || return 1
    active=$(read_correlated_unit_activity "$scope" "$unit" "$enabled") || return 1
    printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active"
  done
}

read_effective_runtime() { # scope unit -> active|pid|control-group-or--|slice|result
  local scope=$1 unit=$2 output line key value
  local active='' pid='' control_group='' effective_slice='' result=''
  local active_count=0 pid_count=0 control_count=0 slice_count=0 result_count=0
  output=$(scoped_systemctl "$scope" show "$unit" \
    --property=ActiveState --property=MainPID --property=ControlGroup \
    --property=Slice --property=Result 2>"$null_file") || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    key=${line%%=*}
    value=${line#*=}
    case "$key" in
      ActiveState) active_count=$((active_count + 1)); active=$value ;;
      MainPID) pid_count=$((pid_count + 1)); pid=$value ;;
      ControlGroup) control_count=$((control_count + 1)); control_group=$value ;;
      Slice) slice_count=$((slice_count + 1)); effective_slice=$value ;;
      Result) result_count=$((result_count + 1)); result=$value ;;
      *) return 1 ;;
    esac
  done <<< "$output"
  [ "$active_count" -eq 1 ] && [ "$pid_count" -eq 1 ] \
    && [ "$control_count" -eq 1 ] && [ "$slice_count" -eq 1 ] \
    && [ "$result_count" -eq 1 ] || return 1
  case "$active" in active|inactive) ;; *) return 1 ;; esac
  is_uint "$pid" || return 1
  case "$control_group" in ''|/*) ;; *) return 1 ;; esac
  [[ "$effective_slice" =~ ^[A-Za-z0-9_.@:-]+$ ]] || return 1
  [[ "$result" =~ ^[A-Za-z0-9_.@:-]*$ ]] || return 1
  [ -n "$control_group" ] || control_group=-
  printf '%s|%s|%s|%s|%s\n' "$active" "$pid" "$control_group" "$effective_slice" "$result"
}

capture_monitor_runtime_states() {
  local unit state runtime active pid control_group effective_slice result
  for unit in "${monitor_runtime_units[@]}"; do
    state=$(read_unit_state system is-active "$unit") || return 1
    runtime=$(read_effective_runtime system "$unit") || return 1
    IFS='|' read -r active pid control_group effective_slice result <<< "$runtime"
    [ "$active" = "$state" ] || return 1
    if [ "$active" = active ]; then
      [ "$pid" -gt 0 ] && [ "$control_group" != - ] || return 1
    else
      [ "$pid" -eq 0 ] || return 1
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' \
      "$unit" "$active" "$pid" "$control_group" "$effective_slice"
  done
}

worker_runtime_has_exact_identity() { # active pid control-group-or-- effective-slice
  local active=$1 pid=$2 control_group=$3 effective_slice=$4
  case "$effective_slice" in system.slice|legaltech.slice) ;; *) return 1 ;; esac
  case "$active" in
    active)
      is_uint "$pid" && [ "$pid" -gt 0 ] \
        && [ "$control_group" = "/$effective_slice/estrado-pjud-worker.service" ]
      ;;
    inactive)
      [ "$pid" = 0 ] && [ "$control_group" = - ]
      ;;
    *) return 1 ;;
  esac
}

capture_worker_runtime_state() {
  local runtime active pid control_group effective_slice result state
  state=$(read_unit_state system is-active estrado-pjud-worker.service) || return 1
  runtime=$(read_effective_runtime system estrado-pjud-worker.service) || return 1
  IFS='|' read -r active pid control_group effective_slice result <<< "$runtime"
  [ "$active" = "$state" ] \
    && worker_runtime_has_exact_identity "$active" "$pid" "$control_group" "$effective_slice" \
    || return 1
  printf '%s\t%s\t%s\t%s\t%s\n' estrado-pjud-worker.service \
    "$active" "$pid" "$control_group" "$effective_slice"
}

load_and_validate_unit_states() {
  local unit enabled active extra count=0
  desired_enabled_states=()
  desired_active_states=()
  while IFS=$'\t' read -r unit enabled active extra; do
    [ "$count" -lt "${#tracked_units[@]}" ] && [ "$unit" = "${tracked_units[$count]}" ] || return 1
    case "$enabled" in enabled|disabled|static) ;; absent)
      case "$unit" in legaltech-monitor.timer|legaltech-resource-tracker.timer) ;; *) return 1 ;; esac
      [ "$active" = inactive ] || return 1
      ;; *) return 1 ;; esac
    case "$active" in active|inactive) ;; *) return 1 ;; esac
    [ -z "${extra:-}" ] || return 1
    desired_enabled_states+=("$enabled")
    desired_active_states+=("$active")
    count=$((count + 1))
  done < "$backup_dir/unit-states.tsv"
  [ "$count" -eq "${#tracked_units[@]}" ]
}

load_and_validate_monitor_runtime() {
  local unit active pid control_group effective_slice extra count=0 unit_index
  previous_monitor_pids=()
  previous_monitor_control_groups=()
  previous_monitor_slices=()
  while IFS=$'\t' read -r unit active pid control_group effective_slice extra; do
    [ "$count" -lt "${#monitor_runtime_units[@]}" ] \
      && [ "$unit" = "${monitor_runtime_units[$count]}" ] || return 1
    unit_index=$((count + 2))
    [ "$active" = "${desired_active_states[$unit_index]}" ] || return 1
    is_uint "$pid" || return 1
    case "$control_group" in -|/*) ;; *) return 1 ;; esac
    [[ "$effective_slice" =~ ^[A-Za-z0-9_.@:-]+$ ]] || return 1
    [ -z "${extra:-}" ] || return 1
    if [ "$active" = active ]; then
      [ "$pid" -gt 0 ] && [ "$control_group" != - ] || return 1
    else
      [ "$pid" -eq 0 ] || return 1
    fi
    previous_monitor_pids+=("$pid")
    previous_monitor_control_groups+=("$control_group")
    previous_monitor_slices+=("$effective_slice")
    count=$((count + 1))
  done < "$backup_dir/monitor-runtime.tsv"
  [ "$count" -eq "${#monitor_runtime_units[@]}" ]
}

load_and_validate_worker_runtime() {
  local unit active pid control_group effective_slice extra count=0
  while IFS=$'\t' read -r unit active pid control_group effective_slice extra; do
    count=$((count + 1))
    [ "$count" -eq 1 ] && [ "$unit" = estrado-pjud-worker.service ] \
      && [ -z "${extra:-}" ] || return 1
    worker_runtime_has_exact_identity "$active" "$pid" "$control_group" "$effective_slice" \
      || return 1
    [ "$active" = "${desired_active_states[1]}" ] || return 1
    captured_worker_active=$active
    captured_worker_pid=$pid
    captured_worker_control_group=$control_group
    captured_worker_slice=$effective_slice
  done < "$backup_dir/worker-runtime.tsv"
  [ "$count" -eq 1 ]
}

validate_root_path() { # path mode
  local fields mode uid gid links expected_mode=$2
  fields=$(stat_fields "$1") || return 1
  IFS='|' read -r mode uid gid links <<< "$fields"
  [ "$mode" = "$expected_mode" ] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ]
}

durable_replace_metadata() { # path exact-content
  local target=$1 content=$2 output parent=${1%/*}
  case "$target" in "$backup_dir"/*) ;; *) return 1 ;; esac
  [ "$parent" = "$backup_dir" ] || return 1
  [ -d "$parent" ] && [ ! -L "$parent" ] && validate_root_path "$parent" 700 || return 1
  if [ -e "$target" ] || [ -L "$target" ]; then
    [ -f "$target" ] && [ ! -L "$target" ] && validate_root_path "$target" 600 || return 1
  fi
  if ! output=$(printf '%s' "$content" | "$python_bin" -c "$durable_metadata_writer" \
      --resource-guards-atomic-write "$target" "$root_uid" "$root_gid" 600 "$test_mode" \
      2>"$null_file"); then
    # A killed writer cannot run its finally block. One best-effort replacement
    # under the same host lock safely consumes an exact stale temporary, but the
    # original persistence failure still aborts the protected operation.
    printf '%s' "$content" | "$python_bin" -c "$durable_metadata_writer" \
      --resource-guards-atomic-write "$target" "$root_uid" "$root_gid" 600 "$test_mode" \
      >"$null_file" 2>&1 || true
    return 1
  fi
  [ "$output" = DURABLE_WRITE_OK ] || return 1
  [ -f "$target" ] && [ ! -L "$target" ] && validate_root_path "$target" 600 || return 1
}

durable_rewrite_existing_metadata() { # path
  local content
  [ -f "$1" ] && [ ! -L "$1" ] && validate_root_path "$1" 600 || return 1
  content=$(<"$1")
  durable_replace_metadata "$1" "$content"$'\n'
}

validate_backup_namespace_parent() {
  local namespace_parent fields mode uid gid links permissions
  namespace_parent=${backup_root%/*}
  [ -n "$namespace_parent" ] || namespace_parent=/
  [ -d "$namespace_parent" ] && [ ! -L "$namespace_parent" ] || return 1
  path_has_symlink_component "$namespace_parent" && return 1
  fields=$(stat_fields "$namespace_parent") || return 1
  IFS='|' read -r mode uid gid links <<< "$fields"
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] && is_uint "$links" || return 1
  [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] || return 1
  permissions=$((8#$mode))
  [ $((permissions & 8#0700)) -eq $((8#0700)) ] \
    && [ $((permissions & 8#0022)) -eq 0 ]
}

durable_sync_backup_tree() {
  local output
  safe_existing_path "$backup_dir" && validate_root_path "$backup_dir" 700 || return 1
  safe_existing_path "$backup_root" && validate_root_path "$backup_root" 700 || return 1
  validate_backup_namespace_parent || return 1
  if ! output=$("$python_bin" -c "$durable_backup_syncer" \
      --resource-guards-fsync-tree "$backup_dir" "$root_uid" "$root_gid" "$test_mode" \
      2>"$null_file"); then
    return 1
  fi
  [ "$output" = DURABLE_SYNC_OK ] || return 1
}

create_backup() {
  local uid=$1 timestamp path index=0 rel fields mode owner group links existed
  umask 077
  # The existing namespace parent is a trusted-root boundary: root ownership,
  # no group/other write, and a symlink-free path prevent an unprivileged actor
  # from replacing it between this gate and leaf creation. The host lock excludes
  # concurrent resource-guard writers; a malicious root process is out of scope.
  validate_backup_namespace_parent || return 1
  path_has_symlink_component "$backup_root" && return 1
  if [ ! -e "$backup_root" ]; then
    [ ! -L "$backup_root" ] || return 1
    "$mkdir_bin" -- "$backup_root" || return 1
    validate_backup_namespace_parent || return 1
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
  : > "$manifest" || return 1
  for path in "${managed_paths[@]}"; do
    index=$((index + 1))
    rel=$(printf 'entries/%04d' "$index")
    if [ -e "$path" ] || [ -L "$path" ]; then
      validate_managed_source "$path" || return 1
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
  capture_unit_states > "$backup_dir/unit-states.tsv" || return 1
  capture_worker_runtime_state > "$backup_dir/worker-runtime.tsv" || return 1
  capture_monitor_runtime_states > "$backup_dir/monitor-runtime.tsv" || return 1
  if [ "${swap_initial_state:-unknown}" = managed ]; then
    printf '%s\n' preexisting > "$backup_dir/swap-state" || return 1
  else
    printf '%s\n' 'not-attempted' > "$backup_dir/swap-state" || return 1
  fi
  "$chmod_bin" 0600 "$backup_dir/expected-sha" "$backup_dir/changes" "$backup_dir/swap-state" \
    "$backup_dir/unit-states.tsv" "$backup_dir/worker-runtime.tsv" \
    "$backup_dir/monitor-runtime.tsv" || return 1
  "$chown_bin" "$root_uid:$root_gid" "$backup_dir/expected-sha" "$backup_dir/changes" "$backup_dir/swap-state" \
    "$backup_dir/unit-states.tsv" "$backup_dir/worker-runtime.tsv" \
    "$backup_dir/monitor-runtime.tsv" || return 1
  durable_rewrite_existing_metadata "$manifest" || return 1
  durable_rewrite_existing_metadata "$backup_dir/expected-sha" || return 1
  durable_replace_metadata "$backup_dir/changes" '' || return 1
  durable_rewrite_existing_metadata "$backup_dir/unit-states.tsv" || return 1
  durable_rewrite_existing_metadata "$backup_dir/worker-runtime.tsv" || return 1
  durable_rewrite_existing_metadata "$backup_dir/monitor-runtime.tsv" || return 1
  if [ "${swap_initial_state:-unknown}" = managed ]; then
    durable_replace_metadata "$backup_dir/swap-state" $'preexisting\n' || return 1
  else
    durable_replace_metadata "$backup_dir/swap-state" $'not-attempted\n' || return 1
  fi
  durable_sync_backup_tree || return 1
  printf '%s\n' "$backup_dir"
}

digest_path() {
  local output digest reported_path
  if [ -f "$1" ] && [ ! -L "$1" ]; then
    output=$("$sha256_bin" -- "$1" 2>"$null_file") || return 1
    case "$output" in *$'\n'*) return 1 ;; esac
    [[ "$output" =~ ^([0-9a-fA-F]{64})\ \ (.+)$ ]] || return 1
    digest=${BASH_REMATCH[1]}
    reported_path=${BASH_REMATCH[2]}
    [ "$reported_path" = "$1" ] || return 1
    printf '%s\n' "${digest,,}"
  elif [ ! -e "$1" ] && [ ! -L "$1" ]; then
    printf '%s\n' absent
  else
    return 1
  fi
}

record_change() {
  local existing content change=$1
  load_and_validate_changes || return 1
  case "$change" in
    api) [ "$changed_api" -eq 0 ] || return 1 ;;
    worker) [ "$changed_worker" -eq 0 ] || return 1 ;;
    worker-stop) [ "$worker_stopped" -eq 0 ] || return 1 ;;
    hermes) [ "$changed_hermes" -eq 0 ] || return 1 ;;
    monitor) [ "$changed_monitor" -eq 0 ] || return 1 ;;
    tracker) [ "$changed_tracker" -eq 0 ] || return 1 ;;
    *) return 1 ;;
  esac
  existing=$(<"$backup_dir/changes")
  if [ -n "$existing" ]; then content="$existing"$'\n'; else content=''; fi
  content+="$change"$'\n'
  durable_replace_metadata "$backup_dir/changes" "$content" || return 1
  load_and_validate_changes
}

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
  # Unit/property names come only from the fixed contracts below. Never print
  # returned values: EnvironmentFiles and future properties may be sensitive.
  output=$("$systemctl_bin" show "$unit" --all "${arguments[@]}" 2>"$null_file") \
    || { fail "systemd contract query failed: $unit"; return 1; }
  for ((index = 0; index < ${#properties[@]}; index++)); do
    property=${properties[$index]}
    expected=${expected_values[$index]}
    count=0
    while IFS= read -r line || [ -n "$line" ]; do
      key=${line%%=*}
      value=${line#*=}
      if [ "$key" = "$property" ]; then
        count=$((count + 1))
        [ "$value" = "$expected" ] \
          || { fail "systemd contract mismatch: $unit $property"; return 1; }
      fi
    done <<< "$output"
    [ "$count" -eq 1 ] \
      || { fail "systemd contract missing or duplicated: $unit $property"; return 1; }
  done
}

verify_tracker_environment_files() {
  local output
  # systemctl show omits empty arrays even with --all on systemd 255.
  # Ask the typed D-Bus property instead of accepting a missing show field.
  output=$("$busctl_bin" --system get-property org.freedesktop.systemd1 \
    /org/freedesktop/systemd1/unit/legaltech_2dresource_2dtracker_2eservice \
    org.freedesktop.systemd1.Service EnvironmentFiles 2>"$null_file") \
    || { fail 'tracker EnvironmentFiles query failed'; return 1; }
  [ "$output" = 'a(sb) 0' ] \
    || { fail 'tracker EnvironmentFiles is not an empty typed array'; return 1; }
}

verify_monitor_configuration() { # 0=legacy admission, 1=installed fragments
  local installed=$1 unit object property output
  case "$installed" in 0|1) ;; *) return 1 ;; esac
  for unit in legaltech-monitor.service legaltech-resource-tracker.service \
    legaltech-monitor.timer legaltech-resource-tracker.timer; do
    case "$unit" in
      legaltech-monitor.service) object=legaltech_2dmonitor_2eservice ;;
      legaltech-resource-tracker.service) object=legaltech_2dresource_2dtracker_2eservice ;;
      legaltech-monitor.timer) object=legaltech_2dmonitor_2etimer ;;
      legaltech-resource-tracker.timer) object=legaltech_2dresource_2dtracker_2etimer ;;
    esac
    # Load metadata for absent timers too; no start/reload or activation.
    "$systemctl_bin" show "$unit" --property=LoadState >"$null_file" 2>&1 || return 1
    for property in DropInPaths NeedDaemonReload; do
      output=$("$busctl_bin" --system get-property org.freedesktop.systemd1 \
        "/org/freedesktop/systemd1/unit/$object" org.freedesktop.systemd1.Unit \
        "$property" 2>"$null_file") \
        || { fail 'monitor configuration metadata is unavailable'; return 1; }
      case "$property:$output" in
        'DropInPaths:as 0'|'NeedDaemonReload:b false') ;;
        *) fail 'monitor override or unreloaded configuration is not allowed'; return 1 ;;
      esac
    done
    if [ "$installed" = 1 ]; then
      # Alongside the source digest check, no overrides + fresh exact fragment
      # proves the command is the declared local-only monitor, not an old
      # ExecStart override inheriting optional Telegram credentials.
      show_contract "$unit" FragmentPath "$systemd_dir/$unit" || return 1
    fi
  done
}

show_runtime_contract() { # scope unit expected-active exact|hermes-prefix expected-cgroup
  local scope=$1 unit=$2 expected_active=$3 match_mode=$4 expected_cgroup=$5
  local output line key value load_state='' active_state='' main_pid='' control_group=''
  local load_count=0 active_count=0 pid_count=0 control_count=0 line_count=0
  local -a arguments=(--property=LoadState --property=ActiveState)
  case "$match_mode" in
    exact-slice) ;;
    exact-service|hermes-service) arguments+=(--property=MainPID) ;;
    *) return 1 ;;
  esac
  arguments+=(--property=ControlGroup)
  output=$(scoped_systemctl "$scope" show "$unit" "${arguments[@]}" 2>"$null_file") || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line_count=$((line_count + 1))
    case "$line" in *$'\r'*|*$'\t'*|*' '*) return 1 ;; esac
    key=${line%%=*}
    value=${line#*=}
    [ "$key" != "$line" ] || return 1
    case "$value" in *$'\n'*|*$'\r'*|*$'\t'*) return 1 ;; esac
    case "$key" in
      LoadState) load_count=$((load_count + 1)); load_state=$value ;;
      ActiveState) active_count=$((active_count + 1)); active_state=$value ;;
      MainPID) pid_count=$((pid_count + 1)); main_pid=$value ;;
      ControlGroup) control_count=$((control_count + 1)); control_group=$value ;;
      *) return 1 ;;
    esac
  done <<< "$output"
  [ "$load_count" -eq 1 ] && [ "$active_count" -eq 1 ] \
    && [ "$control_count" -eq 1 ] || return 1
  case "$match_mode" in
    exact-slice) [ "$pid_count" -eq 0 ] && [ "$line_count" -eq 3 ] || return 1 ;;
    *) [ "$pid_count" -eq 1 ] && [ "$line_count" -eq 4 ] || return 1 ;;
  esac
  [ "$load_state" = loaded ] && [ "$active_state" = "$expected_active" ] || return 1
  case "$expected_active:$match_mode" in
    inactive:exact-service|inactive:hermes-service)
      [ "$main_pid" = 0 ] && [ -z "$control_group" ] || return 1
      ;;
    active:exact-slice)
      [ "$control_group" = "$expected_cgroup" ] || return 1
      ;;
    active:exact-service)
      is_uint "$main_pid" && [ "$main_pid" -gt 0 ] \
        && [ "$control_group" = "$expected_cgroup" ] || return 1
      ;;
    active:hermes-service)
      is_uint "$main_pid" && [ "$main_pid" -gt 0 ] || return 1
      [[ "$control_group" =~ ^/[A-Za-z0-9_.@:/-]+$ ]] || return 1
      case "$control_group" in *'//'*) return 1 ;; esac
      case "$control_group" in
        "$expected_cgroup"/*/"$unit") ;;
        *) return 1 ;;
      esac
      ;;
    *) return 1 ;;
  esac
}

runtime_expected_activity() { # scope unit tracked-index
  local scope=$1 unit=$2 index=$3
  if [ "${#desired_active_states[@]}" -eq "${#tracked_units[@]}" ]; then
    printf '%s\n' "${desired_active_states[$index]}"
  else
    read_unit_state "$scope" is-active "$unit"
  fi
}

verify_runtime_postflight() { # Hermes uid
  local uid=$1 expected unit index
  show_runtime_contract system legaltech.slice active exact-slice /legaltech.slice || return 1
  expected=$(runtime_expected_activity system estrado-pjud.service 0) || return 1
  show_runtime_contract system estrado-pjud.service "$expected" exact-service \
    /legaltech.slice/estrado-pjud.service || return 1
  expected=$(runtime_expected_activity system estrado-pjud-worker.service 1) || return 1
  show_runtime_contract system estrado-pjud-worker.service "$expected" exact-service \
    "$WORKER_CGROUP" || return 1
  show_runtime_contract system "user-$uid.slice" active exact-slice \
    "/user.slice/user-$uid.slice" || return 1
  for ((index = system_unit_count; index < ${#tracked_units[@]}; index++)); do
    unit=${tracked_units[$index]}
    expected=$(runtime_expected_activity user "$unit" "$index") || return 1
    show_runtime_contract user "$unit" "$expected" hermes-service \
      "/user.slice/user-$uid.slice" || return 1
  done
}

verify_timer_schedule() {
  local unit=$1 output line boot=0 active=0 calendar=0
  local schedule_pattern='^TimersMonotonic=\{ (OnBootUSec|OnUnitActiveUSec)=5min ; next_elapse=([[:alnum:].]+( [[:alnum:].]+)*|\[not set\]) \}$'
  # systemd exposes a repeated composite property, not direct On* properties.
  # Even --all omits empty arrays on systemd 255. Any nonempty calendar entry
  # is forbidden; an omitted calendar property therefore means no schedule.
  output=$(LC_ALL=C "$systemctl_bin" show "$unit" --all \
    --property=TimersMonotonic --property=TimersCalendar 2>"$null_file") || return 1
  while IFS= read -r line; do
    if [ "$line" = 'TimersCalendar=' ]; then
      calendar=$((calendar + 1))
    elif [[ "$line" =~ $schedule_pattern ]]; then
      case "${BASH_REMATCH[1]}" in
        OnBootUSec) boot=$((boot + 1)) ;;
        OnUnitActiveUSec) active=$((active + 1)) ;;
      esac
    else
      return 1
    fi
  done <<< "$output"
  [ "$boot" -eq 1 ] && [ "$active" -eq 1 ] && [ "$calendar" -le 1 ]
}

run_postflight() {
  local uid timer target enabled active
  uid=$(validate_hermes_inventory) || { fail 'postflight Hermes inventory is unknown'; return 1; }
  show_contract legaltech.slice CPUWeight 1000 MemoryLow 3221225472 MemoryHigh 6442450944 MemoryMax 8589934592 || return 1
  show_contract estrado-pjud.service Slice legaltech.slice MemoryHigh 3221225472 MemoryMax 4294967296 CPUQuotaPerSecUSec 2s CPUWeight 500 TasksMax 512 || return 1
  show_contract estrado-pjud-worker.service PartOf legaltech.slice Slice legaltech.slice MemoryHigh 2147483648 MemoryMax 3221225472 CPUQuotaPerSecUSec 2s CPUWeight 800 TasksMax 512 || return 1
  show_contract "user-$uid.slice" MemoryHigh 2147483648 MemoryMax 2621440000 TasksMax 1024 CPUWeight 200 || return 1
  verify_runtime_postflight "$uid" || return 1
  show_contract legaltech-monitor.service \
    Type oneshot User root PartOf '' Result success ExecMainStatus 0 \
    EnvironmentFiles '/etc/legaltech-monitoring.env (ignore_errors=yes)' \
    WorkingDirectory /opt/legaltech-monitoring \
    NoNewPrivileges yes PrivateTmp yes ProtectSystem strict ProtectHome yes \
    StateDirectory legaltech-monitor StateDirectoryMode 0750 \
    LogsDirectory legaltech LogsDirectoryMode 0750 \
    ReadWritePaths '/var/lib/legaltech-monitor /var/log/legaltech' \
    RestrictAddressFamilies '~' Slice system.slice MemoryMax 134217728 \
    CPUQuotaPerSecUSec 200ms TasksMax 64 || return 1
  show_contract legaltech-resource-tracker.service \
    Type oneshot User root PartOf '' Result success ExecMainStatus 0 \
    WorkingDirectory /opt/legaltech-monitoring \
    NoNewPrivileges yes PrivateTmp yes ProtectSystem strict ProtectHome yes \
    StateDirectory '' LogsDirectory '' ReadWritePaths /var/log/legaltech/resources.csv \
    RestrictAddressFamilies AF_UNIX Slice system.slice MemoryMax 134217728 \
    CPUQuotaPerSecUSec 200ms TasksMax 64 || return 1
  verify_tracker_environment_files || return 1
  verify_monitor_configuration 1 || return 1
  for timer in legaltech-monitor.timer legaltech-resource-tracker.timer; do
    case "$timer" in
      legaltech-monitor.timer) target=legaltech-monitor.service ;;
      legaltech-resource-tracker.timer) target=legaltech-resource-tracker.service ;;
    esac
    show_contract "$timer" Unit "$target" Persistent yes RandomizedDelayUSec 1min || return 1
    verify_timer_schedule "$timer" || { fail 'timer schedule contract is invalid'; return 1; }
    enabled=$(read_unit_state system is-enabled "$timer") || return 1
    active=$(read_unit_state system is-active "$timer") || return 1
    [ "$enabled" = enabled ] && [ "$active" = active ] || return 1
  done
  "$swap_bin" verify >"$null_file" 2>&1 || return 1
  check_public_health || return 1
  printf '%s\n' 'POSTFLIGHT OK'
}

validate_backup_dir() {
  local leaf=${1##*/} stored_sha
  case "$1" in "$backup_root"/*) ;; *) return 1 ;; esac
  [ "${1%/*}" = "$backup_root" ] || return 1
  [[ "$leaf" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || return 1
  safe_existing_path "$1" && validate_root_path "$1" 700 || return 1
  [ -d "$1/entries" ] && [ ! -L "$1/entries" ] || return 1
  validate_root_path "$1/entries" 700 || return 1
  [ -f "$1/manifest.tsv" ] && [ ! -L "$1/manifest.tsv" ] || return 1
  validate_root_path "$1/manifest.tsv" 600 || return 1
  [ -f "$1/changes" ] && [ ! -L "$1/changes" ] || return 1
  validate_root_path "$1/changes" 600 || return 1
  [ -f "$1/swap-state" ] && [ ! -L "$1/swap-state" ] || return 1
  validate_root_path "$1/swap-state" 600 || return 1
  [ -f "$1/expected-sha" ] && [ ! -L "$1/expected-sha" ] || return 1
  validate_root_path "$1/expected-sha" 600 || return 1
  stored_sha=$(<"$1/expected-sha")
  [[ "$stored_sha" =~ ^[0-9a-f]{40}$ ]] || return 1
  [ -z "$expected_sha" ] || [ "$stored_sha" = "$expected_sha" ] || return 1
  [ -f "$1/unit-states.tsv" ] && [ ! -L "$1/unit-states.tsv" ] || return 1
  validate_root_path "$1/unit-states.tsv" 600 || return 1
  [ -f "$1/monitor-runtime.tsv" ] && [ ! -L "$1/monitor-runtime.tsv" ] || return 1
  validate_root_path "$1/monitor-runtime.tsv" 600 || return 1
  [ -f "$1/worker-runtime.tsv" ] && [ ! -L "$1/worker-runtime.tsv" ] || return 1
  validate_root_path "$1/worker-runtime.tsv" 600 || return 1
}

validate_manifest() {
  local path existed rel mode owner group extra backup_path fields bmode bowner bgroup _blinks
  local seen='|' seen_rel='|' count=0 managed expected_rel
  while IFS=$'\t' read -r path existed rel mode owner group extra; do
    [ -n "$path" ] && [ -z "${extra:-}" ] && is_managed_path "$path" || return 1
    [ "$count" -lt "${#managed_paths[@]}" ] && [ "$path" = "${managed_paths[$count]}" ] || return 1
    case "$seen" in *"|$path|"*) return 1 ;; esac
    seen="$seen$path|"
    count=$((count + 1))
    case "$existed" in
      1)
        [[ "$mode" =~ ^[0-7]{3,4}$ ]] && is_uint "$owner" && is_uint "$group" || return 1
        expected_rel=$(printf 'entries/%04d' "$count")
        [ "$rel" = "$expected_rel" ] || return 1
        case "$seen_rel" in *"|$rel|"*) return 1 ;; esac
        seen_rel="$seen_rel$rel|"
        backup_path="$backup_dir/$rel"
        safe_existing_path "$backup_path" || return 1
        if [ "$path" = "$monitoring_dir" ]; then
          [ -d "$backup_path" ] || return 1
        else
          [ -f "$backup_path" ] || return 1
        fi
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
  changed_api=0 changed_worker=0 worker_stopped=0 changed_hermes=0 changed_monitor=0 changed_tracker=0
  while IFS= read -r change || [ -n "$change" ]; do
    case "$change" in
      api) [ "$changed_api" -eq 0 ] || return 1; changed_api=1 ;;
      worker) [ "$changed_worker" -eq 0 ] || return 1; changed_worker=1 ;;
      worker-stop) [ "$worker_stopped" -eq 0 ] || return 1; worker_stopped=1 ;;
      hermes) [ "$changed_hermes" -eq 0 ] || return 1; changed_hermes=1 ;;
      monitor) [ "$changed_monitor" -eq 0 ] || return 1; changed_monitor=1 ;;
      tracker) [ "$changed_tracker" -eq 0 ] || return 1; changed_tracker=1 ;;
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
    if [ "$skip_swap" -eq 1 ] && { [ "$path" = "$fstab_file" ] \
      || [ "$path" = "$sysctl_file" ] || [ "$path" = "$swappiness_metadata_file" ]; }; then continue; fi
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

restore_enabled_state() { # system|user unit enabled|disabled|static
  local scope=$1 unit=$2 desired=$3 current action
  current=$(read_unit_state "$scope" is-enabled "$unit") || return 1
  case "$desired" in
    absent)
      [ "$current" = absent ] || return 1
      ;;
    static)
      [ "$current" = static ] || return 1
      ;;
    enabled|disabled)
      if [ "$current" != "$desired" ]; then
        case "$desired" in enabled) action=enable ;; disabled) action=disable ;; esac
        scoped_systemctl "$scope" "$action" "$unit" >"$null_file" 2>&1 || return 1
      fi
      ;;
    *) return 1 ;;
  esac
  current=$(read_unit_state "$scope" is-enabled "$unit") || return 1
  [ "$current" = "$desired" ]
}

worker_restoration_window_allows() { # 0=no capability, 1=admitted changed-worker restore
  local worker_restore_capability=$1
  case "$worker_restore_capability" in 0|1) ;; *) return 1 ;; esac
  maintenance_window_is_open && return 0
  [ "$worker_restore_capability" -eq 1 ]
}

read_monitor_restore_activity() { # unit, captured enablement, restored-config capability
  local unit=$1 enabled=$2 recovery_allowed=$3 output rc=0
  if output=$(read_correlated_unit_activity system "$unit" "$enabled"); then
    printf '%s\n' "$output"
    return 0
  fi
  [ "$recovery_allowed" = 1 ] || return 1
  # Only after validated manifest restoration. Never treat failed as healthy
  # during admission, or extend this recovery to API/worker/Hermes fences.
  case "$unit" in
    legaltech-monitor.service|legaltech-resource-tracker.service|\
    legaltech-monitor.timer|legaltech-resource-tracker.timer) ;;
    *) return 1 ;;
  esac
  output=$("$systemctl_bin" is-active "$unit" 2>"$null_file") || rc=$?
  [ "$output" = failed ] || return 1
  case "$rc" in
    3) ;;
    4)
      # Removed running timers retain failed state after daemon-reload, with
      # rc=4. Recover only the two originally absent timers, never services.
      [ "$enabled" = absent ] || return 1
      case "$unit" in legaltech-monitor.timer|legaltech-resource-tracker.timer) ;; *) return 1 ;; esac
      show_contract "$unit" LoadState not-found FragmentPath '' || return 1
      [ "$(read_unit_state system is-enabled "$unit")" = absent ] || return 1
      ;;
    *) return 1 ;;
  esac
  printf 'ROLLBACK: recovering failed monitoring unit %s\n' "$unit" >&2
  "$systemctl_bin" stop "$unit" >"$null_file" 2>&1 || return 1
  "$systemctl_bin" reset-failed "$unit" >"$null_file" 2>&1 || return 1
  # reset-failed is not a success claim: re-read activity with the original
  # strict rules, including absent/inactive correlation for these two timers.
  read_correlated_unit_activity system "$unit" "$enabled"
}

restore_unit_states() { # 0=no capability, 1=admitted changed-worker restore
  local worker_restore_capability=$1 monitor_recovery_allowed=${2:-0}
  local index unit desired current rc=0 action
  case "$worker_restore_capability" in 0|1) ;; *) return 1 ;; esac
  case "$monitor_recovery_allowed" in 0|1) ;; *) return 1 ;; esac
  for ((index = 0; index < system_unit_count; index++)); do
    unit=${tracked_units[$index]}
    desired=${desired_enabled_states[$index]}
    restore_enabled_state system "$unit" "$desired" || rc=1
  done

  for ((index = 0; index < system_unit_count; index++)); do
    unit=${tracked_units[$index]}
    desired=${desired_active_states[$index]}
    current=$(read_monitor_restore_activity "$unit" \
      "${desired_enabled_states[$index]}" "$monitor_recovery_allowed") || { rc=1; continue; }
    if [ "$unit" = estrado-pjud.service ] && [ "$changed_api" -eq 1 ]; then
      if [ "$desired" = active ]; then
        "$systemctl_bin" restart "$unit" >"$null_file" 2>&1 || rc=1
      elif [ "$current" = active ]; then
        "$systemctl_bin" stop "$unit" >"$null_file" 2>&1 || rc=1
      fi
    elif [ "$unit" = estrado-pjud-worker.service ] \
      && { [ "$changed_worker" -eq 1 ] || [ "$worker_stopped" -eq 1 ]; }; then
      if [ "$desired" = active ]; then
        if [ "$worker_restore_allowed" -eq 1 ] \
          && load_worker_fence_config \
          && worker_restoration_window_allows "$worker_restore_capability"; then
          if "$systemctl_bin" start "$unit" >"$null_file" 2>&1; then
            worker_runtime_matches_capture active || rc=1
          else
            rc=1
          fi
        else
          rc=1
        fi
      elif [ "$current" = active ]; then
        "$systemctl_bin" stop "$unit" >"$null_file" 2>&1 || rc=1
      fi
    elif { [ "$unit" = legaltech-monitor.service ] && [ "$changed_monitor" -eq 1 ]; } \
      || { [ "$unit" = legaltech-resource-tracker.service ] && [ "$changed_tracker" -eq 1 ]; }; then
      if [ "$desired" = active ]; then
        "$systemctl_bin" restart "$unit" >"$null_file" 2>&1 || rc=1
      elif [ "$current" = active ]; then
        "$systemctl_bin" stop "$unit" >"$null_file" 2>&1 || rc=1
      fi
    elif [ "$current" != "$desired" ]; then
      if [ "$unit" = estrado-pjud-worker.service ] && [ "$desired" = active ]; then
        if load_worker_fence_config \
          && worker_restoration_window_allows "$worker_restore_capability"; then
          "$systemctl_bin" start "$unit" >"$null_file" 2>&1 || rc=1
        else
          rc=1
        fi
      else
        case "$desired" in active) action=start ;; inactive) action=stop ;; esac
        "$systemctl_bin" "$action" "$unit" >"$null_file" 2>&1 || rc=1
      fi
    fi
    current=$(read_correlated_unit_activity system "$unit" \
      "${desired_enabled_states[$index]}") || { rc=1; continue; }
    [ "$current" = "$desired" ] || rc=1
    if [ "$unit" = estrado-pjud-worker.service ] \
      && { [ "$changed_worker" -eq 1 ] || [ "$worker_stopped" -eq 1 ]; }; then
      worker_runtime_matches_capture "$desired" || rc=1
    fi
  done
  return "$rc"
}

restore_hermes_unit_states() {
  local index unit desired current rc=0
  [ "$changed_hermes" -eq 1 ] || return 0
  for ((index = system_unit_count; index < ${#tracked_units[@]}; index++)); do
    unit=${tracked_units[$index]}
    desired=${desired_enabled_states[$index]}
    restore_enabled_state user "$unit" "$desired" || rc=1
  done
  for ((index = system_unit_count; index < ${#tracked_units[@]}; index++)); do
    unit=${tracked_units[$index]}
    desired=${desired_active_states[$index]}
    current=$(read_correlated_unit_activity user "$unit" \
      "${desired_enabled_states[$index]}") || { rc=1; continue; }
    if [ "$desired" = active ]; then
      scoped_systemctl user restart "$unit" >"$null_file" 2>&1 || rc=1
    elif [ "$current" = active ]; then
      scoped_systemctl user stop "$unit" >"$null_file" 2>&1 || rc=1
    fi
    current=$(read_correlated_unit_activity user "$unit" \
      "${desired_enabled_states[$index]}") || { rc=1; continue; }
    [ "$current" = "$desired" ] || rc=1
  done
  return "$rc"
}

run_swap_mutator() { # apply|rollback
  case "$1" in apply|rollback) ;; *) return 1 ;; esac
  is_uint "$resource_lock_fd" || return 1
  LEGALTECH_RESOURCE_LOCK_FD="$resource_lock_fd" "$swap_bin" "$1"
}

inspect_swap_state() {
  local output
  is_uint "$resource_lock_fd" || return 1
  output=$(LEGALTECH_RESOURCE_LOCK_FD="$resource_lock_fd" "$swap_bin" preflight \
    2>"$null_file") || return 1
  case "$output" in clean|managed) printf '%s\n' "$output" ;; *) return 1 ;; esac
}

inspect_swap_rollback_state() {
  local output
  is_uint "$resource_lock_fd" || return 1
  output=$(LEGALTECH_RESOURCE_LOCK_FD="$resource_lock_fd" "$swap_bin" rollback-preflight \
    2>"$null_file") || return 1
  case "$output" in
    clean|apply-swapfile|apply-mkswap|apply-fstab|apply-sysctl|apply-swappiness|\
    apply-swapon|managed-active|rollback-swappiness|rollback-swapoff|\
    rollback-fstab|rollback-sysctl|rollback-swapfile|rollback-metadata)
      printf '%s\n' "$output"
      ;;
    *) return 1 ;;
  esac
}

validate_swap_rollback_authority() {
  local marker observed
  marker=$(<"$backup_dir/swap-state")
  case "$marker" in
    attempted)
      observed=$(inspect_swap_rollback_state) \
        || { fail 'rollback swap ownership state is unsafe or unknown'; return 1; }
      ;;
    not-attempted)
      observed=$(inspect_swap_state) \
        || { fail 'rollback swap ownership state is unsafe or unknown'; return 1; }
      [ "$observed" = clean ] \
        || { fail 'rollback swap marker conflicts with owned live state'; return 1; }
      ;;
    preexisting)
      observed=$(inspect_swap_state) \
        || { fail 'rollback preexisting swap state is unsafe or unknown'; return 1; }
      [ "$observed" = managed ] \
        || { fail 'rollback preexisting swap state changed unexpectedly'; return 1; }
      ;;
    *) fail 'rollback swap metadata is invalid'; return 1 ;;
  esac
  swap_state=$marker
}

do_rollback() { # backup-dir worker-restore-capability automatic-compensation
  local uid rollback_rc=0 swap_rc=0 swap_state monitor_recovery_allowed=1
  local worker_restore_capability=${2:-0} automatic_compensation=${3:-0}
  case "$worker_restore_capability" in 0|1) ;; *) return 1 ;; esac
  case "$automatic_compensation" in 0|1) ;; *) return 1 ;; esac
  backup_dir=$1
  uid=$(validate_hermes_inventory) || { fail 'rollback cannot validate Hermes inventory'; return 1; }
  build_managed_paths "$uid"
  validate_backup_dir "$backup_dir" || { fail 'rollback backup is unsafe or invalid'; return 1; }
  validate_manifest || { fail 'rollback manifest is unsafe or invalid'; return 1; }
  load_and_validate_changes || { fail 'rollback affected-unit metadata is invalid'; return 1; }
  load_and_validate_unit_states || { fail 'rollback live-unit metadata is invalid'; return 1; }
  load_and_validate_worker_runtime || { fail 'rollback worker runtime metadata is invalid'; return 1; }
  load_and_validate_monitor_runtime || { fail 'rollback monitor runtime metadata is invalid'; return 1; }
  validate_swap_rollback_authority || return 1
  worker_restore_allowed=1
  if [ "${desired_active_states[1]}" = active ] \
    && { [ "$changed_worker" -eq 1 ] || [ "$worker_stopped" -eq 1 ]; } \
    && ! worker_restoration_window_allows "$worker_restore_capability"; then
    fail 'rollback active worker restoration is outside the maintenance window'
    return 1
  fi
  if [ "${desired_active_states[1]}" = active ] \
    && { [ "$changed_worker" -eq 1 ] || [ "$worker_stopped" -eq 1 ]; } \
    && ! quiesce_worker_for_restore; then
    rollback_rc=1
    worker_restore_allowed=0
  fi
  case "$swap_state" in
    attempted)
      if ! run_swap_mutator rollback >"$null_file" 2>&1; then swap_rc=1; rollback_rc=1; fi
      ;;
    not-attempted|preexisting) ;;
    *) return 1 ;;
  esac
  if ! restore_manifest "$swap_rc"; then
    rollback_rc=1
    monitor_recovery_allowed=0
  fi
  if ! "$systemctl_bin" daemon-reload >"$null_file" 2>&1; then
    rollback_rc=1
    monitor_recovery_allowed=0
  fi
  restore_unit_states "$worker_restore_capability" "$monitor_recovery_allowed" || rollback_rc=1
  restore_hermes_unit_states || rollback_rc=1
  if [ "$rollback_rc" -ne 0 ]; then
    if [ "$automatic_compensation" -eq 0 ]; then
      printf 'ROLLBACK INCOMPLETO: reintente con el BACKUP_DIR validado: %s\n' \
        "$backup_dir" >&2
    fi
    return 1
  fi
  printf '%s\n' "ROLLBACK OK: $backup_dir"
}

rollback_once=0
automatic_rollback() {
  [ "$rollback_once" -eq 0 ] || return 1
  [ "$mutation_started" -eq 1 ] || return 1
  is_uint "$resource_lock_fd" || return 1
  case "$worker_restore_window_capability" in 0|1) ;; *) return 1 ;; esac
  rollback_once=1
  if do_rollback "$backup_dir" "$worker_restore_window_capability" 1; then return 0; fi
  printf 'ROLLBACK INCOMPLETO: reintente con el BACKUP_DIR validado: %s\n' \
    "$backup_dir" >&2
  return 1
}

stop_legacy_monitor_if_changed() { # change-flag monitor-array-index system-unit-index
  local changed=$1 monitor_index=$2 system_index=$3 unit current
  [ "$changed" -eq 1 ] || return 0
  unit=${monitor_runtime_units[$monitor_index]}
  [ "${desired_active_states[$system_index]}" = active ] || return 0
  "$systemctl_bin" stop "$unit" >"$null_file" 2>&1 || return 1
  current=$(read_unit_state system is-active "$unit") || return 1
  [ "$current" = inactive ]
}

old_pid_is_gone() {
  local pid=$1 output rc
  [ "$pid" -gt 0 ] || return 0
  if output=$("$ps_bin" -p "$pid" -o unit= 2>"$null_file"); then
    return 1
  else
    rc=$?
  fi
  [ "$rc" -eq 1 ] && [ -z "$output" ]
}

read_worker_runtime() {
  read_effective_runtime system estrado-pjud-worker.service
}

worker_runtime_is_active() {
  local runtime active pid control_group effective_slice result
  runtime=$(read_worker_runtime) || return 1
  IFS='|' read -r active pid control_group effective_slice result <<< "$runtime"
  [ "$active" = active ] && [ "$pid" -gt 0 ] \
    && [ "$control_group" = "$WORKER_CGROUP" ] \
    && [ "$effective_slice" = legaltech.slice ]
}

worker_runtime_is_stopped() { # old PID
  local old_pid=$1 expected_slice=$2 runtime active pid control_group effective_slice result
  runtime=$(read_worker_runtime) || return 1
  IFS='|' read -r active pid control_group effective_slice result <<< "$runtime"
  [ "$active" = inactive ] && [ "$pid" -eq 0 ] \
    && [ "$control_group" = - ] && [ "$effective_slice" = "$expected_slice" ] \
    && old_pid_is_gone "$old_pid"
}

worker_runtime_matches_capture() { # expected active|inactive
  local expected_active=$1 runtime active pid control_group effective_slice result
  runtime=$(read_worker_runtime) || return 1
  IFS='|' read -r active pid control_group effective_slice result <<< "$runtime"
  [ "$active" = "$expected_active" ] \
    && [ "$effective_slice" = "$captured_worker_slice" ] || return 1
  case "$expected_active" in
    active)
      [ "$pid" -gt 0 ] && [ "$control_group" = "$captured_worker_control_group" ]
      ;;
    inactive) [ "$pid" = 0 ] && [ "$control_group" = - ] ;;
    *) return 1 ;;
  esac
}

stop_worker_for_change() {
  local runtime active old_pid old_control_group effective_slice result
  runtime=$(read_worker_runtime) || return 1
  IFS='|' read -r active old_pid old_control_group effective_slice result <<< "$runtime"
  [ "$active" = "$captured_worker_active" ] && [ "$active" = active ] \
    && [ "$old_pid" = "$captured_worker_pid" ] \
    && [ "$old_control_group" = "$captured_worker_control_group" ] \
    && [ "$effective_slice" = "$captured_worker_slice" ] \
    && worker_runtime_has_exact_identity "$active" "$old_pid" \
      "$old_control_group" "$effective_slice" || return 1
  record_change worker-stop || return 1
  mutation_started=1
  "$systemctl_bin" stop estrado-pjud-worker.service >"$null_file" 2>&1 || return 1
  [ "$(read_unit_state system is-active estrado-pjud-worker.service)" = inactive ] || return 1
  worker_runtime_is_stopped "$old_pid" "$effective_slice"
}

verify_started_worker_is_idle() {
  local attempt count
  worker_runtime_is_active || return 1
  for ((attempt = 1; attempt <= WORKER_POST_START_HEARTBEAT_ATTEMPTS; attempt++)); do
    if worker_heartbeat_is_idle 1 "$worker_pre_stop_heartbeat_order"; then
      count=$(active_claim_count) || return 1
      [ "$count" = 0 ]
      return
    fi
    [ "$attempt" -lt "$WORKER_POST_START_HEARTBEAT_ATTEMPTS" ] || return 1
    "$sleep_bin" "$worker_heartbeat_poll_delay_seconds" >"$null_file" 2>&1 || return 1
  done
  return 1
}

quiesce_worker_for_restore() {
  local runtime active pid control_group effective_slice result
  runtime=$(read_worker_runtime) || return 1
  IFS='|' read -r active pid control_group effective_slice result <<< "$runtime"
  case "$active" in
    inactive)
      worker_runtime_has_exact_identity "$active" "$pid" "$control_group" "$effective_slice"
      ;;
    active)
      worker_runtime_has_exact_identity "$active" "$pid" "$control_group" "$effective_slice" \
        || return 1
      "$systemctl_bin" stop estrado-pjud-worker.service >"$null_file" 2>&1 || return 1
      [ "$(read_unit_state system is-active estrado-pjud-worker.service)" = inactive ] || return 1
      worker_runtime_is_stopped "$pid" "$effective_slice"
      ;;
    *) return 1 ;;
  esac
}

verify_monitor_migration() { # change-flag monitor-array-index
  local changed=$1 monitor_index=$2 unit runtime active pid control_group effective_slice result old_pid
  [ "$changed" -eq 1 ] || return 0
  unit=${monitor_runtime_units[$monitor_index]}
  runtime=$(read_effective_runtime system "$unit") || return 1
  IFS='|' read -r active pid control_group effective_slice result <<< "$runtime"
  [ "$active" = inactive ] && [ "$pid" -eq 0 ] \
    && [ "$control_group" = - ] && [ "$effective_slice" = system.slice ] \
    && [ "$result" = success ] || return 1
  old_pid=${previous_monitor_pids[$monitor_index]}
  old_pid_is_gone "$old_pid"
}

run_apply_steps() {
  local uid=$1 api_path worker_path worker_dropin_path hermes_path monitor_path tracker_path
  local worker_source worker_dropin_source desired_worker desired_worker_dropin worker_count
  local monitor_source tracker_source before_api before_worker before_worker_dropin before_hermes before_monitor before_tracker
  local desired_monitor desired_tracker after_api after_worker after_worker_dropin after_hermes after_monitor after_tracker
  local monitor_will_change=0 tracker_will_change=0 worker_will_change=0 provision_rc=0
  api_path="$systemd_dir/estrado-pjud.service"
  worker_path="$systemd_dir/estrado-pjud-worker.service"
  worker_dropin_path="$systemd_dir/estrado-pjud-worker.service.d/xvfb.conf"
  worker_source="$repo_dir/ops/systemd/estrado-pjud-worker.service"
  worker_dropin_source="$repo_dir/ops/systemd/estrado-pjud-worker.service.d/xvfb.conf"
  hermes_path="$systemd_dir/user-$uid.slice.d/50-legaltech-resource-limits.conf"
  monitor_path="$systemd_dir/legaltech-monitor.service"
  tracker_path="$systemd_dir/legaltech-resource-tracker.service"
  monitor_source="$repo_dir/ops/systemd/legaltech-monitor.service"
  tracker_source="$repo_dir/ops/systemd/legaltech-resource-tracker.service"
  before_api=$(digest_path "$api_path") || return 1
  before_worker=$(digest_path "$worker_path") || return 1
  before_worker_dropin=$(digest_path "$worker_dropin_path") || return 1
  desired_worker=$(digest_path "$worker_source") || return 1
  desired_worker_dropin=$(digest_path "$worker_dropin_source") || return 1
  before_hermes=$(digest_path "$hermes_path") || return 1
  before_monitor=$(digest_path "$monitor_path") || return 1
  before_tracker=$(digest_path "$tracker_path") || return 1
  desired_monitor=$(digest_path "$monitor_source") || return 1
  desired_tracker=$(digest_path "$tracker_source") || return 1
  [ "$before_monitor" = "$desired_monitor" ] || monitor_will_change=1
  [ "$before_tracker" = "$desired_tracker" ] || tracker_will_change=1
  if [ "$before_worker" != "$desired_worker" ] \
    || [ "$before_worker_dropin" != "$desired_worker_dropin" ]; then
    worker_will_change=1
  fi
  apply_phase=backup
  create_backup "$uid" >"$null_file" || return 1
  validate_backup_dir "$backup_dir" && validate_manifest \
    && load_and_validate_unit_states && load_and_validate_worker_runtime \
    && load_and_validate_monitor_runtime || return 1
  check_git || return 1
  apply_phase=worker-admission
  if [ "$worker_will_change" -eq 1 ] && [ "${desired_active_states[1]}" = active ]; then
    load_worker_fence_config || return 1
    apply_maintenance_window_is_open || return 1
    worker_restore_window_capability=1
    worker_heartbeat_is_idle 0 || return 1
    worker_pre_stop_heartbeat_order=$worker_last_heartbeat_order
    worker_count=$(active_claim_count) || return 1
    [ "$worker_count" = 0 ] || return 1
  fi
  apply_phase=worker-drain
  if [ "$worker_will_change" -eq 1 ] && [ "${desired_active_states[1]}" = active ]; then
    stop_worker_for_change || return 1
    wait_for_zero_claims || return 1
  fi
  if [ "$monitor_will_change" -eq 1 ]; then record_change monitor || return 1; fi
  if [ "$tracker_will_change" -eq 1 ]; then record_change tracker || return 1; fi
  mutation_started=1
  apply_phase=legacy-monitor-stop
  stop_legacy_monitor_if_changed "$monitor_will_change" 0 2 || return 1
  stop_legacy_monitor_if_changed "$tracker_will_change" 1 3 || return 1
  apply_phase=provision
  PROV_ENABLE_PJUD_WORKER=0 PROV_SKIP_CADDY=1 "$provision_bin" >"$null_file" 2>&1 || provision_rc=1
  after_api=$(digest_path "$api_path") || return 1
  after_worker=$(digest_path "$worker_path") || return 1
  after_worker_dropin=$(digest_path "$worker_dropin_path") || return 1
  after_hermes=$(digest_path "$hermes_path") || return 1
  after_monitor=$(digest_path "$monitor_path") || return 1
  after_tracker=$(digest_path "$tracker_path") || return 1
  if [ "$before_api" != "$after_api" ]; then record_change api || return 1; fi
  if [ "$worker_will_change" -eq 1 ]; then
    [ "$after_worker" = "$desired_worker" ] \
      && [ "$after_worker_dropin" = "$desired_worker_dropin" ] || return 1
    record_change worker || return 1
  else
    [ "$after_worker" = "$before_worker" ] \
      && [ "$after_worker_dropin" = "$before_worker_dropin" ] || return 1
  fi
  if [ "$before_hermes" != "$after_hermes" ]; then record_change hermes || return 1; fi
  [ "$after_monitor" = "$desired_monitor" ] || return 1
  [ "$after_tracker" = "$desired_tracker" ] || return 1
  [ "$provision_rc" -eq 0 ] || return 1
  restore_enabled_state system estrado-pjud.service "${desired_enabled_states[0]}" || return 1
  if [ "${swap_initial_state:-unknown}" = clean ]; then
    durable_replace_metadata "$backup_dir/swap-state" $'attempted\n' || return 1
  fi
  apply_phase=swap
  run_swap_mutator apply >"$null_file" 2>&1 || return 1
  "$swap_bin" verify >"$null_file" 2>&1 || return 1
  apply_phase=reload-and-migrate
  "$systemctl_bin" daemon-reload >"$null_file" 2>&1 || return 1
  verify_monitor_migration "$monitor_will_change" 0 || return 1
  verify_monitor_migration "$tracker_will_change" 1 || return 1
  if [ "$before_api" != "$after_api" ]; then
    apply_phase=api-runtime
    if [ "${desired_active_states[0]}" = active ]; then
      [ "$(read_unit_state system is-active estrado-pjud.service)" = active ] || return 1
      "$systemctl_bin" restart estrado-pjud.service >"$null_file" 2>&1 || return 1
      show_runtime_contract system estrado-pjud.service active exact-service \
        /legaltech.slice/estrado-pjud.service || return 1
    else
      [ "$(read_unit_state system is-active estrado-pjud.service)" = inactive ] || return 1
    fi
  fi
  if [ "$before_hermes" != "$after_hermes" ]; then
    apply_phase=hermes-runtime
    local hermes_index hermes_unit hermes_activity
    for ((hermes_index = system_unit_count; hermes_index < ${#tracked_units[@]}; hermes_index++)); do
      hermes_unit=${tracked_units[$hermes_index]}
      hermes_activity=$(read_unit_state user is-active "$hermes_unit") || return 1
      if [ "${desired_active_states[$hermes_index]}" = active ]; then
        [ "$hermes_activity" = active ] || return 1
        scoped_systemctl user restart "$hermes_unit" >"$null_file" 2>&1 || return 1
        show_runtime_contract user "$hermes_unit" active hermes-service \
          "/user.slice/user-$uid.slice" || return 1
      else
        [ "$hermes_activity" = inactive ] || return 1
      fi
    done
  fi
  if [ "$worker_will_change" -eq 1 ]; then
    apply_phase=worker-runtime
    if [ "${desired_active_states[1]}" = active ]; then
      [ "$(read_unit_state system is-active estrado-pjud-worker.service)" = inactive ] || return 1
      "$systemctl_bin" start estrado-pjud-worker.service >"$null_file" 2>&1 || return 1
      show_runtime_contract system estrado-pjud-worker.service active exact-service \
        "$WORKER_CGROUP" || return 1
      verify_started_worker_is_idle || return 1
    else
      [ "$(read_unit_state system is-active estrado-pjud-worker.service)" = inactive ] || return 1
    fi
  fi
  apply_phase=timers
  verify_monitor_configuration 1 || return 1
  "$systemctl_bin" start legaltech-monitor.timer legaltech-resource-tracker.timer >"$null_file" 2>&1 || return 1
  apply_phase=tracker-sample
  "$python_bin" "$monitoring_dir/resource-tracker.py" --once >"$null_file" 2>&1 || return 1
  apply_phase=monitor-evaluation
  "$python_bin" "$monitoring_dir/monitor.py" --dry-run --delivery local >"$null_file" 2>&1 || return 1
  # Exercise the real oneshot sandboxes, not only Python outside systemd.
  # The declared monitor uses local delivery; this never sends Telegram.
  apply_phase=monitor-sandbox
  "$systemctl_bin" start legaltech-monitor.service legaltech-resource-tracker.service >"$null_file" 2>&1 || return 1
  apply_phase=postflight
  run_postflight || return 1
}

run_apply() {
  local uid apply_phase=initial-digests
  worker_restore_window_capability=0
  run_preflight || return 1
  uid=$(validate_hermes_inventory) || return 1
  build_managed_paths "$uid"
  backup_dir=''
  mutation_started=0
  if ! run_apply_steps "$uid"; then
    fail "apply failed in phase: $apply_phase" || true
    if [ -n "$backup_dir" ] && [ "$mutation_started" -eq 1 ]; then automatic_rollback || true; fi
    return 1
  fi
  printf '%s\n' "APPLY OK; backup: $backup_dir"
}

case "$command_name" in
  apply|rollback) acquire_resource_mutation_lock || exit "$EXIT_ERROR" ;;
esac

case "$command_name" in
  preflight) run_preflight ;;
  apply) run_apply ;;
  postflight) run_postflight ;;
  rollback) do_rollback "$requested_backup" 0 0 ;;
esac
