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
  printf '%s\n' 'usage: configure-swap.sh {preflight|apply|verify|rollback}' >&2
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
  preflight|apply|verify|rollback) ;;
  *) usage ;;
esac

test_mode=${SWAP_TEST_MODE:-0}
override_present=0
for variable_name in \
  SWAP_FILE SWAP_FSTAB_FILE SWAP_SYSCTL_FILE SWAP_PROC_SWAPS_FILE \
  SWAP_DF_BIN SWAP_FALLOCATE_BIN SWAP_DD_BIN SWAP_CHMOD_BIN \
  SWAP_MKSWAP_BIN SWAP_SWAPON_BIN SWAP_SWAPOFF_BIN SWAP_SYSCTL_BIN \
  SWAP_FREE_BIN SWAP_CP_BIN SWAP_MV_BIN SWAP_STAT_BIN SWAP_RM_BIN
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
    SWAP_DF_BIN SWAP_FALLOCATE_BIN SWAP_DD_BIN SWAP_CHMOD_BIN \
    SWAP_MKSWAP_BIN SWAP_SWAPON_BIN SWAP_SWAPOFF_BIN SWAP_SYSCTL_BIN \
    SWAP_FREE_BIN SWAP_CP_BIN SWAP_MV_BIN SWAP_STAT_BIN SWAP_RM_BIN
  do
    [ "${!variable_name+x}" = x ] || usage
  done
fi

swap_file=${SWAP_FILE:-/swapfile}
fstab_file=${SWAP_FSTAB_FILE:-/etc/fstab}
sysctl_file=${SWAP_SYSCTL_FILE:-/etc/sysctl.d/60-legaltech-swap.conf}
proc_swaps_file=${SWAP_PROC_SWAPS_FILE:-/proc/swaps}

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

for absolute_value in \
  "$swap_file" "$fstab_file" "$sysctl_file" "$proc_swaps_file" \
  "$df_bin" "$fallocate_bin" "$dd_bin" "$chmod_bin" "$mkswap_bin" \
  "$swapon_bin" "$swapoff_bin" "$sysctl_bin" "$free_bin" "$cp_bin" \
  "$mv_bin" "$stat_bin" "$rm_bin"
do
  case "$absolute_value" in
    /*) ;;
    *) usage ;;
  esac
done

fstab_state=invalid
active_target_count=0
swap_file_exists=0
sysctl_state=absent
current_temp=''

cleanup_temp() {
  if [ -n "$current_temp" ] && { [ -e "$current_temp" ] || [ -L "$current_temp" ]; }; then
    set +e
    "$rm_bin" "$current_temp" >/dev/null 2>&1
  fi
}
trap cleanup_temp EXIT

inspect_fstab() {
  [ -f "$fstab_file" ] && [ ! -L "$fstab_file" ] || return 1

  local line first second third rest
  local inside=0 begins=0 ends=0 managed_entries=0 unmanaged_entries=0
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "$BEGIN_MARKER" ]; then
      [ "$inside" -eq 0 ] || return 1
      inside=1
      begins=$((begins + 1))
      continue
    fi
    if [ "$line" = "$END_MARKER" ]; then
      [ "$inside" -eq 1 ] || return 1
      inside=0
      ends=$((ends + 1))
      continue
    fi

    first=''; second=''; third=''; rest=''
    read -r first second third rest <<< "$line" || true
    if [ "$inside" -eq 1 ]; then
      if [ "$first" = "$swap_file" ] && [ "$second" = none ] && \
         [ "$third" = swap ] && [ "$rest" = 'sw 0 0' ]; then
        managed_entries=$((managed_entries + 1))
      else
        return 1
      fi
    elif [ "$first" = "$swap_file" ] && [ "${first#\#}" = "$first" ]; then
      unmanaged_entries=$((unmanaged_entries + 1))
    fi
  done < "$fstab_file" || return 1

  [ "$inside" -eq 0 ] || return 1
  [ "$unmanaged_entries" -eq 0 ] || return 1
  if [ "$begins" -eq 0 ] && [ "$ends" -eq 0 ] && [ "$managed_entries" -eq 0 ]; then
    fstab_state=absent
    return 0
  fi
  if [ "$begins" -eq 1 ] && [ "$ends" -eq 1 ] && [ "$managed_entries" -eq 1 ]; then
    fstab_state=managed
    return 0
  fi
  return 1
}

inspect_active_swaps() {
  [ -f "$proc_swaps_file" ] && [ ! -L "$proc_swaps_file" ] || return 1

  local line device type size used priority extra line_number=0
  active_target_count=0
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
    active_target_count=$((active_target_count + 1))
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

  local metadata kind mode size extra
  metadata=$("$stat_bin" -c '%F|%a|%s' "$swap_file") || return 1
  kind=''; mode=''; size=''; extra=''
  IFS='|' read -r kind mode size extra <<< "$metadata" || return 1
  [ "$kind" = 'regular file' ] && [ "$size" = "$SWAP_BYTES" ] && \
    [ -z "$extra" ] || return 1
  [ "$require_mode" -eq 0 ] || [ "$mode" = 600 ]
}

inspect_sysctl_file() {
  sysctl_state=absent
  if [ ! -e "$sysctl_file" ] && [ ! -L "$sysctl_file" ]; then
    return 0
  fi
  [ -f "$sysctl_file" ] && [ ! -L "$sysctl_file" ] || return 1

  local content=''
  if IFS= read -r -d '' content < "$sysctl_file"; then
    :
  else
    [ "$?" -eq 1 ] || return 1
  fi
  [ "$content" = $'vm.swappiness=10\n' ] || return 1
  sysctl_state=managed
}

inspect_fstab_backup() {
  local backup_path="${fstab_file}.legaltech-swap.bak"
  if [ ! -e "$backup_path" ] && [ ! -L "$backup_path" ]; then
    return 0
  fi
  [ -f "$backup_path" ] && [ ! -L "$backup_path" ]
}

inspect_managed_state() {
  inspect_fstab || return 1
  inspect_fstab_backup || return 1
  inspect_active_swaps || return 1
  inspect_swap_file || return 1
  inspect_sysctl_file || return 1

  if [ "$fstab_state" = absent ] && [ "$swap_file_exists" -eq 0 ] && \
     [ "$active_target_count" -eq 0 ] && [ "$sysctl_state" = absent ]; then
    printf '%s\n' clean
    return 0
  fi
  if [ "$fstab_state" = managed ] && [ "$swap_file_exists" -eq 1 ] && \
     [ "$active_target_count" -eq 1 ] && [ "$sysctl_state" = managed ]; then
    printf '%s\n' managed
    return 0
  fi
  return 1
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
  [ "$state" = clean ] || [ "$state" = managed ]
}

replace_fstab_with_managed_block() {
  local original=''
  if IFS= read -r -d '' original < "$fstab_file"; then
    :
  else
    [ "$?" -eq 1 ] || return 1
  fi
  current_temp="${fstab_file}.legaltech-swap.tmp.$$"
  [ ! -e "$current_temp" ] && [ ! -L "$current_temp" ] || return 1

  "$cp_bin" -p "$fstab_file" "${fstab_file}.legaltech-swap.bak" || return 1
  "$cp_bin" -p "$fstab_file" "$current_temp" || return 1
  {
    if [ -n "$original" ] && [ "${original: -1}" != $'\n' ]; then printf '\n'; fi
    printf '%s\n%s none swap sw 0 0\n%s\n' \
      "$BEGIN_MARKER" "$swap_file" "$END_MARKER"
  } >> "$current_temp" || return 1

  "$mv_bin" "$current_temp" "$fstab_file" || return 1
  current_temp=''
}

replace_fstab_without_managed_block() {
  local line inside=0
  current_temp="${fstab_file}.legaltech-swap.tmp.$$"
  [ ! -e "$current_temp" ] && [ ! -L "$current_temp" ] || return 1
  : > "$current_temp" || return 1

  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "$BEGIN_MARKER" ]; then
      [ "$inside" -eq 0 ] || return 1
      inside=1
      continue
    fi
    if [ "$line" = "$END_MARKER" ]; then
      [ "$inside" -eq 1 ] || return 1
      inside=0
      continue
    fi
    if [ "$inside" -eq 0 ]; then
      printf '%s\n' "$line" >> "$current_temp" || return 1
    fi
  done < "$fstab_file" || return 1
  [ "$inside" -eq 0 ] || return 1

  "$cp_bin" -p "$fstab_file" "${fstab_file}.legaltech-swap.bak" || return 1
  "$mv_bin" "$current_temp" "$fstab_file" || return 1
  current_temp=''
}

verify_managed() {
  local state swappiness
  state=$(inspect_managed_state) || return 1
  [ "$state" = managed ] || return 1
  swappiness=$("$sysctl_bin" -n vm.swappiness) || return 1
  [ "$swappiness" = 10 ]
}

apply_swap() {
  local state
  check_free_disk || return 1
  state=$(inspect_managed_state) || return 1
  if [ "$state" = managed ]; then
    verify_managed
    return
  fi
  [ "$state" = clean ] || return 1

  if ! "$fallocate_bin" -l "$SWAP_BYTES" "$swap_file"; then
    "$dd_bin" if=/dev/zero "of=$swap_file" bs=1M count=4096 conv=fsync status=none || return 1
  fi
  inspect_swap_file 0 || return 1
  [ "$swap_file_exists" -eq 1 ] || return 1
  "$chmod_bin" 0600 "$swap_file" || return 1
  inspect_swap_file || return 1
  "$mkswap_bin" "$swap_file" || return 1
  "$swapon_bin" "$swap_file" || return 1
  replace_fstab_with_managed_block || return 1
  printf '%s\n' 'vm.swappiness=10' > "$sysctl_file" || return 1
  "$sysctl_bin" -p "$sysctl_file" >/dev/null || return 1
  verify_managed
}

read_memory_safety() {
  local output line label _a b _c _d _e f extra available='' used=''
  output=$("$free_bin" -b) || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    label=''; _a=''; b=''; _c=''; _d=''; _e=''; f=''; extra=''
    read -r label _a b _c _d _e f extra <<< "$line" || true
    case "$label" in
      Mem:)
        [ -z "$available" ] && is_uint "$f" && [ -z "$extra" ] || return 1
        available=$f
        ;;
      Swap:)
        [ -z "$used" ] && is_uint "$b" && [ -z "$extra" ] || return 1
        used=$b
        ;;
    esac
  done <<< "$output"
  is_uint "$available" && is_uint "$used" || return 1
  [ "$available" -gt $((used + RAM_MARGIN_BYTES)) ]
}

rollback_swap() {
  local state
  state=$(inspect_managed_state) || return 1
  if [ "$state" = clean ]; then
    return 0
  fi
  [ "$state" = managed ] || return 1
  read_memory_safety || return 1

  "$swapoff_bin" "$swap_file" || return 1
  replace_fstab_without_managed_block || return 1
  "$rm_bin" "$sysctl_file" || return 1
  "$rm_bin" "$swap_file" || return 1
}

case "$command_name" in
  preflight) preflight_state || fail ;;
  apply) apply_swap || fail ;;
  verify) verify_managed || fail ;;
  rollback) rollback_swap || fail ;;
esac

printf '%s\n' 'OK: swap operation completed'
