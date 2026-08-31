#!/usr/bin/env bash
# Shared transaction primitives. Source only; no bootstrap or stop fallback.
# Every caller must trap wm_close, prepare BEFORE mutation, and finish only after
# its full postflight. Failure/rollback never calls finish.

wm_source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

wm_error() { printf '%s\n' 'ERROR: maintenance admission unsafe; no automatic release' >&2; return 1; }

wm_init() {
  local name
  local -a overrides=(WM_PYTHON WM_FLOCK WM_DATE WM_SLEEP WM_POLL_ATTEMPTS WM_POLL_SECONDS
    WM_CONTROL_DIR WM_ACK_DIR WM_PROC_ROOT WM_SYSTEMCTL WM_GLOBAL_LOCK WM_JOURNAL_ROOT
    WM_HEALTH_URL WM_ROOT_UID WM_ROOT_GID WM_WORKER_UID WM_WORKER_GID)
  wm_test=${WM_TEST_MODE:-0}
  case "$wm_test" in 0|1) ;; *) return 2 ;; esac
  for name in "${overrides[@]}"; do
    if [ "$wm_test" = 1 ]; then
      [ "${!name+x}" = x ] || return 2
    else
      [ "${!name+x}" != x ] || return 2
    fi
  done
  [ "$wm_test" = 1 ] || [ "$EUID" -eq 0 ] || return 1
  wm_python=${WM_PYTHON:-/usr/bin/python3}
  wm_flock=${WM_FLOCK:-/usr/bin/flock}
  wm_date=${WM_DATE:-/usr/bin/date}
  wm_sleep=${WM_SLEEP:-/usr/bin/sleep}
  wm_poll_attempts=${WM_POLL_ATTEMPTS:-900}
  wm_poll_seconds=${WM_POLL_SECONDS:-1}
  wm_control_dir=${WM_CONTROL_DIR:-/var/lib/worker-maintenance}
  wm_global_lock=${WM_GLOBAL_LOCK:-/run/lock/legaltech-resource-guards.lock}
  [[ "$wm_poll_attempts" =~ ^[0-9]+$ ]] && [ "$wm_poll_attempts" -gt 0 ] && [ "$wm_poll_attempts" -le 900 ] || return 2
  [[ "$wm_poll_seconds" =~ ^[0-9]+$ ]] && [ "$wm_poll_seconds" -le 1 ] || return 2
  for name in "$wm_python" "$wm_flock" "$wm_date" "$wm_sleep" "$wm_control_dir" "$wm_global_lock"; do
    case "$name" in /*) ;; *) return 2 ;; esac
  done
  wm_args=()
  if [ "$wm_test" = 1 ]; then
    wm_args=(--test-mode --control-dir "$WM_CONTROL_DIR" --ack-dir "$WM_ACK_DIR"
      --proc-root "$WM_PROC_ROOT" --systemctl "$WM_SYSTEMCTL" --global-lock "$WM_GLOBAL_LOCK"
      --journal-root "$WM_JOURNAL_ROOT" --health-url "$WM_HEALTH_URL"
      --root-uid "$WM_ROOT_UID" --root-gid "$WM_ROOT_GID" --worker-uid "$WM_WORKER_UID" --worker-gid "$WM_WORKER_GID")
  fi
  wm_global_fd=${WM_GLOBAL_FD:-}
  wm_admission_fd=${WM_ADMISSION_FD:-}
  wm_operation_id=${WM_OPERATION_ID:-}
  wm_identity=${WM_IDENTITY:-}
  wm_delegated=0
  wm_cli_root=$wm_source_root
  wm_runtime_snapshot=''
  if [ -n "$wm_global_fd$wm_admission_fd$wm_operation_id$wm_identity" ]; then
    [[ "$wm_global_fd" =~ ^[0-9]+$ ]] && [[ "$wm_admission_fd" =~ ^[0-9]+$ ]] \
      && [ -n "$wm_operation_id" ] && [ -n "$wm_identity" ] || return 2
    wm_delegated=1
  fi
}

wm_cli() { "$wm_python" "$wm_cli_root/ops/worker-maintenance.py" "${wm_args[@]}" "$@"; }

wm_pin_runtime() {
  # Deploy replaces its own checkout. Freeze only the reviewed stdlib operator
  # code before that mutation, so rollback/finalization never import the target.
  [ -z "$wm_runtime_snapshot" ] || return 0
  wm_snapshot_parent=/run
  [ "$wm_test" = 0 ] || wm_snapshot_parent=${wm_control_dir%/*}
  wm_runtime_snapshot=$(mktemp -d "$wm_snapshot_parent/worker-maintenance-runtime.XXXXXXXX") || return 1
  mkdir -p "$wm_runtime_snapshot/ops" "$wm_runtime_snapshot/estrado-pjud-service/worker" || return 1
  cp "$wm_source_root/ops/worker-maintenance.py" "$wm_runtime_snapshot/ops/" || return 1
  cp "$wm_source_root/estrado-pjud-service/worker/maintenance_store.py" \
    "$wm_source_root/estrado-pjud-service/worker/__init__.py" \
    "$wm_runtime_snapshot/estrado-pjud-service/worker/" || return 1
  wm_cli_root=$wm_runtime_snapshot
}

wm_pin_worker_contract() {
  local repo=$1 path
  wm_pin_runtime || return 1
  wm_contract_paths=(worker/__init__.py worker/__main__.py worker/maintenance.py
    worker/maintenance_store.py worker/metrics.py worker/sd_notify.py worker/config.py
    worker/session_pool.py worker/maintenance_heartbeat.py worker/proxy_control.py
    app/__init__.py app/r2.py app/minter.py app/playwright_runtime.py
    app/ojv/__init__.py app/ojv/session.py app/ojv/browser_login.py)
  mkdir -p "$wm_runtime_snapshot/contract/worker" "$wm_runtime_snapshot/contract/app/ojv" || return 1
  for path in "${wm_contract_paths[@]}"; do
    [ -f "$repo/estrado-pjud-service/$path" ] && [ ! -L "$repo/estrado-pjud-service/$path" ] || return 1
    cp "$repo/estrado-pjud-service/$path" "$wm_runtime_snapshot/contract/$path" || return 1
  done
}

wm_check_worker_contract() {
  local repo=$1 revision=$2 path
  for path in "${wm_contract_paths[@]}"; do
    if ! git -C "$repo" show "$revision:estrado-pjud-service/$path" 2>/dev/null \
      | cmp -s "$wm_runtime_snapshot/contract/$path" -; then
      wm_error
      return 1
    fi
  done
}

wm_close() {
  # Close only our references; never LOCK_UN the parent's shared description.
  if [ -n "${wm_admission_fd:-}" ]; then exec {wm_admission_fd}<&-; wm_admission_fd=''; fi
  if [ -n "${wm_global_fd:-}" ]; then exec {wm_global_fd}>&-; wm_global_fd=''; fi
  if [ -n "${wm_runtime_snapshot:-}" ]; then
    case "$wm_runtime_snapshot" in "$wm_snapshot_parent"/worker-maintenance-runtime.*) rm -rf -- "$wm_runtime_snapshot" ;; esac
    wm_runtime_snapshot=''
  fi
}

wm_acquire_global() {
  if [ "$wm_delegated" -eq 0 ]; then
    [ ! -L "$wm_global_lock" ] || return 1
    if [ ! -e "$wm_global_lock" ]; then
      ( umask 077; set -o noclobber; : > "$wm_global_lock" ) 2>/dev/null || return 1
    fi
    # Reject FIFO/device/unsafe metadata before the shell's potentially blocking
    # write-open. The held descriptor is still authenticated independently below.
    wm_cli status --check-lock-path || return 1
    exec {wm_global_fd}>>"$wm_global_lock" || return 1
    "$wm_flock" -n "$wm_global_fd" || { wm_error; return 1; }
  fi
  wm_cli status --check-lock --global-fd "$wm_global_fd" || return 1
  [ "$wm_delegated" -eq 1 ] || wm_capability
}

wm_window() {
  local hour
  hour=$(TZ=America/Santiago LC_ALL=C "$wm_date" +%H) || return 1
  [[ "$hour" =~ ^[0-9]{2}$ ]] || return 1
  hour=$((10#$hour))
  { [ "$hour" -ge 20 ] && [ "$hour" -le 23 ]; } || [ "$hour" -le 3 ]
}

wm_capability() {
  local status state _old_operation extra
  status=$(wm_cli status --require-open) || return 1
  read -r state _old_operation wm_identity extra <<< "$status"
  [ "$state" = open ] && [ -n "$wm_identity" ] && [ -z "${extra:-}" ] || return 1
}

wm_begin() {
  wm_capability || return 1
  wm_pin_runtime || return 1
  if [ -z "$wm_operation_id" ]; then
    wm_operation_id=$("$wm_python" -c 'import uuid; print(uuid.uuid4())') || return 1
  fi
  wm_cli begin --operation-id "$wm_operation_id" --identity "$wm_identity" \
    --global-fd "$wm_global_fd" >/dev/null || return 1
  printf 'MAINTENANCE_OPERATION_ID=%s\n' "$wm_operation_id"
}

wm_wait() {
  local attempt now deadline remaining timeout proof_identity
  local -a identity_args=(--identity "$wm_identity")
  [ -z "${1:-}" ] || identity_args=(--new-instance-from "$1")
  now=$("$wm_python" -c 'import time; print(int(time.monotonic()))') || return 1
  deadline=$((now + 900))
  [ -n "$wm_admission_fd" ] || exec {wm_admission_fd}<"$wm_control_dir/admission.lock" || return 1
  for ((attempt=1; attempt<=wm_poll_attempts; attempt++)); do
    now=$("$wm_python" -c 'import time; print(int(time.monotonic()))') || return 1
    remaining=$((deadline - now))
    [ "$remaining" -gt 0 ] || break
    timeout=$remaining
    [ "$timeout" -le 10 ] || timeout=10
    if "$wm_flock" -n "$wm_admission_fd" 2>/dev/null \
      && proof_identity=$(wm_cli verify-ack --operation-id "$wm_operation_id" "${identity_args[@]}" \
        --global-fd "$wm_global_fd" --admission-fd "$wm_admission_fd" --timeout-seconds "$timeout" 2>/dev/null); then
      now=$("$wm_python" -c 'import time; print(int(time.monotonic()))') || return 1
      [ "$now" -lt "$deadline" ] || break
      wm_identity=$proof_identity
      return 0
    fi
    now=$("$wm_python" -c 'import time; print(int(time.monotonic()))') || return 1
    [ "$now" -lt "$deadline" ] || break
    [ "$attempt" -eq "$wm_poll_attempts" ] || "$wm_sleep" "$wm_poll_seconds" || return 1
  done
  wm_error
}

wm_prepare() {
  if [ "$wm_delegated" -eq 1 ]; then
    wm_window || return 1
    wm_cli status --delegated --operation-id "$wm_operation_id" --global-fd "$wm_global_fd" \
      --admission-fd "$wm_admission_fd" --identity "$wm_identity" || return 1
    return 0
  fi
  wm_window && wm_begin && wm_wait && wm_window
}

wm_verify_current() {
  wm_cli verify-ack --operation-id "$wm_operation_id" --identity "$wm_identity" \
    --global-fd "$wm_global_fd" --admission-fd "$wm_admission_fd" >/dev/null
}

wm_verify_restarted() {
  wm_wait "$wm_identity"
}

wm_delegate() {
  WM_GLOBAL_FD="$wm_global_fd" WM_ADMISSION_FD="$wm_admission_fd" \
    WM_OPERATION_ID="$wm_operation_id" WM_IDENTITY="$wm_identity" "$@"
}

wm_finish() {
  [ "$wm_delegated" -eq 0 ] || return 0
  # This is the final command after the caller's durable success/postflight.
  # A publication uncertainty is NOT an installation failure/rollback trigger.
  wm_cli finish --operation-id "$wm_operation_id" --identity "$wm_identity" \
    --global-fd "$wm_global_fd" --admission-fd "$wm_admission_fd" "$@"
}
