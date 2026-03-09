#!/bin/bash
# Health check para el worker
# Enviar email si el worker está down

EMAIL="tu@email.com"  # CAMBIAR por tu email

# Verificar que el worker esté activo
if ! systemctl is-active --quiet estrado-pjud-worker; then
    echo "Worker is down!" | mail -s "[ALERTA] Estrado Worker Down" "$EMAIL"
    echo "$(date '+%Y-%m-%d %H:%M') WORKER DOWN" >> /var/log/estrado-worker-health.log
fi

# Verificar logs recientes (últimos 5 min)
if ! journalctl -u estrado-pjud-worker --since "5 minutes ago" --quiet 2>&1 | grep -q "Worker ready"; then
    echo "Worker may be stuck (no heartbeat in 5 min)" | mail -s "[ALERTA] Estrado Worker Stuck" "$EMAIL"
    echo "$(date '+%Y-%m-%d %H:%M') WORKER STUCK" >> /var/log/estrado-worker-health.log
fi
