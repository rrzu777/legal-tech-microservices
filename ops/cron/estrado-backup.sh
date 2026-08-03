#!/bin/bash
# Estrado — Backup diario del estado NO-git del servicio (#9)
# Corre como ROOT. Todo lo que un `git clone` no reconstruye: el .env del
# microservicio, el cookie store F5 (los bundles minteados), el crontab vivo,
# /etc/estrado-cron.env y el logrotate. Rota 7 días en /root/estrado-backups.
#
# SIN OFFSITE, y es una decisión, no una omisión: el tar contiene
# OJV_PROXY_URL, SUPABASE_SERVICE_KEY y CRON_SECRET, y la regla del proyecto
# es que esas credenciales NO salen del VPS. Este backup cubre borrado o
# corrupción accidental; contra la pérdida total del VPS el runbook es
# re-provisionar los secretos desde sus fuentes (panel del proxy, Supabase,
# Vercel), no restaurar un tar. hermes-backup.sh (que sí sube a R2) respalda
# solo el estado de Hermes, que no lleva estos secretos.
set -uo pipefail

SERVICE_DIR=/opt/legal-tech-microservices/estrado-pjud-service
DEST=/root/estrado-backups
mkdir -p "$DEST"; chmod 700 "$DEST"
STAMP=$(date -u +%Y%m%d-%H%M%S)
# Sin esta guarda, un mktemp fallido deja WORK="" y el staging se arma en la
# RAÍZ del filesystem (mkdir -p "/estrado-..."), con los secretos copiados ahí
# y un trap que limpia "" — o sea nada. La garantía del header muere ahí.
WORK=$(mktemp -d /tmp/estrado-backup.XXXXXX) || { echo "$(date '+%Y-%m-%d %H:%M') estrado-backup ERROR: mktemp falló" >&2; exit 1; }
[ -d "$WORK" ] || { echo "$(date '+%Y-%m-%d %H:%M') estrado-backup ERROR: staging inválido" >&2; exit 1; }
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/estrado-$STAMP"
mkdir -p "$STAGE"

# El cookie store vive donde diga el .env (COOKIE_STORE_PATH pisa el default;
# buscarlo en una ruta fija ya nos mandó una vez a una conclusión falsa).
COOKIE_STORE_PATH=$(set -a; . "$SERVICE_DIR/.env" 2>/dev/null; set +a; printf '%s' "${COOKIE_STORE_PATH:-$SERVICE_DIR/.cookies.json}")

# Cada fuente se copia si existe; las que falten se anotan en MISSING para que
# el resumen final lo diga en vez de fallar en silencio.
MISSING=""
copia() { # <origen> <nombre dentro del backup>
  if [ -r "$1" ]; then
    cp -a "$1" "$STAGE/$2"
  else
    MISSING+="$1 "
  fi
}

copia "$SERVICE_DIR/.env" service.env
copia "$COOKIE_STORE_PATH" cookies.json
copia /etc/estrado-cron.env estrado-cron.env
copia /etc/logrotate.d/estrado-cron logrotate-estrado-cron

# El crontab vivo, no el snapshot: el snapshot ya vive en git. Si `crontab -l`
# falla se anota, no se confunde con un crontab vacío (ver chequeo 10).
if CRON_LIVE=$(crontab -l 2>/dev/null); then
  printf '%s\n' "$CRON_LIVE" > "$STAGE/crontab.live"
else
  MISSING+="crontab-l "
fi

OUT="$DEST/estrado-$STAMP.tar.gz"
# tar sin chequear + exit 0 incondicional = "backup verde" para siempre con
# tars rotos: el mismo patrón de los 120 días de crons en 404. Si falla, se
# borra el parcial para que el chequeo 11 del watchdog vea el hueco.
if ! tar czf "$OUT" -C "$WORK" "estrado-$STAMP"; then
  echo "$(date '+%Y-%m-%d %H:%M') estrado-backup ERROR: tar falló, backup descartado" >&2
  rm -f "$OUT"
  exit 1
fi
chmod 600 "$OUT"

# Rotación local: conservar los 7 más recientes
ls -1t "$DEST"/estrado-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f

# El timestamp usa el formato local de run-cron.sh: estas líneas van al mismo
# /var/log/estrado-cron.log (el crontab redirige ahí, NO a /dev/null — un
# aviso que muere en /dev/null es el silencio que este script dice combatir).
echo "$(date '+%Y-%m-%d %H:%M') estrado-backup: $OUT ($(du -h "$OUT" | cut -f1))"
if [ -n "$MISSING" ]; then
  echo "$(date '+%Y-%m-%d %H:%M') estrado-backup AVISO: fuentes ausentes: $MISSING"
  exit 1
fi
exit 0
