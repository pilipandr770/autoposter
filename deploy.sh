#!/bin/bash
# =============================================================================
# deploy.sh — Первичный деплой autoposter на Hostinger VPS
# Использование: bash deploy.sh
# =============================================================================
set -euo pipefail

VPS_IP="187.124.6.120"
VPS_USER="root"
REMOTE_DIR="/opt/autoposter"

echo "============================================"
echo "  Деплой autoposter → $VPS_IP"
echo "============================================"
echo ""

# ── Шаг 1: Синхронизируем файлы проекта ──────────────────────────────────
echo "[1/4] Загружаем файлы на сервер..."
rsync -avz --progress \
    --exclude='.git/' \
    --exclude='data/' \
    --exclude='.env' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    ./ "${VPS_USER}@${VPS_IP}:${REMOTE_DIR}/"

# ── Шаг 2: Удалённая настройка ────────────────────────────────────────────
echo ""
echo "[2/4] Настраиваем файрвол и окружение..."
ssh "${VPS_USER}@${VPS_IP}" bash <<'ENDSSH'
    set -e
    cd /opt/autoposter

    # Файрвол: закрываем прямой доступ к портам приложения
    # (доступ только через Traefik на 80/443)
    ufw allow 22/tcp   comment 'SSH'     2>/dev/null || true
    ufw allow 80/tcp   comment 'HTTP'    2>/dev/null || true
    ufw allow 443/tcp  comment 'HTTPS'   2>/dev/null || true
    ufw deny  5000/tcp comment 'Autoposter direct (blocked)' 2>/dev/null || true
    ufw deny  6080/tcp comment 'noVNC direct (blocked)'      2>/dev/null || true
    ufw --force enable
    echo "✅ Файрвол настроен"

    # Создаём .env если не существует
    if [ ! -f .env ]; then
        cp .env.example .env
        echo ""
        echo "⚠️  ВАЖНО: Файл .env создан из шаблона."
        echo "   Заполните переменную DOMAIN и WEB_SECRET_KEY:"
        echo "   nano /opt/autoposter/.env"
        echo ""
    else
        echo "✅ .env уже существует"
    fi

    # Создаём папки данных (на хосте, монтируются как volume)
    mkdir -p data/{sessions,media,db}
    echo "✅ Папки data/ созданы"
ENDSSH

# ── Шаг 3: Билд и запуск ─────────────────────────────────────────────────
echo ""
echo "[3/4] Собираем Docker-образ и запускаем..."
ssh "${VPS_USER}@${VPS_IP}" bash <<'ENDSSH'
    set -e
    cd /opt/autoposter

    # Проверяем что DOMAIN задан
    if grep -q '^DOMAIN=$' .env || ! grep -q '^DOMAIN=' .env; then
        echo "❌ Ошибка: переменная DOMAIN не задана в .env"
        echo "   Выполните: nano /opt/autoposter/.env"
        echo "   Добавьте строку: DOMAIN=ваш.домен.или.srv1425385.hstgr.cloud"
        exit 1
    fi

    docker compose -f docker-compose.yml -f docker-compose.prod.yml build --pull
    docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --remove-orphans

    echo ""
    echo "✅ Контейнер запущен"
    docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
ENDSSH

# ── Шаг 4: Итог ───────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "✅ Деплой завершён!"
echo ""
echo "Следующие шаги:"
echo "  1. Проверь Web UI: https://ВАШ_ДОМЕН"
echo "  2. noVNC (встроенный браузер для логина):"
echo "     ssh -L 6080:localhost:6080 root@${VPS_IP}"
echo "     → открой http://localhost:6080"
echo "  3. Логи: ssh root@${VPS_IP} 'docker logs -f autoposter'"
echo "============================================"
