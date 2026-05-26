#!/bin/bash
# =============================================================================
# update.sh — Быстрое обновление уже задеплоенного autoposter
# Использование: bash update.sh  (из папки проекта)
# =============================================================================
set -euo pipefail

VPS_IP="187.124.6.120"
VPS_USER="root"
REMOTE_DIR="/opt/autoposter"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/hostinger_vps}"

echo "Загружаем обновления на сервер..."
rsync -avz --progress \
    --exclude='.git/' \
    --exclude='data/' \
    --exclude='.env' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    -e "ssh -i ${SSH_KEY}" \
    ./ "${VPS_USER}@${VPS_IP}:${REMOTE_DIR}/"

echo "Пересобираем и перезапускаем..."
ssh -i "${SSH_KEY}" "${VPS_USER}@${VPS_IP}" \
    "cd ${REMOTE_DIR} && \
     docker compose up -d --build --remove-orphans && \
     docker compose ps"

echo "✅ Обновление завершено"
