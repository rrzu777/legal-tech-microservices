#!/bin/bash
# Estrado — Watchdog inteligente del sync PJUD (#1)
# Corre como ROOT (lee systemd + journal + prod DB). Silencioso si todo está sano.
# Ante anomalía: junta contexto y pide diagnóstico a Luna (hermes -z), lo manda a Telegram.
# Complementa a legaltech-monitor (que solo avisa up/down); esto DIAGNOSTICA.
set -uo pipefail

ENV="${WD_ENV:-/opt/legal-tech-microservices/estrado-pjud-service/.env}"
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
# Rutas inyectables para poder testear el script sin tocar el estado real ni
# mandar nada a Telegram. Ver ops/cron/tests/test-watchdog.sh.
WD_STATE_DIR="${WD_STATE_DIR:-/var/tmp}"
STATE="$WD_STATE_DIR/estrado-wd-state"
COOLDOWN="${WD_COOLDOWN:-10800}"   # 3h: no re-alertar la MISMA anomalía dentro de esta ventana

# 1. API de scraping viva
systemctl is-active --quiet estrado-pjud.service || add "estrado-pjud.service (API PJUD) NO está activo." "api-down"

# 2. Disco y memoria
DISK=$(df / | awk 'NR==2{gsub("%","",$5); print $5}')
[ "${DISK:-0}" -ge 88 ] && add "Disco raíz al ${DISK}% (umbral 88%)." "disk"
MEMAVAIL=$(free -m | awk '/^Mem:/{print $7}')
[ "${MEMAVAIL:-9999}" -lt 400 ] && add "RAM disponible baja: ${MEMAVAIL}MB (<400MB)." "mem"

# 3. Causas con error de sync o bloqueadas
# El chequeo original cruzaba last_sync_status=error CON tracking_status=active, y esa
# combinación no existe: cuando una causa falla, el worker le mueve el tracking_status.
# Devolvía */0 siempre — verificado el 29 jul con 3 causas genuinamente rotas en la base.
# `tracking_status` es la señal de calidad: los errores de infra no lo tocan.
#
# Suspendidas: alerta UNA vez por causa. El anti-spam global es por cooldown de 3h y la
# suspensión es terminal, así que sin esto la misma causa avisaría cada 3h para siempre.
# El archivo guarda los IDs ya avisados y se REESCRIBE entero en cada corrida: si una
# causa se reactiva sale de la lista, y si vuelve a caer se avisa de nuevo.
SUSP_STATE="$WD_STATE_DIR/estrado-wd-suspended"
SUSP_JSON=$(curl -s -m 20 "$API/cases?select=id,case_number&tracking_status=eq.suspended" "${AUTH[@]}" 2>/dev/null || true)
SUSP_IDS=$(printf '%s' "$SUSP_JSON" | jq -r '.[]?.id' 2>/dev/null | sort || true)
touch "$SUSP_STATE"
NEW_IDS=$(comm -23 <(printf '%s\n' "$SUSP_IDS" | grep -v '^$' | sort) <(sort "$SUSP_STATE") || true)
if [ -n "${NEW_IDS// }" ]; then
  NEW_NUMS=$(printf '%s' "$SUSP_JSON" \
              | jq -r --argjson want "$(printf '%s\n' "$NEW_IDS" | grep -v '^$' | jq -R . | jq -s .)" \
                   '[.[] | select(.id as $i | $want | index($i))] | map(.case_number) | join(", ")' 2>/dev/null || true)
  add "Causa(s) con monitoreo SUSPENDIDO: ${NEW_NUMS:-$NEW_IDS}. Es terminal: no se reintenta sola, hay que reactivarla a mano desde la ficha de la causa." "suspended"
fi
printf '%s\n' "$SUSP_IDS" | grep -v '^$' | sort > "$SUSP_STATE"

TRACK_ERR=$(cnt "cases?select=id&tracking_status=eq.error")
[ "${TRACK_ERR:-0}" -ge 3 ] && add "${TRACK_ERR} causas con tracking_status=error." "track-err"
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

# 7. Salud de los crons de la app (los que run-cron.sh le pega a Vercel).
#    Nadie miraba este log: run-cron.sh apuntó a un dominio viejo del 2026-04-01 al
#    2026-07-29 y las 1183 corridas que devolvieron 404 pasaron inadvertidas 4 meses.
#
#    ZONA HORARIA: run-cron.sh escribe con `date` LOCAL (el VPS es Europe/Berlin),
#    mientras que NOW de acá arriba es UTC. Comparar una contra otra da mal cerca de
#    medianoche. El corte se calcula con `date` LOCAL y, como el formato es
#    YYYY-MM-DD HH:MM, el orden lexicográfico ES el cronológico: alcanza un awk, sin
#    mktime (que en mawk no existe) y sin un fork de `date` por línea.
CRON_LOG="${CRON_LOG:-/var/log/estrado-cron.log}"
if [ ! -r "$CRON_LOG" ]; then
  add "No puedo leer $CRON_LOG — los crons de la app quedan sin vigilancia." "cron-log-missing"
else
  CRON_CUTOFF=$(date -d '-24 hours' '+%Y-%m-%d %H:%M')
  CRON_RECENT=$(awk -v c="$CRON_CUTOFF" 'substr($0,1,16) >= c' "$CRON_LOG" 2>/dev/null || true)
  if [ -z "${CRON_RECENT// }" ]; then
    # El piso normal son ~10 corridas por día. Cero en 24h no es "poca carga": es el
    # crontab borrado, run-cron.sh sin permiso de ejecución o el disco lleno. Este es
    # el modo de falla que un chequeo de "¿hay errores?" NO atrapa — el log deja de
    # crecer y todo se ve sano.
    add "Ningún cron de la app corrió en las últimas 24h (el piso normal son ~10). Revisar el crontab de root y los permisos de /opt/estrado-cron/run-cron.sh." "cron-silent"
  else
    # Se mira la ÚLTIMA corrida de cada endpoint, no todas: la pregunta es "¿esto
    # está roto AHORA?". Un 404 suelto que se recuperó en la corrida siguiente no
    # despierta a nadie — con el cooldown de 3h, un blip alertaría 8 veces mientras
    # sigue dentro de la ventana de 24h. Un endpoint roto de verdad falla también en
    # su última corrida, y los que corren una vez al día tienen esa única corrida
    # como la última, así que igual se atrapan.
    CRON_BAD=$(printf '%s\n' "$CRON_RECENT" | grep -F ' - HTTP ' \
                | awk '{ultimo[$3] = $NF} END {for (e in ultimo) if (ultimo[e] != 200) print e, ultimo[e]}' \
                | sort || true)
    if [ -n "${CRON_BAD// }" ]; then
      CRON_DETAIL=$(printf '%s\n' "$CRON_BAD" | awk '{printf "    %s -> HTTP %s\n", $1, $2}' || true)
      # La firma lleva los códigos: 404 en todo (APP_URL roto) y 401 (CRON_SECRET
      # desincronizado con Vercel) son incidentes distintos, y si el segundo aparece
      # mientras el primero está en cooldown NO puede quedar tapado.
      CRON_CODES=$(printf '%s\n' "$CRON_BAD" | awk '{print $2}' | sort -u | paste -sd, - || true)
      # Ojo: $(...) come el \n final de CRON_DETAIL, así que la pista va con su propio salto.
      add "Crons de la app fallando (últimas 24h):"$'\n'"$CRON_DETAIL"$'\n'"    404 en todos = APP_URL roto; 401 = CRON_SECRET desincronizado con Vercel; 000 = no hubo conexión." "cron-fail:$CRON_CODES"
    fi
  fi
fi

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

# Modo prueba: imprime lo que habría alertado y sale. No llama a Luna ni a Telegram.
if [ "${DRY_RUN:-0}" = "1" ]; then
  printf 'ANOMALIES:\n%sSIG: %s\n' "$ANOMALIES" "$SIG"
  exit 0
fi

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
