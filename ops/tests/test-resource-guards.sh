#!/usr/bin/env bash
# Behavioral fake-root tests for the fail-closed resource guard orchestrator.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$ROOT/ops/resource-guards.sh"
TMP_RAW="$(mktemp -d)"
TMP="$(cd "$TMP_RAW" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
EXPECTED_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
SECRET_SENTINEL=fixture-credential-not-a-token

ok() { printf '  ok   %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
expect_eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got=$2 want=$3)"; fi
}
expect_contains() {
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (missing safe diagnostic: $3)" ;; esac
}
expect_missing() {
  case "$2" in *"$3"*) bad "$1 (unexpected sensitive/unsafe text: $3)" ;; *) ok "$1" ;; esac
}
expect_before() {
  local first second
  first=$(grep -n -m1 -F "$2" "$EVENTS" 2>/dev/null | cut -d: -f1 || true)
  second=$(grep -n -m1 -F "$3" "$EVENTS" 2>/dev/null | cut -d: -f1 || true)
  if [ -n "$first" ] && [ -n "$second" ] && [ "$first" -lt "$second" ]; then
    ok "$1"
  else
    bad "$1 (first=${first:-missing} second=${second:-missing})"
  fi
}
expect_last_before() {
  local first second
  first=$(grep -n -F "$2" "$EVENTS" 2>/dev/null | tail -1 | cut -d: -f1 || true)
  second=$(grep -n -F "$3" "$EVENTS" 2>/dev/null | tail -1 | cut -d: -f1 || true)
  if [ -n "$first" ] && [ -n "$second" ] && [ "$first" -lt "$second" ]; then ok "$1"; else bad "$1 (first=${first:-missing} second=${second:-missing})"; fi
}
expect_second_before() {
  local first second
  first=$(grep -n -F "$2" "$EVENTS" 2>/dev/null | sed -n '2p' | cut -d: -f1 || true)
  second=$(grep -n -m1 -F "$3" "$EVENTS" 2>/dev/null | cut -d: -f1 || true)
  if [ -n "$first" ] && [ -n "$second" ] && [ "$first" -lt "$second" ]; then ok "$1"; else bad "$1 (first=${first:-missing} second=${second:-missing})"; fi
}
expect_count() {
  local count
  count=$(grep -c -F "$2" "$EVENTS" 2>/dev/null || true)
  expect_eq "$1" "$count" "$3"
}
expect_exact_count() {
  local count
  count=$(grep -c -x -F "$2" "$EVENTS" 2>/dev/null || true)
  expect_eq "$1" "$count" "$3"
}

write_stub() { # name, body on stdin
  local name="$1"
  { printf '%s\n' '#!/usr/bin/env bash' 'set -u'; cat; } > "$BIN/$name"
  chmod +x "$BIN/$name"
}

setup() {
  CASE_DIR="$TMP/case-$RANDOM-$RANDOM"
  FAKE="$CASE_DIR/root"
  STATE="$CASE_DIR/state"
  BIN="$CASE_DIR/bin"
  EVENTS="$STATE/events"
  mkdir -p "$FAKE/repo/estrado-pjud-service" "$FAKE/repo/ops/systemd" "$FAKE/systemd" "$FAKE/run" "$CASE_DIR/tmp" \
    "$FAKE/etc/sysctl.d" "$FAKE/etc/caddy" "$FAKE/etc/logrotate.d" \
    "$FAKE/monitoring" "$FAKE/backups" "$STATE" "$BIN"
  chmod 700 "$FAKE/backups"
  : > "$EVENTS"
  : > "$CASE_DIR/null"
  printf '%s\n' "$EXPECTED_SHA" > "$STATE/git-sha"
  : > "$STATE/git-status"
  printf '%s\n' 1787356980 > "$STATE/now"
  printf '%s\n' 20 > "$STATE/local-hour"
  printf '%s\n' 20240309T160000Z > "$STATE/backup-timestamp"
  printf '%s\n' 200 > "$STATE/juristrack-code"
  printf '%s\n' 200 > "$STATE/estrado-code"
  printf '%s\n' 0 > "$STATE/claim-count"
  printf '%s\n' safe > "$STATE/heartbeat-pre"
  printf '%s\n' safe-new > "$STATE/heartbeat-post"
  printf 'SUPABASE_URL=https://db.invalid\nSUPABASE_SERVICE_KEY=%s\nWORKER_ID=worker-1\nPJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false\nOJV_PROXY_URL=https://proxy.invalid\n' "$SECRET_SENTINEL" \
    > "$FAKE/repo/estrado-pjud-service/.env"
  chmod 640 "$FAKE/repo/estrado-pjud-service/.env"

  for file in legaltech.slice estrado-pjud.service estrado-pjud-worker.service \
    legaltech-monitor.service legaltech-resource-tracker.service \
    legaltech-monitor.timer legaltech-resource-tracker.timer; do
    printf 'original %s\n' "$file" > "$FAKE/systemd/$file"
  done
  printf '%s\n' '[Service]' 'Slice=legaltech.slice' 'ExecStart=/bin/true' \
    > "$FAKE/systemd/estrado-pjud-worker.service"
  cp "$FAKE/systemd/legaltech-monitor.service" "$FAKE/repo/ops/systemd/legaltech-monitor.service"
  cp "$FAKE/systemd/legaltech-resource-tracker.service" "$FAKE/repo/ops/systemd/legaltech-resource-tracker.service"
  mkdir -p "$FAKE/systemd/estrado-pjud-worker.service.d" \
    "$FAKE/systemd/user-1002.slice.d"
  printf 'original xvfb\n' > "$FAKE/systemd/estrado-pjud-worker.service.d/xvfb.conf"
  mkdir -p "$FAKE/repo/ops/systemd/estrado-pjud-worker.service.d"
  cp "$FAKE/systemd/estrado-pjud-worker.service" "$FAKE/repo/ops/systemd/estrado-pjud-worker.service"
  cp "$FAKE/systemd/estrado-pjud-worker.service.d/xvfb.conf" \
    "$FAKE/repo/ops/systemd/estrado-pjud-worker.service.d/xvfb.conf"
  printf 'original hermes\n' > "$FAKE/systemd/user-1002.slice.d/50-legaltech-resource-limits.conf"
  printf 'monitor credential placeholder\n' > "$FAKE/monitoring.env"
  chmod 600 "$FAKE/monitoring.env"
  printf 'UUID=root / ext4 defaults 0 1\n' > "$FAKE/etc/fstab"
  printf 'old sysctl\n' > "$FAKE/etc/sysctl.d/60-legaltech-swap.conf"
  SWAPPINESS_METADATA_FILE="$FAKE/etc/sysctl.d/60-legaltech-swap.previous"
  printf 'old caddy\n' > "$FAKE/etc/caddy/Caddyfile"
  printf 'old logrotate\n' > "$FAKE/etc/logrotate.d/legaltech-resources"
  printf 'old monitor\n' > "$FAKE/monitoring/monitor.py"
  printf 'old tracker\n' > "$FAKE/monitoring/resource-tracker.py"
  printf 'outside original\n' > "$FAKE/outside"

  printf '%s\n' enabled > "$STATE/unit-estrado-pjud.service-enabled"
  printf '%s\n' active > "$STATE/unit-estrado-pjud.service-active"
  printf '%s\n' loaded > "$STATE/unit-estrado-pjud.service-load"
  printf '%s\n' 4200 > "$STATE/unit-estrado-pjud.service-main-pid"
  printf '%s\n' /legaltech.slice/estrado-pjud.service \
    > "$STATE/unit-estrado-pjud.service-control-group"
  printf '%s\n' loaded > "$STATE/unit-legaltech.slice-load"
  printf '%s\n' active > "$STATE/unit-legaltech.slice-active"
  printf '%s\n' /legaltech.slice > "$STATE/unit-legaltech.slice-control-group"
  printf '%s\n' disabled > "$STATE/unit-estrado-pjud-worker.service-enabled"
  printf '%s\n' inactive > "$STATE/unit-estrado-pjud-worker.service-active"
  printf '%s\n' loaded > "$STATE/unit-estrado-pjud-worker.service-load"
  printf '%s\n' 0 > "$STATE/unit-estrado-pjud-worker.service-main-pid"
  : > "$STATE/unit-estrado-pjud-worker.service-control-group"
  printf '%s\n' legaltech.slice > "$STATE/unit-estrado-pjud-worker.service-slice"
  printf '%s\n' success > "$STATE/unit-estrado-pjud-worker.service-result"
  for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
    printf '%s\n' disabled > "$STATE/unit-$unit-enabled"
    printf '%s\n' inactive > "$STATE/unit-$unit-active"
    printf '%s\n' 0 > "$STATE/unit-$unit-main-pid"
    : > "$STATE/unit-$unit-control-group"
    printf '%s\n' system.slice > "$STATE/unit-$unit-slice"
    printf '%s\n' success > "$STATE/unit-$unit-result"
  done
  for unit in legaltech-monitor.timer legaltech-resource-tracker.timer; do
    printf '%s\n' enabled > "$STATE/unit-$unit-enabled"
    printf '%s\n' active > "$STATE/unit-$unit-active"
  done
  for unit in hermes-gateway.service hermes-dashboard.service; do
    printf '%s\n' enabled > "$STATE/user-unit-$unit-enabled"
    printf '%s\n' active > "$STATE/user-unit-$unit-active"
    printf '%s\n' loaded > "$STATE/user-unit-$unit-load"
    case "$unit" in hermes-gateway.service) pid=4301 ;; *) pid=4302 ;; esac
    printf '%s\n' "$pid" > "$STATE/user-unit-$unit-main-pid"
    printf '/user.slice/user-1002.slice/user@1002.service/app.slice/%s\n' "$unit" \
      > "$STATE/user-unit-$unit-control-group"
  done
  printf '%s\n' loaded > "$STATE/unit-user-1002.slice-load"
  printf '%s\n' active > "$STATE/unit-user-1002.slice-active"
  printf '%s\n' /user.slice/user-1002.slice > "$STATE/unit-user-1002.slice-control-group"

  if [ -d "$TMP/stub-bin" ]; then
    cp -R "$TMP/stub-bin/." "$BIN/"
  else
  write_stub git <<'EOF'
printf 'git %s\n' "$*" >> "$RG_TEST_STATE/events"
case "${1:-}" in
  status) cat "$RG_TEST_STATE/git-status" ;;
  rev-parse) cat "$RG_TEST_STATE/git-sha" ;;
  *) exit 1 ;;
esac
EOF
  write_stub df <<'EOF'
bytes=${RG_TEST_DISK_BYTES:-9663676416}
printf 'df %s\n' "$*" >> "$RG_TEST_STATE/events"
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
printf 'fake 20000000 1 %s 1%% /\n' "$((bytes / 1024))"
[ ! -e "$RG_TEST_STATE/df-fail-after-output" ]
EOF
  write_stub free <<'EOF'
bytes=${RG_TEST_RAM_BYTES:-7516192768}
printf '               total        used        free      shared  buff/cache   available\n'
printf 'Mem:     16000000000 1 1 1 1 %s\n' "$bytes"
[ ! -e "$RG_TEST_STATE/free-fail-after-output" ]
EOF
  write_stub id <<'EOF'
if [ -e "$RG_TEST_STATE/id-fail" ]; then exit 1; fi
case "${1:-}" in
  -u) printf '%s\n' 1002 ;;
  -nu) printf '%s\n' hermes ;;
  -g) printf '%s\n' "$RG_TEST_ROOT_GID" ;;
  *) exit 1 ;;
esac
EOF
  write_stub ps <<'EOF'
[ ! -e "$RG_TEST_STATE/ps-fail" ] || exit 1
if [ "${1:-}" = -p ]; then
  pid=${2:-}
  [ "${3:-}" = -o ] && [ "${4:-}" = unit= ] || exit 1
  printf 'ps pid=%s\n' "$pid" >> "$RG_TEST_STATE/events"
  [ -f "$RG_TEST_STATE/pid-$pid-unit" ] || exit 1
  cat "$RG_TEST_STATE/pid-$pid-unit"
  exit 0
fi
printf '%s\n' 'user@1002.service hermes-gateway.service' 'user@1002.service hermes-dashboard.service'
EOF
  write_stub systemctl <<'EOF'
printf 'systemctl %s\n' "$*" >> "$RG_TEST_STATE/events"
if [ -f "$RG_TEST_STATE/fail-command" ] && [ "$*" = "$(cat "$RG_TEST_STATE/fail-command")" ]; then
  if [ -e "$RG_TEST_STATE/fail-command-once" ]; then
    rm -f "$RG_TEST_STATE/fail-command" "$RG_TEST_STATE/fail-command-once"
  fi
  exit 1
fi
if [ "${1:-}" = daemon-reload ] && [ -e "$RG_TEST_STATE/daemon-fail" ]; then exit 1; fi
if [ "${1:-}" = list-unit-files ]; then
  case "$(cat "$RG_TEST_STATE/list-shape" 2>/dev/null || true)" in
    unexpected-state) printf '%s\n' 'estrado-pjud.service disabled enabled' ;;
    extra) printf '%s\n' 'estrado-pjud.service enabled enabled extra' ;;
    *) printf '%s\n' 'estrado-pjud.service enabled enabled' 'hermes-gateway.service enabled enabled' 'hermes-dashboard.service enabled disabled' ;;
  esac
  exit 0
fi
state_file() { printf '%s/unit-%s-%s' "$RG_TEST_STATE" "$1" "$2"; }
user_state_file() { printf '%s/user-unit-%s-%s' "$RG_TEST_STATE" "$1" "$2"; }
if [ "${1:-}" = --user ]; then
  command=${3:-}; unit=${4:-}
  if [ "$command" = show ]; then
    properties=()
    for arg in "$@"; do case "$arg" in --property=*) properties+=("${arg#--property=}") ;; esac; done
    for property in "${properties[@]}"; do
      if [ -e "$RG_TEST_STATE/property-omit" ] \
        && [ "$unit:$property" = "$(cat "$RG_TEST_STATE/property-omit")" ]; then
        continue
      fi
      if [ -e "$RG_TEST_STATE/property-bad" ] \
        && [ "$unit:$property" = "$(cat "$RG_TEST_STATE/property-bad")" ]; then
        value=$(cat "$RG_TEST_STATE/property-bad-value" 2>/dev/null || printf drifted)
      else
        case "$property" in
          LoadState) value=$(cat "$(user_state_file "$unit" load)") || exit 1 ;;
          ActiveState) value=$(cat "$(user_state_file "$unit" active)") || exit 1 ;;
          MainPID)
            if [ "$(cat "$(user_state_file "$unit" active)")" = inactive ]; then value=0
            else value=$(cat "$(user_state_file "$unit" main-pid)") || exit 1; fi ;;
          ControlGroup)
            if [ "$(cat "$(user_state_file "$unit" active)")" = inactive ]; then value=
            else value=$(cat "$(user_state_file "$unit" control-group)") || exit 1; fi ;;
          *) exit 1 ;;
        esac
      fi
      printf '%s=%s\n' "$property" "$value"
      if [ -e "$RG_TEST_STATE/property-duplicate" ] \
        && [ "$unit:$property" = "$(cat "$RG_TEST_STATE/property-duplicate")" ]; then
        printf '%s=%s\n' "$property" "$value"
      fi
    done
    if [ -e "$RG_TEST_STATE/property-extra" ] \
      && [ "$unit" = "$(cat "$RG_TEST_STATE/property-extra")" ]; then
      printf '%s\n' 'Unexpected=loaded'
    fi
    exit 0
  fi
  if [ "$command" = list-unit-files ]; then
    for candidate in hermes-gateway.service hermes-dashboard.service; do
      value=$(cat "$(user_state_file "$candidate" enabled)") || exit 1
      [ "$value" != enabled ] || printf '%s enabled enabled\n' "$candidate"
    done
    exit 0
  fi
  case "$command" in
    is-enabled|is-active)
      kind=${command#is-}; value=$(cat "$(user_state_file "$unit" "$kind")") || exit 1
      printf '%s\n' "$value"
      if [ "$kind" = enabled ]; then
        case "$value" in
          enabled) exit 0 ;;
          static) [ ! -e "$RG_TEST_STATE/static-status-fail" ] ;;
          disabled) exit 1 ;;
          not-found) exit 1 ;;
          *) exit 4 ;;
        esac
      else
        case "$value" in active) exit 0 ;; inactive) exit 3 ;; *) exit 4 ;; esac
      fi
      ;;
    enable|disable)
      case "$command" in enable) value=enabled ;; disable) value=disabled ;; esac
      shift 3
      for candidate in "$@"; do
        current=$(cat "$(user_state_file "$candidate" enabled)") || exit 1
        [ "$current" = static ] || printf '%s\n' "$value" > "$(user_state_file "$candidate" enabled)"
      done
      ;;
    start|stop|restart)
      case "$command" in stop) value=inactive ;; *) value=active ;; esac
      shift 3
      for candidate in "$@"; do printf '%s\n' "$value" > "$(user_state_file "$candidate" active)"; done
      ;;
    *) exit 1 ;;
  esac
  exit 0
fi
if [ "${1:-}" = show ]; then
  unit=${2:-}
  property='' value_mode=0
  properties=()
  previous=''
  for arg in "$@"; do
    if [ "$previous" = --property ]; then property=$arg; properties+=("$arg"); fi
    case "$arg" in --property=*) property=${arg#--property=}; properties+=("$property") ;; --value) value_mode=1 ;; esac
    previous=$arg
  done
  if [ "$property" = User ]; then
    case "$unit" in hermes-*) printf '%s\n' hermes ;; *) printf '\n' ;; esac
    exit 0
  fi
  if [ -e "$RG_TEST_STATE/postflight-fail" ] && [ -e "$RG_TEST_STATE/postflight-armed" ]; then
    # Fail one postflight read, then let rollback prove the restored runtime.
    rm -f "$RG_TEST_STATE/postflight-armed"
    exit 1
  fi
  property_value() {
  if [ -e "$RG_TEST_STATE/property-bad" ]; then
    bad_key=$(cat "$RG_TEST_STATE/property-bad")
    [ -n "$bad_key" ] || bad_key=legaltech.slice:MemoryMax
    if [ "$unit:$1" = "$bad_key" ]; then
      if [ -e "$RG_TEST_STATE/property-bad-value" ]; then
        cat "$RG_TEST_STATE/property-bad-value"
      else
        echo drifted
      fi
      return
    fi
  fi
  case "$unit:$1" in
    legaltech.slice:LoadState|legaltech.slice:ActiveState|legaltech.slice:ControlGroup|user-1002.slice:LoadState|user-1002.slice:ActiveState|user-1002.slice:ControlGroup|estrado-pjud.service:LoadState|estrado-pjud.service:ActiveState|estrado-pjud.service:MainPID|estrado-pjud.service:ControlGroup) {
      case "$1" in
        LoadState) suffix=load ;;
        ActiveState) suffix=active ;;
        MainPID) suffix=main-pid ;;
        ControlGroup) suffix=control-group ;;
      esac
      if [ "$unit" = estrado-pjud.service ] \
        && [ "$(cat "$RG_TEST_STATE/unit-$unit-active")" = inactive ]; then
        case "$1" in MainPID) printf '%s\n' 0 ;; ControlGroup) printf '\n' ;; *) cat "$RG_TEST_STATE/unit-$unit-$suffix" ;; esac
      else
        cat "$RG_TEST_STATE/unit-$unit-$suffix"
      fi
    } ;;
    legaltech.slice:CPUWeight) echo 1000 ;;
    legaltech.slice:MemoryLow) echo 3221225472 ;;
    legaltech.slice:MemoryHigh) echo 6442450944 ;;
    legaltech.slice:MemoryMax) echo 8589934592 ;;
    estrado-pjud.service:Slice) echo legaltech.slice ;;
    estrado-pjud.service:MemoryHigh) echo 3221225472 ;;
    estrado-pjud.service:MemoryMax) echo 4294967296 ;;
    estrado-pjud.service:CPUQuotaPerSecUSec) echo 2s ;;
    estrado-pjud.service:CPUWeight) echo 500 ;;
    estrado-pjud.service:TasksMax) echo 512 ;;
    estrado-pjud-worker.service:PartOf) echo legaltech.slice ;;
    estrado-pjud-worker.service:MemoryHigh) echo 2147483648 ;;
    estrado-pjud-worker.service:MemoryMax) echo 3221225472 ;;
    estrado-pjud-worker.service:CPUQuotaPerSecUSec) echo 2s ;;
    estrado-pjud-worker.service:CPUWeight) echo 800 ;;
    estrado-pjud-worker.service:TasksMax) echo 512 ;;
    user-1002.slice:MemoryHigh) echo 2147483648 ;;
    user-1002.slice:MemoryMax) echo 2621440000 ;;
    user-1002.slice:TasksMax) echo 1024 ;;
    user-1002.slice:CPUWeight) echo 200 ;;
    estrado-pjud-worker.service:Slice|legaltech-monitor.service:Slice|legaltech-resource-tracker.service:Slice) cat "$RG_TEST_STATE/unit-$unit-slice" ;;
    estrado-pjud-worker.service:LoadState) cat "$RG_TEST_STATE/unit-$unit-load" ;;
    estrado-pjud-worker.service:MainPID|legaltech-monitor.service:MainPID|legaltech-resource-tracker.service:MainPID) cat "$RG_TEST_STATE/unit-$unit-main-pid" ;;
    estrado-pjud-worker.service:ControlGroup|legaltech-monitor.service:ControlGroup|legaltech-resource-tracker.service:ControlGroup) cat "$RG_TEST_STATE/unit-$unit-control-group" ;;
    estrado-pjud-worker.service:ActiveState|legaltech-monitor.service:ActiveState|legaltech-resource-tracker.service:ActiveState) cat "$RG_TEST_STATE/unit-$unit-active" ;;
    estrado-pjud-worker.service:Result|legaltech-monitor.service:Result|legaltech-resource-tracker.service:Result) cat "$RG_TEST_STATE/unit-$unit-result" ;;
    legaltech-monitor.service:Type|legaltech-resource-tracker.service:Type) echo oneshot ;;
    legaltech-monitor.service:User|legaltech-resource-tracker.service:User) echo root ;;
    legaltech-monitor.service:WorkingDirectory|legaltech-resource-tracker.service:WorkingDirectory) echo /opt/legaltech-monitoring ;;
    legaltech-monitor.service:NoNewPrivileges|legaltech-resource-tracker.service:NoNewPrivileges) echo yes ;;
    legaltech-monitor.service:PrivateTmp|legaltech-resource-tracker.service:PrivateTmp) echo yes ;;
    legaltech-monitor.service:ProtectSystem|legaltech-resource-tracker.service:ProtectSystem) echo strict ;;
    legaltech-monitor.service:ProtectHome|legaltech-resource-tracker.service:ProtectHome) echo yes ;;
    legaltech-monitor.service:MemoryMax|legaltech-resource-tracker.service:MemoryMax) echo 134217728 ;;
    legaltech-monitor.service:CPUQuotaPerSecUSec|legaltech-resource-tracker.service:CPUQuotaPerSecUSec) echo 200ms ;;
    legaltech-monitor.service:TasksMax|legaltech-resource-tracker.service:TasksMax) echo 64 ;;
    legaltech-monitor.service:StateDirectory) echo legaltech-monitor ;;
    legaltech-monitor.service:StateDirectoryMode) echo 0750 ;;
    legaltech-monitor.service:LogsDirectory) echo legaltech ;;
    legaltech-monitor.service:LogsDirectoryMode) echo 0750 ;;
    legaltech-monitor.service:ReadWritePaths) echo '/var/lib/legaltech-monitor /var/log/legaltech' ;;
    legaltech-monitor.service:RestrictAddressFamilies) echo '' ;;
    legaltech-monitor.service:PartOf) echo '' ;;
    legaltech-monitor.service:EnvironmentFiles) echo '/etc/legaltech-monitoring.env (ignore_errors=yes)' ;;
    legaltech-resource-tracker.service:StateDirectory|legaltech-resource-tracker.service:LogsDirectory) echo '' ;;
    legaltech-resource-tracker.service:ReadWritePaths) echo /var/log/legaltech/resources.csv ;;
    legaltech-resource-tracker.service:RestrictAddressFamilies) echo AF_UNIX ;;
    legaltech-resource-tracker.service:PartOf|legaltech-resource-tracker.service:EnvironmentFiles) echo '' ;;
    legaltech-monitor.timer:Unit) echo legaltech-monitor.service ;;
    legaltech-resource-tracker.timer:Unit) echo legaltech-resource-tracker.service ;;
    legaltech-monitor.timer:OnBootUSec|legaltech-resource-tracker.timer:OnBootUSec) echo 5min ;;
    legaltech-monitor.timer:OnUnitActiveUSec|legaltech-resource-tracker.timer:OnUnitActiveUSec) echo 5min ;;
    legaltech-monitor.timer:Persistent|legaltech-resource-tracker.timer:Persistent) echo yes ;;
    legaltech-monitor.timer:RandomizedDelayUSec|legaltech-resource-tracker.timer:RandomizedDelayUSec) echo 1min ;;
    *) exit 1 ;;
  esac; }
  if [ "$value_mode" -eq 1 ]; then property_value "$property"; else
    for property in "${properties[@]}"; do
      if [ -e "$RG_TEST_STATE/property-omit" ] \
        && [ "$unit:$property" = "$(cat "$RG_TEST_STATE/property-omit")" ]; then
        continue
      fi
      value=$(property_value "$property") || exit 1
      printf '%s=%s\n' "$property" "$value"
      if [ -e "$RG_TEST_STATE/property-duplicate" ] \
        && [ "$unit:$property" = "$(cat "$RG_TEST_STATE/property-duplicate")" ]; then
        printf '%s=%s\n' "$property" "$value"
      fi
    done
    if [ -e "$RG_TEST_STATE/property-extra" ] \
      && [ "$unit" = "$(cat "$RG_TEST_STATE/property-extra")" ]; then
      printf '%s\n' 'Unexpected=loaded'
    fi
  fi
  exit 0
fi
case "${1:-}" in
  is-enabled|is-active)
    kind=${1#is-}; unit=${2:-}
    if [ "$kind" = enabled ] && [ "$unit" = estrado-pjud.service ] \
      && [ -f "$RG_TEST_STATE/api-is-enabled-output" ]; then
      cat "$RG_TEST_STATE/api-is-enabled-output"
      exit "$(cat "$RG_TEST_STATE/api-is-enabled-status")"
    fi
    value=$(cat "$(state_file "$unit" "$kind")") || exit 1
    printf '%s\n' "$value"
    if [ "$kind" = enabled ]; then
      case "$value" in
        enabled) exit 0 ;;
        static) [ ! -e "$RG_TEST_STATE/static-status-fail" ] ;;
        disabled) exit 1 ;;
        not-found) exit 1 ;;
        *) exit 4 ;;
      esac
    else
      case "$value" in active) exit 0 ;; inactive) exit 3 ;; *) exit 4 ;; esac
    fi
    ;;
  enable|disable)
    action=$1; shift
    case "$action" in enable) value=enabled ;; disable) value=disabled ;; esac
    for unit in "$@"; do
      [ "$unit" = -- ] && continue
      current=$(cat "$(state_file "$unit" enabled)") || exit 1
      if [ "$action" = disable ] && [ "$unit" = estrado-pjud.service ] \
        && [ -e "$RG_TEST_STATE/ignore-first-api-disable" ]; then
        rm -f "$RG_TEST_STATE/ignore-first-api-disable"
        continue
      fi
      [ "$current" = static ] || printf '%s\n' "$value" > "$(state_file "$unit" enabled)"
    done
    ;;
  start|stop|restart)
    action=$1; shift
    case "$action" in stop) value=inactive ;; *) value=active ;; esac
    for unit in "$@"; do
      [ "$unit" = -- ] && continue
      if [ "$unit" = estrado-pjud-worker.service ] && [ "$action" = stop ] \
        && [ -e "$RG_TEST_STATE/worker-stop-keeps-active" ]; then
        continue
      fi
      printf '%s\n' "$value" > "$(state_file "$unit" active)"
      case "$unit" in
        estrado-pjud-worker.service)
          old_pid=$(cat "$RG_TEST_STATE/unit-$unit-main-pid") || exit 1
          if [ "$action" = stop ]; then
            if [ ! -e "$RG_TEST_STATE/worker-residual-runtime" ]; then
              case "$old_pid" in 0) ;; *) rm -f "$RG_TEST_STATE/pid-$old_pid-unit" ;; esac
              printf '%s\n' 0 > "$RG_TEST_STATE/unit-$unit-main-pid"
              : > "$RG_TEST_STATE/unit-$unit-control-group"
            fi
          else
            new_pid=5201
            printf '%s\n' "$new_pid" > "$RG_TEST_STATE/unit-$unit-main-pid"
            if [ -e "$RG_TEST_STATE/worker-wrong-start-cgroup" ]; then
              printf '%s\n' /system.slice/estrado-pjud-worker.service \
                > "$RG_TEST_STATE/unit-$unit-control-group"
            else
              worker_slice=$(cat "$RG_TEST_STATE/unit-$unit-slice") || exit 1
              printf '/%s/%s\n' "$worker_slice" "$unit" \
                > "$RG_TEST_STATE/unit-$unit-control-group"
            fi
            printf '%s\n' "$unit" > "$RG_TEST_STATE/pid-$new_pid-unit"
          fi
          ;;
        legaltech-monitor.service|legaltech-resource-tracker.service)
          old_pid=$(cat "$RG_TEST_STATE/unit-$unit-main-pid") || exit 1
          case "$old_pid" in 0) ;; *) rm -f "$RG_TEST_STATE/pid-$old_pid-unit" ;; esac
          if [ "$value" = active ]; then
            case "$unit" in legaltech-monitor.service) new_pid=5101 ;; *) new_pid=5102 ;; esac
            printf '%s\n' "$new_pid" > "$RG_TEST_STATE/unit-$unit-main-pid"
            printf '/%s/%s\n' "$(cat "$RG_TEST_STATE/unit-$unit-slice")" "$unit" > "$RG_TEST_STATE/unit-$unit-control-group"
            printf '%s\n' "$unit" > "$RG_TEST_STATE/pid-$new_pid-unit"
          else
            printf '%s\n' 0 > "$RG_TEST_STATE/unit-$unit-main-pid"
            : > "$RG_TEST_STATE/unit-$unit-control-group"
          fi
          ;;
      esac
    done
    ;;
  daemon-reload)
    if grep -q '^Slice=legaltech.slice$' \
      "$RG_SYSTEMD_DIR/estrado-pjud-worker.service" 2>/dev/null; then
      printf '%s\n' legaltech.slice \
        > "$RG_TEST_STATE/unit-estrado-pjud-worker.service-slice"
    else
      printf '%s\n' system.slice \
        > "$RG_TEST_STATE/unit-estrado-pjud-worker.service-slice"
    fi
    for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
      if grep -q '^changed monitor definition$' "$RG_SYSTEMD_DIR/$unit" 2>/dev/null; then
        printf '%s\n' system.slice > "$RG_TEST_STATE/unit-$unit-slice"
      elif grep -q '^legacy monitor definition$' "$RG_SYSTEMD_DIR/$unit" 2>/dev/null; then
        printf '%s\n' legaltech.slice > "$RG_TEST_STATE/unit-$unit-slice"
      fi
    done
    for unit in legaltech-monitor.timer legaltech-resource-tracker.timer; do
      if [ ! -e "$RG_SYSTEMD_DIR/$unit" ]; then
        printf '%s\n' not-found > "$RG_TEST_STATE/unit-$unit-enabled"
        printf '%s\n' inactive > "$RG_TEST_STATE/unit-$unit-active"
      fi
    done
    exit 0
    ;;
  *) exit 1 ;;
esac
EOF
  write_stub curl <<'EOF'
case "$*" in *fixture-credential-not-a-token*) printf '%s\n' 'credential leaked in argv' >> "$RG_TEST_STATE/events"; exit 97 ;; esac
config=''
url=''
previous=''
for arg in "$@"; do
  if [ "$previous" = --config ]; then config=$arg; fi
  case "$arg" in https://*) url=$arg ;; esac
  previous=$arg
done
if [ -n "$config" ]; then
  url=$(sed -n 's/^url = "\(.*\)"$/\1/p' "$config")
  if grep -q '^request = "HEAD"$' "$config"; then
    prefer=$(grep -c -F 'header = "Prefer: count=exact"' "$config" || true)
    range=$(grep -c -F 'header = "Range: 0-0"' "$config" || true)
    printf 'curl claims method=HEAD %s prefer=%s range=%s\n' "$url" "$prefer" "$range" >> "$RG_TEST_STATE/events"
    header=$(sed -n 's/^dump-header = "\(.*\)"$/\1/p' "$config")
    if [ -s "$RG_TEST_STATE/claim-sequence" ]; then
      claim_state=$(sed -n '1p' "$RG_TEST_STATE/claim-sequence")
      sed '1d' "$RG_TEST_STATE/claim-sequence" > "$RG_TEST_STATE/claim-sequence.next"
      mv "$RG_TEST_STATE/claim-sequence.next" "$RG_TEST_STATE/claim-sequence"
    else
      claim_state=$(cat "$RG_TEST_STATE/claim-count")
    fi
    case "$claim_state" in
      missing) printf 'HTTP/1.1 200 OK\r\n\r\n' > "$header" ;;
      malformed) printf 'HTTP/1.1 200 OK\r\nContent-Range: */*\r\n\r\n' > "$header" ;;
      wildcard) printf 'HTTP/1.1 200 OK\r\nContent-Range: */2\r\n\r\n' > "$header" ;;
      httpfail) exit 22 ;;
      output-then-fail) printf 'HTTP/1.1 200 OK\r\nContent-Range: */0\r\n\r\n' > "$header"; printf '200'; exit 22 ;;
      zero-star) printf 'HTTP/1.1 200 OK\r\nContent-Range: */0\r\n\r\n' > "$header" ;;
      value) count=$(cat "$RG_TEST_STATE/claim-value"); printf 'HTTP/1.1 200 OK\r\nContent-Range: 0-0/%s\r\n\r\n' "$count" > "$header" ;;
      *) printf 'HTTP/1.1 200 OK\r\nContent-Range: 0-0/%s\r\n\r\n' "$claim_state" > "$header" ;;
    esac
    printf '200'
    if [ -e "$RG_TEST_STATE/claim-after-first" ]; then
      next=$(cat "$RG_TEST_STATE/claim-after-first")
      case "$next" in nonzero) next=1 ;; esac
      printf '%s\n' "$next" > "$RG_TEST_STATE/claim-count"
      rm "$RG_TEST_STATE/claim-after-first"
    fi
    exit 0
  fi
  printf 'curl heartbeat %s\n' "$url" >> "$RG_TEST_STATE/events"
  output=$(sed -n 's/^output = "\(.*\)"$/\1/p' "$config")
  heartbeat_calls_file="$RG_TEST_STATE/heartbeat-calls"
  heartbeat_calls=0
  [ ! -f "$heartbeat_calls_file" ] || heartbeat_calls=$(cat "$heartbeat_calls_file")
  heartbeat_calls=$((heartbeat_calls + 1))
  printf '%s\n' "$heartbeat_calls" > "$heartbeat_calls_file"
  if [ -s "$RG_TEST_STATE/heartbeat-sequence" ]; then
    heartbeat_state=$(sed -n '1p' "$RG_TEST_STATE/heartbeat-sequence")
    sed '1d' "$RG_TEST_STATE/heartbeat-sequence" > "$RG_TEST_STATE/heartbeat-sequence.next"
    mv "$RG_TEST_STATE/heartbeat-sequence.next" "$RG_TEST_STATE/heartbeat-sequence"
  elif [ "$heartbeat_calls" -eq 1 ]; then
    heartbeat_state=$(cat "$RG_TEST_STATE/heartbeat-pre")
  else
    heartbeat_state=$(cat "$RG_TEST_STATE/heartbeat-post")
  fi
  case "$heartbeat_state" in
    safe) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    safe-historical-mint) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":7}}]' > "$output" ;;
    safe-new) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:02:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    safe-subsecond) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00.100000Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    safe-new-subsecond) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00.900000Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    starting-new) printf '[{"status":"starting","last_heartbeat_at":"2026-08-22T00:01:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    wrong-worker)
      case "$url" in
        *'worker_id=eq.worker-1'*) printf '[]' > "$output" ;;
        *) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
      esac
      ;;
    zero-rows) printf '[]' > "$output" ;;
    multiple) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}},{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    stale) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-21T23:50:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    future) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:04:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    running|stopped|paused|unknown) printf '[{"status":"%s","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' "$heartbeat_state" > "$output" ;;
    missing-metadata) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z"}]' > "$output" ;;
    malformed-metadata) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":[]}]' > "$output" ;;
    override-true) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":true,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    proxy-paused) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"paused","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    proxy-unavailable) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"unavailable","proxy_control_reason":null,"mint_attempts":0}}]' > "$output" ;;
    telemetry-unavailable) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":"telemetry_unavailable","mint_attempts":0}}]' > "$output" ;;
    mint-nonzero) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:02:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":1}}]' > "$output" ;;
    malformed) printf '{"message":"unsafe-detail"}' > "$output" ;;
    httpfail) exit 22 ;;
    output-then-fail) printf '[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]' > "$output"; printf '200'; exit 22 ;;
  esac
  printf '200'
  exit 0
fi
case "$url" in
  *juristrack.cl/api*) code=$(cat "$RG_TEST_STATE/estrado-code"); printf '%s\n' 'curl health estrado' >> "$RG_TEST_STATE/events" ;;
  *juristrack.cl/*) code=$(cat "$RG_TEST_STATE/juristrack-code"); printf '%s\n' 'curl health juristrack' >> "$RG_TEST_STATE/events" ;;
  *) exit 1 ;;
esac
health_calls_file="$RG_TEST_STATE/health-calls"
health_calls=0
[ ! -f "$health_calls_file" ] || health_calls=$(cat "$health_calls_file")
health_calls=$((health_calls + 1))
printf '%s\n' "$health_calls" > "$health_calls_file"
if [ -e "$RG_TEST_STATE/health-after-first" ] && [ "$health_calls" -gt 2 ]; then code=503; fi
printf '%s' "$code"
[ "$code" = 200 ]
EOF
  write_stub date <<'EOF'
case "$*" in
  '-u +%s') cat "$RG_TEST_STATE/now" ;;
  '-u +%Y%m%dT%H%M%SZ') cat "$RG_TEST_STATE/backup-timestamp" ;;
  '+%H')
    cat "$RG_TEST_STATE/local-hour"
    [ ! -e "$RG_TEST_STATE/date-hour-fail-after-output" ]
    ;;
  *'@'*'+%Y-%m-%dT%H:%M:%SZ') echo 2026-08-21T20:03:00Z ;;
  *'2026-08-22T00:00:00Z'*'+%s%N') echo 1787356800000000000 ;;
  *'2026-08-22T00:00:00Z'*'+%s') echo 1787356800 ;;
  *'2026-08-22T00:00:00.100000Z'*'+%s%N') echo 1787356800100000000 ;;
  *'2026-08-22T00:00:00.900000Z'*'+%s%N') echo 1787356800900000000 ;;
  *'2026-08-22T00:00:00.100000Z'*'+%s') echo 1787356800 ;;
  *'2026-08-22T00:00:00.900000Z'*'+%s') echo 1787356800 ;;
  *'2026-08-22T00:02:00Z'*'+%s%N') echo 1787356920000000000 ;;
  *'2026-08-22T00:02:00Z'*'+%s') echo 1787356920 ;;
  *'2026-08-22T00:01:00Z'*'+%s%N') echo 1787356860000000000 ;;
  *'2026-08-22T00:01:00Z'*'+%s') echo 1787356860 ;;
  *'2026-08-21T23:50:00Z'*'+%s%N') echo 1787356200000000000 ;;
  *'2026-08-21T23:50:00Z'*'+%s') echo 1787356200 ;;
  *'2026-08-22T00:04:00Z'*'+%s%N') echo 1787357040000000000 ;;
  *'2026-08-22T00:04:00Z'*'+%s') echo 1787357040 ;;
  *) exit 1 ;;
esac
EOF
  write_stub flock <<'EOF'
case "${1:-}" in
  -n)
    if [ -e "$RG_TEST_STATE/flock-fail-after-output" ]; then
      printf '%s\n' acquired
      exit 7
    fi
    if mkdir "$RG_TEST_STATE/resource-lock-held" 2>/dev/null; then
      printf '%s\n' "$PPID" > "$RG_TEST_STATE/resource-lock-held/owner"
      exit 0
    fi
    [ "$(cat "$RG_TEST_STATE/resource-lock-held/owner" 2>/dev/null || true)" = "$PPID" ]
    ;;
  -u)
    [ "$(cat "$RG_TEST_STATE/resource-lock-held/owner" 2>/dev/null || true)" = "$PPID" ] || exit 1
    rm -rf "$RG_TEST_STATE/resource-lock-held"
    ;;
  *) exit 2 ;;
esac
EOF
  write_stub readlink <<'EOF'
case "${1:-}" in
  "$RG_TEST_FD_ROOT"/[0-9]*)
    fd=${1##*/}
    [ -e "/dev/fd/$fd" ] || exit 1
    printf '%s\n' "$RG_LOCK_FILE"
    [ ! -e "$RG_TEST_STATE/readlink-fail-after-output" ]
    ;;
  *) exit 1 ;;
esac
EOF
  write_stub sleep <<'EOF'
printf 'sleep %s\n' "$*" >> "$RG_TEST_STATE/events"
exit 0
EOF
  write_stub stat <<'EOF'
path=${@: -1}
[ -e "$path" ] || [ -L "$path" ] || exit 1
if [ -d "$path" ]; then mode=$(/usr/bin/stat -f '%Lp' "$path"); else mode=$(/usr/bin/stat -f '%Lp' "$path"); fi
uid=$(/usr/bin/stat -f '%u' "$path")
gid=$(/usr/bin/stat -f '%g' "$path")
links=$(/usr/bin/stat -f '%l' "$path")
if [ -e "$RG_TEST_STATE/credential-wrong-gid" ] && [ "$path" = "$RG_CREDENTIAL_FILE" ]; then gid=$((gid + 1)); fi
if [ -e "$RG_TEST_STATE/backup-parent-wrong-gid" ] \
  && [ "$path" = "${RG_BACKUP_ROOT%/*}" ]; then gid=$((gid + 1)); fi
printf '%s|%s|%s|%s\n' "$mode" "$uid" "$gid" "$links"
EOF
  write_stub sha256 <<'EOF'
path=${@: -1}
[ -f "$path" ] || exit 1
digest=$(/usr/bin/shasum -a 256 "$path" | awk '{print $1}')
case "$(cat "$RG_TEST_STATE/sha-mode" 2>/dev/null || true)" in
  malformed) printf 'not-a-digest  %s\n' "$path" ;;
  wrong-path) printf '%s  %s.other\n' "$digest" "$path" ;;
  extra) printf '%s  %s\n%s  %s\n' "$digest" "$path" "$digest" "$path" ;;
  fail-after-output) printf '%s  %s\n' "$digest" "$path"; exit 1 ;;
  *) printf '%s  %s\n' "$digest" "$path" ;;
esac
EOF
  write_stub provision <<'EOF'
printf '%s\n' provision >> "$RG_TEST_STATE/events"
if [ -p "$RG_TEST_STATE/hold-ready" ] && mkdir "$RG_TEST_STATE/provision-owner" 2>/dev/null; then
  printf '%s\n' ready > "$RG_TEST_STATE/hold-ready"
  IFS= read -r _release < "$RG_TEST_STATE/hold-release"
fi
[ "${PROV_SKIP_CADDY:-0}" = 1 ] || { printf '%s\n' caddy-not-skipped >> "$RG_TEST_STATE/events"; exit 98; }
for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
  [ "$(cat "$RG_TEST_STATE/unit-$unit-enabled")" = static ] || printf '%s\n' disabled > "$RG_TEST_STATE/unit-$unit-enabled"
done
printf '%s\n' enabled > "$RG_TEST_STATE/unit-estrado-pjud.service-enabled"
for timer in legaltech-monitor.timer legaltech-resource-tracker.timer; do
  if [ ! -e "$RG_SYSTEMD_DIR/$timer" ]; then
    printf 'new %s\n' "$timer" > "$RG_SYSTEMD_DIR/$timer"
  fi
done
printf '%s\n' enabled > "$RG_TEST_STATE/unit-legaltech-monitor.timer-enabled"
printf '%s\n' enabled > "$RG_TEST_STATE/unit-legaltech-resource-tracker.timer-enabled"
if [ -f "$RG_TEST_STATE/hermes-enabled-after-provision" ]; then
  while read -r unit value extra; do
    [ -n "${unit:-}" ] && [ -z "${extra:-}" ] || exit 1
    printf '%s\n' "$value" > "$RG_TEST_STATE/user-unit-$unit-enabled"
  done < "$RG_TEST_STATE/hermes-enabled-after-provision"
fi
case "${RG_TEST_MUTATE:-none}" in
  all)
    printf 'changed api\n' >> "$RG_SYSTEMD_DIR/estrado-pjud.service"
    cp "$RG_REPO_DIR/ops/systemd/estrado-pjud-worker.service" \
      "$RG_SYSTEMD_DIR/estrado-pjud-worker.service"
    printf 'changed hermes\n' >> "$RG_SYSTEMD_DIR/user-1002.slice.d/50-legaltech-resource-limits.conf"
    cp "$RG_REPO_DIR/ops/systemd/legaltech-monitor.service" "$RG_SYSTEMD_DIR/legaltech-monitor.service"
    cp "$RG_REPO_DIR/ops/systemd/legaltech-resource-tracker.service" "$RG_SYSTEMD_DIR/legaltech-resource-tracker.service"
    ;;
  api) printf 'changed api\n' >> "$RG_SYSTEMD_DIR/estrado-pjud.service" ;;
  dropin) cp "$RG_REPO_DIR/ops/systemd/estrado-pjud-worker.service.d/xvfb.conf" \
    "$RG_SYSTEMD_DIR/estrado-pjud-worker.service.d/xvfb.conf" ;;
  hermes) printf 'changed hermes\n' >> "$RG_SYSTEMD_DIR/user-1002.slice.d/50-legaltech-resource-limits.conf" ;;
  monitors)
    cp "$RG_REPO_DIR/ops/systemd/legaltech-monitor.service" "$RG_SYSTEMD_DIR/legaltech-monitor.service"
    cp "$RG_REPO_DIR/ops/systemd/legaltech-resource-tracker.service" "$RG_SYSTEMD_DIR/legaltech-resource-tracker.service"
    ;;
esac
if [ -e "$RG_TEST_STATE/mutate-outside" ]; then printf 'outside changed\n' > "$RG_TEST_OUTSIDE"; fi
if [ -e "$RG_TEST_STATE/create-sysctl" ]; then printf 'new sysctl\n' > "$RG_SYSCTL_FILE"; fi
if [ -e "$RG_TEST_STATE/mutate-credential" ]; then printf 'changed protected config\n' > "$RG_CREDENTIAL_FILE"; fi
[ ! -e "$RG_TEST_STATE/provision-fail" ] || exit 1
EOF
  write_stub swap <<'EOF'
printf 'swap %s\n' "$1" >> "$RG_TEST_STATE/events"
case "$1" in
  apply|rollback)
    fd=${LEGALTECH_RESOURCE_LOCK_FD:-}
    case "$fd" in ''|*[!0-9]*) exit 96 ;; esac
    [ -e "/dev/fd/$fd" ] || exit 96
    [ "$(cat "$RG_TEST_STATE/resource-lock-held/owner" 2>/dev/null || true)" = "$PPID" ] || exit 96
    printf 'swap handoff %s\n' "$1" >> "$RG_TEST_STATE/events"
    ;;
esac
if [ "$1" = preflight ]; then
  [ ! -e "$RG_TEST_STATE/swap-preflight-fail" ] || exit 1
  if [ -e "$RG_TEST_STATE/swap-applied" ]; then printf '%s\n' managed; else printf '%s\n' clean; fi
  exit 0
fi
if [ "$1" = rollback-preflight ]; then
  [ ! -e "$RG_TEST_STATE/swap-rollback-preflight-fail" ] || exit 1
  if [ -e "$RG_TEST_STATE/swap-crash-state" ]; then
    cat "$RG_TEST_STATE/swap-crash-state"
  elif [ -e "$RG_TEST_STATE/swap-applied" ]; then
    printf '%s\n' managed-active
  elif [ -e "$RG_TEST_STATE/swap-partial-fstab" ]; then
    printf '%s\n' rollback-fstab
  elif [ -e "$RG_TEST_STATE/swap-partial-sysctl" ]; then
    printf '%s\n' rollback-sysctl
  elif [ -e "$RG_TEST_STATE/swap-partial-swapfile" ]; then
    printf '%s\n' rollback-swapfile
  elif [ -e "$RG_TEST_STATE/swap-partial-metadata" ]; then
    printf '%s\n' rollback-metadata
  else
    printf '%s\n' clean
  fi
  exit 0
fi
if [ "$1" = apply ] && [ -e "$RG_TEST_STATE/swap-crash-state" ]; then
  exit 1
fi
if [ "$1" = apply ] && [ -e "$RG_TEST_STATE/swap-apply-compensation-fail" ]; then
  rm -f "$RG_TEST_STATE/swap-applied"
  : > "$RG_TEST_STATE/swap-deactivated"
  : > "$RG_TEST_STATE/swap-partial-swapfile"
  : > "$RG_TEST_STATE/swap-partial-metadata"
  case "$(cat "$RG_TEST_STATE/swap-apply-compensation-fail")" in
    fstab)
      : > "$RG_TEST_STATE/swap-partial-fstab"
      : > "$RG_TEST_STATE/swap-partial-sysctl"
      ;;
    sysctl) : > "$RG_TEST_STATE/swap-partial-sysctl" ;;
    *) exit 97 ;;
  esac
  exit 1
fi
if [ "$1" = apply ] && [ -e "$RG_TEST_STATE/swap-apply-fail" ]; then exit 1; fi
if [ "$1" = apply ]; then : > "$RG_TEST_STATE/swap-applied"; fi
if [ "$1" = apply ] && [ -e "$RG_TEST_STATE/create-swap-metadata" ]; then
  printf '%s\n' 60 > "$RG_SWAPPINESS_METADATA_FILE"
  chmod 600 "$RG_SWAPPINESS_METADATA_FILE"
fi
if [ "$1" = verify ]; then
  [ ! -e "$RG_TEST_STATE/swap-verify-fail" ] || exit 1
  [ ! -e "$RG_TEST_STATE/swap-live-drift" ] || exit 1
  [ -e "$RG_TEST_STATE/swap-applied" ] || exit 1
fi
if [ "$1" = rollback ]; then
  if [ -e "$RG_TEST_STATE/swap-crash-state" ] \
    && [ -e "$RG_TEST_STATE/swap-crash-rollback-fail-once" ]; then
    rm -f "$RG_TEST_STATE/swap-crash-rollback-fail-once"
    exit 1
  fi
  if [ -e "$RG_TEST_STATE/swap-apply-compensation-fail" ]; then exit 1; fi
  if [ -e "$RG_TEST_STATE/swap-rollback-post-deactivate-fail" ]; then
    rm -f "$RG_TEST_STATE/swap-applied"
    : > "$RG_TEST_STATE/swap-deactivated"
    exit 1
  fi
  [ ! -e "$RG_TEST_STATE/swap-rollback-fail" ] || exit 1
  rm -f "$RG_TEST_STATE/swap-applied"
  rm -f "$RG_TEST_STATE/swap-deactivated"
  rm -f "$RG_TEST_STATE/swap-partial-fstab"
  rm -f "$RG_TEST_STATE/swap-partial-sysctl"
  rm -f "$RG_TEST_STATE/swap-partial-swapfile"
  rm -f "$RG_TEST_STATE/swap-partial-metadata"
  rm -f "$RG_TEST_STATE/swap-crash-state"
  rm -f "$RG_SWAPPINESS_METADATA_FILE"
fi
exit 0
EOF
  write_stub find <<'EOF'
/usr/bin/find "$@"
rc=$?
[ ! -e "$RG_TEST_STATE/find-fail-after-output" ] || exit 1
exit "$rc"
EOF
  write_stub chmod <<'EOF'
/bin/chmod "$@" || exit 1
target=${@: -1}
if [ "${target##*/}" = manifest.tsv ] && [ -f "$RG_TEST_STATE/corrupt-backup" ]; then
  case "$(cat "$RG_TEST_STATE/corrupt-backup")" in
    missing) /usr/bin/sed '$d' "$target" > "$target.tmp" && /bin/mv "$target.tmp" "$target" ;;
    duplicate) /usr/bin/sed -n '1p' "$target" >> "$target" ;;
    corrupt) /usr/bin/awk 'NR == 1 { sub(/entries\/[0-9][0-9][0-9][0-9]/, "entries/9999") } { print }' "$target" > "$target.tmp" && /bin/mv "$target.tmp" "$target" ;;
    metadata) /bin/chmod 0666 "${target%/*}/entries/0001" ;;
  esac
fi
if [ -e "$RG_TEST_STATE/corrupt-live-state" ]; then
  for candidate in "$@"; do
    if [ "${candidate##*/}" = unit-states.tsv ]; then
      /usr/bin/sed '$d' "$candidate" > "$candidate.tmp" && /bin/mv "$candidate.tmp" "$candidate"
    fi
  done
fi
EOF
  write_stub mkdir <<'EOF'
printf 'mkdir %s\n' "$*" >> "$RG_TEST_STATE/events"
/bin/mkdir "$@" || exit 1
if [ -e "$RG_TEST_STATE/audit-mkdir-mode" ]; then
  unsafe=0
  for path in "$@"; do
    case "$path" in --|-*) continue ;; esac
    [ -d "$path" ] || continue
    mode=$(/usr/bin/stat -f '%Lp' "$path") || exit 1
    printf 'mkdir-mode %s %s\n' "$path" "$mode" >> "$RG_TEST_STATE/events"
    permissions=$((8#$mode))
    if [ $((permissions & 8#0077)) -ne 0 ]; then
      printf 'mkdir-unsafe %s %s\n' "$path" "$mode" >> "$RG_TEST_STATE/events"
      : > "$path/attacker-entry"
      printf 'attacker-entry %s\n' "$path" >> "$RG_TEST_STATE/events"
      unsafe=1
    fi
  done
  [ "$unsafe" -eq 0 ] || exit 91
fi
EOF
  write_stub python <<'EOF'
if [ "${1:-}" = -c ] && [ "${3:-}" = --resource-guards-atomic-write ]; then
  target=${4:-}
  target_name=${target##*/}
  calls_file="$RG_TEST_STATE/durable-calls-$target_name"
  calls=0
  [ ! -f "$calls_file" ] || calls=$(cat "$calls_file")
  calls=$((calls + 1))
  printf '%s\n' "$calls" > "$calls_file"
  printf 'durable-write %s call=%s\n' "$target_name" "$calls" >> "$RG_TEST_STATE/events"
  if [ -f "$RG_TEST_STATE/durable-fail-boundary" ] \
    && [ "$(cat "$RG_TEST_STATE/durable-fail-target")" = "$target_name" ] \
    && [ "$(cat "$RG_TEST_STATE/durable-fail-call")" = "$calls" ]; then
    export RG_DURABLE_TEST_BOUNDARY
    RG_DURABLE_TEST_BOUNDARY=$(cat "$RG_TEST_STATE/durable-fail-boundary")
  fi
  writer_output=$(/usr/bin/python3 "$@")
  writer_rc=$?
  if [ -e "$RG_TEST_STATE/durable-bad-output" ]; then
    printf '%s\n' WRONG
    exit 0
  fi
  printf '%s\n' "$writer_output"
  if [ -e "$RG_TEST_STATE/durable-fail-after-output" ]; then exit 7; fi
  exit "$writer_rc"
fi
if [ "${1:-}" = -c ] && [ "${3:-}" = --resource-guards-fsync-tree ]; then
  printf '%s\n' 'durable-sync backup-tree call=1' >> "$RG_TEST_STATE/events"
  if [ -f "$RG_TEST_STATE/durable-fail-boundary" ] \
    && [ "$(cat "$RG_TEST_STATE/durable-fail-target")" = backup-tree ] \
    && [ "$(cat "$RG_TEST_STATE/durable-fail-call")" = 1 ]; then
    export RG_DURABLE_TEST_BOUNDARY
    RG_DURABLE_TEST_BOUNDARY=$(cat "$RG_TEST_STATE/durable-fail-boundary")
  fi
  /usr/bin/python3 "$@"
  exit $?
fi
printf 'python %s\n' "$*" >> "$RG_TEST_STATE/events"
case "$*" in
  *resource-tracker.py*) [ ! -e "$RG_TEST_STATE/tracker-fail" ] || exit 1 ;;
  *monitor.py*)
    [ ! -e "$RG_TEST_STATE/monitor-fail" ] || exit 1
    [ ! -e "$RG_TEST_STATE/postflight-fail" ] || : > "$RG_TEST_STATE/postflight-armed"
    ;;
esac
exit 0
EOF
  write_stub jq <<'EOF'
/usr/bin/jq "$@"
rc=$?
[ ! -e "$RG_TEST_STATE/jq-fail-after-output" ] || exit 1
exit "$rc"
EOF
  write_stub cp <<'EOF'
printf 'backup-copy %s\n' "${@: -1}" >> "$RG_TEST_STATE/events"
destination=${@: -1}
args=("$@")
source=${args[$(( ${#args[@]} - 2 ))]}
if [ -d "$source" ]; then
  /bin/mkdir -p "$destination"
  /bin/chmod 755 "$destination"
  for item in "$source"/*; do
    [ -f "$item" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do printf '%s\n' "$line"; done < "$item" > "$destination/${item##*/}"
  done
else
  while IFS= read -r line || [ -n "$line" ]; do printf '%s\n' "$line"; done < "$source" > "$destination"
  case "$source:$destination" in
    *:*/entries/0010) /bin/chmod 640 "$destination" ;;
    */entries/0010:*|*.env:*|*/entries/0011:*|*:*/entries/0011) /bin/chmod 600 "$destination" ;;
    *) /bin/chmod 644 "$destination" ;;
  esac
fi
if [ -e "$RG_TEST_STATE/sha-after-backup" ] && [ "${destination##*/}" = 0016 ]; then
  printf '%s\n' bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb > "$RG_TEST_STATE/git-sha"
fi
EOF
    cp -R "$BIN" "$TMP/stub-bin"
  fi

  TEST_UID=$(/usr/bin/id -u)
  TEST_GID=$(/usr/bin/id -g)
}

run_guard() {
  local monitor_unit
  for monitor_unit in legaltech-monitor.service legaltech-resource-tracker.service; do
    cp "$FAKE/systemd/$monitor_unit" "$FAKE/repo/ops/systemd/$monitor_unit"
  done
  case "${TEST_MUTATE:-none}" in
    all|monitors)
      for monitor_unit in legaltech-monitor.service legaltech-resource-tracker.service; do
        printf 'changed monitor definition\n' >> "$FAKE/repo/ops/systemd/$monitor_unit"
      done
      ;;
  esac
  case "${TEST_MUTATE:-none}" in
    all)
      printf 'changed worker source\n' >> "$FAKE/repo/ops/systemd/estrado-pjud-worker.service"
      ;;
    dropin)
      printf 'changed worker drop-in source\n' \
        >> "$FAKE/repo/ops/systemd/estrado-pjud-worker.service.d/xvfb.conf"
      ;;
  esac
  set +e
  OUT=$(env \
    RG_TEST_MODE=1 RG_TEST_STATE="$STATE" RG_TEST_OUTSIDE="$FAKE/outside" \
    RG_TEST_ROOT_UID="$TEST_UID" RG_TEST_ROOT_GID="$TEST_GID" \
    RG_REPO_DIR="$FAKE/repo" RG_SYSTEMD_DIR="$FAKE/systemd" RG_TMP_ROOT="$CASE_DIR/tmp" RG_DISK_PATH="$FAKE" RG_NULL_FILE="$CASE_DIR/null" \
    RG_CREDENTIAL_FILE="$FAKE/repo/estrado-pjud-service/.env" \
    RG_BACKUP_ROOT="${TEST_BACKUP_ROOT_OVERRIDE:-$FAKE/backups}" RG_MONITORING_DIR="$FAKE/monitoring" \
    RG_MONITOR_ENV_FILE="$FAKE/monitoring.env" RG_FSTAB_FILE="$FAKE/etc/fstab" \
    RG_SYSCTL_FILE="$FAKE/etc/sysctl.d/60-legaltech-swap.conf" \
    RG_SWAPPINESS_METADATA_FILE="$SWAPPINESS_METADATA_FILE" \
    RG_CADDYFILE="$FAKE/etc/caddy/Caddyfile" \
    RG_LOGROTATE_FILE="$FAKE/etc/logrotate.d/legaltech-resources" \
    RG_JURISTRACK_HEALTH_URL=https://juristrack.cl/ \
    RG_ESTRADO_HEALTH_URL=https://estrado.juristrack.cl/api/v1/health \
    RG_GIT_BIN="$BIN/git" RG_DF_BIN="$BIN/df" RG_FREE_BIN="$BIN/free" \
    RG_ID_BIN="$BIN/id" RG_PS_BIN="$BIN/ps" RG_SYSTEMCTL_BIN="$BIN/systemctl" \
    RG_CURL_BIN="$BIN/curl" RG_DATE_BIN="$BIN/date" RG_STAT_BIN="$BIN/stat" \
    RG_FLOCK_BIN="$BIN/flock" RG_READLINK_BIN="$BIN/readlink" \
    RG_LOCK_FILE="$FAKE/run/legaltech-resource-guards.lock" RG_FD_ROOT="$FAKE/fd" \
    RG_TEST_FD_ROOT="$FAKE/fd" \
    RG_SLEEP_BIN="$BIN/sleep" \
    RG_SHA256_BIN="$BIN/sha256" RG_FIND_BIN="$BIN/find" RG_CP_BIN="$BIN/cp" \
    RG_RM_BIN=/bin/rm RG_MKDIR_BIN="$BIN/mkdir" RG_CHMOD_BIN="$BIN/chmod" \
    RG_CHOWN_BIN=/usr/sbin/chown RG_MKTEMP_BIN=/usr/bin/mktemp RG_JQ_BIN="$BIN/jq" \
    RG_PROVISION_BIN="$BIN/provision" RG_SWAP_BIN="$BIN/swap" RG_PYTHON_BIN="$BIN/python" \
    RG_TEST_DISK_BYTES="${TEST_DISK_BYTES:-9663676416}" \
    RG_TEST_RAM_BYTES="${TEST_RAM_BYTES:-7516192768}" \
    RG_WORKER_FENCE_POLL_DELAY_SECONDS=0 \
    RG_WORKER_HEARTBEAT_POLL_DELAY_SECONDS="${TEST_HEARTBEAT_POLL_DELAY_OVERRIDE-0}" \
    RG_TEST_MUTATE="${TEST_MUTATE:-none}" \
    bash "$SCRIPT" "$@" 2>&1)
  RC=$?
  set -e
}

reset_preflight_state() {
  : > "$EVENTS"
  : > "$STATE/git-status"
  printf '%s\n' "$EXPECTED_SHA" > "$STATE/git-sha"
  printf '%s\n' 200 > "$STATE/juristrack-code"
  printf '%s\n' 200 > "$STATE/estrado-code"
  printf '%s\n' 0 > "$STATE/claim-count"
  : > "$STATE/claim-sequence"
  printf '%s\n' safe > "$STATE/heartbeat-pre"
  printf '%s\n' safe-new > "$STATE/heartbeat-post"
  rm -f "$STATE/heartbeat-calls" "$STATE/id-fail" "$STATE/ps-fail" \
    "$STATE/claim-after-first" "$STATE/date-hour-fail-after-output" \
    "$STATE/jq-fail-after-output"
  unset TEST_DISK_BYTES TEST_RAM_BYTES
}

configure_active_worker() {
  printf '%s\n' enabled > "$STATE/unit-estrado-pjud-worker.service-enabled"
  printf '%s\n' active > "$STATE/unit-estrado-pjud-worker.service-active"
  printf '%s\n' 4201 > "$STATE/unit-estrado-pjud-worker.service-main-pid"
  printf '%s\n' /legaltech.slice/estrado-pjud-worker.service \
    > "$STATE/unit-estrado-pjud-worker.service-control-group"
  printf '%s\n' estrado-pjud-worker.service > "$STATE/pid-4201-unit"
}

configure_active_legacy_worker() {
  configure_active_worker
  printf '%s\n' system.slice > "$STATE/unit-estrado-pjud-worker.service-slice"
  printf '%s\n' /system.slice/estrado-pjud-worker.service \
    > "$STATE/unit-estrado-pjud-worker.service-control-group"
  printf '%s\n' '[Unit]' 'Description=legacy worker' '[Service]' 'ExecStart=/bin/true' \
    > "$FAKE/systemd/estrado-pjud-worker.service"
  printf '%s\n' '[Unit]' 'Description=guarded worker' '[Service]' \
    'Slice=legaltech.slice' 'ExecStart=/bin/true' \
    > "$FAKE/repo/ops/systemd/estrado-pjud-worker.service"
}

configure_active_legacy_monitors() {
  local unit pid
  for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
    case "$unit" in legaltech-monitor.service) pid=4101 ;; *) pid=4102 ;; esac
    printf '%s\n' enabled > "$STATE/unit-$unit-enabled"
    printf '%s\n' active > "$STATE/unit-$unit-active"
    printf '%s\n' "$pid" > "$STATE/unit-$unit-main-pid"
    printf '/legaltech.slice/%s\n' "$unit" > "$STATE/unit-$unit-control-group"
    printf '%s\n' legaltech.slice > "$STATE/unit-$unit-slice"
    printf '%s\n' "$unit" > "$STATE/pid-$pid-unit"
    printf 'legacy monitor definition\n' >> "$FAKE/systemd/$unit"
  done
}

run_legacy_monitor_migration_regression() {
  local backup_path unit pid
  echo '== changed active legacy monitors migrate by exact unit and roll back exactly'
  setup
  configure_active_legacy_monitors
  TEST_MUTATE=monitors run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'legacy monitor migration apply succeeds' "$RC" 0
  for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
    case "$unit" in legaltech-monitor.service) pid=4101 ;; *) pid=4102 ;; esac
    expect_exact_count "migration stops exact $unit once" "systemctl stop $unit" 1
    expect_eq "migrated $unit is inactive before timer operation" \
      "$(cat "$STATE/unit-$unit-active")" inactive
    if [ ! -e "$STATE/pid-$pid-unit" ]; then
      ok "legacy PID $pid is absent"
    else
      bad "legacy PID $pid is absent"
    fi
    expect_before "legacy $unit stops before timers are started" \
      "systemctl stop $unit" 'systemctl start legaltech-monitor.timer legaltech-resource-tracker.timer'
    expect_before "old PID $pid is checked before timers are started" \
      "ps pid=$pid" 'systemctl start legaltech-monitor.timer legaltech-resource-tracker.timer'
  done
  backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
  expect_eq 'backup captures both legacy monitor runtime rows' \
    "$(wc -l < "$backup_path/monitor-runtime.tsv" 2>/dev/null | tr -d ' ')" 2

  setup
  configure_active_legacy_monitors
  : > "$STATE/health-after-first"
  TEST_MUTATE=monitors run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'later failure keeps apply failed after monitor rollback' "$RC" 1
  for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
    expect_eq "rollback restores $unit enabled" "$(cat "$STATE/unit-$unit-enabled")" enabled
    expect_eq "rollback restores $unit active" "$(cat "$STATE/unit-$unit-active")" active
    expect_exact_count "rollback restarts restored legacy $unit once" "systemctl restart $unit" 1
  done
  expect_contains 'legacy monitor rollback reports complete' "$OUT" 'ROLLBACK OK'
}

run_activity_preservation_regressions() {
  echo '== changed units preserve captured activity independently'
  setup
  TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'changed inactive worker apply succeeds' "$RC" 0
  expect_eq 'changed inactive worker remains inactive' \
    "$(cat "$STATE/unit-estrado-pjud-worker.service-active")" inactive
  expect_count 'changed inactive worker is never restarted' \
    'systemctl restart estrado-pjud-worker.service' 0
  expect_count 'changed inactive worker requires no heartbeat query' 'curl heartbeat' 0
  expect_count 'changed inactive worker requires no claims query' 'curl claims' 0

  setup
  configure_active_worker
  TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'changed active worker apply succeeds' "$RC" 0
  expect_exact_count 'changed active worker stops once' \
    'systemctl stop estrado-pjud-worker.service' 1
  expect_exact_count 'changed active worker starts once' \
    'systemctl start estrado-pjud-worker.service' 1
  expect_count 'changed active worker checks pre-stop and post-start heartbeat' \
    'curl heartbeat' 2
  expect_count 'changed active worker checks pre-stop, post-stop, and post-start claims' \
    'curl claims' 3
  expect_count 'changed active worker is never restarted' \
    'systemctl restart estrado-pjud-worker.service' 0

  setup
  printf '%s\n' inactive > "$STATE/unit-estrado-pjud.service-active"
  TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'changed inactive API apply succeeds' "$RC" 0
  expect_eq 'changed inactive API remains inactive' \
    "$(cat "$STATE/unit-estrado-pjud.service-active")" inactive
  expect_count 'changed inactive API is never restarted' 'systemctl restart estrado-pjud.service' 0

  setup
  printf '%s\n' active > "$STATE/user-unit-hermes-gateway.service-active"
  printf '%s\n' inactive > "$STATE/user-unit-hermes-dashboard.service-active"
  TEST_MUTATE=hermes run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'mixed Hermes activity apply succeeds' "$RC" 0
  expect_exact_count 'active Hermes gateway restarts individually' \
    'systemctl --user --machine=hermes@.host restart hermes-gateway.service' 1
  expect_count 'inactive Hermes dashboard receives no restart' \
    'restart hermes-dashboard.service' 0
  expect_count 'inactive Hermes dashboard receives no start' \
    'systemctl --user --machine=hermes@.host start hermes-dashboard.service' 0
  expect_eq 'inactive Hermes dashboard remains inactive' \
    "$(cat "$STATE/user-unit-hermes-dashboard.service-active")" inactive
}

configure_absent_timer_base() {
  local timer
  for timer in legaltech-monitor.timer legaltech-resource-tracker.timer; do
    rm -f "$FAKE/systemd/$timer"
    printf '%s\n' not-found > "$STATE/unit-$timer-enabled"
    printf '%s\n' inactive > "$STATE/unit-$timer-active"
  done
}

run_absent_timer_regressions() {
  local timer
  echo '== first rollout supports truly absent timer units and restores absence'
  setup
  configure_absent_timer_base
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'apply from absent timer base succeeds' "$RC" 0
  for timer in legaltech-monitor.timer legaltech-resource-tracker.timer; do
    if [ -f "$FAKE/systemd/$timer" ]; then ok "$timer is created"; else bad "$timer is created"; fi
    expect_eq "$timer becomes enabled" "$(cat "$STATE/unit-$timer-enabled")" enabled
    expect_eq "$timer becomes active" "$(cat "$STATE/unit-$timer-active")" active
  done

  setup
  configure_absent_timer_base
  : > "$STATE/health-after-first"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'later failure rolls first rollout back' "$RC" 1
  for timer in legaltech-monitor.timer legaltech-resource-tracker.timer; do
    if [ ! -e "$FAKE/systemd/$timer" ]; then ok "$timer file is restored absent"; else bad "$timer file is restored absent"; fi
    expect_eq "$timer enablement verifies not-found" "$(cat "$STATE/unit-$timer-enabled")" not-found
    expect_eq "$timer activity verifies inactive" "$(cat "$STATE/unit-$timer-active")" inactive
    expect_count "rollback never disables absent $timer" "systemctl disable $timer" 0
    expect_count "rollback never stops absent $timer" "systemctl stop $timer" 0
  done
  expect_contains 'absent timer rollback reports complete' "$OUT" 'ROLLBACK OK'

  setup
  configure_absent_timer_base
  printf '%s\n' active > "$STATE/unit-legaltech-monitor.timer-active"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'contradictory absent active timer refuses apply' "$RC" 1
  expect_count 'contradictory absent active timer runs no provision' provision 0
}

run_swappiness_namespace_regressions() {
  local backup_path
  echo '== resource guard manifest owns exact swappiness metadata namespace'
  setup
  : > "$STATE/create-swap-metadata"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'apply with managed swappiness metadata succeeds' "$RC" 0
  backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
  expect_eq 'manifest declares exact swappiness metadata path once' \
    "$(grep -cF "$SWAPPINESS_METADATA_FILE" "$backup_path/manifest.tsv" 2>/dev/null || true)" 1
  expect_eq 'manifest expands by exactly one managed path' \
    "$(wc -l < "$backup_path/manifest.tsv" | tr -d ' ')" 16

  setup
  printf '%s\n' 60 > "$SWAPPINESS_METADATA_FILE"
  chmod 0644 "$SWAPPINESS_METADATA_FILE"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'unsafe pre-existing swappiness metadata refuses apply' "$RC" 1
  expect_count 'unsafe swappiness metadata blocks before provision' provision 0

  setup
  : > "$STATE/create-swap-metadata"
  : > "$STATE/health-after-first"
  : > "$STATE/swap-rollback-fail"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'failed swap rollback leaves apply failed' "$RC" 1
  if [ -f "$SWAPPINESS_METADATA_FILE" ]; then ok 'failed swap rollback retains retry metadata'; else bad 'failed swap rollback retains retry metadata'; fi
  expect_contains 'failed metadata rollback is loud' "$OUT" 'ROLLBACK INCOMPLETO'
}

run_incomplete_rollback_retry_regression() {
  local failed_output backup_path retry_path invalid_path
  echo '== incomplete automatic rollback reports only its validated retry backup'
  setup
  : > "$STATE/create-swap-metadata"
  : > "$STATE/postflight-fail"
  : > "$STATE/swap-rollback-post-deactivate-fail"
  TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
  failed_output=$OUT
  backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)

  expect_eq 'post-deactivation rollback failure keeps apply failed' "$RC" 1
  if [ -e "$STATE/swap-deactivated" ] && [ ! -e "$STATE/swap-applied" ]; then
    ok 'failed rollback leaves the exact swap target deactivated'
  else
    bad 'failed rollback leaves the exact swap target deactivated'
  fi
  expect_contains 'incomplete rollback emits fixed retry diagnostic' "$failed_output" \
    'ROLLBACK INCOMPLETO: reintente con el BACKUP_DIR validado: '
  expect_contains 'retry diagnostic includes the exact validated backup' \
    "$failed_output" "$backup_path"
  expect_missing 'retry diagnostic exposes no service credential' \
    "$failed_output" "$SECRET_SENTINEL"
  retry_path=$(printf '%s\n' "$failed_output" \
    | sed -n 's/^ROLLBACK INCOMPLETO: reintente con el BACKUP_DIR validado: \(.*\)$/\1/p')
  expect_eq 'retry diagnostic yields exactly one validated backup path' \
    "$retry_path" "$backup_path"

  rm -f "$STATE/swap-rollback-post-deactivate-fail"
  run_guard rollback --backup-dir "$retry_path"
  expect_eq 'retry command from the diagnostic converges' "$RC" 0
  expect_contains 'retry command confirms the same backup' "$OUT" \
    "ROLLBACK OK: $backup_path"
  if [ ! -e "$STATE/swap-deactivated" ] && [ ! -e "$SWAPPINESS_METADATA_FILE" ]; then
    ok 'retry command removes the remaining validated swap artifacts'
  else
    bad 'retry command removes the remaining validated swap artifacts'
  fi

  invalid_path="$FAKE/external-$SECRET_SENTINEL"
  run_guard rollback --backup-dir "$invalid_path"
  expect_eq 'external unvalidated backup is rejected' "$RC" 1
  expect_missing 'external unvalidated path is never reflected' "$OUT" "$invalid_path"
  expect_missing 'external unvalidated path leaks no sentinel' "$OUT" "$SECRET_SENTINEL"
}

run_apply_compensation_retry_regressions() {
  local scenario failed_output backup_path retry_path expected_fstab
  echo '== failed swap apply compensation remains diagnosable and retryable through the orchestrator'
  for scenario in fstab sysctl; do
    setup
    printf '%s\n' "$scenario" > "$STATE/swap-apply-compensation-fail"
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    failed_output=$OUT
    backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)

    expect_eq "$scenario compensation failure keeps resource apply failed" "$RC" 1
    if [ -e "$STATE/swap-deactivated" ]; then
      ok "$scenario compensation failure leaves the exact swap target inactive"
    else
      bad "$scenario compensation failure leaves the exact swap target inactive"
    fi
    case "$scenario" in
      fstab) expected_fstab=present ;;
      sysctl) expected_fstab=absent ;;
    esac
    if { [ "$expected_fstab" = present ] && [ -e "$STATE/swap-partial-fstab" ]; } \
      || { [ "$expected_fstab" = absent ] && [ ! -e "$STATE/swap-partial-fstab" ]; }; then
      ok "$scenario compensation preserves the expected fstab phase"
    else
      bad "$scenario compensation preserves the expected fstab phase"
    fi
    if [ -e "$STATE/swap-partial-sysctl" ] \
      && [ -e "$STATE/swap-partial-swapfile" ] \
      && [ -e "$STATE/swap-partial-metadata" ]; then
      ok "$scenario compensation does not advance past its failed cleanup boundary"
    else
      bad "$scenario compensation does not advance past its failed cleanup boundary"
    fi
    expect_contains "$scenario automatic rollback emits the fixed incomplete diagnostic" \
      "$failed_output" 'ROLLBACK INCOMPLETO: reintente con el BACKUP_DIR validado: '
    expect_contains "$scenario diagnostic includes the exact validated backup" \
      "$failed_output" "$backup_path"
    expect_missing "$scenario diagnostic exposes no service credential" \
      "$failed_output" "$SECRET_SENTINEL"
    retry_path=$(printf '%s\n' "$failed_output" \
      | sed -n 's/^ROLLBACK INCOMPLETO: reintente con el BACKUP_DIR validado: \(.*\)$/\1/p')
    expect_eq "$scenario diagnostic yields one exact BACKUP_DIR" "$retry_path" "$backup_path"

    rm -f "$STATE/swap-apply-compensation-fail"
    run_guard rollback --backup-dir "$retry_path"
    expect_eq "$scenario exact BACKUP_DIR retry converges" "$RC" 0
    expect_contains "$scenario retry confirms the same validated backup" \
      "$OUT" "ROLLBACK OK: $backup_path"
    if [ ! -e "$STATE/swap-deactivated" ] \
      && [ ! -e "$STATE/swap-partial-fstab" ] \
      && [ ! -e "$STATE/swap-partial-sysctl" ] \
      && [ ! -e "$STATE/swap-partial-swapfile" ] \
      && [ ! -e "$STATE/swap-partial-metadata" ]; then
      ok "$scenario exact BACKUP_DIR retry removes the remaining validated swap state"
    else
      bad "$scenario exact BACKUP_DIR retry removes the remaining validated swap state"
    fi
  done
}

run_api_enablement_regressions() {
  echo '== successful apply preserves exact API enablement without changing activity'

  setup
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'enabled active API apply succeeds' "$RC" 0
  expect_eq 'enabled API remains enabled' \
    "$(cat "$STATE/unit-estrado-pjud.service-enabled")" enabled
  expect_eq 'enabled API remains active' \
    "$(cat "$STATE/unit-estrado-pjud.service-active")" active
  expect_count 'enabled API is never stopped' 'systemctl stop estrado-pjud.service' 0

  setup
  printf '%s\n' disabled > "$STATE/unit-estrado-pjud.service-enabled"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'disabled active API apply succeeds' "$RC" 0
  expect_eq 'disabled API is reconciled back to disabled' \
    "$(cat "$STATE/unit-estrado-pjud.service-enabled")" disabled
  expect_eq 'disabled API remains active throughout' \
    "$(cat "$STATE/unit-estrado-pjud.service-active")" active
  expect_exact_count 'disabled API is reconciled once' \
    'systemctl disable estrado-pjud.service' 1
  expect_count 'disabled active API is never stopped' \
    'systemctl stop estrado-pjud.service' 0
  expect_count 'enablement reconciliation never restarts API' \
    'systemctl restart estrado-pjud.service' 0

  setup
  printf '%s\n' disabled > "$STATE/unit-estrado-pjud.service-enabled"
  printf '%s\n' 'disable estrado-pjud.service' > "$STATE/fail-command"
  : > "$STATE/fail-command-once"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'API enablement command failure fails apply' "$RC" 1
  expect_eq 'command failure rollback restores API disabled' \
    "$(cat "$STATE/unit-estrado-pjud.service-enabled")" disabled
  expect_eq 'command failure rollback preserves API active' \
    "$(cat "$STATE/unit-estrado-pjud.service-active")" active
  expect_eq 'command failure invokes transaction rollback once' \
    "$(printf '%s\n' "$OUT" | grep -cF 'ROLLBACK OK' || true)" 1
  expect_contains 'command failure rollback completes' "$OUT" 'ROLLBACK OK'

  setup
  printf '%s\n' disabled > "$STATE/unit-estrado-pjud.service-enabled"
  : > "$STATE/ignore-first-api-disable"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'post-reconciliation mismatch fails apply' "$RC" 1
  expect_eq 'mismatch rollback restores API disabled' \
    "$(cat "$STATE/unit-estrado-pjud.service-enabled")" disabled
  expect_eq 'mismatch rollback preserves API active' \
    "$(cat "$STATE/unit-estrado-pjud.service-active")" active
  expect_eq 'mismatch invokes transaction rollback once' \
    "$(printf '%s\n' "$OUT" | grep -cF 'ROLLBACK OK' || true)" 1
  expect_contains 'mismatch rollback completes' "$OUT" 'ROLLBACK OK'

  setup
  printf '%s\n' 'enabled enabled' > "$STATE/api-is-enabled-output"
  printf '%s\n' 0 > "$STATE/api-is-enabled-status"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'ambiguous API enablement output fails closed' "$RC" 1
  expect_count 'ambiguous enablement blocks before provision' provision 0

  setup
  printf '%s\n' disabled > "$STATE/api-is-enabled-output"
  printf '%s\n' 0 > "$STATE/api-is-enabled-status"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'valid-looking API enablement with wrong status fails closed' "$RC" 1
  expect_count 'wrong enablement status blocks before provision' provision 0
}

write_safe_worker_env() {
  printf 'SUPABASE_URL=https://db.invalid\nSUPABASE_SERVICE_KEY=%s\nWORKER_ID=worker-1\nPJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false\nOJV_PROXY_URL=https://proxy.invalid\n' \
    "$SECRET_SENTINEL" > "$FAKE/repo/estrado-pjud-service/.env"
  chmod 640 "$FAKE/repo/estrado-pjud-service/.env"
}

configure_worker_precondition_scenario() {
  local scenario=$1 env_file="$FAKE/repo/estrado-pjud-service/.env"
  case "$scenario" in
    before-window) printf '%s\n' 19 > "$STATE/local-hour" ;;
    after-window) printf '%s\n' 04 > "$STATE/local-hour" ;;
    hour-missing) : > "$STATE/local-hour" ;;
    hour-multiline) printf '20\n21\n' > "$STATE/local-hour" ;;
    hour-nondecimal) printf '%s\n' xx > "$STATE/local-hour" ;;
    hour-producer-fail) : > "$STATE/date-hour-fail-after-output" ;;
    worker-missing) /usr/bin/sed '/^WORKER_ID=/d' "$env_file" > "$env_file.next"; /bin/mv "$env_file.next" "$env_file" ;;
    worker-duplicate) printf '%s\n' 'WORKER_ID=worker-2' >> "$env_file" ;;
    worker-empty) /usr/bin/sed 's/^WORKER_ID=.*/WORKER_ID=/' "$env_file" > "$env_file.next"; /bin/mv "$env_file.next" "$env_file" ;;
    worker-invalid) /usr/bin/sed 's/^WORKER_ID=.*/WORKER_ID=worker secret/' "$env_file" > "$env_file.next"; /bin/mv "$env_file.next" "$env_file" ;;
    override-missing) /usr/bin/sed '/^PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=/d' "$env_file" > "$env_file.next"; /bin/mv "$env_file.next" "$env_file" ;;
    override-duplicate) printf '%s\n' 'PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false' >> "$env_file" ;;
    override-true) /usr/bin/sed 's/^PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=.*/PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=true/' "$env_file" > "$env_file.next"; /bin/mv "$env_file.next" "$env_file" ;;
    override-malformed) /usr/bin/sed 's/^PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=.*/PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=FALSE/' "$env_file" > "$env_file.next"; /bin/mv "$env_file.next" "$env_file" ;;
    validation-once-true) printf '%s\n' 'PJUD_OFF_HOURS_VALIDATION_ONCE=true' >> "$env_file" ;;
    validation-once-duplicate) printf '%s\n' 'PJUD_OFF_HOURS_VALIDATION_ONCE=false' 'PJUD_OFF_HOURS_VALIDATION_ONCE=false' >> "$env_file" ;;
    validation-once-malformed) printf '%s\n' 'PJUD_OFF_HOURS_VALIDATION_ONCE=FALSE' >> "$env_file" ;;
    heartbeat-wrong-worker) printf '%s\n' wrong-worker > "$STATE/heartbeat-pre" ;;
    heartbeat-zero) printf '%s\n' zero-rows > "$STATE/heartbeat-pre" ;;
    heartbeat-multiple) printf '%s\n' multiple > "$STATE/heartbeat-pre" ;;
    heartbeat-stale) printf '%s\n' stale > "$STATE/heartbeat-pre" ;;
    heartbeat-future) printf '%s\n' future > "$STATE/heartbeat-pre" ;;
    heartbeat-running) printf '%s\n' running > "$STATE/heartbeat-pre" ;;
    heartbeat-stopped) printf '%s\n' stopped > "$STATE/heartbeat-pre" ;;
    heartbeat-paused) printf '%s\n' paused > "$STATE/heartbeat-pre" ;;
    heartbeat-unknown) printf '%s\n' unknown > "$STATE/heartbeat-pre" ;;
    metadata-missing) printf '%s\n' missing-metadata > "$STATE/heartbeat-pre" ;;
    metadata-malformed) printf '%s\n' malformed-metadata > "$STATE/heartbeat-pre" ;;
    metadata-override) printf '%s\n' override-true > "$STATE/heartbeat-pre" ;;
    proxy-paused) printf '%s\n' proxy-paused > "$STATE/heartbeat-pre" ;;
    proxy-unavailable) printf '%s\n' proxy-unavailable > "$STATE/heartbeat-pre" ;;
    proxy-telemetry) printf '%s\n' telemetry-unavailable > "$STATE/heartbeat-pre" ;;
    heartbeat-producer-fail) printf '%s\n' output-then-fail > "$STATE/heartbeat-pre" ;;
    jq-producer-fail) : > "$STATE/jq-fail-after-output" ;;
    claims-producer-fail) printf '%s\n' output-then-fail > "$STATE/claim-count" ;;
    claims-active) printf '%s\n' 1 > "$STATE/claim-count" ;;
    *) return 1 ;;
  esac
}

run_worker_post_start_wait_regressions() {
  local poll
  local -a heartbeat_sequence=(safe)
  echo '== post-start wait spans a real heartbeat interval and accepts subsecond-new idle state'
  setup
  configure_active_worker
  for ((poll = 0; poll < 13; poll++)); do heartbeat_sequence+=(starting-new); done
  heartbeat_sequence+=(safe-new)
  printf '%s\n' "${heartbeat_sequence[@]}" > "$STATE/heartbeat-sequence"
  printf '%s\n' 0 0 0 > "$STATE/claim-sequence"
  TEST_HEARTBEAT_POLL_DELAY_OVERRIDE='' TEST_MUTATE=dropin \
    run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'delayed idle heartbeat after thirteen starting polls succeeds' "$RC" 0
  expect_exact_count 'delayed idle heartbeat exercises thirteen production-delay polls' 'sleep 5' 13
  expect_count 'production-delay coverage performs no zero-delay heartbeat polls' 'sleep 0' 0

  setup
  configure_active_worker
  printf '%s\n' safe-subsecond > "$STATE/heartbeat-pre"
  printf '%s\n' safe-new-subsecond > "$STATE/heartbeat-post"
  printf '%s\n' 0 0 0 > "$STATE/claim-sequence"
  TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'strictly newer idle heartbeat in the same second succeeds' "$RC" 0
}

run_worker_fence_regressions() {
  local scenario rollback_count backup_path
  local -a unsafe_scenarios=(
    before-window after-window hour-missing hour-multiline hour-nondecimal hour-producer-fail
    worker-missing worker-duplicate worker-empty worker-invalid
    override-missing override-duplicate override-true override-malformed
    validation-once-true validation-once-duplicate validation-once-malformed
    heartbeat-wrong-worker heartbeat-zero heartbeat-multiple heartbeat-stale heartbeat-future
    heartbeat-running heartbeat-stopped heartbeat-paused heartbeat-unknown
    metadata-missing metadata-malformed metadata-override
    proxy-paused proxy-unavailable proxy-telemetry heartbeat-producer-fail jq-producer-fail
    claims-producer-fail claims-active
  )

  echo '== changed active worker refuses every unsafe fence precondition before stop or provision'
  for scenario in "${unsafe_scenarios[@]}"; do
    setup
    configure_active_worker
    configure_worker_precondition_scenario "$scenario"
    TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$scenario refuses changed-active apply" "$RC" 1
    expect_exact_count "$scenario performs no worker stop" \
      'systemctl stop estrado-pjud-worker.service' 0
    expect_count "$scenario performs no provision" provision 0
    expect_missing "$scenario diagnostic hides service credential" "$OUT" "$SECRET_SENTINEL"
    expect_missing "$scenario diagnostic hides proxy URL" "$OUT" 'proxy.invalid'
    expect_missing "$scenario diagnostic hides heartbeat body" "$OUT" 'last_heartbeat_at'
    expect_missing "$scenario diagnostic hides proxy reason detail" "$OUT" 'telemetry_unavailable'
  done

  echo '== pre-stop heartbeat permits historical mint attempts but post-start requires zero'
  setup
  configure_active_worker
  printf '%s\n' safe-historical-mint > "$STATE/heartbeat-pre"
  printf '%s\n' 0 0 0 > "$STATE/claim-sequence"
  TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'historical pre-stop mint attempts remain safe' "$RC" 0

  run_worker_post_start_wait_regressions

  echo '== changed active worker follows the stop-drain-start transaction exactly'
  setup
  configure_active_worker
  printf '%s\n' 0 1 0 0 > "$STATE/claim-sequence"
  TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'safe changed-active worker apply succeeds' "$RC" 0
  expect_last_before 'backup completes before immutable SHA recheck' backup-copy 'git rev-parse HEAD'
  expect_last_before 'SHA recheck precedes safe pre-stop heartbeat' 'git rev-parse HEAD' 'curl heartbeat'
  expect_before 'pre-stop heartbeat precedes zero-claim check' 'curl heartbeat' 'curl claims'
  expect_before 'pre-stop zero claims precede worker stop' 'curl claims' 'systemctl stop estrado-pjud-worker.service'
  expect_last_before 'worker stop precedes inactive verification' 'systemctl stop estrado-pjud-worker.service' 'systemctl is-active estrado-pjud-worker.service'
  expect_before 'worker stop precedes bounded post-stop drain' 'systemctl stop estrado-pjud-worker.service' 'sleep 0'
  expect_before 'bounded drain completes before provision' 'sleep 0' provision
  expect_before 'provision precedes daemon reload' provision 'systemctl daemon-reload'
  expect_before 'daemon reload precedes worker start' 'systemctl daemon-reload' 'systemctl start estrado-pjud-worker.service'
  expect_last_before 'worker start precedes post-start heartbeat' 'systemctl start estrado-pjud-worker.service' 'curl heartbeat'
  expect_last_before 'post-start heartbeat precedes final zero claims' 'curl heartbeat' 'curl claims'
  expect_last_before 'final zero claims precede postflight' 'curl claims' 'systemctl show legaltech.slice'
  expect_exact_count 'worker is stopped exactly once' 'systemctl stop estrado-pjud-worker.service' 1
  expect_exact_count 'replacement worker is started exactly once' 'systemctl start estrado-pjud-worker.service' 1
  expect_count 'worker is never restarted' 'systemctl restart estrado-pjud-worker.service' 0
  expect_contains 'heartbeat query filters exact encoded worker identity' "$(cat "$EVENTS")" \
    'sync_worker_heartbeats?worker_id=eq.worker-1&select=status,last_heartbeat_at,metadata'
  backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
  expect_eq 'durable worker-stop marker is recorded exactly once' \
    "$(grep -cxF worker-stop "$backup_path/changes" 2>/dev/null || true)" 1

  echo '== inactive and unchanged workers execute no fence operations'
  setup
  TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'changed inactive worker apply succeeds' "$RC" 0
  expect_count 'changed inactive worker performs no protected heartbeat query' 'curl heartbeat' 0
  expect_count 'changed inactive worker performs no protected claims query' 'curl claims' 0
  expect_count 'changed inactive worker performs no worker stop' 'stop estrado-pjud-worker.service' 0
  expect_count 'changed inactive worker performs no worker start' 'start estrado-pjud-worker.service' 0

  setup
  configure_active_worker
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'unchanged active worker apply succeeds' "$RC" 0
  expect_count 'unchanged active worker performs no protected heartbeat query' 'curl heartbeat' 0
  expect_count 'unchanged active worker performs no protected claims query' 'curl claims' 0
  expect_count 'unchanged active worker performs no worker stop' 'stop estrado-pjud-worker.service' 0
  expect_count 'unchanged active worker performs no worker start' 'start estrado-pjud-worker.service' 0

  echo '== persistent post-stop claims roll back once and restore only with start'
  setup
  configure_active_worker
  printf '%s\n' 0 1 1 1 1 1 > "$STATE/claim-sequence"
  TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'persistent drain timeout fails apply' "$RC" 1
  expect_count 'persistent drain timeout never provisions' provision 0
  expect_eq 'persistent drain timeout invokes exactly one rollback' \
    "$(printf '%s\n' "$OUT" | grep -cF 'ROLLBACK OK' || true)" 1
  expect_exact_count 'rollback starts restored worker exactly once' \
    'systemctl start estrado-pjud-worker.service' 1
  expect_count 'rollback never restarts worker' 'systemctl restart estrado-pjud-worker.service' 0

  echo '== worker fence failures each trigger one rollback and no paid action'
  for scenario in stop-failure inactive-mismatch residual-runtime start-failure wrong-cgroup missing-new-heartbeat mint-attempt new-claim; do
    setup
    configure_active_worker
    printf '%s\n' 0 0 0 > "$STATE/claim-sequence"
    case "$scenario" in
      stop-failure)
        printf '%s\n' 'stop estrado-pjud-worker.service' > "$STATE/fail-command"
        : > "$STATE/fail-command-once"
        ;;
      inactive-mismatch) : > "$STATE/worker-stop-keeps-active" ;;
      residual-runtime) : > "$STATE/worker-residual-runtime" ;;
      start-failure)
        printf '%s\n' 'start estrado-pjud-worker.service' > "$STATE/fail-command"
        : > "$STATE/fail-command-once"
        ;;
      wrong-cgroup) : > "$STATE/worker-wrong-start-cgroup" ;;
      missing-new-heartbeat) printf '%s\n' safe > "$STATE/heartbeat-post" ;;
      mint-attempt) printf '%s\n' mint-nonzero > "$STATE/heartbeat-post" ;;
      new-claim) printf '%s\n' 0 0 1 > "$STATE/claim-sequence" ;;
    esac
    TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$scenario fails changed-active apply" "$RC" 1
    rollback_count=$(printf '%s\n' "$OUT" | grep -Ec 'ROLLBACK (OK|INCOMPLETO)' || true)
    expect_eq "$scenario triggers exactly one rollback result" "$rollback_count" 1
    expect_count "$scenario rollback never restarts worker" 'systemctl restart estrado-pjud-worker.service' 0
    if [ "$scenario" = missing-new-heartbeat ]; then
      expect_exact_count 'missing heartbeat uses the bounded 75-second wait budget' 'sleep 0' 15
      expect_count 'missing heartbeat never sleeps the production delay in tests' 'sleep 5' 0
    fi
    for forbidden in '/api/v1/sync' '/proxy' '/session/mint' '/retry'; do
      expect_missing "$scenario emits no $forbidden action" "$(cat "$EVENTS")" "$forbidden"
    done
  done
}

run_worker_pre_target_cgroup_regressions() {
  local backup_path
  echo '== active worker migrates from its captured old runtime identity to the target cgroup'
  setup
  configure_active_legacy_worker
  printf '%s\n' 0 0 0 > "$STATE/claim-sequence"
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'legacy system.slice worker apply succeeds' "$RC" 0
  expect_before 'legacy runtime is captured before worker stop' \
    'systemctl show estrado-pjud-worker.service --property=ActiveState' \
    'systemctl stop estrado-pjud-worker.service'
  expect_before 'legacy worker is proven gone before provision' \
    'ps pid=4201' provision
  expect_eq 'replacement worker runs only in target cgroup' \
    "$(cat "$STATE/unit-estrado-pjud-worker.service-control-group")" \
    /legaltech.slice/estrado-pjud-worker.service
  backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
  expect_eq 'backup records one exact legacy worker runtime row' \
    "$(wc -l < "$backup_path/worker-runtime.tsv" 2>/dev/null | tr -d ' ')" 1
  expect_contains 'backup records the old system slice identity' \
    "$(cat "$backup_path/worker-runtime.tsv" 2>/dev/null)" \
    $'estrado-pjud-worker.service\tactive\t4201\t/system.slice/estrado-pjud-worker.service\tsystem.slice'

  echo '== rollback restores and proves the captured old worker identity'
  setup
  configure_active_legacy_worker
  printf '%s\n' 0 0 0 > "$STATE/claim-sequence"
  : > "$STATE/health-after-first"
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'later failure keeps legacy-worker apply failed' "$RC" 1
  expect_count 'legacy-worker rollback happens only after provisioning the target' provision 1
  expect_contains 'legacy-worker rollback completes' "$OUT" 'ROLLBACK OK'
  expect_eq 'rollback restores legacy effective slice' \
    "$(cat "$STATE/unit-estrado-pjud-worker.service-slice")" system.slice
  expect_eq 'rollback restores legacy cgroup identity' \
    "$(cat "$STATE/unit-estrado-pjud-worker.service-control-group")" \
    /system.slice/estrado-pjud-worker.service

  echo '== captured old identity is exact and inactive legacy paths remain query-free'
  setup
  configure_active_legacy_worker
  printf '%s\n' /custom.slice/estrado-pjud-worker.service \
    > "$STATE/unit-estrado-pjud-worker.service-control-group"
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'old cgroup inconsistent with effective Slice fails closed' "$RC" 1
  expect_count 'ambiguous old identity stops no worker' 'stop estrado-pjud-worker.service' 0
  expect_count 'ambiguous old identity performs no provision' provision 0

  setup
  configure_active_legacy_worker
  printf '%s\n' inactive > "$STATE/unit-estrado-pjud-worker.service-active"
  printf '%s\n' 0 > "$STATE/unit-estrado-pjud-worker.service-main-pid"
  : > "$STATE/unit-estrado-pjud-worker.service-control-group"
  rm -f "$STATE/pid-4201-unit"
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'inactive legacy worker migrates without activation' "$RC" 0
  expect_count 'inactive legacy worker performs no protected heartbeat query' 'curl heartbeat' 0
  expect_count 'inactive legacy worker performs no protected claims query' 'curl claims' 0
  expect_count 'inactive legacy worker is never stopped' 'stop estrado-pjud-worker.service' 0
  expect_count 'inactive legacy worker is never started' 'start estrado-pjud-worker.service' 0

  setup
  : > "$STATE/postflight-fail"
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'later failure keeps inactive-worker apply failed' "$RC" 1
  expect_contains 'rollback proves and restores captured inactive worker identity' \
    "$OUT" 'ROLLBACK OK'
  expect_eq 'rollback leaves captured inactive worker without a cgroup' \
    "$(cat "$STATE/unit-estrado-pjud-worker.service-active")|$(cat "$STATE/unit-estrado-pjud-worker.service-main-pid")|$(cat "$STATE/unit-estrado-pjud-worker.service-control-group")|$(cat "$STATE/unit-estrado-pjud-worker.service-slice")" \
    'inactive|0||legaltech.slice'

  setup
  configure_active_legacy_worker
  printf '%s\n' 0 0 0 > "$STATE/claim-sequence"
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
  printf '%s\t%s\t%s\t%s\t%s\n' estrado-pjud-worker.service active 4201 \
    /custom.slice/estrado-pjud-worker.service custom.slice \
    > "$backup_path/worker-runtime.tsv"
  : > "$EVENTS"
  run_guard rollback --backup-dir "$backup_path"
  expect_eq 'corrupt captured worker identity rejects standalone rollback' "$RC" 1
  expect_count 'corrupt worker metadata performs no swap rollback' 'swap rollback' 0
  expect_count 'corrupt worker metadata performs no daemon reload' 'systemctl daemon-reload' 0
}

run_mutator_lock_regressions() {
  local first_pid backup_path backups_before provision_before
  echo '== a held resource apply excludes every other host mutator'
  setup
  mkfifo "$STATE/hold-ready" "$STATE/hold-release"
  (
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    printf '%s\n' "$RC" > "$STATE/first-rc"
    printf '%s' "$OUT" > "$STATE/first-out"
  ) &
  first_pid=$!
  IFS= read -r _ready < "$STATE/hold-ready"
  backups_before=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')
  provision_before=$(grep -cxF provision "$EVENTS" 2>/dev/null || true)
  printf '%s\n' 20240309T160001Z > "$STATE/backup-timestamp"
  TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'second apply is rejected while the first owns the host lock' "$RC" 1
  expect_contains 'second apply reports fixed lock contention' "$OUT" 'another resource mutation is already in progress'
  expect_eq 'contended apply creates no backup' \
    "$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" "$backups_before"
  expect_eq 'contended apply never provisions' \
    "$(grep -cxF provision "$EVENTS" 2>/dev/null || true)" "$provision_before"

  backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
  run_guard rollback --backup-dir "$backup_path"
  expect_eq 'rollback is rejected while apply owns the host lock' "$RC" 1
  expect_contains 'contended rollback reports fixed lock contention' "$OUT" 'another resource mutation is already in progress'
  expect_count 'contended rollback does not delegate swap rollback' 'swap rollback' 0

  printf '%s\n' release > "$STATE/hold-release"
  wait "$first_pid"
  if [ "$(cat "$STATE/first-rc")" = 0 ]; then
    ok 'first owner retains the lock through its complete apply'
  else
    bad "first owner retains the lock through its complete apply (output=$(cat "$STATE/first-out"); events=$(tail -n 20 "$EVENTS" | tr '\n' ';'))"
  fi

  rm -f "$STATE/hold-ready" "$STATE/hold-release"
  printf '%s\n' 20240309T160002Z > "$STATE/backup-timestamp"
  TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'normal exit leaves no stale host lock' "$RC" 0

  echo '== unsafe lock identity and producer failures block before every mutation'
  for scenario in unsafe-mode readlink-failure flock-failure; do
    setup
    case "$scenario" in
      unsafe-mode)
        : > "$FAKE/run/legaltech-resource-guards.lock"
        chmod 0644 "$FAKE/run/legaltech-resource-guards.lock"
        ;;
      readlink-failure) : > "$STATE/readlink-fail-after-output" ;;
      flock-failure) : > "$STATE/flock-fail-after-output" ;;
    esac
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$scenario lock gate fails closed" "$RC" 1
    expect_count "$scenario creates no backup entries" backup-copy 0
    expect_count "$scenario performs no provision" provision 0
    expect_count "$scenario performs no swap mutation" 'swap apply' 0
  done
}

run_runtime_cgroup_regressions() {
  local scenario key value mutate rollback_count
  echo '== exact runtime cgroups fail closed and roll back once'
  for scenario in legaltech-slice api worker hermes-slice hermes-gateway hermes-dashboard; do
    setup
    mutate=api
    case "$scenario" in
      legaltech-slice)
        key=legaltech.slice:ControlGroup value=/system.slice/legaltech.slice ;;
      api)
        key=estrado-pjud.service:ControlGroup value=/system.slice/estrado-pjud.service ;;
      worker)
        configure_active_worker
        : > "$STATE/worker-wrong-start-cgroup"
        key='' value=''
        mutate=dropin ;;
      hermes-slice)
        key=user-1002.slice:ControlGroup value=/user.slice/user-9999.slice ;;
      hermes-gateway)
        key=hermes-gateway.service:ControlGroup
        value=/user.slice/user-1002.slice/user@1002.service/app.slice/wrong.service
        mutate=hermes ;;
      hermes-dashboard)
        key=hermes-dashboard.service:ControlGroup value=/system.slice/hermes-dashboard.service
        mutate=hermes ;;
    esac
    if [ -n "$key" ]; then
      printf '%s\n' "$key" > "$STATE/property-bad"
      printf '%s\n' "$value" > "$STATE/property-bad-value"
    fi
    TEST_MUTATE=$mutate run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$scenario wrong valid cgroup refuses apply" "$RC" 1
    rollback_count=$(grep -c -x -F 'swap rollback' "$EVENTS" 2>/dev/null || true)
    expect_eq "$scenario mismatch triggers one rollback" "$rollback_count" 1
  done

  echo '== runtime parser rejects missing duplicate malformed and extra properties'
  for scenario in missing-active duplicate-pid malformed-pid missing-cgroup extra-property; do
    setup
    case "$scenario" in
      missing-active) printf '%s\n' legaltech.slice:ActiveState > "$STATE/property-omit" ;;
      duplicate-pid) printf '%s\n' estrado-pjud.service:MainPID > "$STATE/property-duplicate" ;;
      malformed-pid)
        printf '%s\n' estrado-pjud.service:MainPID > "$STATE/property-bad"
        printf '%s\n' '42x' > "$STATE/property-bad-value" ;;
      missing-cgroup) printf '%s\n' user-1002.slice:ControlGroup > "$STATE/property-omit" ;;
      extra-property) printf '%s\n' legaltech.slice > "$STATE/property-extra" ;;
    esac
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$scenario refuses apply" "$RC" 1
    expect_exact_count "$scenario triggers one rollback" 'swap rollback' 1
  done

  echo '== exact active runtime passes and captured inactivity needs no live cgroup'
  setup
  configure_active_worker
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'all exact active cgroups pass' "$RC" 0

  setup
  printf '%s\n' inactive > "$STATE/user-unit-hermes-dashboard.service-active"
  printf '%s\n' 0 > "$STATE/user-unit-hermes-dashboard.service-main-pid"
  : > "$STATE/user-unit-hermes-dashboard.service-control-group"
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'captured inactive worker and Hermes unit need no cgroup' "$RC" 0
  expect_count 'captured inactive worker state is runtime-probed once' \
    'systemctl show estrado-pjud-worker.service --property=LoadState' 1
  expect_count 'captured inactive Hermes state is runtime-probed once' \
    'systemctl --user --machine=hermes@.host show hermes-dashboard.service --property=LoadState' 1
}

run_durable_metadata_regressions() {
  local boundary backup_path

  echo '== initial transaction metadata must be durably committed before mutation'
  for boundary in before-file-fsync after-file-fsync before-rename after-rename before-dir-fsync after-dir-fsync; do
    setup
    printf '%s\n' expected-sha > "$STATE/durable-fail-target"
    printf '%s\n' 1 > "$STATE/durable-fail-call"
    printf '%s\n' "$boundary" > "$STATE/durable-fail-boundary"
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$boundary initial metadata failure refuses apply" "$RC" 1
    expect_count "$boundary performs no provision" provision 0
    expect_count "$boundary performs no swap apply" 'swap apply' 0
  done

  for scenario in bad-output fail-after-output; do
    setup
    : > "$STATE/durable-$scenario"
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$scenario durable producer refuses apply" "$RC" 1
    expect_count "$scenario durable producer performs no provision" provision 0
    expect_count "$scenario durable producer performs no swap apply" 'swap apply' 0
  done

  for boundary in before-tree-file-fsync after-tree-file-fsync before-tree-dir-fsync after-tree-dir-fsync; do
    setup
    printf '%s\n' backup-tree > "$STATE/durable-fail-target"
    printf '%s\n' 1 > "$STATE/durable-fail-call"
    printf '%s\n' "$boundary" > "$STATE/durable-fail-boundary"
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$boundary backup durability failure refuses apply" "$RC" 1
    expect_count "$boundary backup failure performs no provision" provision 0
    expect_count "$boundary backup failure performs no swap apply" 'swap apply' 0
  done

  echo '== first rollout durably publishes a newly-created backup root entry'
  for boundary in before-root-parent-fsync after-root-parent-fsync \
    crash-before-root-parent-fsync crash-after-root-parent-fsync; do
    setup
    rm -rf "$FAKE/backups"
    printf '%s\n' backup-tree > "$STATE/durable-fail-target"
    printf '%s\n' 1 > "$STATE/durable-fail-call"
    printf '%s\n' "$boundary" > "$STATE/durable-fail-boundary"
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$boundary backup-root publication failure refuses apply" "$RC" 1
    expect_count "$boundary backup-root failure performs no provision" provision 0
    expect_count "$boundary backup-root failure performs no swap apply" 'swap apply' 0
    expect_count "$boundary backup-root failure performs no systemd stop" 'systemctl stop' 0
    expect_count "$boundary backup-root failure performs no systemd start" 'systemctl start' 0
    expect_count "$boundary backup-root failure performs no systemd restart" 'systemctl restart' 0
    expect_count "$boundary backup-root failure performs no daemon reload" 'systemctl daemon-reload' 0
  done

  echo '== backup namespace parent identity and mode are strict'
  for scenario in writable wrong-group missing-parent symlink-component; do
    setup
    backup_artifact_root="$FAKE/backups"
    case "$scenario" in
      writable) chmod 0777 "$FAKE" ;;
      wrong-group) : > "$STATE/backup-parent-wrong-gid" ;;
      missing-parent)
        TEST_BACKUP_ROOT_OVERRIDE="$CASE_DIR/missing-parent/backups"
        backup_artifact_root=$TEST_BACKUP_ROOT_OVERRIDE
        ;;
      symlink-component)
        mkdir -p "$CASE_DIR/backup-namespace/backups"
        chmod 0700 "$CASE_DIR/backup-namespace/backups"
        ln -s "$CASE_DIR/backup-namespace" "$CASE_DIR/backup-link"
        TEST_BACKUP_ROOT_OVERRIDE="$CASE_DIR/backup-link/backups"
        backup_artifact_root=$TEST_BACKUP_ROOT_OVERRIDE
        ;;
    esac
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$scenario backup namespace parent refuses apply" "$RC" 1
    expect_count "$scenario backup namespace performs no recursive backup mkdir" 'mkdir -p --' 0
    expect_count "$scenario backup namespace performs no backup leaf mkdir" 'mkdir --' 0
    expect_count "$scenario backup namespace copies no protected backup" backup-copy 0
    expect_eq "$scenario backup namespace creates no backup artifact" \
      "$(find "$backup_artifact_root" -mindepth 1 -print 2>/dev/null | wc -l | tr -d ' ')" 0
    expect_count "$scenario backup namespace performs no provision" provision 0
    expect_count "$scenario backup namespace performs no swap apply" 'swap apply' 0
    expect_count "$scenario backup namespace performs no systemd stop" 'systemctl stop' 0
    expect_count "$scenario backup namespace performs no systemd start" 'systemctl start' 0
    expect_count "$scenario backup namespace performs no systemd restart" 'systemctl restart' 0
    expect_count "$scenario backup namespace performs no daemon reload" 'systemctl daemon-reload' 0
    unset TEST_BACKUP_ROOT_OVERRIDE
  done

  echo '== backup directories are private at creation under a hostile inherited umask'
  for scenario in first-root existing-root; do
    setup
    : > "$STATE/audit-mkdir-mode"
    if [ "$scenario" = first-root ]; then
      rm -rf "$FAKE/backups"
      expected_private_mkdirs=3
    else
      expected_private_mkdirs=2
    fi
    previous_umask=$(umask)
    umask 000
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    umask "$previous_umask"
    expect_eq "$scenario hostile umask apply succeeds" "$RC" 0
    expect_count "$scenario exposes no backup directory at mkdir return" mkdir-unsafe 0
    expect_count "$scenario admits no attacker entry during mkdir window" attacker-entry 0
    expect_count "$scenario creates every new backup directory as 0700" ' 700' "$expected_private_mkdirs"
    unsafe_count=$(grep -c -F mkdir-unsafe "$EVENTS" 2>/dev/null || true)
    backup_copy_count=$(grep -c -F backup-copy "$EVENTS" 2>/dev/null || true)
    protected_effect_count=$(grep -E -c '^(provision|swap apply|systemctl (stop|start|restart|daemon-reload))' "$EVENTS" 2>/dev/null || true)
    if [ "$unsafe_count" -eq 0 ] || { [ "$backup_copy_count" -eq 0 ] && [ "$protected_effect_count" -eq 0 ]; }; then
      ok "$scenario unsafe mkdir cannot reach secret copy or protected effect"
    else
      bad "$scenario unsafe mkdir reached copy/effect (copies=$backup_copy_count effects=$protected_effect_count)"
    fi
  done

  echo '== abrupt writer exits retain no authority to begin protected effects'
  for boundary in crash-after-file-fsync crash-after-rename crash-after-dir-fsync; do
    setup
    configure_active_worker
    printf '%s\n' 0 0 0 > "$STATE/claim-sequence"
    printf '%s\n' changes > "$STATE/durable-fail-target"
    printf '%s\n' 2 > "$STATE/durable-fail-call"
    printf '%s\n' "$boundary" > "$STATE/durable-fail-boundary"
    TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$boundary worker marker crash refuses apply" "$RC" 1
    expect_exact_count "$boundary performs no worker stop" \
      'systemctl stop estrado-pjud-worker.service' 0
    expect_count "$boundary performs no provision" provision 0
    expect_eq "$boundary safely consumes exact stale metadata temporaries" \
      "$(find "$FAKE/backups" -name '.changes.*' -print | wc -l | tr -d ' ')" 0
  done

  echo '== durable worker-stop marker precedes the protected stop'
  for boundary in before-file-fsync after-file-fsync before-rename after-rename before-dir-fsync after-dir-fsync; do
    setup
    configure_active_worker
    printf '%s\n' 0 0 0 > "$STATE/claim-sequence"
    printf '%s\n' changes > "$STATE/durable-fail-target"
    printf '%s\n' 2 > "$STATE/durable-fail-call"
    printf '%s\n' "$boundary" > "$STATE/durable-fail-boundary"
    TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$boundary worker marker failure refuses apply" "$RC" 1
    expect_exact_count "$boundary performs no worker stop" \
      'systemctl stop estrado-pjud-worker.service' 0
    expect_count "$boundary performs no provision" provision 0
  done

  echo '== attempted swap marker is confirmed durable before swap apply'
  for boundary in before-file-fsync after-file-fsync before-rename after-rename before-dir-fsync after-dir-fsync; do
    setup
    printf '%s\n' swap-state > "$STATE/durable-fail-target"
    printf '%s\n' 2 > "$STATE/durable-fail-call"
    printf '%s\n' "$boundary" > "$STATE/durable-fail-boundary"
    TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$boundary attempted marker failure refuses apply" "$RC" 1
    expect_count "$boundary never starts swap apply" 'swap apply' 0
  done

  echo '== stale and malformed swap ownership metadata never reports false rollback success'
  setup
  TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'fixture apply creates exact owned swap state' "$RC" 0
  backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
  printf '%s\n' not-attempted > "$backup_path/swap-state"
  : > "$EVENTS"
  run_guard rollback --backup-dir "$backup_path"
  expect_eq 'stale not-attempted marker with owned swap fails closed' "$RC" 1
  expect_missing 'stale marker never declares rollback success' "$OUT" 'ROLLBACK OK'
  expect_count 'stale marker is not guessed into destructive swap rollback' 'swap rollback' 0
  expect_eq 'stale marker leaves owned swap evidence for operator recovery' \
    "$(test -e "$STATE/swap-applied"; echo $?)" 0

  printf '%s' attempt > "$backup_path/swap-state"
  : > "$EVENTS"
  run_guard rollback --backup-dir "$backup_path"
  expect_eq 'truncated swap marker fails closed' "$RC" 1
  expect_missing 'truncated marker never declares rollback success' "$OUT" 'ROLLBACK OK'
  expect_count 'truncated marker performs no swap rollback' 'swap rollback' 0
}

run_rollback_precheck_regressions() {
  local scenario backup_path
  echo '== rollback proves swap ownership before quiescing a changed active worker'
  for scenario in stale truncated conflicting; do
    setup
    configure_active_worker
    printf '%s\n' 0 0 0 > "$STATE/claim-sequence"
    TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
    expect_eq "$scenario fixture apply succeeds" "$RC" 0
    backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
    case "$scenario" in
      stale) printf '%s\n' not-attempted > "$backup_path/swap-state" ;;
      truncated) printf '%s' attempt > "$backup_path/swap-state" ;;
      conflicting)
        printf '%s\n' preexisting > "$backup_path/swap-state"
        rm -f "$STATE/swap-applied"
        ;;
    esac
    : > "$EVENTS"
    run_guard rollback --backup-dir "$backup_path"
    expect_eq "$scenario marker refuses rollback" "$RC" 1
    expect_count "$scenario marker performs no worker stop" \
      'systemctl stop estrado-pjud-worker.service' 0
    expect_count "$scenario marker performs no worker start" \
      'systemctl start estrado-pjud-worker.service' 0
    expect_count "$scenario marker performs no worker restart" \
      'systemctl restart estrado-pjud-worker.service' 0
    expect_count "$scenario marker performs no provision" provision 0
    expect_count "$scenario marker performs no swap rollback" 'swap rollback' 0
    expect_count "$scenario marker performs no manifest reload" \
      'systemctl daemon-reload' 0
    expect_eq "$scenario marker leaves worker active" \
      "$(cat "$STATE/unit-estrado-pjud-worker.service-active")" active
  done
}

run_orchestrated_swap_crash_regression() {
  local backup_path retry_path
  echo '== outer attempted authority delegates inner crash recovery and exact retry converges'
  setup
  printf '%s\n' apply-fstab > "$STATE/swap-crash-state"
  : > "$STATE/swap-crash-rollback-fail-once"
  TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq 'inner crash keeps outer apply failed' "$RC" 1
  expect_exact_count 'outer attempted marker delegates automatic swap rollback once' \
    'swap rollback' 1
  expect_contains 'failed automatic crash recovery reports exact retry authority' \
    "$OUT" 'ROLLBACK INCOMPLETO'
  backup_path=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
  retry_path=$(printf '%s\n' "$OUT" \
    | sed -n 's/^ROLLBACK INCOMPLETO: reintente con el BACKUP_DIR validado: \(.*\)$/\1/p')
  expect_eq 'crash recovery diagnostic returns the same backup directory' \
    "$retry_path" "$backup_path"
  expect_eq 'outer transaction retains durable attempted authority' \
    "$(cat "$backup_path/swap-state")" attempted

  : > "$EVENTS"
  run_guard rollback --backup-dir "$retry_path"
  expect_eq 'same backup directory retry converges after inner crash' "$RC" 0
  expect_exact_count 'same backup retry delegates swap rollback once' 'swap rollback' 1
  expect_contains 'same backup retry confirms exact authority' \
    "$OUT" "ROLLBACK OK: $backup_path"
  if [ ! -e "$STATE/swap-crash-state" ]; then
    ok 'same backup retry removes the exact inner crash state'
  else
    bad 'same backup retry removes the exact inner crash state'
  fi
}

if [ "${RESOURCE_GUARDS_FOCUS:-}" = swap-crash-recovery ]; then
  run_orchestrated_swap_crash_regression
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = rollback-precheck ]; then
  run_rollback_precheck_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = worker-heartbeat-wait ]; then
  run_worker_post_start_wait_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = worker-pre-target-cgroup ]; then
  run_worker_pre_target_cgroup_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = mutator-lock ]; then
  run_mutator_lock_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = runtime-cgroups ]; then
  run_runtime_cgroup_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = durable-metadata ]; then
  run_durable_metadata_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = worker-fence ]; then
  run_worker_fence_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = api-enablement ]; then
  run_api_enablement_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = legacy-monitors ]; then
  run_legacy_monitor_migration_regression
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = activity ]; then
  run_activity_preservation_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = absent-timers ]; then
  run_absent_timer_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = swappiness-namespace ]; then
  run_swappiness_namespace_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = rollback-retry-diagnostic ]; then
  run_incomplete_rollback_retry_regression
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

if [ "${RESOURCE_GUARDS_FOCUS:-}" = swap-apply-compensation-retry ]; then
  run_apply_compensation_retry_regressions
  echo
  echo "$PASS ok, $FAIL fail"
  [ "$FAIL" -eq 0 ]
  exit
fi

run_legacy_monitor_migration_regression
run_activity_preservation_regressions
run_absent_timer_regressions
run_swappiness_namespace_regressions
run_incomplete_rollback_retry_regression
run_apply_compensation_retry_regressions
run_orchestrated_swap_crash_regression
run_rollback_precheck_regressions
run_api_enablement_regressions
run_worker_pre_target_cgroup_regressions
run_mutator_lock_regressions
run_worker_fence_regressions
run_runtime_cgroup_regressions

echo '== explicit test guard rejects a partial override set'
setup
set +e
OUT=$(RG_REPO_DIR="$FAKE/repo" bash "$SCRIPT" preflight --expected-sha "$EXPECTED_SHA" 2>&1)
RC=$?
set -e
expect_eq 'partial override exits usage/error' "$RC" 2
expect_count 'partial override executes no host command' 'systemctl ' 0
set +e
OUT=$(PROV_REPO_DIR="$FAKE/repo" bash "$SCRIPT" preflight --expected-sha "$EXPECTED_SHA" 2>&1)
RC=$?
set -e
expect_eq 'ambient provision path override is rejected' "$RC" 2
expect_count 'ambient provision override executes no host command' 'systemctl ' 0

echo '== preflight remains host-local and leaves unchanged worker telemetry untouched'
setup
configure_active_worker
run_guard preflight --expected-sha "$EXPECTED_SHA"
expect_eq 'safe preflight exits zero' "$RC" 0
expect_count 'preflight performs no heartbeat query' 'curl heartbeat' 0
expect_count 'preflight performs no claims query' 'curl claims' 0
expect_contains 'preflight disk target is injected inside the fake root' "$(cat "$EVENTS")" "df -Pk $FAKE"
expect_missing 'service credential never appears in curl argv' "$(cat "$EVENTS")" 'credential leaked in argv'
expect_missing 'preflight does not expose service credential' "$OUT$(cat "$EVENTS")" "$SECRET_SENTINEL"
printf '%s\n' zero-star > "$STATE/claim-count"
run_guard preflight --expected-sha "$EXPECTED_SHA"
expect_eq 'irrelevant claim fixture does not affect host-local preflight' "$RC" 0

echo '== systemd inventory accepts real three-column output and rejects ambiguity'
setup
run_guard preflight --expected-sha "$EXPECTED_SHA"
expect_eq 'UNIT STATE PRESET inventory is accepted' "$RC" 0
for shape in unexpected-state extra; do
  setup
  printf '%s\n' "$shape" > "$STATE/list-shape"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq "$shape inventory refuses apply" "$RC" 1
  expect_count "$shape inventory causes no provision" provision 0
done

echo '== structurally managed swap with live drift is unknown before backup'
setup
: > "$STATE/swap-applied"
: > "$STATE/swap-live-drift"
before_fstab=$(cat "$FAKE/etc/fstab")
run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'managed swap drift refuses apply' "$RC" 1
expect_count 'managed swap drift never calls swap apply' 'swap apply' 0
expect_count 'managed swap drift never calls swap rollback' 'swap rollback' 0
expect_count 'managed swap drift never provisions' provision 0
expect_eq 'managed swap drift changes no managed file' "$(cat "$FAKE/etc/fstab")" "$before_fstab"

echo '== gate-producing command output followed by nonzero is unknown'
for dependency in df free; do
  setup
  : > "$STATE/$dependency-fail-after-output"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq "$dependency valid-looking output plus failure refuses apply" "$RC" 1
  expect_count "$dependency failure causes no provision" provision 0
done
setup
: > "$STATE/find-fail-after-output"
run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'find empty-looking result plus failure refuses apply' "$RC" 1
expect_count 'find failure causes no provision' provision 0

echo '== protected service configuration is parsed as data and fails closed'
setup
configure_active_worker
printf '%s\n' 'SUPABASE_SERVICE_KEY=second-placeholder' >> "$FAKE/repo/estrado-pjud-service/.env"
TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'duplicate required credential refuses changed-worker apply' "$RC" 1
expect_count 'duplicate required credential performs no worker stop' 'stop estrado-pjud-worker.service' 0
expect_count 'duplicate required credential performs no provision' provision 0
expect_missing 'duplicate credential values are never diagnosed' "$OUT" 'second-placeholder'
setup
configure_active_worker
chmod 644 "$FAKE/repo/estrado-pjud-service/.env"
TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'world-readable service configuration refuses changed-worker apply' "$RC" 1
expect_count 'world-readable configuration performs no worker stop' 'stop estrado-pjud-worker.service' 0
expect_count 'world-readable configuration performs no provision' provision 0

run_negative_preflight() {
  local scenario=$1 provision_count backup_count
  trap - EXIT
  setup
  case "$scenario" in
    dirty) echo dirty > "$STATE/git-status" ;;
    sha) echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb > "$STATE/git-sha" ;;
    disk) TEST_DISK_BYTES=8589934591 ;;
    ram) TEST_RAM_BYTES=6442450943 ;;
    uid) : > "$STATE/id-fail" ;;
    juris) echo 503 > "$STATE/juristrack-code" ;;
    estrado) echo 503 > "$STATE/estrado-code" ;;
  esac
  run_guard apply --expected-sha "$EXPECTED_SHA"
  provision_count=$(grep -c -F provision "$EVENTS" 2>/dev/null || true)
  backup_count=$(grep -c -F backup-copy "$EVENTS" 2>/dev/null || true)
  printf '%s|%s|%s|%s\n' "$scenario" "$RC" "$provision_count" "$backup_count"
}

negative_scenarios=(dirty sha disk ram uid juris estrado)
negative_pids=()
for scenario in "${negative_scenarios[@]}"; do
  run_negative_preflight "$scenario" > "$TMP/negative-$scenario" &
  negative_pids+=("$!")
done
for pid in "${negative_pids[@]}"; do wait "$pid"; done
for scenario in "${negative_scenarios[@]}"; do
  echo "== apply refuses unsafe preflight: $scenario"
  IFS='|' read -r _scenario RC provision_count backup_count < "$TMP/negative-$scenario"
  expect_eq "$scenario refuses apply" "$RC" 1
  expect_eq "$scenario runs no provision mutation" "$provision_count" 0
  expect_eq "$scenario creates no backup content" "$backup_count" 0
done

echo '== GNU sha256sum output is bound to one exact requested path'
for sha_mode in malformed wrong-path extra fail-after-output; do
  setup
  printf '%s\n' "$sha_mode" > "$STATE/sha-mode"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq "$sha_mode digest refuses apply" "$RC" 1
  expect_count "$sha_mode digest causes no provision" provision 0
  expect_count "$sha_mode digest creates no backup" backup-copy 0
done

echo '== apply backs up before mutation and orders affected restarts safely'
setup
configure_active_worker
TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'apply succeeds' "$RC" 0
expect_before 'backup precedes provision' backup-copy provision
expect_before 'provision precedes swap apply' provision 'swap apply'
expect_last_before 'swap apply precedes final verify' 'swap apply' 'swap verify'
expect_before 'daemon reload precedes API restart' 'systemctl daemon-reload' 'systemctl restart estrado-pjud.service'
expect_before 'worker stop precedes provision' 'systemctl stop estrado-pjud-worker.service' provision
expect_last_before 'daemon reload precedes worker start' 'systemctl daemon-reload' 'systemctl start estrado-pjud-worker.service'
expect_count 'worker fence checks pre-stop and post-start heartbeat' 'curl heartbeat' 2
expect_count 'worker fence checks pre-stop, post-stop, and post-start claims' 'curl claims' 3
expect_count 'API restarted once when changed' 'systemctl restart estrado-pjud.service' 1
expect_exact_count 'worker stopped once when changed and idle' 'systemctl stop estrado-pjud-worker.service' 1
expect_exact_count 'worker started once when changed and idle' 'systemctl start estrado-pjud-worker.service' 1
expect_count 'worker never restarts during fenced apply' 'systemctl restart estrado-pjud-worker.service' 0
expect_exact_count 'active Hermes gateway restarts once when drop-in changes' 'systemctl --user --machine=hermes@.host restart hermes-gateway.service' 1
expect_exact_count 'active Hermes dashboard restarts once when drop-in changes' 'systemctl --user --machine=hermes@.host restart hermes-dashboard.service' 1
expect_before 'timers start before tracker invocation' 'systemctl start legaltech-monitor.timer legaltech-resource-tracker.timer' 'python '
monitor_invocations=$(grep -F "python $FAKE/monitoring/monitor.py" "$EVENTS" 2>/dev/null || true)
expect_exact_count 'monitor is invoked exactly once in dry-run mode' "python $FAKE/monitoring/monitor.py --dry-run" 1
expect_missing 'monitor dry-run invocation excludes once mode' "$monitor_invocations" '--once'
expect_missing 'monitor dry-run invocation excludes synthetic alert mode' "$monitor_invocations" '--test-alert'
expect_missing 'orchestration output contains no service credential' "$OUT$(cat "$EVENTS")" "$SECRET_SENTINEL"
expect_missing 'suppressed dependency output contains no service credential' "$(cat "$CASE_DIR/null")" "$SECRET_SENTINEL"
SUCCESS_EVENTS=$(cat "$EVENTS")
BACKUP_DIR=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
expect_eq 'service credential backup is root-only 0600' "$(/usr/bin/stat -f '%Lp' "$BACKUP_DIR/entries/0010")" 600
expect_missing 'manifest stores metadata but no credential content' "$(cat "$BACKUP_DIR/manifest.tsv")" "$SECRET_SENTINEL"
expect_eq 'manifest has one record for every exact managed path' "$(wc -l < "$BACKUP_DIR/manifest.tsv" | tr -d ' ')" 16
expect_eq 'timestamped backup directory is 0700' "$(/usr/bin/stat -f '%Lp' "$BACKUP_DIR")" 700
if [ -f "$BACKUP_DIR/unit-states.tsv" ]; then
  live_state_lines=$(wc -l < "$BACKUP_DIR/unit-states.tsv" | tr -d ' ')
else
  live_state_lines=missing
fi
expect_eq 'backup records six system and two Hermes live unit states' "$live_state_lines" 8
expect_missing 'live unit metadata contains no credential content' "$(cat "$BACKUP_DIR/unit-states.tsv" 2>/dev/null || true)" "$SECRET_SENTINEL"
printf '%s\n' "$FAKE/outside"$'\t0\t-\t-\t-\t-' >> "$BACKUP_DIR/manifest.tsv"
before_tampered_rollback=$(cat "$FAKE/systemd/estrado-pjud.service")
: > "$EVENTS"
run_guard rollback --backup-dir "$BACKUP_DIR"
expect_eq 'tampered manifest refuses standalone rollback' "$RC" 1
expect_eq 'tampered manifest causes no partial restore' "$(cat "$FAKE/systemd/estrado-pjud.service")" "$before_tampered_rollback"
expect_count 'tampered manifest never reaches swap rollback' 'swap rollback' 0

echo '== apply rechecks the exact SHA after backup and before first mutation'
setup
: > "$STATE/sha-after-backup"
run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'post-backup SHA drift refuses apply' "$RC" 1
expect_count 'post-backup SHA drift runs no provision mutation' provision 0
expect_count 'post-backup SHA drift does not invoke swap rollback before swap apply' 'swap rollback' 0

echo '== corrupt live-state metadata refuses before first mutation'
setup
: > "$STATE/corrupt-live-state"
run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'missing live-state row refuses apply' "$RC" 1
expect_count 'corrupt live-state metadata causes no provision' provision 0
expect_count 'corrupt live-state metadata causes no systemd effect' 'systemctl daemon-reload' 0

echo '== apply rejects an existing unsafe backup root rather than taking it over'
setup
chmod 777 "$FAKE/backups"
run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'unsafe backup root refuses apply' "$RC" 1
expect_count 'unsafe backup root runs no provision mutation' provision 0

echo '== backup refuses symlink and hardlink managed targets'
run_unsafe_backup_target() {
  local kind=$1 provision_count
  trap - EXIT
  setup
  case "$kind" in
    symlink)
      rm "$FAKE/systemd/legaltech.slice"
      ln -s "$FAKE/outside" "$FAKE/systemd/legaltech.slice"
      ;;
    hardlink) ln "$FAKE/systemd/estrado-pjud.service" "$FAKE/api-hardlink" ;;
  esac
  run_guard apply --expected-sha "$EXPECTED_SHA"
  provision_count=$(grep -c -F provision "$EVENTS" 2>/dev/null || true)
  printf '%s|%s|%s\n' "$kind" "$RC" "$provision_count"
}
for kind in symlink hardlink; do run_unsafe_backup_target "$kind" > "$TMP/unsafe-$kind" & done
wait
for kind in symlink hardlink; do
  IFS='|' read -r _kind RC provision_count < "$TMP/unsafe-$kind"
  expect_eq "$kind managed target refuses apply" "$RC" 1
  expect_eq "$kind managed target runs no provision mutation" "$provision_count" 0
done

echo '== every existing manifest source has safe type, owner, group and mode'
run_unsafe_source() {
  local scenario=$1 provision_count swap_count
  trap - EXIT
  setup
  case "$scenario" in
    credential-gid) : > "$STATE/credential-wrong-gid" ;;
    unit-writable) chmod 0664 "$FAKE/systemd/estrado-pjud.service" ;;
    runtime-writable) chmod 0775 "$FAKE/monitoring" ;;
    monitor-env-writable) chmod 0620 "$FAKE/monitoring.env" ;;
  esac
  run_guard apply --expected-sha "$EXPECTED_SHA"
  provision_count=$(grep -c -F provision "$EVENTS" 2>/dev/null || true)
  swap_count=$(grep -c -F 'swap apply' "$EVENTS" 2>/dev/null || true)
  printf '%s|%s|%s|%s\n' "$scenario" "$RC" "$provision_count" "$swap_count"
}
source_scenarios=(credential-gid unit-writable runtime-writable monitor-env-writable)
for scenario in "${source_scenarios[@]}"; do run_unsafe_source "$scenario" > "$TMP/source-$scenario" & done
wait
for scenario in "${source_scenarios[@]}"; do
  IFS='|' read -r _scenario RC provision_count swap_count < "$TMP/source-$scenario"
  expect_eq "$scenario source refuses apply" "$RC" 1
  expect_eq "$scenario source causes no provision" "$provision_count" 0
  expect_eq "$scenario source causes no swap mutation" "$swap_count" 0
done

echo '== generated backup is fully validated before first mutation'
run_corrupt_backup() {
  local scenario=$1 provision_count effect_count
  trap - EXIT
  setup
  printf '%s\n' "$scenario" > "$STATE/corrupt-backup"
  run_guard apply --expected-sha "$EXPECTED_SHA"
  provision_count=$(grep -c -F provision "$EVENTS" 2>/dev/null || true)
  effect_count=$(grep -Ec '^systemctl (daemon-reload|restart|start|stop|enable|disable) ' "$EVENTS" 2>/dev/null || true)
  printf '%s|%s|%s|%s\n' "$scenario" "$RC" "$provision_count" "$effect_count"
}
backup_corruptions=(missing duplicate corrupt metadata)
for scenario in "${backup_corruptions[@]}"; do run_corrupt_backup "$scenario" > "$TMP/backup-$scenario" & done
wait
for scenario in "${backup_corruptions[@]}"; do
  IFS='|' read -r _scenario RC provision_count effect_count < "$TMP/backup-$scenario"
  expect_eq "$scenario generated backup refuses apply" "$RC" 1
  expect_eq "$scenario generated backup causes no provision" "$provision_count" 0
  expect_eq "$scenario generated backup causes no systemd effect" "$effect_count" 0
done

echo '== worker digest includes the managed Xvfb drop-in'
setup
configure_active_worker
TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'drop-in-only apply succeeds' "$RC" 0
expect_exact_count 'drop-in-only change stops worker once' 'systemctl stop estrado-pjud-worker.service' 1
expect_exact_count 'drop-in-only change starts worker once' 'systemctl start estrado-pjud-worker.service' 1
expect_count 'drop-in-only change checks heartbeat before and after start' 'curl heartbeat' 2
expect_count 'drop-in-only change checks claims across all three fence phases' 'curl claims' 3
expect_count 'drop-in-only change never restarts worker' 'systemctl restart estrado-pjud-worker.service' 0

echo '== partial provision failure records affected units for rollback restart'
setup
: > "$STATE/provision-fail"
before_api=$(cat "$FAKE/systemd/estrado-pjud.service")
TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'partial provision failure exits nonzero' "$RC" 1
expect_eq 'partial provision failure restores API unit' "$(cat "$FAKE/systemd/estrado-pjud.service")" "$before_api"
expect_count 'partial provision failure restarts restored affected API once' 'systemctl restart estrado-pjud.service' 1
expect_count 'partial provision failure does not roll back untouched swap' 'swap rollback' 0

echo '== rollback preserves a swap configuration that predated apply'
setup
: > "$STATE/swap-applied"
: > "$STATE/postflight-fail"
TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'failed apply with preexisting swap exits nonzero' "$RC" 1
expect_count 'preexisting swap does not invoke destructive swap rollback' 'swap rollback' 0
if [ -e "$STATE/swap-applied" ]; then ok 'preexisting active swap remains present'; else bad 'preexisting active swap was removed'; fi

echo '== every post-backup mutation boundary fails closed with one rollback'
run_mutation_failure() {
  local scenario=$1 rollback_count
  trap - EXIT
  setup
  case "$scenario" in
    swap-apply) : > "$STATE/swap-apply-fail" ;;
    swap-verify) : > "$STATE/swap-verify-fail" ;;
    daemon) : > "$STATE/daemon-fail" ;;
    api-restart) echo 'restart estrado-pjud.service' > "$STATE/fail-command" ;;
    hermes-restart) echo '--user --machine=hermes@.host restart hermes-gateway.service' > "$STATE/fail-command" ;;
    timers) echo 'start legaltech-monitor.timer legaltech-resource-tracker.timer' > "$STATE/fail-command" ;;
    tracker) : > "$STATE/tracker-fail" ;;
    monitor) : > "$STATE/monitor-fail" ;;
    postflight-property) : > "$STATE/property-bad" ;;
    postflight-health) : > "$STATE/health-after-first" ;;
  esac
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  rollback_count=$(grep -c -x -F 'swap rollback' "$EVENTS" 2>/dev/null || true)
  printf '%s|%s|%s\n' "$scenario" "$RC" "$rollback_count"
}
mutation_scenarios=(swap-apply swap-verify daemon api-restart hermes-restart timers tracker monitor postflight-property postflight-health)
mutation_pids=()
for scenario in "${mutation_scenarios[@]}"; do
  run_mutation_failure "$scenario" > "$TMP/mutation-$scenario" &
  mutation_pids+=("$!")
done
for pid in "${mutation_pids[@]}"; do wait "$pid"; done
for scenario in "${mutation_scenarios[@]}"; do
  IFS='|' read -r _scenario RC rollback_count < "$TMP/mutation-$scenario"
  expect_eq "$scenario boundary exits nonzero" "$RC" 1
  expect_eq "$scenario invokes swap rollback exactly once" "$rollback_count" 1
done

echo '== postflight rejects drift in every previously omitted live contract class'
run_postflight_drift() {
  local key=$1
  trap - EXIT
  setup
  : > "$STATE/swap-applied"
  printf '%s\n' "$key" > "$STATE/property-bad"
  run_guard postflight
  printf '%s|%s\n' "$key" "$RC"
}
postflight_drifts=(
  estrado-pjud.service:MemoryMax
  estrado-pjud-worker.service:PartOf
  legaltech-monitor.service:ProtectSystem
  legaltech-monitor.service:StateDirectoryMode
  legaltech-resource-tracker.service:RestrictAddressFamilies
  legaltech-resource-tracker.service:StateDirectory
  legaltech-monitor.timer:Unit
  legaltech-resource-tracker.timer:OnBootUSec
  legaltech-monitor.timer:OnUnitActiveUSec
  legaltech-resource-tracker.timer:Persistent
  legaltech-monitor.timer:RandomizedDelayUSec
)
for key in "${postflight_drifts[@]}"; do run_postflight_drift "$key" > "$TMP/postflight-${key//[:.]/-}" & done
wait
for key in "${postflight_drifts[@]}"; do
  IFS='|' read -r _key RC < "$TMP/postflight-${key//[:.]/-}"
  expect_eq "$key drift refuses postflight" "$RC" 1
done

echo '== postflight rejects inherited ownership and credential responsibility drift'
run_postflight_override() {
  local scenario=$1 key=$2 value=$3
  trap - EXIT
  setup
  : > "$STATE/swap-applied"
  printf '%s\n' "$key" > "$STATE/property-bad"
  printf '%s' "$value" > "$STATE/property-bad-value"
  run_guard postflight
  printf '%s|%s\n' "$scenario" "$RC"
}
postflight_override_scenarios=(
  monitor-partof
  tracker-partof
  tracker-credential-env
  monitor-missing-env
  monitor-extra-env
)
run_postflight_override monitor-partof legaltech-monitor.service:PartOf legaltech.slice > "$TMP/postflight-monitor-partof" &
run_postflight_override tracker-partof legaltech-resource-tracker.service:PartOf legaltech.slice > "$TMP/postflight-tracker-partof" &
run_postflight_override tracker-credential-env legaltech-resource-tracker.service:EnvironmentFiles '/etc/legaltech-monitoring.env (ignore_errors=yes)' > "$TMP/postflight-tracker-credential-env" &
run_postflight_override monitor-missing-env legaltech-monitor.service:EnvironmentFiles '' > "$TMP/postflight-monitor-missing-env" &
run_postflight_override monitor-extra-env legaltech-monitor.service:EnvironmentFiles '/etc/legaltech-monitoring.env (ignore_errors=yes) /etc/extra.env (ignore_errors=no)' > "$TMP/postflight-monitor-extra-env" &
wait
for scenario in "${postflight_override_scenarios[@]}"; do
  IFS='|' read -r _scenario RC < "$TMP/postflight-$scenario"
  expect_eq "$scenario refuses postflight" "$RC" 1
done

setup
: > "$STATE/swap-applied"
run_guard postflight
expect_eq 'complete real-shape postflight fixture succeeds' "$RC" 0

echo '== unchanged units are not restarted'
setup
run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'unchanged apply succeeds' "$RC" 0
expect_count 'unchanged API is not restarted' 'systemctl restart estrado-pjud.service' 0
expect_count 'unchanged worker is not restarted' 'systemctl restart estrado-pjud-worker.service' 0
expect_count 'unchanged Hermes is not restarted' 'restart hermes-gateway.service' 0

echo '== immediate worker gate refuses ambiguous or active claims after mutation'
run_immediate_gate() {
  local state=$1 worker_count
  trap - EXIT
  setup
  configure_active_worker
  printf '%s\n' 0 > "$STATE/claim-count"
  # Curl flips the count fixture after its first successful claims query.
  printf '%s\n' "$state" > "$STATE/claim-after-first"
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  worker_count=$(grep -c -F 'systemctl restart estrado-pjud-worker.service' "$EVENTS" 2>/dev/null || true)
  printf '%s|%s|%s\n' "$state" "$RC" "$worker_count"
}
for state in malformed nonzero; do
  run_immediate_gate "$state" > "$TMP/immediate-$state" &
done
wait
for state in malformed nonzero; do
  IFS='|' read -r _state RC worker_count < "$TMP/immediate-$state"
  expect_eq "$state immediate gate fails apply" "$RC" 1
  expect_eq "$state never restarts worker" "$worker_count" 0
done

echo '== failed postflight rolls back exactly once and restores only manifest paths'
setup
rm "$FAKE/etc/sysctl.d/60-legaltech-swap.conf"
: > "$STATE/create-sysctl"
: > "$STATE/mutate-outside"
: > "$STATE/mutate-credential"
: > "$STATE/postflight-fail"
before_api=$(cat "$FAKE/systemd/estrado-pjud.service")
before_credential=$(cat "$FAKE/repo/estrado-pjud-service/.env")
TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'postflight failure exits nonzero' "$RC" 1
expect_exact_count 'automatic rollback delegates swap once' 'swap rollback' 1
expect_last_before 'rollback delegates swap before reloading restored units' 'swap rollback' 'systemctl daemon-reload'
expect_last_before 'rollback daemon reload precedes affected API restart' 'systemctl daemon-reload' 'systemctl restart estrado-pjud.service'
expect_count 'affected API restarts once on apply and once on rollback' 'systemctl restart estrado-pjud.service' 2
expect_count 'rollback does not restart unaffected worker' 'systemctl restart estrado-pjud-worker.service' 0
expect_count 'rollback does not restart unaffected Hermes' 'restart hermes-gateway.service' 0
expect_eq 'manifest restores an existing API unit' "$(cat "$FAKE/systemd/estrado-pjud.service")" "$before_api"
expect_eq 'rollback restores protected credential bytes without printing them' "$(cat "$FAKE/repo/estrado-pjud-service/.env")" "$before_credential"
expect_eq 'rollback restores protected credential mode from manifest metadata' "$(/usr/bin/stat -f '%Lp' "$FAKE/repo/estrado-pjud-service/.env")" 640
if [ ! -e "$FAKE/etc/sysctl.d/60-legaltech-swap.conf" ]; then ok 'rollback removes exact newly-created managed path'; else bad 'rollback left newly-created managed path'; fi
expect_eq 'rollback does not restore an unlisted path' "$(cat "$FAKE/outside")" 'outside changed'

echo '== rollback restores exact legacy enable and timer activity states'
setup
printf '%s\n' disabled > "$STATE/unit-estrado-pjud.service-enabled"
printf '%s\n' inactive > "$STATE/unit-estrado-pjud.service-active"
for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
  printf '%s\n' enabled > "$STATE/unit-$unit-enabled"
done
for unit in legaltech-monitor.timer legaltech-resource-tracker.timer; do
  printf '%s\n' disabled > "$STATE/unit-$unit-enabled"
  printf '%s\n' inactive > "$STATE/unit-$unit-active"
done
: > "$STATE/postflight-fail"
TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'legacy-state rollout fails on postflight' "$RC" 1
expect_eq 'rollback restores API disabled state' "$(cat "$STATE/unit-estrado-pjud.service-enabled")" disabled
expect_eq 'rollback restores API inactive state' "$(cat "$STATE/unit-estrado-pjud.service-active")" inactive
expect_eq 'rollback restores worker disabled state' "$(cat "$STATE/unit-estrado-pjud-worker.service-enabled")" disabled
expect_eq 'rollback restores worker inactive state' "$(cat "$STATE/unit-estrado-pjud-worker.service-active")" inactive
for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
  expect_eq "rollback restores legacy $unit enable" "$(cat "$STATE/unit-$unit-enabled")" enabled
done
for unit in legaltech-monitor.timer legaltech-resource-tracker.timer; do
  expect_eq "rollback restores $unit disabled" "$(cat "$STATE/unit-$unit-enabled")" disabled
  expect_eq "rollback restores $unit inactive" "$(cat "$STATE/unit-$unit-active")" inactive
done
expect_contains 'complete live-state restore reports rollback OK' "$OUT" 'ROLLBACK OK'

echo '== static one-shots are captured and preserved without enable actions'
setup
for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
  printf '%s\n' static > "$STATE/unit-$unit-enabled"
done
: > "$STATE/postflight-fail"
TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'static one-shot rollout reaches postflight and rolls back' "$RC" 1
for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
  expect_eq "rollback preserves static $unit" "$(cat "$STATE/unit-$unit-enabled")" static
  expect_count "rollback never enables static $unit" "systemctl enable $unit" 0
  expect_count "rollback never disables static $unit" "systemctl disable $unit" 0
done
expect_contains 'static one-shot rollback completes' "$OUT" 'ROLLBACK OK'

for invalid_static in status-mismatch unknown-output; do
  setup
  if [ "$invalid_static" = status-mismatch ]; then
    printf '%s\n' static > "$STATE/unit-legaltech-monitor.service-enabled"
    : > "$STATE/static-status-fail"
  else
    printf '%s\n' indirect > "$STATE/unit-legaltech-monitor.service-enabled"
  fi
  run_guard apply --expected-sha "$EXPECTED_SHA"
  expect_eq "$invalid_static enablement state refuses apply" "$RC" 1
  expect_count "$invalid_static enablement state causes no provision" provision 0
done

echo '== Hermes rollback restores exact mixed enabled and active states'
setup
printf '%s\n' enabled > "$STATE/user-unit-hermes-gateway.service-enabled"
printf '%s\n' active > "$STATE/user-unit-hermes-gateway.service-active"
printf '%s\n' static > "$STATE/user-unit-hermes-dashboard.service-enabled"
printf '%s\n' inactive > "$STATE/user-unit-hermes-dashboard.service-active"
printf '%s\n' 'hermes-gateway.service disabled' > "$STATE/hermes-enabled-after-provision"
: > "$STATE/postflight-fail"
TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'mixed Hermes rollout fails on postflight' "$RC" 1
expect_eq 'rollback re-enables gateway' "$(cat "$STATE/user-unit-hermes-gateway.service-enabled")" enabled
expect_eq 'rollback restarts previously active gateway' "$(cat "$STATE/user-unit-hermes-gateway.service-active")" active
expect_eq 'rollback preserves static dashboard' "$(cat "$STATE/user-unit-hermes-dashboard.service-enabled")" static
expect_eq 'rollback keeps previously inactive dashboard inactive' "$(cat "$STATE/user-unit-hermes-dashboard.service-active")" inactive
expect_count 'rollback enables gateway exactly once' 'systemctl --user --machine=hermes@.host enable hermes-gateway.service' 1
expect_exact_count 'active gateway restarts once on apply and once on rollback' 'systemctl --user --machine=hermes@.host restart hermes-gateway.service' 2
expect_exact_count 'inactive dashboard is never stopped unnecessarily' 'systemctl --user --machine=hermes@.host stop hermes-dashboard.service' 0
expect_count 'rollback never enables static dashboard' 'systemctl --user --machine=hermes@.host enable hermes-dashboard.service' 0
expect_contains 'mixed Hermes rollback completes' "$OUT" 'ROLLBACK OK'

setup
printf '%s\n' disabled > "$STATE/user-unit-hermes-gateway.service-enabled"
printf '%s\n' inactive > "$STATE/user-unit-hermes-gateway.service-active"
printf '%s\n' enabled > "$STATE/user-unit-hermes-dashboard.service-enabled"
printf '%s\n' active > "$STATE/user-unit-hermes-dashboard.service-active"
printf '%s\n' 'hermes-gateway.service enabled' 'hermes-dashboard.service disabled' > "$STATE/hermes-enabled-after-provision"
: > "$STATE/postflight-fail"
TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'inverse Hermes rollout fails on postflight' "$RC" 1
expect_eq 'rollback disables gateway' "$(cat "$STATE/user-unit-hermes-gateway.service-enabled")" disabled
expect_eq 'rollback keeps gateway inactive' "$(cat "$STATE/user-unit-hermes-gateway.service-active")" inactive
expect_eq 'rollback re-enables dashboard' "$(cat "$STATE/user-unit-hermes-dashboard.service-enabled")" enabled
expect_eq 'rollback restarts active dashboard' "$(cat "$STATE/user-unit-hermes-dashboard.service-active")" active
expect_count 'rollback disables gateway exactly once' 'systemctl --user --machine=hermes@.host disable hermes-gateway.service' 1
expect_exact_count 'inactive gateway is never stopped unnecessarily' 'systemctl --user --machine=hermes@.host stop hermes-gateway.service' 0
expect_count 'rollback enables dashboard exactly once' 'systemctl --user --machine=hermes@.host enable hermes-dashboard.service' 1
expect_exact_count 'active dashboard restarts once on apply and once on rollback' 'systemctl --user --machine=hermes@.host restart hermes-dashboard.service' 2
expect_contains 'inverse Hermes rollback completes' "$OUT" 'ROLLBACK OK'

echo '== partial live-state restore is loud and never reports success'
setup
printf '%s\n' disabled > "$STATE/unit-legaltech-monitor.timer-enabled"
printf '%s\n' inactive > "$STATE/unit-legaltech-monitor.timer-active"
: > "$STATE/postflight-fail"
printf '%s\n' 'disable legaltech-monitor.timer' > "$STATE/fail-command"
TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'failed exact timer restore leaves apply failed' "$RC" 1
expect_contains 'failed exact timer restore is diagnosed incomplete' "$OUT" 'ROLLBACK INCOMPLETO'
expect_missing 'failed exact timer restore never prints success' "$OUT" 'ROLLBACK OK'

echo '== rollback failure at the swap RAM gate is loud and namespace-limited'
setup
: > "$STATE/postflight-fail"
: > "$STATE/swap-rollback-fail"
: > "$STATE/mutate-outside"
TEST_MUTATE=api run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'unsafe swap rollback leaves apply failed' "$RC" 1
expect_exact_count 'unsafe swap rollback attempted exactly once' 'swap rollback' 1
expect_contains 'unsafe rollback is diagnosed without secret material' "$OUT" 'ROLLBACK INCOMPLETO'
expect_eq 'unlisted path remains untouched on rollback failure' "$(cat "$FAKE/outside")" 'outside changed'

echo '== public subcommands never call forbidden PJUD actions'
for forbidden in '/api/v1/sync' '/proxy' '/session/mint' '/retry'; do
  expect_missing "no $forbidden endpoint/action" "$SUCCESS_EVENTS" "$forbidden"
done

echo
echo "$PASS ok, $FAIL fail"
[ "$FAIL" -eq 0 ]
