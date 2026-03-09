#!/bin/bash
set -euo pipefail

echo "🚀 Deploying Estrado Worker..."

cd /opt/legal-tech-microservices/estrado-pjud-service

# Pull latest changes
echo "📦 Pulling latest changes..."
git pull origin main

# FIX PERMISIONES DEL .ENV (previene PermissionError)
echo "🔒 Fixing .env permissions..."
sudo chown www-data:www-data .env
sudo chmod 640 .env

# Install dependencies
echo "📦 Installing dependencies..."
source .venv/bin/activate
pip install -r requirements.txt

# Reload systemd
echo "🔄 Reloading systemd..."
sudo systemctl daemon-reload

# Restart worker
echo "🔄 Restarting worker..."
sudo systemctl restart estrado-pjud-worker

# Restart API
echo "🔄 Restarting API..."
sudo systemctl restart estrado-pjud

# Show status
echo "📊 Services status:"
sudo systemctl status estrado-pjud-worker estrado-pjud --no-pager

echo "✅ Deploy complete!"

# Fix .env permissions
echo "🔒 Fixing .env permissions..."
bash /opt/legal-tech-microservices/estrado-pjud-service/scripts/fix-env-permissions.sh
