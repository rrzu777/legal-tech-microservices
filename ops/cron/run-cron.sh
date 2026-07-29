#!/bin/bash
# Estrado Cron Runner
# Calls Vercel app cron endpoints with authentication

# Config fuera del script: /etc/estrado-cron.env (modo 600 root) define APP_URL y
# CRON_SECRET. Vivían hardcodeados acá, y por eso un cambio de dominio tumbó TODOS
# los crons de la app durante 120 días (2026-04-01 → 2026-07-29, 1183 respuestas 404)
# sin dejar rastro en ningún repo ni disparar una sola alerta.
ENV_FILE="${ESTRADO_CRON_ENV:-/etc/estrado-cron.env}"
CRON_LOG="${CRON_LOG:-/var/log/estrado-cron.log}"

if [ ! -r "$ENV_FILE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M') ERROR: falta $ENV_FILE" >> "$CRON_LOG"
    exit 1
fi
set -a; . "$ENV_FILE"; set +a
: "${APP_URL:?APP_URL no definido en $ENV_FILE}"
: "${CRON_SECRET:?CRON_SECRET no definido en $ENV_FILE}"

ENDPOINT="$1"

# Validate endpoint parameter
if [ -z "$ENDPOINT" ]; then
    echo "$(date '+%Y-%m-%d %H:%M') ERROR: No endpoint provided" >> "$CRON_LOG"
    exit 1
fi

# Make request with logging
HTTP_CODE=$(curl -sf -H "Authorization: Bearer $CRON_SECRET" "$APP_URL$ENDPOINT" \
  -o /dev/null -w "%{http_code}" \
  2>&1)

# Log result
echo "$(date '+%Y-%m-%d %H:%M') $ENDPOINT - HTTP $HTTP_CODE" >> "$CRON_LOG" 2>&1

# Exit with error if request failed
if [ "$HTTP_CODE" -ne 200 ]; then
    exit 1
fi

exit 0
