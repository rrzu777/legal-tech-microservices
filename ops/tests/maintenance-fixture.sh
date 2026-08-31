#!/usr/bin/env bash
# Explicit, complete test boundary; fake protocol only in legacy shell suites.
maintenance_fixture() {
  local root=$1 ops=$2
  mkdir -p "$root/maintenance-control" "$root/maintenance-ack" "$root/proc"
  touch "$root/maintenance-control/admission.lock"
  chmod 750 "$root/maintenance-control"
  chmod 700 "$root/maintenance-ack"
  chmod 640 "$root/maintenance-control/admission.lock"
  export WM_TEST_MODE=1 WM_FIXTURE_ROOT="$root"
  export WM_PYTHON="$ops/tests/maintenance-fake-python.py"
  export WM_FLOCK=/usr/bin/flock WM_DATE="$root/maintenance-date" WM_SLEEP=/bin/sleep
  export WM_POLL_ATTEMPTS=2 WM_POLL_SECONDS=0
  export WM_CONTROL_DIR="$root/maintenance-control" WM_ACK_DIR="$root/maintenance-ack"
  export WM_PROC_ROOT="$root/proc" WM_SYSTEMCTL="$root/systemctl"
  export WM_GLOBAL_LOCK="$root/global.lock" WM_JOURNAL_ROOT="$root/journals"
  export WM_HEALTH_URL="file://$root/health"
  WM_ROOT_UID=$(id -u); WM_ROOT_GID=$(id -g)
  export WM_ROOT_UID WM_ROOT_GID WM_WORKER_UID="$WM_ROOT_UID" WM_WORKER_GID="$WM_ROOT_GID"
  printf '#!/bin/sh\nprintf "20\\n"\n' > "$WM_DATE"
  chmod +x "$WM_DATE"
}
