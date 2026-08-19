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
  mkdir -p "$FAKE/repo/estrado-pjud-service" "$FAKE/systemd" "$CASE_DIR/tmp" \
    "$FAKE/etc/sysctl.d" "$FAKE/etc/caddy" "$FAKE/etc/logrotate.d" \
    "$FAKE/monitoring" "$FAKE/backups" "$STATE" "$BIN"
  chmod 700 "$FAKE/backups"
  : > "$EVENTS"
  : > "$CASE_DIR/null"
  printf '%s\n' "$EXPECTED_SHA" > "$STATE/git-sha"
  : > "$STATE/git-status"
  printf '%s\n' 1710000000 > "$STATE/now"
  printf '%s\n' 200 > "$STATE/juristrack-code"
  printf '%s\n' 200 > "$STATE/estrado-code"
  printf '%s\n' 0 > "$STATE/claim-count"
  printf '%s\n' fresh > "$STATE/heartbeat"
  printf 'SUPABASE_URL=https://db.invalid\nSUPABASE_SERVICE_KEY=%s\n' "$SECRET_SENTINEL" \
    > "$FAKE/repo/estrado-pjud-service/.env"
  chmod 640 "$FAKE/repo/estrado-pjud-service/.env"

  for file in legaltech.slice estrado-pjud.service estrado-pjud-worker.service \
    legaltech-monitor.service legaltech-resource-tracker.service \
    legaltech-monitor.timer legaltech-resource-tracker.timer; do
    printf 'original %s\n' "$file" > "$FAKE/systemd/$file"
  done
  mkdir -p "$FAKE/systemd/estrado-pjud-worker.service.d" \
    "$FAKE/systemd/user-1002.slice.d"
  printf 'original xvfb\n' > "$FAKE/systemd/estrado-pjud-worker.service.d/xvfb.conf"
  printf 'original hermes\n' > "$FAKE/systemd/user-1002.slice.d/50-legaltech-resource-limits.conf"
  printf 'monitor credential placeholder\n' > "$FAKE/monitoring.env"
  chmod 600 "$FAKE/monitoring.env"
  printf 'UUID=root / ext4 defaults 0 1\n' > "$FAKE/etc/fstab"
  printf 'old sysctl\n' > "$FAKE/etc/sysctl.d/60-legaltech-swap.conf"
  printf 'old caddy\n' > "$FAKE/etc/caddy/Caddyfile"
  printf 'old logrotate\n' > "$FAKE/etc/logrotate.d/legaltech-resources"
  printf 'old monitor\n' > "$FAKE/monitoring/monitor.py"
  printf 'old tracker\n' > "$FAKE/monitoring/resource-tracker.py"
  printf 'outside original\n' > "$FAKE/outside"

  printf '%s\n' enabled > "$STATE/unit-estrado-pjud.service-enabled"
  printf '%s\n' active > "$STATE/unit-estrado-pjud.service-active"
  printf '%s\n' disabled > "$STATE/unit-estrado-pjud-worker.service-enabled"
  printf '%s\n' inactive > "$STATE/unit-estrado-pjud-worker.service-active"
  for unit in legaltech-monitor.service legaltech-resource-tracker.service; do
    printf '%s\n' disabled > "$STATE/unit-$unit-enabled"
    printf '%s\n' inactive > "$STATE/unit-$unit-active"
  done
  for unit in legaltech-monitor.timer legaltech-resource-tracker.timer; do
    printf '%s\n' enabled > "$STATE/unit-$unit-enabled"
    printf '%s\n' active > "$STATE/unit-$unit-active"
  done

  if [ -d "$TMP/stub-bin" ]; then
    cp -R "$TMP/stub-bin/." "$BIN/"
  else
  write_stub git <<'EOF'
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
printf '%s\n' 'user@1002.service hermes-gateway.service' 'user@1002.service hermes-dashboard.service'
EOF
  write_stub systemctl <<'EOF'
printf 'systemctl %s\n' "$*" >> "$RG_TEST_STATE/events"
if [ -f "$RG_TEST_STATE/fail-command" ] && [ "$*" = "$(cat "$RG_TEST_STATE/fail-command")" ]; then exit 1; fi
if [ "${1:-}" = daemon-reload ] && [ -e "$RG_TEST_STATE/daemon-fail" ]; then exit 1; fi
if [ "${1:-}" = list-unit-files ]; then
  case "$(cat "$RG_TEST_STATE/list-shape" 2>/dev/null || true)" in
    unexpected-state) printf '%s\n' 'estrado-pjud.service disabled enabled' ;;
    extra) printf '%s\n' 'estrado-pjud.service enabled enabled extra' ;;
    *) printf '%s\n' 'estrado-pjud.service enabled enabled' 'hermes-gateway.service enabled enabled' 'hermes-dashboard.service enabled disabled' ;;
  esac
  exit 0
fi
if [ "${1:-}" = --user ] && [ "${3:-}" = list-unit-files ]; then
  printf '%s\n' 'hermes-gateway.service enabled enabled' 'hermes-dashboard.service enabled disabled'
  exit 0
fi
if [ "${1:-}" = --user ] && [ "${3:-}" = restart ]; then exit 0; fi
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
  [ ! -e "$RG_TEST_STATE/postflight-fail" ] || exit 1
  property_value() {
  if [ -e "$RG_TEST_STATE/property-bad" ]; then
    bad_key=$(cat "$RG_TEST_STATE/property-bad")
    [ -n "$bad_key" ] || bad_key=legaltech.slice:MemoryMax
    if [ "$unit:$1" = "$bad_key" ]; then echo drifted; return; fi
  fi
  case "$unit:$1" in
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
    estrado-pjud-worker.service:Slice) echo legaltech.slice ;;
    estrado-pjud-worker.service:MemoryHigh) echo 2147483648 ;;
    estrado-pjud-worker.service:MemoryMax) echo 3221225472 ;;
    estrado-pjud-worker.service:CPUQuotaPerSecUSec) echo 2s ;;
    estrado-pjud-worker.service:CPUWeight) echo 800 ;;
    estrado-pjud-worker.service:TasksMax) echo 512 ;;
    user-1002.slice:MemoryHigh) echo 2147483648 ;;
    user-1002.slice:MemoryMax) echo 2621440000 ;;
    user-1002.slice:TasksMax) echo 1024 ;;
    user-1002.slice:CPUWeight) echo 200 ;;
    legaltech-monitor.service:Slice|legaltech-resource-tracker.service:Slice) echo system.slice ;;
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
    legaltech-resource-tracker.service:StateDirectory|legaltech-resource-tracker.service:LogsDirectory) echo '' ;;
    legaltech-resource-tracker.service:ReadWritePaths) echo /var/log/legaltech/resources.csv ;;
    legaltech-resource-tracker.service:RestrictAddressFamilies) echo AF_UNIX ;;
    legaltech-monitor.timer:Unit) echo legaltech-monitor.service ;;
    legaltech-resource-tracker.timer:Unit) echo legaltech-resource-tracker.service ;;
    legaltech-monitor.timer:OnBootUSec|legaltech-resource-tracker.timer:OnBootUSec) echo 5min ;;
    legaltech-monitor.timer:OnUnitActiveUSec|legaltech-resource-tracker.timer:OnUnitActiveUSec) echo 5min ;;
    legaltech-monitor.timer:Persistent|legaltech-resource-tracker.timer:Persistent) echo yes ;;
    legaltech-monitor.timer:RandomizedDelayUSec|legaltech-resource-tracker.timer:RandomizedDelayUSec) echo 1min ;;
    *) exit 1 ;;
  esac; }
  if [ "$value_mode" -eq 1 ]; then property_value "$property"; else
    for property in "${properties[@]}"; do printf '%s=%s\n' "$property" "$(property_value "$property")"; done
  fi
  exit 0
fi
state_file() { printf '%s/unit-%s-%s' "$RG_TEST_STATE" "$1" "$2"; }
case "${1:-}" in
  is-enabled|is-active)
    kind=${1#is-}; unit=${2:-}; value=$(cat "$(state_file "$unit" "$kind")") || exit 1
    printf '%s\n' "$value"
    if [ "$kind" = enabled ]; then [ "$value" = enabled ]; else [ "$value" = active ]; fi
    ;;
  enable|disable)
    action=$1; shift
    case "$action" in enable) value=enabled ;; disable) value=disabled ;; esac
    for unit in "$@"; do [ "$unit" = -- ] || printf '%s\n' "$value" > "$(state_file "$unit" enabled)"; done
    ;;
  start|stop|restart)
    action=$1; shift
    case "$action" in stop) value=inactive ;; *) value=active ;; esac
    for unit in "$@"; do [ "$unit" = -- ] || printf '%s\n' "$value" > "$(state_file "$unit" active)"; done
    ;;
  daemon-reload) exit 0 ;;
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
    case "$(cat "$RG_TEST_STATE/claim-count")" in
      missing) printf 'HTTP/1.1 200 OK\r\n\r\n' > "$header" ;;
      malformed) printf 'HTTP/1.1 200 OK\r\nContent-Range: */*\r\n\r\n' > "$header" ;;
      wildcard) printf 'HTTP/1.1 200 OK\r\nContent-Range: */2\r\n\r\n' > "$header" ;;
      httpfail) exit 22 ;;
      zero-star) printf 'HTTP/1.1 200 OK\r\nContent-Range: */0\r\n\r\n' > "$header" ;;
      value) count=$(cat "$RG_TEST_STATE/claim-value"); printf 'HTTP/1.1 200 OK\r\nContent-Range: 0-0/%s\r\n\r\n' "$count" > "$header" ;;
      *) count=$(cat "$RG_TEST_STATE/claim-count"); printf 'HTTP/1.1 200 OK\r\nContent-Range: 0-0/%s\r\n\r\n' "$count" > "$header" ;;
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
  case "$(cat "$RG_TEST_STATE/heartbeat")" in
    fresh) printf '[{"status":"running","last_heartbeat_at":"2024-03-09T15:59:00Z"}]' > "$output" ;;
    stale) printf '[{"status":"running","last_heartbeat_at":"2024-03-09T15:40:00Z"}]' > "$output" ;;
    malformed) printf '{"message":"unsafe-detail"}' > "$output" ;;
    httpfail) exit 22 ;;
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
  '-u +%Y%m%dT%H%M%SZ') echo 20240309T160000Z ;;
  *'@'*'+%Y-%m-%dT%H:%M:%SZ') echo 2024-03-09T12:00:00Z ;;
  *'2024-03-09T15:59:00Z'*'+%s') echo 1709999940 ;;
  *'2024-03-09T15:40:00Z'*'+%s') echo 1709998800 ;;
  *) exit 1 ;;
esac
EOF
  write_stub stat <<'EOF'
path=${@: -1}
[ -e "$path" ] || [ -L "$path" ] || exit 1
if [ -d "$path" ]; then mode=$(/usr/bin/stat -f '%Lp' "$path"); else mode=$(/usr/bin/stat -f '%Lp' "$path"); fi
uid=$(/usr/bin/stat -f '%u' "$path")
gid=$(/usr/bin/stat -f '%g' "$path")
links=$(/usr/bin/stat -f '%l' "$path")
if [ -e "$RG_TEST_STATE/credential-wrong-gid" ] && [ "$path" = "$RG_CREDENTIAL_FILE" ]; then gid=$((gid + 1)); fi
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
[ "${PROV_SKIP_CADDY:-0}" = 1 ] || { printf '%s\n' caddy-not-skipped >> "$RG_TEST_STATE/events"; exit 98; }
printf '%s\n' disabled > "$RG_TEST_STATE/unit-legaltech-monitor.service-enabled"
printf '%s\n' disabled > "$RG_TEST_STATE/unit-legaltech-resource-tracker.service-enabled"
printf '%s\n' enabled > "$RG_TEST_STATE/unit-estrado-pjud.service-enabled"
printf '%s\n' enabled > "$RG_TEST_STATE/unit-legaltech-monitor.timer-enabled"
printf '%s\n' enabled > "$RG_TEST_STATE/unit-legaltech-resource-tracker.timer-enabled"
case "${RG_TEST_MUTATE:-none}" in
  all)
    printf 'changed api\n' >> "$RG_SYSTEMD_DIR/estrado-pjud.service"
    printf 'changed worker\n' >> "$RG_SYSTEMD_DIR/estrado-pjud-worker.service"
    printf 'changed hermes\n' >> "$RG_SYSTEMD_DIR/user-1002.slice.d/50-legaltech-resource-limits.conf"
    ;;
  api) printf 'changed api\n' >> "$RG_SYSTEMD_DIR/estrado-pjud.service" ;;
  dropin) printf 'changed xvfb\n' >> "$RG_SYSTEMD_DIR/estrado-pjud-worker.service.d/xvfb.conf" ;;
esac
if [ -e "$RG_TEST_STATE/mutate-outside" ]; then printf 'outside changed\n' > "$RG_TEST_OUTSIDE"; fi
if [ -e "$RG_TEST_STATE/create-sysctl" ]; then printf 'new sysctl\n' > "$RG_SYSCTL_FILE"; fi
if [ -e "$RG_TEST_STATE/mutate-credential" ]; then printf 'changed protected config\n' > "$RG_CREDENTIAL_FILE"; fi
[ ! -e "$RG_TEST_STATE/provision-fail" ] || exit 1
EOF
  write_stub swap <<'EOF'
printf 'swap %s\n' "$1" >> "$RG_TEST_STATE/events"
if [ "$1" = preflight ]; then
  [ ! -e "$RG_TEST_STATE/swap-preflight-fail" ] || exit 1
  if [ -e "$RG_TEST_STATE/swap-applied" ]; then printf '%s\n' managed; else printf '%s\n' clean; fi
  exit 0
fi
if [ "$1" = apply ] && [ -e "$RG_TEST_STATE/swap-apply-fail" ]; then exit 1; fi
if [ "$1" = apply ]; then : > "$RG_TEST_STATE/swap-applied"; fi
if [ "$1" = verify ]; then
  [ ! -e "$RG_TEST_STATE/swap-verify-fail" ] || exit 1
  [ ! -e "$RG_TEST_STATE/swap-live-drift" ] || exit 1
  [ -e "$RG_TEST_STATE/swap-applied" ] || exit 1
fi
if [ "$1" = rollback ]; then
  [ ! -e "$RG_TEST_STATE/swap-rollback-fail" ] || exit 1
  rm -f "$RG_TEST_STATE/swap-applied"
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
if [ "${target##*/}" = unit-states.tsv ] && [ -e "$RG_TEST_STATE/corrupt-live-state" ]; then
  /usr/bin/sed '$d' "$target" > "$target.tmp" && /bin/mv "$target.tmp" "$target"
fi
EOF
  write_stub python <<'EOF'
printf 'python %s\n' "$*" >> "$RG_TEST_STATE/events"
case "$*" in
  *resource-tracker.py*) [ ! -e "$RG_TEST_STATE/tracker-fail" ] || exit 1 ;;
  *monitor.py*) [ ! -e "$RG_TEST_STATE/monitor-fail" ] || exit 1 ;;
esac
exit 0
EOF
  write_stub jq <<'EOF'
/usr/bin/jq "$@"
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
if [ -e "$RG_TEST_STATE/sha-after-backup" ] && [ "${destination##*/}" = 0015 ]; then
  printf '%s\n' bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb > "$RG_TEST_STATE/git-sha"
fi
EOF
    cp -R "$BIN" "$TMP/stub-bin"
  fi

  TEST_UID=$(/usr/bin/id -u)
  TEST_GID=$(/usr/bin/id -g)
}

run_guard() {
  set +e
  OUT=$(env \
    RG_TEST_MODE=1 RG_TEST_STATE="$STATE" RG_TEST_OUTSIDE="$FAKE/outside" \
    RG_TEST_ROOT_UID="$TEST_UID" RG_TEST_ROOT_GID="$TEST_GID" \
    RG_REPO_DIR="$FAKE/repo" RG_SYSTEMD_DIR="$FAKE/systemd" RG_TMP_ROOT="$CASE_DIR/tmp" RG_DISK_PATH="$FAKE" RG_NULL_FILE="$CASE_DIR/null" \
    RG_CREDENTIAL_FILE="$FAKE/repo/estrado-pjud-service/.env" \
    RG_BACKUP_ROOT="$FAKE/backups" RG_MONITORING_DIR="$FAKE/monitoring" \
    RG_MONITOR_ENV_FILE="$FAKE/monitoring.env" RG_FSTAB_FILE="$FAKE/etc/fstab" \
    RG_SYSCTL_FILE="$FAKE/etc/sysctl.d/60-legaltech-swap.conf" \
    RG_CADDYFILE="$FAKE/etc/caddy/Caddyfile" \
    RG_LOGROTATE_FILE="$FAKE/etc/logrotate.d/legaltech-resources" \
    RG_JURISTRACK_HEALTH_URL=https://juristrack.cl/ \
    RG_ESTRADO_HEALTH_URL=https://estrado.juristrack.cl/api/v1/health \
    RG_GIT_BIN="$BIN/git" RG_DF_BIN="$BIN/df" RG_FREE_BIN="$BIN/free" \
    RG_ID_BIN="$BIN/id" RG_PS_BIN="$BIN/ps" RG_SYSTEMCTL_BIN="$BIN/systemctl" \
    RG_CURL_BIN="$BIN/curl" RG_DATE_BIN="$BIN/date" RG_STAT_BIN="$BIN/stat" \
    RG_SHA256_BIN="$BIN/sha256" RG_FIND_BIN="$BIN/find" RG_CP_BIN="$BIN/cp" \
    RG_RM_BIN=/bin/rm RG_MKDIR_BIN=/bin/mkdir RG_CHMOD_BIN="$BIN/chmod" \
    RG_CHOWN_BIN=/usr/sbin/chown RG_MKTEMP_BIN=/usr/bin/mktemp RG_JQ_BIN="$BIN/jq" \
    RG_PROVISION_BIN="$BIN/provision" RG_SWAP_BIN="$BIN/swap" RG_PYTHON_BIN="$BIN/python" \
    RG_TEST_DISK_BYTES="${TEST_DISK_BYTES:-9663676416}" \
    RG_TEST_RAM_BYTES="${TEST_RAM_BYTES:-7516192768}" \
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
  printf '%s\n' fresh > "$STATE/heartbeat"
  rm -f "$STATE/id-fail" "$STATE/ps-fail" "$STATE/claim-after-first"
  unset TEST_DISK_BYTES TEST_RAM_BYTES
}

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

echo '== preflight accepts only a fully known safe state'
setup
run_guard preflight --expected-sha "$EXPECTED_SHA"
expect_eq 'safe preflight exits zero' "$RC" 0
expect_count 'preflight checks one heartbeat' 'curl heartbeat' 1
expect_count 'preflight checks one exact claim count' 'curl claims' 1
expect_contains 'preflight disk target is injected inside the fake root' "$(cat "$EVENTS")" "df -Pk $FAKE"
expect_contains 'claim adapter uses exact count-only filter and 14400-second UTC cutoff' "$(cat "$EVENTS")" \
  'curl claims method=HEAD https://db.invalid/rest/v1/cases?select=id&sync_worker_id=not.is.null&sync_claimed_at=gte.2024-03-09T12:00:00Z prefer=1 range=1'
expect_contains 'heartbeat adapter requests one operational row only' "$(cat "$EVENTS")" \
  'curl heartbeat https://db.invalid/rest/v1/sync_worker_heartbeats?select=status,last_heartbeat_at&order=last_heartbeat_at.desc&limit=1'
expect_missing 'service credential never appears in curl argv' "$(cat "$EVENTS")" 'credential leaked in argv'
expect_missing 'preflight does not expose service credential' "$OUT$(cat "$EVENTS")" "$SECRET_SENTINEL"
printf '%s\n' zero-star > "$STATE/claim-count"
run_guard preflight --expected-sha "$EXPECTED_SHA"
expect_eq 'exact wildcard zero Content-Range form is accepted' "$RC" 0

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
printf '%s\n' 'SUPABASE_SERVICE_KEY=second-placeholder' >> "$FAKE/repo/estrado-pjud-service/.env"
run_guard preflight --expected-sha "$EXPECTED_SHA"
expect_eq 'duplicate required credential refuses preflight' "$RC" 1
expect_missing 'duplicate credential values are never diagnosed' "$OUT" 'second-placeholder'
setup
chmod 644 "$FAKE/repo/estrado-pjud-service/.env"
run_guard preflight --expected-sha "$EXPECTED_SHA"
expect_eq 'world-readable service configuration refuses preflight' "$RC" 1

run_negative_preflight() {
  local scenario=$1 provision_count backup_count
  trap - EXIT
  STATE="$TMP/negative-state-$scenario"
  EVENTS="$STATE/events"
  mkdir -p "$STATE"
  printf '%s\n' 1710000000 > "$STATE/now"
  reset_preflight_state
  case "$scenario" in
    dirty) echo dirty > "$STATE/git-status" ;;
    sha) echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb > "$STATE/git-sha" ;;
    disk) TEST_DISK_BYTES=8589934591 ;;
    ram) TEST_RAM_BYTES=6442450943 ;;
    uid) : > "$STATE/id-fail" ;;
    juris) echo 503 > "$STATE/juristrack-code" ;;
    estrado) echo 503 > "$STATE/estrado-code" ;;
    stale) echo stale > "$STATE/heartbeat" ;;
    missing|malformed|wildcard|httpfail) echo "$scenario" > "$STATE/claim-count" ;;
    nonzero) echo 1 > "$STATE/claim-count" ;;
  esac
  run_guard apply --expected-sha "$EXPECTED_SHA"
  provision_count=$(grep -c -F provision "$EVENTS" 2>/dev/null || true)
  backup_count=$(grep -c -F backup-copy "$EVENTS" 2>/dev/null || true)
  printf '%s|%s|%s|%s\n' "$scenario" "$RC" "$provision_count" "$backup_count"
}

negative_scenarios=(dirty sha disk ram uid juris estrado stale missing malformed wildcard nonzero httpfail)
negative_pids=()
setup
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
TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'apply succeeds' "$RC" 0
expect_before 'backup precedes provision' backup-copy provision
expect_before 'provision precedes swap apply' provision 'swap apply'
expect_last_before 'swap apply precedes final verify' 'swap apply' 'swap verify'
expect_before 'daemon reload precedes API restart' 'systemctl daemon-reload' 'systemctl restart estrado-pjud.service'
expect_second_before 'immediate second count precedes worker restart' 'curl claims' 'systemctl restart estrado-pjud-worker.service'
expect_count 'heartbeat gate is repeated for worker restart' 'curl heartbeat' 2
expect_count 'claim gate is repeated for worker restart' 'curl claims' 2
expect_count 'API restarted once when changed' 'systemctl restart estrado-pjud.service' 1
expect_count 'worker restarted once when changed and idle' 'systemctl restart estrado-pjud-worker.service' 1
expect_count 'Hermes services restarted once when drop-in changed' 'systemctl --user --machine=hermes@.host restart hermes-gateway.service hermes-dashboard.service' 1
expect_before 'timers start before tracker invocation' 'systemctl start legaltech-monitor.timer legaltech-resource-tracker.timer' 'python '
expect_contains 'monitor is invoked only in dry-run mode' "$(cat "$EVENTS")" 'monitor.py --once --dry-run'
expect_missing 'orchestration output contains no service credential' "$OUT$(cat "$EVENTS")" "$SECRET_SENTINEL"
expect_missing 'suppressed dependency output contains no service credential' "$(cat "$CASE_DIR/null")" "$SECRET_SENTINEL"
SUCCESS_EVENTS=$(cat "$EVENTS")
BACKUP_DIR=$(find "$FAKE/backups" -mindepth 1 -maxdepth 1 -type d -print -quit)
expect_eq 'service credential backup is root-only 0600' "$(/usr/bin/stat -f '%Lp' "$BACKUP_DIR/entries/0010")" 600
expect_missing 'manifest stores metadata but no credential content' "$(cat "$BACKUP_DIR/manifest.tsv")" "$SECRET_SENTINEL"
expect_eq 'manifest has one record for every exact managed path' "$(wc -l < "$BACKUP_DIR/manifest.tsv" | tr -d ' ')" 15
expect_eq 'timestamped backup directory is 0700' "$(/usr/bin/stat -f '%Lp' "$BACKUP_DIR")" 700
if [ -f "$BACKUP_DIR/unit-states.tsv" ]; then
  live_state_lines=$(wc -l < "$BACKUP_DIR/unit-states.tsv" | tr -d ' ')
else
  live_state_lines=missing
fi
expect_eq 'backup records exactly six live unit states' "$live_state_lines" 6
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
TEST_MUTATE=dropin run_guard apply --expected-sha "$EXPECTED_SHA"
expect_eq 'drop-in-only apply succeeds' "$RC" 0
expect_count 'drop-in-only change restarts worker' 'systemctl restart estrado-pjud-worker.service' 1
expect_count 'drop-in-only change repeats heartbeat gate' 'curl heartbeat' 2
expect_count 'drop-in-only change repeats exact claim gate' 'curl claims' 2

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
    hermes-restart) echo '--user --machine=hermes@.host restart hermes-gateway.service hermes-dashboard.service' > "$STATE/fail-command" ;;
    timers) echo 'start legaltech-monitor.timer legaltech-resource-tracker.timer' > "$STATE/fail-command" ;;
    tracker) : > "$STATE/tracker-fail" ;;
    monitor) : > "$STATE/monitor-fail" ;;
    postflight-property) : > "$STATE/property-bad" ;;
    postflight-health) : > "$STATE/health-after-first" ;;
  esac
  TEST_MUTATE=all run_guard apply --expected-sha "$EXPECTED_SHA"
  rollback_count=$(grep -c -F 'swap rollback' "$EVENTS" 2>/dev/null || true)
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
expect_count 'automatic rollback delegates swap once' 'swap rollback' 1
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
expect_count 'unsafe swap rollback attempted exactly once' 'swap rollback' 1
expect_contains 'unsafe rollback is diagnosed without secret material' "$OUT" 'ROLLBACK INCOMPLETO'
expect_eq 'unlisted path remains untouched on rollback failure' "$(cat "$FAKE/outside")" 'outside changed'

echo '== public subcommands never call forbidden PJUD actions'
for forbidden in '/api/v1/sync' '/proxy' '/session/mint' '/retry'; do
  expect_missing "no $forbidden endpoint/action" "$SUCCESS_EVENTS" "$forbidden"
done

echo
echo "$PASS ok, $FAIL fail"
[ "$FAIL" -eq 0 ]
