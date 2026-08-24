#!/usr/bin/env bash
# Gestión explícita y fail-closed del swap de emergencia de LegalTech.
# Los overrides existen sólo para el harness aislado y requieren
# SWAP_TEST_MODE=1; una ejecución normal siempre usa los paths del VPS.
set -euo pipefail

readonly EXIT_ERROR=1
readonly EXIT_USAGE=2
readonly SWAP_BYTES=4294967296
readonly MIN_FREE_BYTES=8589934592
readonly RAM_MARGIN_BYTES=1073741824
readonly BEGIN_MARKER='# BEGIN LEGALTECH MANAGED SWAP'
readonly END_MARKER='# END LEGALTECH MANAGED SWAP'

fail() {
  printf '%s\n' 'ERROR: swap state is unsafe or invalid' >&2
  exit "$EXIT_ERROR"
}

usage() {
  printf '%s\n' 'usage: configure-swap.sh {preflight|rollback-preflight|apply|verify|rollback}' >&2
  exit "$EXIT_USAGE"
}

is_uint() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

command_name=${1:-}
[ "$#" -eq 1 ] || usage
case "$command_name" in
  preflight|rollback-preflight|apply|verify|rollback) ;;
  *) usage ;;
esac

test_mode=${SWAP_TEST_MODE:-0}
override_present=0
for variable_name in \
  SWAP_FILE SWAP_FSTAB_FILE SWAP_SYSCTL_FILE SWAP_PROC_SWAPS_FILE \
  SWAP_SWAPPINESS_METADATA_FILE SWAP_TEST_ROOT_UID SWAP_TEST_ROOT_GID \
  SWAP_DF_BIN SWAP_FALLOCATE_BIN SWAP_DD_BIN SWAP_CHMOD_BIN \
  SWAP_MKSWAP_BIN SWAP_SWAPON_BIN SWAP_SWAPOFF_BIN SWAP_SYSCTL_BIN \
  SWAP_FREE_BIN SWAP_CP_BIN SWAP_MV_BIN SWAP_STAT_BIN SWAP_RM_BIN \
  SWAP_FLOCK_BIN SWAP_READLINK_BIN SWAP_PYTHON_BIN SWAP_LOCK_FILE SWAP_FD_ROOT \
  SWAP_TEST_CRASH_AFTER
do
  if [ "${!variable_name+x}" = x ]; then
    override_present=1
  fi
done
if [ "$override_present" -eq 1 ] && [ "$test_mode" != 1 ]; then
  usage
fi
if [ "$test_mode" != 0 ] && [ "$test_mode" != 1 ]; then
  usage
fi
if [ "$test_mode" = 1 ]; then
  for variable_name in \
    SWAP_FILE SWAP_FSTAB_FILE SWAP_SYSCTL_FILE SWAP_PROC_SWAPS_FILE \
    SWAP_SWAPPINESS_METADATA_FILE SWAP_TEST_ROOT_UID SWAP_TEST_ROOT_GID \
    SWAP_DF_BIN SWAP_FALLOCATE_BIN SWAP_DD_BIN SWAP_CHMOD_BIN \
    SWAP_MKSWAP_BIN SWAP_SWAPON_BIN SWAP_SWAPOFF_BIN SWAP_SYSCTL_BIN \
    SWAP_FREE_BIN SWAP_CP_BIN SWAP_MV_BIN SWAP_STAT_BIN SWAP_RM_BIN \
    SWAP_FLOCK_BIN SWAP_READLINK_BIN SWAP_PYTHON_BIN SWAP_LOCK_FILE SWAP_FD_ROOT \
    SWAP_TEST_CRASH_AFTER
  do
    [ "${!variable_name+x}" = x ] || usage
  done
fi

swap_file=${SWAP_FILE:-/swapfile}
fstab_file=${SWAP_FSTAB_FILE:-/etc/fstab}
sysctl_file=${SWAP_SYSCTL_FILE:-/etc/sysctl.d/60-legaltech-swap.conf}
swappiness_metadata_file=${SWAP_SWAPPINESS_METADATA_FILE:-/etc/sysctl.d/60-legaltech-swap.previous}
proc_swaps_file=${SWAP_PROC_SWAPS_FILE:-/proc/swaps}
root_uid=${SWAP_TEST_ROOT_UID:-0}
root_gid=${SWAP_TEST_ROOT_GID:-0}

df_bin=${SWAP_DF_BIN:-/usr/bin/df}
fallocate_bin=${SWAP_FALLOCATE_BIN:-/usr/bin/fallocate}
dd_bin=${SWAP_DD_BIN:-/usr/bin/dd}
chmod_bin=${SWAP_CHMOD_BIN:-/usr/bin/chmod}
mkswap_bin=${SWAP_MKSWAP_BIN:-/usr/sbin/mkswap}
swapon_bin=${SWAP_SWAPON_BIN:-/usr/sbin/swapon}
swapoff_bin=${SWAP_SWAPOFF_BIN:-/usr/sbin/swapoff}
sysctl_bin=${SWAP_SYSCTL_BIN:-/usr/sbin/sysctl}
free_bin=${SWAP_FREE_BIN:-/usr/bin/free}
cp_bin=${SWAP_CP_BIN:-/usr/bin/cp}
mv_bin=${SWAP_MV_BIN:-/usr/bin/mv}
stat_bin=${SWAP_STAT_BIN:-/usr/bin/stat}
rm_bin=${SWAP_RM_BIN:-/usr/bin/rm}
flock_bin=${SWAP_FLOCK_BIN:-/usr/bin/flock}
readlink_bin=${SWAP_READLINK_BIN:-/usr/bin/readlink}
python_bin=${SWAP_PYTHON_BIN:-/usr/bin/python3}
lock_file=${SWAP_LOCK_FILE:-/run/lock/legaltech-resource-guards.lock}
fd_root=${SWAP_FD_ROOT:-/proc/self/fd}

for absolute_value in \
  "$swap_file" "$fstab_file" "$sysctl_file" "$swappiness_metadata_file" "$proc_swaps_file" \
  "$df_bin" "$fallocate_bin" "$dd_bin" "$chmod_bin" "$mkswap_bin" \
  "$swapon_bin" "$swapoff_bin" "$sysctl_bin" "$free_bin" "$cp_bin" \
  "$mv_bin" "$stat_bin" "$rm_bin" \
  "$flock_bin" "$readlink_bin" "$python_bin" "$lock_file" "$fd_root"
do
  case "$absolute_value" in
    /*) ;;
    *) usage ;;
  esac
done
is_uint "$root_uid" && is_uint "$root_gid" || usage

fstab_state=invalid
fstab_mode=''
backup_state=invalid
backup_mode=''
active_target_count=0
active_target_used_bytes=0
swap_file_exists=0
sysctl_state=absent
swappiness_metadata_state=absent
previous_swappiness=''
apply_phase=''
rollback_state=invalid
transaction_temp_state=invalid
transaction_temps=()
generated_temp=''
current_temp=''
mutation_lock_fd=''
mutation_lock_owned=0

cleanup_temp() {
  if [ -n "$current_temp" ] && { [ -e "$current_temp" ] || [ -L "$current_temp" ]; }; then
    set +e
    "$rm_bin" "$current_temp" >/dev/null 2>&1
  fi
  if [ "$mutation_lock_owned" -eq 1 ] && [ -n "$mutation_lock_fd" ]; then
    "$flock_bin" -u "$mutation_lock_fd" >/dev/null 2>&1 || true
    exec {mutation_lock_fd}>&-
  fi
}
trap cleanup_temp EXIT

test_crash_after() {
  [ "$test_mode" = 1 ] || return 0
  [ "${SWAP_TEST_CRASH_AFTER:-}" = "$1" ] || return 0
  kill -KILL "$$"
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

validate_mutation_lock_file() {
  local metadata kind mode size links uid gid extra
  [ -f "$lock_file" ] && [ ! -L "$lock_file" ] \
    && ! path_has_symlink_component "$lock_file" || return 1
  metadata=$("$stat_bin" -c '%F|%a|%s|%h|%u|%g' "$lock_file") || return 1
  IFS='|' read -r kind mode size links uid gid extra <<< "$metadata" || return 1
  [ "$kind" = 'regular file' ] && [ "$mode" = 600 ] && [ "$links" = 1 ] \
    && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] \
    && [ -z "$extra" ]
}

lock_fd_targets_exact_file() { # fd
  local fd=$1 target
  is_uint "$fd" && [ "$fd" -ge 3 ] || return 1
  target=$("$readlink_bin" "$fd_root/$fd" 2>/dev/null) || return 1
  [ "$target" = "$lock_file" ] && validate_mutation_lock_file
}

acquire_mutation_lock() {
  local inherited_fd=${LEGALTECH_RESOURCE_LOCK_FD:-}
  if [ -n "$inherited_fd" ]; then
    lock_fd_targets_exact_file "$inherited_fd" || return 1
    "$flock_bin" -n "$inherited_fd" >/dev/null 2>&1 || return 1
    return 0
  fi
  if [ ! -e "$lock_file" ] && [ ! -L "$lock_file" ]; then
    path_has_symlink_component "${lock_file%/*}" && return 1
    ( umask 077; set -o noclobber; : > "$lock_file" ) 2>/dev/null || true
  fi
  validate_mutation_lock_file || return 1
  exec {mutation_lock_fd}>"$lock_file" || return 1
  lock_fd_targets_exact_file "$mutation_lock_fd" || return 1
  if ! "$flock_bin" -n "$mutation_lock_fd" >/dev/null 2>&1; then
    printf '%s\n' 'ERROR: another resource mutation is already in progress' >&2
    return 1
  fi
  mutation_lock_owned=1
}

inspect_fstab() {
  [ -f "$fstab_file" ] && [ ! -L "$fstab_file" ] || return 1

  local metadata kind mode size links uid gid extra line first _rest
  local stage=0 blocks=0
  local expected_entry="$swap_file none swap sw 0 0"
  fstab_mode=''
  metadata=$("$stat_bin" -c '%F|%a|%s|%h|%u|%g' "$fstab_file") || return 1
  kind=''; mode=''; size=''; links=''; uid=''; gid=''; extra=''
  IFS='|' read -r kind mode size links uid gid extra <<< "$metadata" || return 1
  [ "$kind" = 'regular file' ] && is_uint "$size" && [ "$links" = 1 ] && \
    [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] && [ -z "$extra" ] || return 1
  case "$mode" in 600|640|644) ;; *) return 1 ;; esac
  fstab_mode=$mode
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$stage" -eq 1 ]; then
      [ "$line" = "$expected_entry" ] || return 1
      stage=2
      continue
    fi
    if [ "$stage" -eq 2 ]; then
      [ "$line" = "$END_MARKER" ] || return 1
      stage=0
      blocks=$((blocks + 1))
      continue
    fi
    if [ "$line" = "$BEGIN_MARKER" ]; then
      [ "$blocks" -eq 0 ] || return 1
      stage=1
      continue
    fi
    [ "$line" != "$END_MARKER" ] || return 1

    first=''; _rest=''
    read -r first _rest <<< "$line" || true
    [ "$first" != "$swap_file" ] || [ "${first#\#}" != "$first" ] || return 1
  done < "$fstab_file" || return 1

  [ "$stage" -eq 0 ] || return 1
  if [ "$blocks" -eq 0 ]; then
    fstab_state=absent
    return 0
  fi
  if [ "$blocks" -eq 1 ]; then
    fstab_state=managed
    return 0
  fi
  return 1
}

inspect_active_swaps() {
  [ -f "$proc_swaps_file" ] && [ ! -L "$proc_swaps_file" ] || return 1

  local line device type size used priority extra line_number=0
  active_target_count=0
  active_target_used_bytes=0
  while IFS= read -r line || [ -n "$line" ]; do
    line_number=$((line_number + 1))
    if [ "$line_number" -eq 1 ]; then
      device=''; type=''; size=''; used=''; priority=''; extra=''
      read -r device type size used priority extra <<< "$line" || return 1
      [ "$device" = Filename ] && [ "$type" = Type ] && \
        [ "$size" = Size ] && [ "$used" = Used ] && \
        [ "$priority" = Priority ] && [ -z "$extra" ] || return 1
      continue
    fi
    [ -n "$line" ] || continue
    device=''; type=''; size=''; used=''; priority=''; extra=''
    read -r device type size used priority extra <<< "$line" || return 1
    [ -n "$device" ] && [ -n "$type" ] && is_uint "$size" && \
      is_uint "$used" && [ -n "$priority" ] && [ -z "$extra" ] || return 1
    [ "$device" = "$swap_file" ] || return 1
    [ "$type" = file ] || return 1
    [ "$used" -le 9007199254740991 ] || return 1
    active_target_count=$((active_target_count + 1))
    active_target_used_bytes=$((used * 1024))
  done < "$proc_swaps_file" || return 1
  [ "$line_number" -ge 1 ] || return 1
  [ "$active_target_count" -le 1 ] || return 1
}

inspect_swap_file() {
  local require_mode="${1:-1}"
  swap_file_exists=0
  if [ ! -e "$swap_file" ] && [ ! -L "$swap_file" ]; then
    return 0
  fi
  swap_file_exists=1

  local metadata kind mode size links uid gid extra
  metadata=$("$stat_bin" -c '%F|%a|%s|%h|%u|%g' "$swap_file") || return 1
  kind=''; mode=''; size=''; links=''; uid=''; gid=''; extra=''
  IFS='|' read -r kind mode size links uid gid extra <<< "$metadata" || return 1
  [ "$kind" = 'regular file' ] && [ "$size" = "$SWAP_BYTES" ] && \
    [ "$links" = 1 ] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] && \
    [ -z "$extra" ] || return 1
  [ "$require_mode" -eq 0 ] || [ "$mode" = 600 ]
}

inspect_sysctl_file() {
  sysctl_state=absent
  if [ ! -e "$sysctl_file" ] && [ ! -L "$sysctl_file" ]; then
    return 0
  fi
  [ -f "$sysctl_file" ] && [ ! -L "$sysctl_file" ] || return 1

  local metadata kind mode size links uid gid extra content=''
  metadata=$("$stat_bin" -c '%F|%a|%s|%h|%u|%g' "$sysctl_file") || return 1
  kind=''; mode=''; size=''; links=''; uid=''; gid=''; extra=''
  IFS='|' read -r kind mode size links uid gid extra <<< "$metadata" || return 1
  [ "$kind" = 'regular file' ] && [ "$mode" = 600 ] && is_uint "$size" && \
    [ "$links" = 1 ] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] && \
    [ -z "$extra" ] || return 1
  if IFS= read -r -d '' content < "$sysctl_file"; then
    :
  else
    [ "$?" -eq 1 ] || return 1
  fi
  [ "$content" = $'vm.swappiness=10\n' ] || return 1
  sysctl_state=managed
}

valid_swappiness() {
  is_uint "$1" && [ "$1" -le 200 ]
}

validate_swappiness_metadata_file() {
  local path=$1 metadata kind mode size links uid gid extra content
  local version_line original_line phase_line rest original phase
  metadata=$("$stat_bin" -c '%F|%a|%s|%h|%u|%g' "$path") || return 1
  kind=''; mode=''; size=''; links=''; uid=''; gid=''; extra=''
  IFS='|' read -r kind mode size links uid gid extra <<< "$metadata" || return 1
  [ "$kind" = 'regular file' ] && [ "$mode" = 600 ] && is_uint "$size" \
    && [ "$links" = 1 ] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] \
    && [ -z "$extra" ] || return 1
  read_text_file "$path" content || return 1
  case "$content" in *$'\n') ;; *) return 1 ;; esac
  rest=${content#*$'\n'}
  [ "$rest" != "$content" ] || return 1
  version_line=${content%%$'\n'*}
  original_line=${rest%%$'\n'*}
  rest=${rest#*$'\n'}
  [ "$rest" != "$original_line" ] || return 1
  phase_line=${rest%%$'\n'*}
  [ "$rest" = "$phase_line"$'\n' ] || return 1
  [ "$version_line" = version=1 ] || return 1
  case "$original_line" in original_swappiness=*) original=${original_line#*=} ;; *) return 1 ;; esac
  case "$phase_line" in phase=*) phase=${phase_line#*=} ;; *) return 1 ;; esac
  valid_swappiness "$original" || return 1
  case "$phase" in
    swapfile|mkswap|fstab|sysctl|swappiness|swapon|complete|\
    rollback-swappiness|rollback-swapoff|rollback-fstab|rollback-sysctl|\
    rollback-swapfile|rollback-metadata) ;;
    *) return 1 ;;
  esac
  [ "$content" = "version=1"$'\n'"original_swappiness=$original"$'\n'"phase=$phase"$'\n' ] \
    || return 1
  previous_swappiness=$original
  apply_phase=$phase
}

inspect_swappiness_metadata() {
  swappiness_metadata_state=absent
  previous_swappiness=''
  apply_phase=''
  if [ ! -e "$swappiness_metadata_file" ] && [ ! -L "$swappiness_metadata_file" ]; then
    return 0
  fi
  [ -f "$swappiness_metadata_file" ] && [ ! -L "$swappiness_metadata_file" ] || return 1
  validate_swappiness_metadata_file "$swappiness_metadata_file" || return 1
  swappiness_metadata_state=managed
}

read_live_swappiness() {
  local output rc
  if output=$("$sysctl_bin" -n vm.swappiness); then rc=0; else rc=$?; fi
  [ "$rc" -eq 0 ] || return 1
  case "$output" in *$'\n'*) return 1 ;; esac
  valid_swappiness "$output" || return 1
  printf '%s\n' "$output"
}

durable_sync_path() { # sync-file|sync-dir path
  local operation=$1 path=$2 expected output
  case "$operation" in
    sync-file)
      [ -f "$path" ] && [ ! -L "$path" ] || return 1
      expected=file-fsynced
      ;;
    sync-dir)
      [ -d "$path" ] && [ ! -L "$path" ] \
        && ! path_has_symlink_component "$path" || return 1
      expected=directory-fsynced
      ;;
    *) return 1 ;;
  esac
  output=$("$python_bin" - "$operation" "$path" "$root_uid" "$root_gid" <<'PY'
import os
import stat
import sys

operation, path, uid_text, gid_text = sys.argv[1:]
uid, gid = int(uid_text), int(gid_text)
if operation == "sync-file":
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    expected_kind = stat.S_ISREG
    token = "file-fsynced"
elif operation == "sync-dir":
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    expected_kind = stat.S_ISDIR
    token = "directory-fsynced"
else:
    raise RuntimeError("invalid sync operation")
fd = os.open(path, flags)
try:
    metadata = os.fstat(fd)
    if not expected_kind(metadata.st_mode) or metadata.st_uid != uid or metadata.st_gid != gid:
        raise RuntimeError("unsafe sync target")
    if operation == "sync-file" and metadata.st_nlink != 1:
        raise RuntimeError("unsafe sync target identity")
    os.fsync(fd)
finally:
    os.close(fd)
print(token)
PY
  ) || return 1
  [ "$output" = "$expected" ]
}

sync_regular_file() { durable_sync_path sync-file "$1"; }

sync_containing_directory() {
  local parent=${1%/*}
  [ -n "$parent" ] || parent=/
  durable_sync_path sync-dir "$parent"
}

new_transaction_temp_path() { # exact-prefix
  local prefix=$1 output suffix
  output=$("$python_bin" - temp-name "$prefix" "$$" <<'PY'
import secrets
import sys

operation, prefix, pid = sys.argv[1:]
if operation != "temp-name":
    raise RuntimeError("invalid temp-name operation")
if not pid.isdigit() or int(pid) <= 0:
    raise RuntimeError("invalid pid")
print(f"{prefix}{pid}.{secrets.token_hex(8)}")
PY
  ) || return 1
  case "$output" in "$prefix$$."*) ;; *) return 1 ;; esac
  suffix=${output#"$prefix$$."}
  [ "${#suffix}" -eq 16 ] || return 1
  case "$suffix" in *[!0-9a-f]*) return 1 ;; esac
  [ ! -e "$output" ] && [ ! -L "$output" ] || return 1
  generated_temp=$output
}

valid_transaction_temp_path() { # full-path
  local path=$1 prefix suffix pid token
  local fstab_parent=${fstab_file%/*} sysctl_parent=${sysctl_file%/*}
  case "$path" in
    "$fstab_parent/${fstab_file##*/}.legaltech-swap.bak.tmp."*)
      prefix="$fstab_parent/${fstab_file##*/}.legaltech-swap.bak.tmp." ;;
    "$fstab_parent/${fstab_file##*/}.legaltech-swap.tmp."*)
      prefix="$fstab_parent/${fstab_file##*/}.legaltech-swap.tmp." ;;
    "$sysctl_parent/${sysctl_file##*/}.tmp."*)
      prefix="$sysctl_parent/${sysctl_file##*/}.tmp." ;;
    *) return 1 ;;
  esac
  suffix=${path#"$prefix"}
  case "$suffix" in
    *.*) pid=${suffix%%.*}; token=${suffix#*.} ;;
    *) pid=$suffix; token='' ;;
  esac
  is_uint "$pid" && [ "$pid" -gt 0 ] || return 1
  [ "$suffix" = "$pid" ] && return 0
  [ "$suffix" = "$pid.$token" ] && [ "${#token}" -eq 16 ] || return 1
  case "$token" in *[!0-9a-f]*) return 1 ;; esac
}

inspect_transaction_temps() {
  local output path
  local fstab_parent=${fstab_file%/*} sysctl_parent=${sysctl_file%/*}
  transaction_temp_state=absent
  transaction_temps=()
  output=$("$python_bin" - inspect-temps "$root_uid" "$root_gid" \
    "$fstab_parent" "${fstab_file##*/}.legaltech-swap.bak.tmp." \
    "$fstab_parent" "${fstab_file##*/}.legaltech-swap.tmp." \
    "$sysctl_parent" "${sysctl_file##*/}.tmp." <<'PY'
import os
import re
import stat
import sys

operation = sys.argv[1]
if operation != "inspect-temps":
    raise RuntimeError("invalid temp inspection operation")
uid, gid = int(sys.argv[2]), int(sys.argv[3])
items = sys.argv[4:]
if len(items) % 2:
    raise RuntimeError("invalid temp namespace inventory")
candidates = []
for parent, prefix in zip(items[0::2], items[1::2]):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    dir_fd = os.open(parent, flags)
    try:
        directory = os.fstat(dir_fd)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != uid or directory.st_gid != gid:
            raise RuntimeError("unsafe temp parent")
        pattern = re.compile(re.escape(prefix) + r"[1-9][0-9]*(?:\.[0-9a-f]{16})?")
        for name in os.listdir(dir_fd):
            if not name.startswith(prefix):
                continue
            if pattern.fullmatch(name) is None:
                raise RuntimeError("unsafe transaction temp name")
            metadata = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise RuntimeError("unsafe transaction temp")
            if metadata.st_nlink != 1 or metadata.st_uid != uid or metadata.st_gid != gid:
                raise RuntimeError("unsafe transaction temp identity")
            candidates.append(os.path.join(parent, name))
    finally:
        os.close(dir_fd)
for candidate in sorted(candidates):
    print(candidate)
PY
  ) || return 1
  while IFS= read -r path || [ -n "$path" ]; do
    [ -n "$path" ] || continue
    valid_transaction_temp_path "$path" || return 1
    transaction_temps+=("$path")
  done <<< "$output"
  [ "${#transaction_temps[@]}" -eq 0 ] || transaction_temp_state=present
}

cleanup_transaction_temps() {
  local path
  inspect_transaction_temps || return 1
  for path in "${transaction_temps[@]}"; do
    "$rm_bin" "$path" || return 1
  done
  durable_sync_path sync-dir "${fstab_file%/*}" || return 1
  durable_sync_path sync-dir "${sysctl_file%/*}" || return 1
  inspect_transaction_temps || return 1
  [ "$transaction_temp_state" = absent ]
}

validate_managed_sysctl_path() { # path
  local path=$1 metadata kind mode size links uid gid extra content=''
  [ -f "$path" ] && [ ! -L "$path" ] || return 1
  metadata=$("$stat_bin" -c '%F|%a|%s|%h|%u|%g' "$path") || return 1
  kind=''; mode=''; size=''; links=''; uid=''; gid=''; extra=''
  IFS='|' read -r kind mode size links uid gid extra <<< "$metadata" || return 1
  [ "$kind" = 'regular file' ] && [ "$mode" = 600 ] && is_uint "$size" \
    && [ "$links" = 1 ] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] \
    && [ -z "$extra" ] || return 1
  if IFS= read -r -d '' content < "$path"; then :; else [ "$?" -eq 1 ] || return 1; fi
  [ "$content" = $'vm.swappiness=10\n' ]
}

create_managed_sysctl() {
  local temp=''
  new_transaction_temp_path "${sysctl_file}.tmp." || return 1
  temp=$generated_temp
  [ ! -e "$sysctl_file" ] && [ ! -L "$sysctl_file" ] \
    && [ ! -e "$temp" ] && [ ! -L "$temp" ] || return 1
  current_temp=$temp
  ( set -o noclobber; umask 077; printf '%s\n' 'vm.swappiness=10' > "$temp" ) \
    2>/dev/null || return 1
  "$chmod_bin" 0600 "$temp" || return 1
  validate_managed_sysctl_path "$temp" || return 1
  sync_regular_file "$temp" || return 1
  test_crash_after sysctl-temp
  "$mv_bin" "$temp" "$sysctl_file" || return 1
  current_temp=''
  test_crash_after sysctl-file
  sync_containing_directory "$sysctl_file" || return 1
  inspect_sysctl_file || return 1
  [ "$sysctl_state" = managed ]
}

durable_replace_phase_metadata() { # original-swappiness phase
  local value=$1 phase=$2 payload parent
  valid_swappiness "$value" || return 1
  case "$phase" in
    swapfile|mkswap|fstab|sysctl|swappiness|swapon|complete|\
    rollback-swappiness|rollback-swapoff|rollback-fstab|rollback-sysctl|\
    rollback-swapfile|rollback-metadata) ;;
    *) return 1 ;;
  esac
  parent=${swappiness_metadata_file%/*}
  [ -d "$parent" ] && [ ! -L "$parent" ] && ! path_has_symlink_component "$parent" || return 1
  payload="version=1"$'\n'"original_swappiness=$value"$'\n'"phase=$phase"$'\n'
  "$python_bin" - "$swappiness_metadata_file" "$root_uid" "$root_gid" "$payload" <<'PY'
import os
import re
import secrets
import stat
import sys

path, uid_text, gid_text, payload = sys.argv[1:]
uid, gid = int(uid_text), int(gid_text)
parent, name = os.path.split(path)
dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
dir_fd = os.open(parent, dir_flags)
temp_name = f".{name}.{os.getpid()}.{secrets.token_hex(8)}"
temp_fd = None
try:
    try:
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    if current is not None:
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise RuntimeError("unsafe destination")
        if stat.S_IMODE(current.st_mode) != 0o600 or current.st_uid != uid or current.st_gid != gid:
            raise RuntimeError("unsafe destination metadata")
    stale_prefix = f".{name}."
    stale_pattern = re.compile(re.escape(stale_prefix) + r"[1-9][0-9]*\.[0-9a-f]{16}")
    stale_candidates = []
    for candidate in os.listdir(dir_fd):
        if not candidate.startswith(stale_prefix):
            continue
        if stale_pattern.fullmatch(candidate) is None:
            raise RuntimeError("unsafe stale metadata temporary name")
        stale = os.stat(candidate, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISREG(stale.st_mode) or stat.S_IMODE(stale.st_mode) != 0o600:
            raise RuntimeError("unsafe stale metadata temporary")
        if stale.st_nlink != 1 or stale.st_uid != uid or stale.st_gid != gid:
            raise RuntimeError("unsafe stale metadata temporary identity")
        stale_candidates.append(candidate)
    for candidate in stale_candidates:
        os.unlink(candidate, dir_fd=dir_fd)
    if stale_candidates:
        os.fsync(dir_fd)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    temp_fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
    os.fchmod(temp_fd, 0o600)
    if os.fstat(temp_fd).st_uid != uid or os.fstat(temp_fd).st_gid != gid:
        os.fchown(temp_fd, uid, gid)
    data = payload.encode("ascii")
    written = 0
    while written < len(data):
        count = os.write(temp_fd, data[written:])
        if count <= 0:
            raise RuntimeError("short write")
        written += count
    os.fsync(temp_fd)
    os.close(temp_fd)
    temp_fd = None
    os.replace(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.fsync(dir_fd)
    final = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(final.st_mode) or stat.S_IMODE(final.st_mode) != 0o600:
        raise RuntimeError("invalid final metadata")
    if final.st_nlink != 1 or final.st_uid != uid or final.st_gid != gid:
        raise RuntimeError("invalid final identity")
finally:
    if temp_fd is not None:
        os.close(temp_fd)
    try:
        os.unlink(temp_name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    os.close(dir_fd)
PY
  inspect_swappiness_metadata || return 1
  [ "$swappiness_metadata_state" = managed ] \
    && [ "$previous_swappiness" = "$value" ] && [ "$apply_phase" = "$phase" ]
}

create_swappiness_metadata() {
  local value=$1
  [ ! -e "$swappiness_metadata_file" ] && [ ! -L "$swappiness_metadata_file" ] || return 1
  durable_replace_phase_metadata "$value" swapfile || return 1
}

restore_live_swappiness() {
  local output rc restored
  inspect_swappiness_metadata || return 1
  [ "$swappiness_metadata_state" = managed ] || return 1
  if output=$("$sysctl_bin" -w "vm.swappiness=$previous_swappiness"); then rc=0; else rc=$?; fi
  [ "$rc" -eq 0 ] || return 1
  restored=$(read_live_swappiness) || return 1
  [ "$restored" = "$previous_swappiness" ]
}

read_text_file() {
  local source_path="$1" destination_name="$2" value=''
  if IFS= read -r -d '' value < "$source_path"; then
    :
  else
    [ "$?" -eq 1 ] || return 1
  fi
  printf -v "$destination_name" '%s' "$value"
}

validate_backup_metadata() {
  local path="$1" metadata kind mode size links uid gid extra
  backup_mode=''
  metadata=$("$stat_bin" -c '%F|%a|%s|%h|%u|%g' "$path") || return 1
  kind=''; mode=''; size=''; links=''; uid=''; gid=''; extra=''
  IFS='|' read -r kind mode size links uid gid extra <<< "$metadata" || return 1
  [ "$kind" = 'regular file' ] && is_uint "$size" && \
    [ "$links" = 1 ] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] && \
    [ -z "$extra" ] || return 1
  case "$mode" in 600|640|644) ;; *) return 1 ;; esac
  backup_mode=$mode
}

text_files_equal() {
  local first_path="$1" second_path="$2" first_content='' second_content=''
  read_text_file "$first_path" first_content || return 1
  read_text_file "$second_path" second_content || return 1
  [ "$first_content" = "$second_content" ]
}

backup_matches_clean_fstab() {
  text_files_equal "$1" "$fstab_file"
}

backup_reconstructs_managed_fstab() {
  local backup_path="$1" backup_content='' current_content='' expected=''
  read_text_file "$backup_path" backup_content || return 1
  read_text_file "$fstab_file" current_content || return 1
  expected=$backup_content
  if [ -n "$expected" ] && [ "${expected: -1}" != $'\n' ]; then expected+=$'\n'; fi
  expected+="$BEGIN_MARKER"$'\n'
  expected+="$swap_file none swap sw 0 0"$'\n'
  expected+="$END_MARKER"$'\n'
  [ "$current_content" = "$expected" ]
}

inspect_fstab_backup() {
  local backup_path="${fstab_file}.legaltech-swap.bak"
  backup_state=absent
  if [ ! -e "$backup_path" ] && [ ! -L "$backup_path" ]; then
    [ "$fstab_state" = absent ] || [ "$fstab_state" = managed ]
    return
  fi
  [ -f "$backup_path" ] && [ ! -L "$backup_path" ] || return 1
  validate_backup_metadata "$backup_path" || return 1
  case "$fstab_state" in
    absent)
      [ "$backup_mode" = 600 ] || [ "$backup_mode" = "$fstab_mode" ] || return 1
      backup_matches_clean_fstab "$backup_path" || return 1
      backup_state=clean-copy
      ;;
    managed)
      [ "$backup_mode" = "$fstab_mode" ] || return 1
      backup_reconstructs_managed_fstab "$backup_path" || return 1
      backup_state=managed
      ;;
    *) return 1 ;;
  esac
}

inspect_managed_state() {
  inspect_fstab || return 1
  inspect_fstab_backup || return 1
  inspect_active_swaps || return 1
  inspect_swap_file || return 1
  inspect_sysctl_file || return 1
  inspect_swappiness_metadata || return 1

  if [ "$fstab_state" = absent ] && [ "$backup_state" = absent ] && \
     [ "$swap_file_exists" -eq 0 ] && \
     [ "$active_target_count" -eq 0 ] && [ "$sysctl_state" = absent ] \
     && [ "$swappiness_metadata_state" = absent ]; then
    printf '%s\n' clean
    return 0
  fi
  if [ "$fstab_state" = managed ] && [ "$backup_state" = managed ] && \
     [ "$swap_file_exists" -eq 1 ] && \
     [ "$active_target_count" -eq 1 ] && [ "$sysctl_state" = managed ] \
     && [ "$swappiness_metadata_state" = managed ] && [ "$apply_phase" = complete ]; then
    printf '%s\n' managed
    return 0
  fi
  return 1
}

inspect_rollback_state() {
  local live_swappiness tuple
  rollback_state=invalid
  inspect_fstab || return 1
  inspect_fstab_backup || return 1
  inspect_active_swaps || return 1
  inspect_swap_file || return 1
  inspect_sysctl_file || return 1
  inspect_swappiness_metadata || return 1

  if [ "$fstab_state" = absent ] && [ "$backup_state" = absent ] && \
     [ "$swap_file_exists" -eq 0 ] && [ "$active_target_count" -eq 0 ] && \
     [ "$sysctl_state" = absent ] && [ "$swappiness_metadata_state" = absent ]; then
    rollback_state=clean
    printf '%s\n' "$rollback_state"
    return 0
  fi
  [ "$swappiness_metadata_state" = managed ] || return 1
  live_swappiness=$(read_live_swappiness) || return 1
  tuple="$fstab_state:$backup_state:$swap_file_exists:$active_target_count:$sysctl_state:$live_swappiness"
  case "$apply_phase" in
    swapfile)
      case "$tuple" in
        absent:absent:0:0:absent:"$previous_swappiness"|\
        absent:absent:1:0:absent:"$previous_swappiness") rollback_state=apply-swapfile ;;
        *) return 1 ;;
      esac
      ;;
    mkswap)
      [ "$tuple" = "absent:absent:1:0:absent:$previous_swappiness" ] || return 1
      rollback_state=apply-mkswap
      ;;
    fstab)
      case "$tuple" in
        absent:absent:1:0:absent:"$previous_swappiness"|\
        absent:clean-copy:1:0:absent:"$previous_swappiness"|\
        managed:managed:1:0:absent:"$previous_swappiness"|\
        managed:managed:1:1:absent:"$previous_swappiness") rollback_state=apply-fstab ;;
        *) return 1 ;;
      esac
      ;;
    sysctl)
      case "$tuple" in
        managed:managed:1:0:absent:"$previous_swappiness"|\
        managed:managed:1:1:absent:"$previous_swappiness"|\
        managed:managed:1:0:managed:"$previous_swappiness"|\
        managed:managed:1:1:managed:10) rollback_state=apply-sysctl ;;
        *) return 1 ;;
      esac
      ;;
    swappiness)
      case "$tuple" in
        managed:managed:1:0:managed:"$previous_swappiness"|\
        managed:managed:1:0:managed:10|\
        managed:managed:1:1:managed:10) rollback_state=apply-swappiness ;;
        *) return 1 ;;
      esac
      ;;
    swapon)
      case "$tuple" in
        managed:managed:1:0:managed:10|managed:managed:1:1:managed:10) \
          rollback_state=apply-swapon ;;
        *) return 1 ;;
      esac
      ;;
    complete)
      [ "$tuple" = managed:managed:1:1:managed:10 ] || return 1
      rollback_state=managed-active
      ;;
    rollback-swappiness)
      case "$tuple" in
        managed:managed:1:1:managed:10|\
        managed:managed:1:1:managed:"$previous_swappiness"|\
        managed:managed:1:0:managed:10|\
        managed:managed:1:0:managed:"$previous_swappiness") rollback_state=rollback-swappiness ;;
        *) return 1 ;;
      esac
      ;;
    rollback-swapoff)
      case "$tuple" in
        managed:managed:1:1:absent:"$previous_swappiness"|\
        managed:managed:1:0:absent:"$previous_swappiness"|\
        managed:managed:1:1:managed:10|\
        managed:managed:1:1:managed:"$previous_swappiness"|\
        managed:managed:1:0:managed:"$previous_swappiness") rollback_state=rollback-swapoff ;;
        *) return 1 ;;
      esac
      ;;
    rollback-fstab)
      case "$tuple" in
        managed:managed:1:1:absent:"$previous_swappiness"|\
        managed:managed:1:0:absent:"$previous_swappiness"|\
        managed:managed:1:1:managed:10|\
        managed:managed:1:1:managed:"$previous_swappiness"|\
        managed:managed:1:0:managed:"$previous_swappiness"|\
        absent:clean-copy:1:0:absent:"$previous_swappiness"|\
        absent:absent:1:0:managed:10|\
        absent:absent:1:0:managed:"$previous_swappiness"|\
        absent:absent:1:0:absent:"$previous_swappiness") rollback_state=rollback-fstab ;;
        *) return 1 ;;
      esac
      ;;
    rollback-sysctl)
      case "$tuple" in
        absent:absent:1:0:managed:10|\
        absent:absent:1:0:managed:"$previous_swappiness"|\
        absent:absent:1:0:absent:"$previous_swappiness") rollback_state=rollback-sysctl ;;
        *) return 1 ;;
      esac
      ;;
    rollback-swapfile)
      case "$tuple" in
        absent:absent:1:0:absent:"$previous_swappiness"|\
        absent:absent:0:0:absent:"$previous_swappiness") rollback_state=rollback-swapfile ;;
        *) return 1 ;;
      esac
      ;;
    rollback-metadata)
      [ "$tuple" = "absent:absent:0:0:absent:$previous_swappiness" ] || return 1
      rollback_state=rollback-metadata
      ;;
    *) return 1 ;;
  esac
  printf '%s\n' "$rollback_state"
}

check_free_disk() {
  local output line available='' line_number=0
  output=$("$df_bin" --output=avail -B1 /) || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line_number=$((line_number + 1))
    if [ "$line_number" -eq 1 ]; then
      continue
    fi
    line=${line//[[:space:]]/}
    [ -n "$line" ] || continue
    [ -z "$available" ] && is_uint "$line" || return 1
    available=$line
  done <<< "$output"
  is_uint "$available" || return 1
  [ "$available" -ge "$MIN_FREE_BYTES" ]
}

preflight_state() {
  local state
  check_free_disk || return 1
  state=$(inspect_managed_state) || return 1
  case "$state" in
    clean) printf '%s\n' clean ;;
    managed)
      verify_managed || return 1
      printf '%s\n' managed
      ;;
    *) return 1 ;;
  esac
}

replace_fstab_with_managed_block() {
  local original='' backup_path backup_temp=''
  backup_path="${fstab_file}.legaltech-swap.bak"
  read_text_file "$fstab_file" original || return 1
  [ ! -e "$backup_path" ] && [ ! -L "$backup_path" ] || return 1
  new_transaction_temp_path "${backup_path}.tmp." || return 1
  backup_temp=$generated_temp

  current_temp=$backup_temp
  ( set -o noclobber; umask 077; : > "$backup_temp" ) 2>/dev/null || return 1
  "$cp_bin" "$fstab_file" "$backup_temp" || return 1
  validate_backup_metadata "$backup_temp" || return 1
  [ "$backup_mode" = 600 ] || return 1
  backup_matches_clean_fstab "$backup_temp" || return 1
  sync_regular_file "$backup_temp" || return 1
  test_crash_after fstab-backup-temp
  "$mv_bin" -n "$backup_temp" "$backup_path" || return 1
  [ ! -e "$backup_temp" ] && [ ! -L "$backup_temp" ] || return 1
  current_temp=''
  test_crash_after fstab-backup
  sync_containing_directory "$backup_path" || return 1
  validate_backup_metadata "$backup_path" || return 1
  backup_matches_clean_fstab "$backup_path" || return 1

  new_transaction_temp_path "${fstab_file}.legaltech-swap.tmp." || return 1
  current_temp=$generated_temp
  ( set -o noclobber; umask 077; : > "$current_temp" ) 2>/dev/null || return 1
  "$cp_bin" "$fstab_file" "$current_temp" || return 1
  validate_backup_metadata "$current_temp" || return 1
  [ "$backup_mode" = 600 ] || return 1
  backup_matches_clean_fstab "$current_temp" || return 1
  {
    if [ -n "$original" ] && [ "${original: -1}" != $'\n' ]; then printf '\n'; fi
    printf '%s\n%s none swap sw 0 0\n%s\n' \
      "$BEGIN_MARKER" "$swap_file" "$END_MARKER"
  } >> "$current_temp" || return 1

  sync_regular_file "$current_temp" || return 1
  test_crash_after fstab-managed-temp
  "$mv_bin" "$current_temp" "$fstab_file" || return 1
  current_temp=''
  test_crash_after fstab-managed
  sync_containing_directory "$fstab_file" || return 1
}

replace_fstab_without_managed_block() {
  local backup_path="${fstab_file}.legaltech-swap.bak"
  inspect_fstab || return 1
  [ "$fstab_state" = managed ] || return 1
  validate_backup_metadata "$backup_path" || return 1
  [ "$backup_mode" = "$fstab_mode" ] || return 1
  backup_reconstructs_managed_fstab "$backup_path" || return 1
  "$mv_bin" "$backup_path" "$fstab_file" || return 1
  [ ! -e "$backup_path" ] && [ ! -L "$backup_path" ] || return 1
  inspect_fstab || return 1
  [ "$fstab_state" = absent ]
}

cleanup_current_apply() {
  if [ -n "$current_temp" ] && { [ -e "$current_temp" ] || [ -L "$current_temp" ]; }; then
    if "$rm_bin" "$current_temp"; then
      sync_containing_directory "$current_temp" || return 1
      current_temp=''
    else
      return 1
    fi
  fi

  rollback_swap
}

abort_current_apply() {
  cleanup_current_apply || true
  return 1
}

verify_managed() {
  local state swappiness
  state=$(inspect_managed_state) || return 1
  [ "$state" = managed ] || return 1
  swappiness=$(read_live_swappiness) || return 1
  [ "$swappiness" = 10 ]
}

apply_swap() {
  local state original_swappiness
  check_free_disk || return 1
  state=$(inspect_managed_state) || return 1
  if [ "$state" = managed ]; then
    verify_managed
    return
  fi
  [ "$state" = clean ] || return 1

  original_swappiness=$(read_live_swappiness) || return 1
  umask 077
  create_swappiness_metadata "$original_swappiness" || { abort_current_apply; return 1; }
  test_crash_after metadata-only

  if ! "$fallocate_bin" -l "$SWAP_BYTES" "$swap_file"; then
    if ! "$dd_bin" if=/dev/zero "of=$swap_file" bs=1M count=4096 conv=fsync status=none; then
      abort_current_apply
      return 1
    fi
  fi
  test_crash_after swapfile-allocated
  inspect_swap_file 0 || { abort_current_apply; return 1; }
  [ "$swap_file_exists" -eq 1 ] || { abort_current_apply; return 1; }
  "$chmod_bin" 0600 "$swap_file" || { abort_current_apply; return 1; }
  inspect_swap_file || { abort_current_apply; return 1; }
  sync_regular_file "$swap_file" || { abort_current_apply; return 1; }
  sync_containing_directory "$swap_file" || { abort_current_apply; return 1; }
  durable_replace_phase_metadata "$original_swappiness" mkswap \
    || { abort_current_apply; return 1; }
  test_crash_after mkswap-phase
  "$mkswap_bin" "$swap_file" || { abort_current_apply; return 1; }
  test_crash_after mkswap
  sync_regular_file "$swap_file" || { abort_current_apply; return 1; }
  durable_replace_phase_metadata "$original_swappiness" fstab \
    || { abort_current_apply; return 1; }
  test_crash_after fstab-phase
  replace_fstab_with_managed_block || { abort_current_apply; return 1; }
  durable_replace_phase_metadata "$original_swappiness" sysctl \
    || { abort_current_apply; return 1; }
  test_crash_after sysctl-phase
  create_managed_sysctl || { abort_current_apply; return 1; }
  durable_replace_phase_metadata "$original_swappiness" swappiness \
    || { abort_current_apply; return 1; }
  test_crash_after swappiness-phase
  "$sysctl_bin" -p "$sysctl_file" >/dev/null || { abort_current_apply; return 1; }
  [ "$(read_live_swappiness)" = 10 ] || { abort_current_apply; return 1; }
  test_crash_after live-swappiness
  durable_replace_phase_metadata "$original_swappiness" swapon \
    || { abort_current_apply; return 1; }
  test_crash_after swapon-phase
  if ! "$swapon_bin" "$swap_file"; then
    if inspect_active_swaps; then
      [ "$active_target_count" -le 1 ] || return 1
    fi
    abort_current_apply
    return 1
  fi
  test_crash_after swapon
  inspect_rollback_state >/dev/null || { abort_current_apply; return 1; }
  [ "$rollback_state" = apply-swapon ] || { abort_current_apply; return 1; }
  durable_replace_phase_metadata "$original_swappiness" complete \
    || { abort_current_apply; return 1; }
  test_crash_after complete
  verify_managed || { abort_current_apply; return 1; }
}

read_memory_safety() {
  local output rc line label _a _b _c _d _e f extra available=''
  if output=$("$free_bin" -b); then rc=0; else rc=$?; fi
  [ "$rc" -eq 0 ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    label=''; _a=''; _b=''; _c=''; _d=''; _e=''; f=''; extra=''
    read -r label _a _b _c _d _e f extra <<< "$line" || true
    case "$label" in
      Mem:)
        [ -z "$available" ] && is_uint "$f" && [ -z "$extra" ] || return 1
        available=$f
        ;;
    esac
  done <<< "$output"
  is_uint "$available" || return 1
  inspect_active_swaps || return 1
  [ "$active_target_count" -eq 1 ] || return 1
  [ "$active_target_used_bytes" -le $((9223372036854775807 - RAM_MARGIN_BYTES)) ] || return 1
  [ "$available" -gt $((active_target_used_bytes + RAM_MARGIN_BYTES)) ]
}

rollback_swap() {
  local state live restore_phase backup_path="${fstab_file}.legaltech-swap.bak"
  inspect_rollback_state >/dev/null || return 1
  state=$rollback_state
  if [ "$state" = clean ]; then
    sync_containing_directory "$swappiness_metadata_file" || return 1
    return 0
  fi

  case "$apply_phase" in
    rollback-fstab)
      if [ "$fstab_state" = absent ] && [ "$backup_state" = absent ]; then
        sync_containing_directory "$fstab_file" || return 1
      fi
      ;;
    rollback-sysctl)
      [ "$sysctl_state" != absent ] || sync_containing_directory "$sysctl_file" || return 1
      ;;
    rollback-swapfile)
      [ "$swap_file_exists" -ne 0 ] || sync_containing_directory "$swap_file" || return 1
      ;;
  esac

  if [ "$active_target_count" -eq 1 ]; then
    read_memory_safety || return 1
  fi
  live=$(read_live_swappiness) || return 1
  if [ "$live" != "$previous_swappiness" ]; then
    restore_phase=rollback-swappiness
    case "$apply_phase" in rollback-*) restore_phase=$apply_phase ;; esac
    durable_replace_phase_metadata "$previous_swappiness" "$restore_phase" || return 1
    test_crash_after rollback-swappiness-phase
    restore_live_swappiness || return 1
    test_crash_after rollback-live-swappiness
    inspect_rollback_state >/dev/null || return 1
  fi
  if [ "$active_target_count" -eq 1 ]; then
    durable_replace_phase_metadata "$previous_swappiness" rollback-swapoff || return 1
    test_crash_after rollback-swapoff-phase
    "$swapoff_bin" "$swap_file" || return 1
    test_crash_after rollback-swapoff
    inspect_rollback_state >/dev/null || return 1
    [ "$active_target_count" -eq 0 ] || return 1
  fi

  if [ "$fstab_state" = managed ] || [ "$backup_state" = clean-copy ]; then
    durable_replace_phase_metadata "$previous_swappiness" rollback-fstab || return 1
    test_crash_after rollback-fstab-phase
    if [ "$fstab_state" = managed ]; then
      replace_fstab_without_managed_block || return 1
    else
      validate_backup_metadata "$backup_path" || return 1
      backup_matches_clean_fstab "$backup_path" || return 1
      "$rm_bin" "$backup_path" || return 1
    fi
    test_crash_after rollback-fstab
    sync_containing_directory "$fstab_file" || return 1
    inspect_rollback_state >/dev/null || return 1
  fi
  if [ "$sysctl_state" = managed ]; then
    durable_replace_phase_metadata "$previous_swappiness" rollback-sysctl || return 1
    test_crash_after rollback-sysctl-phase
    "$rm_bin" "$sysctl_file" || return 1
    test_crash_after rollback-sysctl
    sync_containing_directory "$sysctl_file" || return 1
    inspect_rollback_state >/dev/null || return 1
  fi
  if [ "$swap_file_exists" -eq 1 ]; then
    durable_replace_phase_metadata "$previous_swappiness" rollback-swapfile || return 1
    test_crash_after rollback-swapfile-phase
    "$rm_bin" "$swap_file" || return 1
    test_crash_after rollback-swapfile
    sync_containing_directory "$swap_file" || return 1
    inspect_rollback_state >/dev/null || return 1
  fi
  [ "$swappiness_metadata_state" = managed ] || return 1
  durable_replace_phase_metadata "$previous_swappiness" rollback-metadata || return 1
  test_crash_after rollback-metadata-phase
  "$rm_bin" "$swappiness_metadata_file" || return 1
  test_crash_after rollback-metadata
  sync_containing_directory "$swappiness_metadata_file" || return 1
  inspect_rollback_state >/dev/null || return 1
  [ "$rollback_state" = clean ]
}

rollback_preflight_state() {
  local state
  state=$(inspect_rollback_state) || return 1
  if [ "$state" = clean ] && [ "$transaction_temp_state" != absent ]; then
    return 1
  fi
  printf '%s\n' "$state"
}

case "$command_name" in
  apply|rollback) acquire_mutation_lock || fail ;;
esac

case "$command_name" in
  apply|rollback) cleanup_transaction_temps || fail ;;
  preflight|verify)
    inspect_transaction_temps || fail
    [ "$transaction_temp_state" = absent ] || fail
    ;;
  rollback-preflight) inspect_transaction_temps || fail ;;
esac

case "$command_name" in
  preflight) preflight_state || fail; exit 0 ;;
  rollback-preflight) rollback_preflight_state || fail; exit 0 ;;
  apply) apply_swap || fail ;;
  verify) verify_managed || fail ;;
  rollback) rollback_swap || fail ;;
esac

printf '%s\n' 'OK: swap operation completed'
