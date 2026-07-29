#!/bin/bash
# Estrado — Watchdog inteligente del sync PJUD (#1)
# Corre como ROOT (lee systemd + journal + prod DB). Silencioso si todo está sano.
# Ante anomalía: junta contexto y pide diagnóstico a Luna (hermes -z), lo manda a Telegram.
# Complementa a legaltech-monitor (que solo avisa up/down); esto DIAGNOSTICA.
set -uo pipefail

ENV=/opt/legal-tech-microservices/estrado-pjud-service/.env
set -a; . "$ENV"; set +a
API="${SUPABASE_URL%/}/rest/v1"
AUTH=(-H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY")
NOW=$(date -u +%Y-%m-%dT%H:%M:%S)

cnt() {
  local cr
  cr=$(curl -s -m 20 -I "$API/$1" "${AUTH[@]}" -H "Prefer: count=exact" -H "Range: 0-0" 2>/dev/null \
        | tr -d '\r' | grep -i '^content-range:' | sed -E 's#.*/([0-9]+|\*)$#\1#')
  echo "${cr:-0}"
}

ANOMALIES=""
SIG=""
# add <texto humano> <tag estable para firma anti-spam>
add() { ANOMALIES+="- $1"$'\n'; SIG+="${2:-x};"; }
STATE=/var/tmp/estrado-wd-state
COOLDOWN=10800   # 3h: no re-alertar la MISMA anomalía dentro de esta ventana

# 1. API de scraping viva
systemctl is-active --quiet estrado-pjud.service || add "estrado-pjud.service (API PJUD) NO está activo." "api-down"

# 2. Disco y memoria
DISK=$(df / | awk 'NR==2{gsub("%","",$5); print $5}')
[ "${DISK:-0}" -ge 88 ] && add "Disco raíz al ${DISK}% (umbral 88%)." "disk"
MEMAVAIL=$(free -m | awk '/^Mem:/{print $7}')
[ "${MEMAVAIL:-9999}" -lt 400 ] && add "RAM disponible baja: ${MEMAVAIL}MB (<400MB)." "mem"

# 3. Causas con error de sync o bloqueadas
SYNC_ERR=$(cnt "cases?select=id&last_sync_status=eq.error&tracking_status=eq.active")
[ "${SYNC_ERR:-0}" -ge 3 ] && add "${SYNC_ERR} causas activas con last_sync_status=error." "sync-err"
BLOCKED=$(cnt "cases?select=id&sync_blocked_until=gte.$NOW")
[ "${BLOCKED:-0}" -ge 3 ] && add "${BLOCKED} causas con sync bloqueado (sync_blocked_until futuro)." "blocked"

# 4. Worker heartbeat fresco (si el worker está en uso). Alerta si el más reciente es viejo.
HB_TS=$(curl -s -m 20 "$API/sync_worker_heartbeats?select=last_heartbeat_at&order=last_heartbeat_at.desc&limit=1" "${AUTH[@]}" 2>/dev/null \
        | sed -E 's/.*"last_heartbeat_at":"([^"]+)".*/\1/')
if [ -n "$HB_TS" ] && [ "$HB_TS" != "[]" ]; then
  HB_EPOCH=$(date -u -d "$HB_TS" +%s 2>/dev/null || echo 0)
  AGE_MIN=$(( ( $(date -u +%s) - HB_EPOCH ) / 60 ))
  # Solo alerta si el worker systemd está activo pero el heartbeat es viejo (>30 min)
  if systemctl is-active --quiet estrado-pjud-worker.service && [ "$AGE_MIN" -gt 30 ]; then
    add "Worker activo pero último heartbeat hace ${AGE_MIN} min (>30)." "hb-stale"
  fi
fi

# 5. Errores recientes en journal de servicios estrado (última hora)
JERR=$(journalctl -u estrado-pjud.service -u estrado-pjud-worker.service --since "-1 hour" -p err --no-pager 2>/dev/null | grep -viE "^-- " | tail -8)
[ -n "$JERR" ] && add "Errores en journal (última hora):"$'\n'"$JERR" "journal-err"

# 6. Salud del propio Hermes (gateway + dashboard son servicios de usuario de hermes)
for hsvc in hermes-gateway hermes-dashboard; do
  sudo -u hermes XDG_RUNTIME_DIR=/run/user/1002 systemctl --user is-active --quiet "$hsvc.service" \
    || add "Servicio de Hermes caído: ${hsvc}.service" "hermes-$hsvc"
done

# Sano → salir en silencio (0 tokens)
[ -z "${ANOMALIES// }" ] && { rm -f "$STATE"; exit 0; }

# Anti-spam: si la MISMA firma se alertó hace < COOLDOWN, salir en silencio
HASH=$(printf '%s' "$SIG" | md5sum | awk '{print $1}')
if [ -f "$STATE" ]; then
  read -r LAST_HASH LAST_TS < "$STATE" 2>/dev/null || true
  if [ "${LAST_HASH:-}" = "$HASH" ] && [ $(( $(date -u +%s) - ${LAST_TS:-0} )) -lt "$COOLDOWN" ]; then
    exit 0
  fi
fi
echo "$HASH $(date -u +%s)" > "$STATE"

# Anomalía → diagnóstico con Luna
PROMPT_FILE=$(mktemp /tmp/estrado-watchdog-prompt.XXXXXX)
chmod 644 "$PROMPT_FILE"
cat > "$PROMPT_FILE" <<EOF
Sos el SRE de guardia de Estrado (SaaS legal chileno; el sync PJUD usa un pool de IPs residenciales
con challenge anti-bot F5). Detecté estas anomalías en el VPS. Dame un diagnóstico BREVE en español
chileno: (1) causa más probable, (2) acción concreta sugerida. Si parece F5/soft-block o pool
degradado, decilo. No inventes; razoná solo sobre esto:

$ANOMALIES
EOF

DIAG=$(sudo -u hermes bash -lc "cd ~ && timeout 120 hermes -z \"\$(cat '$PROMPT_FILE')\"" 2>/dev/null)
rm -f "$PROMPT_FILE"

MSG="🚨 *Estrado watchdog* — $(date -u +'%F %H:%M UTC')

Anomalías:
${ANOMALIES}
🧠 Diagnóstico:
${DIAG:-（Luna no respondió; revisar manualmente.)}"

CHATID=$(sudo -u hermes bash -lc 'grep -E "^TELEGRAM_ALLOWED_USERS=" ~/.hermes/.env | sed -E "s/^[^=]+=//; s/^[^0-9-]*//; s/[^0-9-].*$//"')
if [ -n "$CHATID" ]; then
  printf '%s' "$MSG" | sudo -u hermes bash -lc "cd ~ && hermes send -t 'telegram:${CHATID}'"
else
  printf '%s' "$MSG" | sudo -u hermes bash -lc "cd ~ && hermes send -t telegram"
fi
