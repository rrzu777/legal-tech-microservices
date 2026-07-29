#!/usr/bin/env bash
# Instala los scripts de ops/cron/ en el VPS. Hace backup antes de pisar nada.
#
#   ./ops/cron/deploy-cron.sh [host]        # host por defecto: legaltech-vps
#
# NO toca /etc/estrado-cron.env si ya existe: ahi viven APP_URL y CRON_SECRET.
set -euo pipefail

HOST="${1:-legaltech-vps}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP=$(date +%Y%m%d%H%M)
SCRIPTS=(estrado-watchdog.sh estrado-digest.sh hermes-backup.sh run-cron.sh)

# Guarda: nunca desplegar (ni haber commiteado) un secreto.
if grep -rqE '(eyJ[A-Za-z0-9_-]{20,}|[0-9a-f]{40,})' "$SRC"/*.sh; then
  echo "ABORTA: hay algo que parece un secreto en ops/cron/*.sh" >&2
  exit 1
fi

echo "==> sintaxis"
for f in "${SCRIPTS[@]}"; do bash -n "$SRC/$f"; done

echo "==> backup en $HOST:/opt/estrado-cron/backup-$STAMP/"
ssh "$HOST" "mkdir -p /opt/estrado-cron/backup-$STAMP && cp -a /opt/estrado-cron/*.sh /opt/estrado-cron/backup-$STAMP/ 2>/dev/null || true"

for f in "${SCRIPTS[@]}"; do
  echo "==> $f"
  scp -q "$SRC/$f" "$HOST:/tmp/$f"
  ssh "$HOST" "install -o root -g root -m 700 /tmp/$f /opt/estrado-cron/$f && rm -f /tmp/$f"
done

echo "==> logrotate"
scp -q "$SRC/logrotate/estrado-cron" "$HOST:/tmp/lr-estrado"
ssh "$HOST" "logrotate -d /tmp/lr-estrado >/dev/null && install -o root -g root -m 644 /tmp/lr-estrado /etc/logrotate.d/estrado-cron && rm -f /tmp/lr-estrado"

echo "==> /etc/estrado-cron.env"
ssh "$HOST" 'if [ -r /etc/estrado-cron.env ]; then echo "    ya existe, no se toca"; else
  echo "    FALTA. run-cron.sh no arranca sin el. Crealo con:"
  echo "      printf \"APP_URL=https://juristrack.cl\\nCRON_SECRET=...\\n\" > /etc/estrado-cron.env"
  echo "      chmod 600 /etc/estrado-cron.env"
  exit 1
fi'

echo "==> watchdog en dry-run (no manda nada)"
ssh "$HOST" 'DRY_RUN=1 WD_STATE_DIR=/tmp /opt/estrado-cron/estrado-watchdog.sh; echo "    exit=$?"'

echo "==> un cron de prueba, para confirmar que run-cron.sh sigue vivo"
ssh "$HOST" '/opt/estrado-cron/run-cron.sh /api/cron/stale-sync-recovery; echo "    exit=$?"; tail -1 /var/log/estrado-cron.log | sed "s/^/    /"'

echo
echo "listo. rollback: cp /opt/estrado-cron/backup-$STAMP/*.sh /opt/estrado-cron/"
