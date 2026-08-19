#!/usr/bin/env bash
# Portable contract test for the committed systemd resource units. It parses
# INI sections/properties, then optionally asks systemd-analyze to validate
# systemd syntax when that tool exists on the host.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PASS=0
FAIL=0

ok()  { echo "  ok   $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL $1"; FAIL=$((FAIL + 1)); }

# Emit parsed records as: section<TAB>property<TAB>value. systemd comments are
# only recognized at the start of a line, so preserve values after '=' exactly.
parsed_properties() {
  awk '
    /^[[:space:]]*[#;]/ || /^[[:space:]]*$/ { next }
    /^[[:space:]]*\[[^]]+\][[:space:]]*$/ {
      line = $0
      sub(/^[[:space:]]*\[/, "", line)
      sub(/\][[:space:]]*$/, "", line)
      section = line
      next
    }
    /^[[:space:]]*[^=[:space:]][^=]*=/ {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      key = line
      sub(/[[:space:]]*=.*/, "", key)
      value = line
      sub(/^[^=]*=/, "", value)
      sub(/[[:space:]]*$/, "", value)
      print section "\t" key "\t" value
    }
  ' "$1"
}

# A unique, exact parsed property prevents both a missing resource control and
# an ambiguous duplicate whose effective value would depend on parser rules.
assert_property() { # file section key value
  local file="$ROOT/$1" section="$2" key="$3" expected="$4"
  local matching total
  matching="$(parsed_properties "$file" | awk -F '\t' -v s="$section" -v k="$key" -v v="$expected" '$1 == s && $2 == k && $3 == v { count++ } END { print count + 0 }')"
  total="$(parsed_properties "$file" | awk -F '\t' -v s="$section" -v k="$key" '$1 == s && $2 == k { count++ } END { print count + 0 }')"
  if [ "$matching" -eq 1 ] && [ "$total" -eq 1 ]; then
    ok "$1 [$section] $key=$expected"
  else
    bad "$1 [$section] expected exactly one $key=$expected (matching=$matching, total=$total)"
  fi
}

assert_absent_property() { # file section key [value]
  local file="$ROOT/$1" section="$2" key="$3" expected="${4:-}"
  local count
  if [ -n "$expected" ]; then
    count="$(parsed_properties "$file" | awk -F '\t' -v s="$section" -v k="$key" -v v="$expected" '$1 == s && $2 == k && $3 == v { count++ } END { print count + 0 }')"
    [ "$count" -eq 0 ] && ok "$1 [$section] omits $key=$expected" || bad "$1 [$section] must omit $key=$expected"
  else
    count="$(parsed_properties "$file" | awk -F '\t' -v s="$section" -v k="$key" '$1 == s && $2 == k { count++ } END { print count + 0 }')"
    [ "$count" -eq 0 ] && ok "$1 [$section] omits $key" || bad "$1 [$section] must omit $key"
  fi
}

assert_property ops/systemd/legaltech.slice Slice CPUWeight 1000
assert_property ops/systemd/legaltech.slice Slice MemoryLow 3G
assert_property ops/systemd/legaltech.slice Slice MemoryHigh 6G
assert_property ops/systemd/legaltech.slice Slice MemoryMax 8G

assert_property ops/systemd/estrado-pjud.service Service Slice legaltech.slice
assert_property ops/systemd/estrado-pjud.service Service TasksMax 512

assert_property ops/systemd/estrado-pjud-worker.service Unit PartOf legaltech.slice
assert_property ops/systemd/estrado-pjud-worker.service Service Slice legaltech.slice
assert_property ops/systemd/estrado-pjud-worker.service Service MemoryHigh 2G
assert_property ops/systemd/estrado-pjud-worker.service Service MemoryMax 3G
assert_property ops/systemd/estrado-pjud-worker.service Service CPUQuota 200%
assert_property ops/systemd/estrado-pjud-worker.service Service CPUWeight 800
assert_property ops/systemd/estrado-pjud-worker.service Service TasksMax 512

assert_property ops/systemd-templates/hermes-user.slice.conf Slice MemoryHigh 2G
assert_property ops/systemd-templates/hermes-user.slice.conf Slice MemoryMax 2500M
assert_property ops/systemd-templates/hermes-user.slice.conf Slice TasksMax 1024
assert_property ops/systemd-templates/hermes-user.slice.conf Slice CPUWeight 200

for monitor in ops/systemd/legaltech-monitor.service ops/systemd/legaltech-resource-tracker.service; do
  assert_absent_property "$monitor" Unit PartOf legaltech.slice
  assert_absent_property "$monitor" Service Slice legaltech.slice
done

if command -v systemd-analyze >/dev/null 2>&1; then
  echo "== systemd-analyze verify"
  if systemd-analyze verify \
    "$ROOT/ops/systemd/legaltech.slice" \
    "$ROOT/ops/systemd/estrado-pjud.service" \
    "$ROOT/ops/systemd/estrado-pjud-worker.service"; then
    ok "systemd-analyze verify"
  else
    bad "systemd-analyze verify"
  fi
else
  echo "  skip systemd-analyze verify unavailable on this host (production-only validation)"
fi

echo
echo "$PASS ok, $FAIL fail"
[ "$FAIL" -eq 0 ]
