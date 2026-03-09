#!/bin/bash
# Scraper Failure Monitor
# Revisa las últimas N veces que el scraper se restarteó
# Si hay >3 fallos en 10 min → ALERTA

LOGFILE="/var/log/estrado-scraper-monitor.log"
TELEGRAM_BOT_TOKEN="8343683301:AAEemeviAxm5VGELPnbekIy2lou_0Trh4Zk"
TELEGRAM_CHAT_ID="886820553"
MAX_FAILURES=3
WINDOW_MINUTES=10

send_alert() {
    local message="$1"
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=${message}" \
        -d "parse_mode=Markdown" > /dev/null 2>&1
}

echo "$(date): Checking scraper failures..." >> $LOGFILE

# Contar restarts en la ventana de tiempo
RESTART_COUNT=$(sudo journalctl -u estrado-pjud --since "${WINDOW_MINUTES} minutes ago" 2>&1 | grep -c "Scheduled restart")

# Verificar estado actual
STATUS=$(sudo systemctl is-active estrado-pjud 2>&1)

# Verificar últimos errores
LAST_ERROR=$(sudo journalctl -u estrado-pjud -n 5 --no-pager 2>&1 | grep -i "error\|fail\|permission" | tail -1)

if [ "$RESTART_COUNT" -ge "$MAX_FAILURES" ]; then
    ALERT="🚨 *ALERTA: Scraper en Loop de Restarts*

*Servicio:* estrado-pjud (API)
*Restarts:* $RESTART_COUNT en ${WINDOW_MINUTES} min
*Estado actual:* $STATUS

*Último error:*
\`\`\`
$LAST_ERROR
\`\`\`

*Acciones recomendadas:*
1. \`journalctl -u estrado-pjud -n 50\`
2. \`systemctl status estrado-pjud\`
3. Verificar permisos del .env

_Timestamp: $(date '+%Y-%m-%d %H:%M')_"

    send_alert "$ALERT"
    echo "$(date): ALERT SENT - $RESTART_COUNT restarts in ${WINDOW_MINUTES} min" >> $LOGFILE
elif [ "$STATUS" != "active" ]; then
    ALERT="⚠️ *ALERTA: Scraper INACTIVO*

*Servicio:* estrado-pjud (API)
*Estado:* $STATUS

*Acción:*
\`\`\`
systemctl restart estrado-pjud
\`\`\`

_Timestamp: $(date '+%Y-%m-%d %H:%M')_"

    send_alert "$ALERT"
    echo "$(date): ALERT SENT - Scraper inactive" >> $LOGFILE
else
    echo "$(date): OK - $RESTART_COUNT restarts (threshold: $MAX_FAILURES)" >> $LOGFILE
fi
