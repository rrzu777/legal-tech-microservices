#!/bin/bash
# Fix .env permissions automatically
# Previene PermissionError en el scraper

ENV_FILE="/opt/legal-tech-microservices/estrado-pjud-service/.env"
LOGFILE="/var/log/estrado-env-permissions.log"

echo "$(date): Checking .env permissions..." >> $LOGFILE

# Verificar si existe el archivo
if [ ! -f "$ENV_FILE" ]; then
    echo "$(date): ERROR - .env file not found" >> $LOGFILE
    exit 1
fi

# Obtener owner actual
CURRENT_OWNER=$(stat -c '%U:%G' "$ENV_FILE")
CURRENT_PERMS=$(stat -c '%a' "$ENV_FILE")

# Owner debe ser www-data:www-data
if [ "$CURRENT_OWNER" != "www-data:www-data" ]; then
    echo "$(date): Fixing owner from $CURRENT_OWNER to www-data:www-data" >> $LOGFILE
    sudo chown www-data:www-data "$ENV_FILE"
fi

# Permisos deben ser 640 (rw-r-----)
if [ "$CURRENT_PERMS" != "640" ]; then
    echo "$(date): Fixing permissions from $CURRENT_PERMS to 640" >> $LOGFILE
    sudo chmod 640 "$ENV_FILE"
fi

# Verificar resultado
FINAL_OWNER=$(stat -c '%U:%G' "$ENV_FILE")
FINAL_PERMS=$(stat -c '%a' "$ENV_FILE")

if [ "$FINAL_OWNER" = "www-data:www-data" ] && [ "$FINAL_PERMS" = "640" ]; then
    echo "$(date): OK - Permissions correct" >> $LOGFILE
else
    echo "$(date): WARNING - Permissions still incorrect: $FINAL_OWNER $FINAL_PERMS" >> $LOGFILE
fi
