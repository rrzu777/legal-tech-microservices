#!/bin/bash
# Estrado — Digest diario de negocio (#3)
# Corre como ROOT (lee creds de prod). Compone con Luna via `hermes -z` (solo métricas,
# el service key NUNCA entra al entorno de hermes). Entrega a Telegram via `hermes send`.
set -uo pipefail

ENV="${DIGEST_ENV:-/opt/legal-tech-microservices/estrado-pjud-service/.env}"
set -a; . "$ENV"; set +a
API="${SUPABASE_URL%/}/rest/v1"
AUTH=(-H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY")
SINCE="${DIGEST_SINCE:-$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%S)}"
NOW="${DIGEST_NOW:-$(date -u +%Y-%m-%dT%H:%M:%S)}"
DIGEST_DATE_UTC="${DIGEST_DATE_UTC:-$(date -u +%F)}"

# count exacto vía HEAD (read-only). Un header ausente, wildcard o inválido no
# permite inferir cero: se presenta como "sin datos".
cnt() {
  local cr
  cr=$(curl -s -m 20 -I "$API/$1" "${AUTH[@]}" -H "Prefer: count=exact" -H "Range: 0-0" 2>/dev/null \
        | tr -d '\r' | grep -iE '^content-range:[[:space:]]*0-0/[0-9]+$' \
        | sed -E 's#^[^:]+:[[:space:]]*0-0/([0-9]+)$#\1#')
  case "$cr" in
    ''|*[^0-9]*) echo "sin datos" ;;
    *) echo "$cr" ;;
  esac
}

FIRMS=$(cnt "law_firms?select=id")
TOTAL_USERS=$(cnt "users?select=id")
NEW_USERS=$(cnt "users?select=id&created_at=gte.$SINCE")
TOTAL_CASES=$(cnt "cases?select=id")
ACTIVE_TRACK=$(cnt "cases?select=id&tracking_status=eq.active")
NEW_CASES=$(cnt "cases?select=id&created_at=gte.$SINCE")
NEW_MOV=$(cnt "case_movements?select=id&created_at=gte.$SINCE")
EVENTS_24=$(cnt "product_events?select=id&created_at=gte.$SINCE")
SYNC_ERROR_CURRENT=$(cnt "cases?select=id&last_sync_status=eq.error")
SYNC_BLOCKED_CURRENT=$(cnt "cases?select=id&sync_blocked_until=gte.$NOW")
RUNS_24=$(cnt "case_sync_runs?select=id&created_at=gte.$SINCE")
RUNS_SUCCESS_24=$(cnt "case_sync_runs?select=id&created_at=gte.$SINCE&status=eq.success")
RUNS_ERROR_24=$(cnt "case_sync_runs?select=id&created_at=gte.$SINCE&status=eq.error")
RUNS_BLOCKED_24=$(cnt "case_sync_runs?select=id&created_at=gte.$SINCE&status=eq.blocked")

HB=$(curl -s -m 20 "$API/sync_worker_heartbeats?select=worker_id,status,last_heartbeat_at,cases_synced_today,errors_today,pool_size&order=last_heartbeat_at.desc&limit=1" "${AUTH[@]}" 2>/dev/null)

METRICS="Fecha (UTC): $DIGEST_DATE_UTC
Estudios (law_firms): $FIRMS
Usuarios: $TOTAL_USERS total | +$NEW_USERS en 24h
Causas: $TOTAL_CASES total | $ACTIVE_TRACK en seguimiento PJUD | +$NEW_CASES en 24h
Movimientos nuevos 24h: $NEW_MOV
Product events 24h: $EVENTS_24
Corridas últimas 24h: $RUNS_24 total | $RUNS_SUCCESS_24 success | $RUNS_ERROR_24 error | $RUNS_BLOCKED_24 blocked
Causas con último sync actualmente en error: $SYNC_ERROR_CURRENT
Causas bloqueadas actualmente: $SYNC_BLOCKED_CURRENT
Las corridas de 24h y el estado actual de causas son métricas distintas.
Worker heartbeat (más reciente): $HB"

# Componer con Luna. Prompt+métricas a archivo sin secretos, legible por hermes.
PROMPT_FILE=$(mktemp /tmp/estrado-digest-prompt.XXXXXX)
chmod 644 "$PROMPT_FILE"
cat > "$PROMPT_FILE" <<EOF
Sos el analista de Estrado (SaaS legal chileno para abogados; monitorea causas del Poder Judicial).
Redactá el resumen DIARIO ejecutivo para el fundador, en español chileno, breve y accionable.
Reglas: usá viñetas cortas; abrí con un titular de 1 línea; destacá crecimiento y CUALQUIER problema
de sync (errores, bloqueos, worker sin heartbeat reciente); si todo está sano, decilo en 1 línea.
NO inventes números; usá solo estos. "sin datos" significa desconocido, no cero. No mezcles las
corridas de las últimas 24h con el estado actual de las causas. Terminá con "⚠️ Atención:" solo si
hay algo que requiera acción.

Métricas crudas de las últimas 24h:
$METRICS
EOF

DIGEST=$(sudo -u hermes bash -lc "cd ~ && timeout 120 hermes -z \"\$(cat '$PROMPT_FILE')\"" 2>/dev/null)
rm -f "$PROMPT_FILE"

if [ -z "${DIGEST// }" ]; then
  DIGEST="📊 Digest Estrado — $DIGEST_DATE_UTC
(resumen automático; Luna no respondió)

$METRICS"
fi

# Entregar a Telegram (chat del usuario autorizado de Braun)
CHATID=$(sudo -u hermes bash -lc 'grep -E "^TELEGRAM_ALLOWED_USERS=" ~/.hermes/.env | sed -E "s/^[^=]+=//; s/^[^0-9-]*//; s/[^0-9-].*$//"')
if [ -n "$CHATID" ]; then
  printf '%s' "$DIGEST" | sudo -u hermes bash -lc "cd ~ && hermes send -t 'telegram:${CHATID}'"
else
  printf '%s' "$DIGEST" | sudo -u hermes bash -lc "cd ~ && hermes send -t telegram"
fi
