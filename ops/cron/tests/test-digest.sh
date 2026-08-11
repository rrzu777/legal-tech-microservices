#!/bin/bash
# Harness hermético del digest: sustituye los límites externos, pero ejecuta el
# script real y valida el prompt que Luna recibiría.
set -uo pipefail

DIGEST="${1:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/estrado-digest.sh}"
TMP=$(mktemp -d)
PASS=0; FAIL=0
trap 'rm -rf "$TMP"' EXIT

expect_contains() {
  if printf '%s' "$2" | grep -qF -- "$3"; then
    echo "  ok   $1"; PASS=$((PASS+1))
  else
    echo "  FAIL $1 — esperaba encontrar: $3"; FAIL=$((FAIL+1))
  fi
}

expect_missing() {
  if printf '%s' "$2" | grep -qF -- "$3"; then
    echo "  FAIL $1 — no esperaba: $3"; FAIL=$((FAIL+1))
  else
    echo "  ok   $1"; PASS=$((PASS+1))
  fi
}

mkdir -p "$TMP/bin"
mkdir -p "$TMP/home/.hermes"
printf 'TELEGRAM_ALLOWED_USERS=123\n' > "$TMP/home/.hermes/.env"
printf 'SUPABASE_URL=http://example.invalid\nSUPABASE_SERVICE_KEY=fake-test-key\n' > "$TMP/digest.env"

cat > "$TMP/bin/curl" <<'EOF'
#!/bin/bash
if [[ " $* " == *" -I "* ]]; then
  case "$*" in
    *'law_firms?'*|*'users?'*|*'case_movements?'*|*'product_events?'*) count=0 ;;
    *'cases?select=id&created_at='*) count=0 ;;
    *'cases?select=id&tracking_status='*) count=0 ;;
    *'cases?select=id&last_sync_status=eq.error'*) count=4 ;;
    *'cases?select=id&sync_blocked_until='*) count=3 ;;
    *'case_sync_runs?select=id&created_at='*'&status=eq.success'*) count=9 ;;
    *'case_sync_runs?select=id&created_at='*'&status=eq.error'*) count=2 ;;
    *'case_sync_runs?select=id&created_at='*'&status=eq.blocked'*) count=1 ;;
    *'case_sync_runs?select=id&created_at='*) count=12 ;;
    *'cases?select=id'*) count=0 ;;
    *) count=0 ;;
  esac
  if [ -n "${DIGEST_FAKE_CONTENT_RANGE:-}" ]; then
    printf 'HTTP/1.1 200 OK\r\nContent-Range: %s\r\n\r\n' "$DIGEST_FAKE_CONTENT_RANGE"
  elif [ "${DIGEST_FAKE_MISSING_RANGE:-0}" != 1 ]; then
    printf 'HTTP/1.1 200 OK\r\nContent-Range: 0-0/%s\r\n\r\n' "$count"
  else
    printf 'HTTP/1.1 200 OK\r\n\r\n'
  fi
else
  printf '[]'
fi
EOF
cat > "$TMP/bin/sudo" <<'EOF'
#!/bin/bash
# Preserve the test PATH instead of entering the host user's login environment.
if [ "$1" = '-u' ] && [ "$3" = 'bash' ] && [ "$4" = '-lc' ]; then
  exec bash -c "$5"
fi
exit 1
EOF
cat > "$TMP/bin/hermes" <<'EOF'
#!/bin/bash
if [ "$1" = '-z' ]; then
  printf '%s' "$2" > "$DIGEST_PROMPT_CAPTURE"
  printf 'digest simulado'
else
  cat > "$DIGEST_SEND_CAPTURE"
fi
EOF
cat > "$TMP/bin/timeout" <<'EOF'
#!/bin/bash
shift
exec "$@"
EOF
chmod +x "$TMP/bin/curl" "$TMP/bin/sudo" "$TMP/bin/hermes" "$TMP/bin/timeout"

run() {
  : > "$TMP/prompt"
  : > "$TMP/send"
  PATH="$TMP/bin:$PATH" HOME="$TMP/home" DIGEST_ENV="$TMP/digest.env" \
    DIGEST_SINCE=2026-08-09T12:00:00 DIGEST_NOW=2026-08-10T12:00:00 \
    DIGEST_DATE_UTC=2026-08-10 DIGEST_PROMPT_CAPTURE="$TMP/prompt" \
    DIGEST_SEND_CAPTURE="$TMP/send" bash "$DIGEST"
}

echo '== digest: semántica de corridas y estado actual =='
run
PROMPT=$(<"$TMP/prompt")
expect_contains 'separa las corridas por estado' "$PROMPT" \
  'Corridas últimas 24h: 12 total | 9 success | 2 error | 1 blocked'
expect_contains 'separa errores actuales de las corridas' "$PROMPT" \
  'Causas con último sync actualmente en error: 4'
expect_contains 'separa bloqueos actuales de las corridas' "$PROMPT" \
  'Causas bloqueadas actualmente: 3'
expect_contains 'explica que ventana y estado actual difieren' "$PROMPT" \
  'Las corridas de 24h y el estado actual de causas son métricas distintas.'

echo '== digest: Content-Range ausente no equivale a cero =='
DIGEST_FAKE_MISSING_RANGE=1 run
PROMPT=$(<"$TMP/prompt")
expect_contains 'muestra datos desconocidos como sin datos' "$PROMPT" 'sin datos'
expect_missing 'no inventa una corrida total cero' "$PROMPT" 'Corridas últimas 24h: 0 total'
expect_contains 'prohíbe traducir desconocido a cero' "$PROMPT" \
  '"sin datos" significa desconocido, no cero.'

echo '== digest: Content-Range inválido no se extrae parcialmente =='
DIGEST_FAKE_MISSING_RANGE=0 DIGEST_FAKE_CONTENT_RANGE='*/12' run
PROMPT=$(<"$TMP/prompt")
expect_contains 'wildcard se mantiene como sin datos' "$PROMPT" 'sin datos'
expect_missing 'wildcard no se convierte en doce' "$PROMPT" 'Corridas últimas 24h: 12 total'
DIGEST_FAKE_CONTENT_RANGE='malformed/99' run
PROMPT=$(<"$TMP/prompt")
expect_contains 'header corrupto se mantiene como sin datos' "$PROMPT" 'sin datos'
expect_missing 'header corrupto no se convierte en noventa y nueve' "$PROMPT" 'Corridas últimas 24h: 99 total'

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
