#!/bin/bash
# =============================================================================
# update.sh — Быстрое обновление уже задеплоенного autoposter
# Использование: bash update.sh
# =============================================================================
set -euo pipefail

VPS_IP="187.124.6.120"
VPS_USER="root"
REMOTE_DIR="/opt/autoposter"

echo "Загружаем обновления на сервер..."
rsync -avz --progress \
    --exclude='.git/' \
    --exclude='data/' \
    --exclude='.env' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    ./ "${VPS_USER}@${VPS_IP}:${REMOTE_DIR}/"

echo "Пересобираем и перезапускаем..."
ssh "${VPS_USER}@${VPS_IP}" \
    "cd ${REMOTE_DIR} && \
     docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --remove-orphans && \
     docker compose -f docker-compose.yml -f docker-compose.prod.yml ps"

echo "✅ Обновление завершено"
