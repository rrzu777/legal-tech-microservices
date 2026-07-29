#!/bin/bash
# Backup diario de la memoria/config de Hermes (#5)
# Corre como ROOT. Respalda solo DATOS (state.db, sessions, kanban, config, .env, skills),
# excluye el repo de código y venv (~950MB). Rota 7 días. Sube a R2 si hay creds.
set -uo pipefail

DEST=/root/hermes-backups
mkdir -p "$DEST"; chmod 700 "$DEST"
STAMP=$(date -u +%Y%m%d-%H%M%S)
OUT="$DEST/hermes-$STAMP.tar.gz"

tar czf "$OUT" -C /home/hermes \
  --exclude='.hermes/hermes-agent' \
  --exclude='.hermes/bin' \
  --exclude='.hermes/cache' \
  --exclude='.hermes/logs' \
  --exclude='.hermes/models_dev_cache.json' \
  --exclude='.hermes/sandboxes' \
  .hermes 2>/dev/null
chmod 600 "$OUT"

# Rotación local: conservar los 7 más recientes
ls -1t "$DEST"/hermes-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f

# Offsite opcional a R2 (si /root/.hermes-r2-creds existe con R2_ENDPOINT/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET)
R2CREDS=/root/.hermes-r2-creds
if [ -f "$R2CREDS" ]; then
  set -a; . "$R2CREDS"; set +a
  /opt/legal-tech-microservices/estrado-pjud-service/.venv/bin/python - "$OUT" <<'PY' 2>&1 | tail -1
import os, sys, boto3
f = sys.argv[1]
s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                  aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
s3.upload_file(f, os.environ["R2_BUCKET"], "hermes-backups/" + os.path.basename(f))
print("  -> subido a R2: hermes-backups/" + os.path.basename(f))
PY
fi

echo "backup: $OUT ($(du -h "$OUT" | cut -f1))"
