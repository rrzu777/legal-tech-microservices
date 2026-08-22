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
  SWAP_SWAPPINESS_METADATA_FILE SWAP_TEST_ROOT_UID SWAP_TEST_ROOT_GID \
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
    SWAP_SWAPPINESS_METADATA_FILE SWAP_TEST_ROOT_UID SWAP_TEST_ROOT_GID \
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

for absolute_value in \
  "$swap_file" "$fstab_file" "$sysctl_file" "$swappiness_metadata_file" "$proc_swaps_file" \
  "$df_bin" "$fallocate_bin" "$dd_bin" "$chmod_bin" "$mkswap_bin" \
  "$swapon_bin" "$swapoff_bin" "$sysctl_bin" "$free_bin" "$cp_bin" \
  "$mv_bin" "$stat_bin" "$rm_bin"
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
rollback_state=invalid
current_temp=''
apply_created_swap_file=0
apply_activated_swap=0
apply_activation_unknown=0
apply_created_backup=0
apply_promoted_fstab=0
apply_created_sysctl=0
apply_created_swappiness_metadata=0
apply_swappiness_may_have_changed=0

cleanup_temp() {
  if [ -n "$current_temp" ] && { [ -e "$current_temp" ] || [ -L "$current_temp" ]; }; then
    set +e
    "$rm_bin" "$current_temp" >/dev/null 2>&1
  fi
}
trap cleanup_temp EXIT

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
  local path=$1 metadata kind mode size links uid gid extra content raw
  metadata=$("$stat_bin" -c '%F|%a|%s|%h|%u|%g' "$path") || return 1
  kind=''; mode=''; size=''; links=''; uid=''; gid=''; extra=''
  IFS='|' read -r kind mode size links uid gid extra <<< "$metadata" || return 1
  [ "$kind" = 'regular file' ] && [ "$mode" = 600 ] && is_uint "$size" \
    && [ "$links" = 1 ] && [ "$uid" = "$root_uid" ] && [ "$gid" = "$root_gid" ] \
    && [ -z "$extra" ] || return 1
  read_text_file "$path" content || return 1
  case "$content" in *$'\n') raw=${content%$'\n'} ;; *) return 1 ;; esac
  [ "$content" = "$raw"$'\n' ] && valid_swappiness "$raw" || return 1
  previous_swappiness=$raw
}

inspect_swappiness_metadata() {
  swappiness_metadata_state=absent
  previous_swappiness=''
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

create_swappiness_metadata() {
  local value=$1
  valid_swappiness "$value" || return 1
  [ ! -e "$swappiness_metadata_file" ] && [ ! -L "$swappiness_metadata_file" ] || return 1
  current_temp="${swappiness_metadata_file}.tmp.$$"
  [ ! -e "$current_temp" ] && [ ! -L "$current_temp" ] || return 1
  umask 077
  printf '%s\n' "$value" > "$current_temp" || return 1
  "$chmod_bin" 0600 "$current_temp" || return 1
  validate_swappiness_metadata_file "$current_temp" || return 1
  "$mv_bin" -n "$current_temp" "$swappiness_metadata_file" || return 1
  [ ! -e "$current_temp" ] && [ ! -L "$current_temp" ] || return 1
  current_temp=''
  apply_created_swappiness_metadata=1
  inspect_swappiness_metadata || return 1
  [ "$swappiness_metadata_state" = managed ] && [ "$previous_swappiness" = "$value" ]
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
    [ "$fstab_state" = absent ]
    return
  fi
  [ "$fstab_state" = managed ] || return 1
  [ -f "$backup_path" ] && [ ! -L "$backup_path" ] || return 1
  validate_backup_metadata "$backup_path" || return 1
  [ "$backup_mode" = "$fstab_mode" ] || return 1
  backup_reconstructs_managed_fstab "$backup_path" || return 1
  backup_state=managed
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
     && [ "$swappiness_metadata_state" = managed ]; then
    printf '%s\n' managed
    return 0
  fi
  return 1
}

inspect_rollback_state() {
  local live_swappiness
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

  if [ "$fstab_state" = managed ] && [ "$backup_state" = managed ] && \
     [ "$swap_file_exists" -eq 1 ] && [ "$sysctl_state" = managed ] && \
     [ "$swappiness_metadata_state" = managed ]; then
    if [ "$active_target_count" -eq 1 ]; then
      rollback_state=managed-active
      printf '%s\n' "$rollback_state"
      return 0
    fi
    if [ "$active_target_count" -eq 0 ]; then
      live_swappiness=$(read_live_swappiness) || return 1
      [ "$live_swappiness" = "$previous_swappiness" ] || return 1
      rollback_state=managed-deactivated
      printf '%s\n' "$rollback_state"
      return 0
    fi
    return 1
  fi

  if [ "$fstab_state" = absent ] && [ "$backup_state" = absent ] && \
     [ "$active_target_count" -eq 0 ] && \
     [ "$swappiness_metadata_state" = managed ]; then
    case "$sysctl_state:$swap_file_exists" in
      managed:1|absent:1|absent:0) ;;
      *) return 1 ;;
    esac
    live_swappiness=$(read_live_swappiness) || return 1
    [ "$live_swappiness" = "$previous_swappiness" ] || return 1
    rollback_state=fstab-restored
    printf '%s\n' "$rollback_state"
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
  local original='' backup_path backup_temp
  backup_path="${fstab_file}.legaltech-swap.bak"
  backup_temp="${backup_path}.tmp.$$"
  read_text_file "$fstab_file" original || return 1
  [ ! -e "$backup_path" ] && [ ! -L "$backup_path" ] || return 1
  [ ! -e "$backup_temp" ] && [ ! -L "$backup_temp" ] || return 1

  current_temp=$backup_temp
  "$cp_bin" -p -n "$fstab_file" "$backup_temp" || return 1
  validate_backup_metadata "$backup_temp" || return 1
  backup_matches_clean_fstab "$backup_temp" || return 1
  "$mv_bin" -n "$backup_temp" "$backup_path" || return 1
  [ ! -e "$backup_temp" ] && [ ! -L "$backup_temp" ] || return 1
  current_temp=''
  apply_created_backup=1
  validate_backup_metadata "$backup_path" || return 1
  backup_matches_clean_fstab "$backup_path" || return 1

  current_temp="${fstab_file}.legaltech-swap.tmp.$$"
  [ ! -e "$current_temp" ] && [ ! -L "$current_temp" ] || return 1
  "$cp_bin" -p -n "$fstab_file" "$current_temp" || return 1
  validate_backup_metadata "$current_temp" || return 1
  backup_matches_clean_fstab "$current_temp" || return 1
  {
    if [ -n "$original" ] && [ "${original: -1}" != $'\n' ]; then printf '\n'; fi
    printf '%s\n%s none swap sw 0 0\n%s\n' \
      "$BEGIN_MARKER" "$swap_file" "$END_MARKER"
  } >> "$current_temp" || return 1

  "$mv_bin" "$current_temp" "$fstab_file" || return 1
  current_temp=''
  apply_promoted_fstab=1
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
  local backup_path="${fstab_file}.legaltech-swap.bak"
  local cleanup_failed=0 configuration_safe=1 swapoff_required=0

  if [ "$apply_activation_unknown" -eq 1 ]; then
    return 1
  elif [ "$apply_activated_swap" -eq 1 ]; then
    read_memory_safety || return 1
    swapoff_required=1
  fi

  if [ "$apply_swappiness_may_have_changed" -eq 1 ]; then
    if restore_live_swappiness; then
      apply_swappiness_may_have_changed=0
    else
      return 1
    fi
  fi

  if [ "$swapoff_required" -eq 1 ]; then
    if "$swapoff_bin" "$swap_file" && inspect_active_swaps && \
       [ "$active_target_count" -eq 0 ]; then
      apply_activated_swap=0
    else
      return 1
    fi
  fi

  if [ -n "$current_temp" ] && { [ -e "$current_temp" ] || [ -L "$current_temp" ]; }; then
    if "$rm_bin" "$current_temp"; then
      current_temp=''
    else
      cleanup_failed=1
    fi
  fi

  if [ "$apply_promoted_fstab" -eq 1 ]; then
    if replace_fstab_without_managed_block; then
      apply_promoted_fstab=0
      apply_created_backup=0
    else
      cleanup_failed=1
      configuration_safe=0
    fi
  elif [ "$apply_created_backup" -eq 1 ]; then
    if validate_backup_metadata "$backup_path" && \
       backup_matches_clean_fstab "$backup_path" && \
       "$rm_bin" "$backup_path"; then
      apply_created_backup=0
    else
      cleanup_failed=1
      configuration_safe=0
    fi
  fi

  if [ "$apply_created_sysctl" -eq 1 ]; then
    if "$rm_bin" "$sysctl_file"; then
      apply_created_sysctl=0
    else
      cleanup_failed=1
    fi
  fi

  if [ "$apply_created_swap_file" -eq 1 ] && \
     [ "$apply_activated_swap" -eq 0 ] && [ "$apply_activation_unknown" -eq 0 ] && \
     [ "$configuration_safe" -eq 1 ]; then
    if inspect_swap_file 0; then
      if [ "$swap_file_exists" -eq 0 ] || "$rm_bin" "$swap_file"; then
        apply_created_swap_file=0
      else
        cleanup_failed=1
      fi
    else
      cleanup_failed=1
    fi
  fi


  if [ "$apply_created_swappiness_metadata" -eq 1 ] && [ "$cleanup_failed" -eq 0 ] \
    && [ "$configuration_safe" -eq 1 ]; then
    if validate_swappiness_metadata_file "$swappiness_metadata_file" \
      && "$rm_bin" "$swappiness_metadata_file"; then
      apply_created_swappiness_metadata=0
    else
      cleanup_failed=1
    fi
  fi

  [ "$cleanup_failed" -eq 0 ]
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
  apply_created_swap_file=0
  apply_activated_swap=0
  apply_activation_unknown=0
  apply_created_backup=0
  apply_promoted_fstab=0
  apply_created_sysctl=0
  apply_created_swappiness_metadata=0
  apply_swappiness_may_have_changed=0
  check_free_disk || return 1
  state=$(inspect_managed_state) || return 1
  if [ "$state" = managed ]; then
    verify_managed
    return
  fi
  [ "$state" = clean ] || return 1

  original_swappiness=$(read_live_swappiness) || return 1
  create_swappiness_metadata "$original_swappiness" || { abort_current_apply; return 1; }

  apply_created_swap_file=1
  if ! "$fallocate_bin" -l "$SWAP_BYTES" "$swap_file"; then
    if ! "$dd_bin" if=/dev/zero "of=$swap_file" bs=1M count=4096 conv=fsync status=none; then
      abort_current_apply
      return 1
    fi
  fi
  inspect_swap_file 0 || { abort_current_apply; return 1; }
  [ "$swap_file_exists" -eq 1 ] || { abort_current_apply; return 1; }
  "$chmod_bin" 0600 "$swap_file" || { abort_current_apply; return 1; }
  inspect_swap_file || { abort_current_apply; return 1; }
  "$mkswap_bin" "$swap_file" || { abort_current_apply; return 1; }
  replace_fstab_with_managed_block || { abort_current_apply; return 1; }
  apply_created_sysctl=1
  printf '%s\n' 'vm.swappiness=10' > "$sysctl_file" || { abort_current_apply; return 1; }
  apply_swappiness_may_have_changed=1
  "$sysctl_bin" -p "$sysctl_file" >/dev/null || { abort_current_apply; return 1; }
  if "$swapon_bin" "$swap_file"; then
    apply_activated_swap=1
  else
    if inspect_active_swaps; then
      if [ "$active_target_count" -eq 1 ]; then
        apply_activated_swap=1
      fi
    else
      apply_activation_unknown=1
    fi
    abort_current_apply
    return 1
  fi
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
  local state
  inspect_rollback_state >/dev/null || return 1
  state=$rollback_state
  case "$state" in
    clean) return 0 ;;
    managed-active)
      read_memory_safety || return 1
      restore_live_swappiness || return 1
      "$swapoff_bin" "$swap_file" || return 1
      inspect_rollback_state >/dev/null || return 1
      [ "$rollback_state" = managed-deactivated ] || return 1
      ;;
    managed-deactivated|fstab-restored) ;;
    *) return 1 ;;
  esac

  if [ "$rollback_state" = managed-deactivated ]; then
    replace_fstab_without_managed_block || return 1
    inspect_rollback_state >/dev/null || return 1
    [ "$rollback_state" = fstab-restored ] || return 1
  fi

  [ "$rollback_state" = fstab-restored ] || return 1
  if [ "$sysctl_state" = managed ]; then
    "$rm_bin" "$sysctl_file" || return 1
    inspect_rollback_state >/dev/null || return 1
    [ "$rollback_state" = fstab-restored ] || return 1
  fi
  if [ "$swap_file_exists" -eq 1 ]; then
    "$rm_bin" "$swap_file" || return 1
    inspect_rollback_state >/dev/null || return 1
    [ "$rollback_state" = fstab-restored ] || return 1
  fi
  [ "$swappiness_metadata_state" = managed ] || return 1
  "$rm_bin" "$swappiness_metadata_file" || return 1
  inspect_rollback_state >/dev/null || return 1
  [ "$rollback_state" = clean ]
}

case "$command_name" in
  preflight) preflight_state || fail; exit 0 ;;
  apply) apply_swap || fail ;;
  verify) verify_managed || fail ;;
  rollback) rollback_swap || fail ;;
esac

printf '%s\n' 'OK: swap operation completed'
