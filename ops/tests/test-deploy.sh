#!/usr/bin/env bash
# Tests de ops/deploy.sh. Corren EN EL LAPTOP: todo lo que en el VPS es real
# (git origin, .venv, systemctl, health HTTP) acá se inyecta como stub, que es
# exactamente para lo que el script expone REPO_DIR/SYSTEMCTL/HEALTH_URL/etc.
#
#   ./ops/tests/test-deploy.sh [ruta-a-deploy.sh]
#
# Sale 0 si pasa todo. Nunca escribe fuera de un mktemp -d.
set -uo pipefail

DEPLOY="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy.sh}"
TMP=$(mktemp -d)
PASS=0; FAIL=0

# Health por HTTP de verdad (mismo razonamiento que test-watchdog.sh): con
# file:// curl sale 000 y no distingue "contestó mal" de "no contestó".
PORT=$((18000 + RANDOM % 1000))
mkdir -p "$TMP/www"
( cd "$TMP/www" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 ) &
HTTPD=$!
trap 'kill "$HTTPD" 2>/dev/null; rm -rf "$TMP"' EXIT
for _ in $(seq 1 50); do curl -sf -m 1 "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break; sleep 0.1; done

health_ok()  { echo '{"status":"ok"}' > "$TMP/www/health.json"; }
health_bad() { rm -f "$TMP/www/health.json"; }
HEALTH="http://127.0.0.1:$PORT/health.json"

ok()   { echo "  ok   $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL $1"; FAIL=$((FAIL+1)); }
expect_eq() { [ "$2" = "$3" ] && ok "$1" || { bad "$1 (esperaba '$3', vino '$2')"; }; }
expect_contains() { printf '%s' "$2" | grep -qF -- "$3" && ok "$1" || bad "$1 (no contiene '$3')"; }

# Cada test estrena un par origin/clone. El clone es REPO_DIR; los stubs viven
# como untracked adentro (sobreviven al reset --hard, igual que el .venv real).
setup() { # setup <nombre> — deja ORIGIN, REPO, LOG_SYSCTL, LOG_PIP seteados
  local base="$TMP/$1"
  ORIGIN="$base/origin"; REPO="$base/repo"
  LOG_SYSCTL="$base/systemctl.log"; LOG_PIP="$base/pip.log"
  mkdir -p "$ORIGIN"
  git -C "$ORIGIN" init -q -b main
  git -C "$ORIGIN" -c user.email=t@t -c user.name=t commit -q --allow-empty -m A
  mkdir -p "$ORIGIN/estrado-pjud-service"
  echo "httpx==0.27" > "$ORIGIN/estrado-pjud-service/requirements.txt"
  git -C "$ORIGIN" add estrado-pjud-service/requirements.txt
  git -C "$ORIGIN" -c user.email=t@t -c user.name=t commit -q -m base
  git clone -q "$ORIGIN" "$REPO"

  local venv="$REPO/estrado-pjud-service/.venv/bin"
  mkdir -p "$venv"
  printf '#!/bin/bash\nexit "${FAKE_PYTEST_EXIT:-0}"\n' > "$venv/python"
  printf '#!/bin/bash\necho "$@" >> "%s"\n' "$LOG_PIP" > "$venv/pip"
  printf '#!/bin/bash\necho "$@" >> "%s"\nexit 0\n' "$LOG_SYSCTL" > "$base/systemctl"
  chmod +x "$venv/python" "$venv/pip" "$base/systemctl"
  : > "$LOG_SYSCTL"; : > "$LOG_PIP"
  SYSCTL="$base/systemctl"
}

avanza_origin() { # avanza_origin [archivo] — commit B en origin
  local f="${1:-cambio.txt}"
  echo x >> "$ORIGIN/$f"
  git -C "$ORIGIN" add "$f"
  git -C "$ORIGIN" -c user.email=t@t -c user.name=t commit -q -m B
}

# Sin set -e en este archivo: RC se captura a mano y los asserts guardan solos.
run_deploy() { # run_deploy — usa REPO/SYSCTL/HEALTH; deja RC y OUT
  OUT=$(REPO_DIR="$REPO" SYSTEMCTL="$SYSCTL" HEALTH_URL="$HEALTH" \
        HEALTH_RETRIES=2 HEALTH_SLEEP=0 bash "$DEPLOY" 2>&1)
  RC=$?
}

sha_origin() { git -C "$ORIGIN" rev-parse main; }
sha_repo()   { git -C "$REPO" rev-parse HEAD; }
restarts()   { grep -c '^restart ' "$LOG_SYSCTL" || true; }

echo "== deploy feliz: avanza HEAD, 2 restarts, exit 0"
setup feliz; avanza_origin; health_ok; run_deploy
expect_eq "exit 0" "$RC" "0"
expect_eq "HEAD avanzó al de origin" "$(sha_repo)" "$(sha_origin)"
expect_eq "un restart por unit" "$(restarts)" "2"
expect_contains "verifica is-active" "$(cat "$LOG_SYSCTL")" "is-active"
expect_eq "no instala deps sin cambio de requirements" "$(cat "$LOG_PIP")" ""

echo "== tests rojos: código vuelve, servicios NI SE TOCAN, exit 1"
setup rojos; avanza_origin; health_ok
PREV=$(sha_repo)
FAKE_PYTEST_EXIT=1 run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "cero restarts" "$(restarts)" "0"
expect_contains "lo dice" "$OUT" "TESTS ROJOS"

echo "== health no contesta: rollback con re-restart, exit 1"
setup salud; avanza_origin; health_bad
PREV=$(sha_repo)
run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD volvió al previo" "$(sha_repo)" "$PREV"
expect_eq "restart de ida y de rollback (2 units x 2)" "$(restarts)" "4"
expect_contains "avisa intervención manual" "$OUT" "TAMPOCO sana"

echo "== health falla pero el rollback sana: lo dice"
setup sana; avanza_origin; health_bad
# Stub de systemctl que "sana" el health recién en la ronda de rollback (del
# 3er restart en adelante): determinista, sin sleeps ni races.
printf '#!/bin/bash\necho "$@" >> "%s"\nif [ "$(grep -c "^restart" "%s")" -ge 3 ]; then echo ok > "%s"; fi\nexit 0\n' \
  "$LOG_SYSCTL" "$LOG_SYSCTL" "$TMP/www/health.json" > "$SYSCTL"
chmod +x "$SYSCTL"
run_deploy
expect_eq "exit 1 igual: el deploy NO entró" "$RC" "1"
expect_contains "reporta rollback OK" "$OUT" "Rollback OK"

echo "== árbol sucio: aborta sin tocar nada"
setup sucio; avanza_origin; health_ok
echo hack >> "$REPO/estrado-pjud-service/requirements.txt"
PREV=$(sha_repo)
run_deploy
expect_eq "exit 1" "$RC" "1"
expect_eq "HEAD no se movió" "$(sha_repo)" "$PREV"
expect_eq "cero restarts" "$(restarts)" "0"
expect_contains "lo dice" "$OUT" "ABORTA"

echo "== ya al día: exit 0 y no reinicia nada"
setup aldia; health_ok
run_deploy
expect_eq "exit 0" "$RC" "0"
expect_eq "cero restarts" "$(restarts)" "0"
expect_contains "lo dice" "$OUT" "Ya al día"

echo "== requirements cambió: pip install antes de los tests"
setup deps; avanza_origin estrado-pjud-service/requirements.txt; health_ok
run_deploy
expect_eq "exit 0" "$RC" "0"
expect_contains "instaló deps" "$(cat "$LOG_PIP")" "install"

echo
echo "$PASS ok, $FAIL fail"
[ "$FAIL" -eq 0 ]
