#!/usr/bin/env bash
set -euo pipefail

HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/caddy/install.sh"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

make_caddy() {
  cat > "$T/caddy" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = validate ]; then
  printf '%s\n' "$3" > "$CADDY_CONFIG_LOG"
  cat "$3" > "$CADDY_CONTENT_LOG"
  exit "${CADDY_VALIDATE_RC:-0}"
fi
exit 0
EOF
  chmod +x "$T/caddy"
}
make_systemctl() {
  cat > "$T/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CTL_LOG"
if [ "$1" = is-active ]; then exit "${CTL_ACTIVE_RC:-0}"; fi
if [ "$1" = reload ]; then
  n=$(cat "$CTL_COUNT"); n=$((n + 1)); printf '%s\n' "$n" > "$CTL_COUNT"
  [ "${CTL_FAIL_RELOAD_ALL:-0}" -eq 1 ] && exit 1
  [ "$n" -ne "${CTL_FAIL_RELOAD_ON:-0}" ]
  exit $?
fi
if [ "$1" = start ]; then exit "${CTL_START_RC:-0}"; fi
exit 1
EOF
  chmod +x "$T/systemctl"
}
reset_fixture() {
  rm -f "$T/dest-link" "$T/dest-broken" "$T/dest-lock-link" "$T/dest.lock" "$T/dest.candidate."* "$T/dest.previous."* 2>/dev/null || true
  rmdir "$T/victim" "$T/dest.lock.d" 2>/dev/null || true
  printf '%s\n' old > "$T/dest"
  printf '%s\n' new > "$T/src"
  : > "$T/log"; : > "$T/caddy-config"; : > "$T/caddy-content"
  printf '0\n' > "$T/count"
  export CTL_LOG="$T/log" CTL_COUNT="$T/count" CADDY_CONFIG_LOG="$T/caddy-config" CADDY_CONTENT_LOG="$T/caddy-content"
  unset CADDY_VALIDATE_RC CTL_ACTIVE_RC CTL_FAIL_RELOAD_ON CTL_FAIL_RELOAD_ALL CTL_START_RC
  make_caddy; make_systemctl
}

reset_fixture
CTL_FAIL_RELOAD_ON=1 "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl" && exit 1 || true
[ "$(cat "$T/dest")" = old ]
[ "$(cat "$T/count")" = 2 ]
! grep -qx 'start caddy' "$T/log"
unset CTL_FAIL_RELOAD_ON
"$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"
[ "$(cat "$T/dest")" = new ]
[ "$(cat "$T/count")" = 3 ]

test "$(cat "$T/caddy-config")" != "$T/src"
cmp -s "$T/src" "$T/caddy-content"

reset_fixture
cp "$T/src" "$T/dest"
"$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"
test "$(wc -l < "$T/log")" -eq 0

reset_fixture
ln -s "$T/src" "$T/dest-link"
if "$HELPER" "$T/src" "$T/dest-link" "$T/caddy" "$T/systemctl"; then exit 1; fi
ln -s "$T/missing" "$T/dest-broken"
if "$HELPER" "$T/src" "$T/dest-broken" "$T/caddy" "$T/systemctl"; then exit 1; fi
mkdir "$T/victim"
if "$HELPER" "$T/src" "$T/victim" "$T/caddy" "$T/systemctl"; then exit 1; fi
[ -d "$T/victim" ]
ln -s "$T/victim" "$T/dest-lock-link"
if "$HELPER" "$T/src" "$T/dest-lock-link" "$T/caddy" "$T/systemctl"; then exit 1; fi
ln -s "$T/victim" "$T/dest.lock"
if "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"; then exit 1; fi
test "$(wc -l < "$T/log")" -eq 0

reset_fixture
printf 'do not change\n' > "$T/lock-victim"
ln -s "$T/lock-victim" "$T/dest.lock"
if "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"; then exit 1; fi
[ "$(cat "$T/lock-victim")" = 'do not change' ]
rm "$T/dest.lock"
ln -s "$T/missing" "$T/dest.lock"
if "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"; then exit 1; fi
rm "$T/dest.lock"
mkdir "$T/dest.lock"
if "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"; then exit 1; fi
rmdir "$T/dest.lock"
test "$(wc -l < "$T/log")" -eq 0

reset_fixture
export CADDY_VALIDATE_RC=1
if "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"; then exit 1; fi
[ "$(cat "$T/dest")" = old ]
test "$(wc -l < "$T/log")" -eq 0
test "$(cat "$T/caddy-config")" != "$T/src"

reset_fixture
export CADDY_VALIDATE_RC=0 CTL_FAIL_RELOAD_ALL=1
if "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"; then exit 1; fi
[ "$(cat "$T/dest")" = old ]
[ "$(cat "$T/count")" = 2 ]

reset_fixture
CTL_ACTIVE_RC=1 CTL_START_RC=1 "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl" && exit 1 || true
[ "$(cat "$T/dest")" = old ]
[ "$(grep -c '^start caddy$' "$T/log" || true)" -eq 1 ]

reset_fixture
NEW_PARENT="$T/nonexistent/parent/Caddyfile"
CTL_ACTIVE_RC=1 CTL_START_RC=0 "$HELPER" "$T/src" "$NEW_PARENT" "$T/caddy" "$T/systemctl"
cmp -s "$T/src" "$NEW_PARENT"

reset_fixture
rm "$T/dest"
CTL_ACTIVE_RC=1 CTL_START_RC=1 "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl" && exit 1 || true
[ ! -e "$T/dest" ]

reset_fixture
rm "$T/dest"
CTL_ACTIVE_RC=1 CTL_START_RC=0 "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"
[ "$(cat "$T/dest")" = new ]

reset_fixture
if command -v flock >/dev/null 2>&1; then
  exec 8>>"$T/dest.lock"
  flock -n 8
  if "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"; then exit 1; fi
  exec 8>&-
else
  mkdir "$T/dest.lock.d"
  if "$HELPER" "$T/src" "$T/dest" "$T/caddy" "$T/systemctl"; then exit 1; fi
fi
[ "$(cat "$T/dest")" = old ]
test "$(wc -l < "$T/log")" -eq 0

echo "caddy helper tests: ok"
