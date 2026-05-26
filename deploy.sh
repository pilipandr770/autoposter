#!/bin/bash
# =============================================================================
# deploy.sh — Первичный деплой autoposter на Hostinger VPS
# Использование: bash deploy.sh  (из папки проекта)
# =============================================================================
set -euo pipefail

VPS_IP="187.124.6.120"
VPS_USER="root"
REMOTE_DIR="/opt/autoposter"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/hostinger_vps}"

echo "============================================"
echo "  Деплой autoposter → $VPS_IP"
echo "============================================"
echo ""

# ── Шаг 1: Синхронизируем файлы проекта ──────────────────────────────────
echo "[1/3] Загружаем файлы на сервер..."
rsync -avz --progress \
    --exclude='.git/' \
    --exclude='data/' \
    --exclude='.env' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    -e "ssh -i ${SSH_KEY}" \
    ./ "${VPS_USER}@${VPS_IP}:${REMOTE_DIR}/"

# ── Шаг 2: Копируем .env (если нет на сервере) ───────────────────────────
echo ""
echo "[2/3] Проверяем .env на сервере..."
ssh -i "${SSH_KEY}" "${VPS_USER}@${VPS_IP}" bash <<'ENDSSH'
    set -e
    cd /opt/autoposter
    if [ ! -f .env ]; then
        echo "⚠️  .env не найден — создаём из шаблона. ЗАПОЛНИТЕ перед запуском!"
        cp .env.example .env
    else
        echo "✅ .env уже существует"
    fi
ENDSSH

# Загружаем локальный .env если есть
if [ -f ".env" ]; then
    echo "Загружаем локальный .env..."
    scp -i "${SSH_KEY}" .env "${VPS_USER}@${VPS_IP}:${REMOTE_DIR}/.env"
fi

# ── Шаг 3: Открываем порты и запускаем Docker ────────────────────────────
echo ""
echo "[3/3] Запускаем Docker Compose..."
ssh -i "${SSH_KEY}" "${VPS_USER}@${VPS_IP}" bash <<'ENDSSH'
    set -e
    cd /opt/autoposter

    # Открываем порты в firewall
    ufw allow 22/tcp   comment 'SSH'     2>/dev/null || true
    ufw allow 5000/tcp comment 'Autoposter Web UI' 2>/dev/null || true
    ufw allow 6080/tcp comment 'noVNC'   2>/dev/null || true
    ufw --force enable 2>/dev/null || true

    # Создаём папки для данных
    mkdir -p data/db data/media data/sessions

    # Собираем и запускаем
    docker compose pull --ignore-pull-failures 2>/dev/null || true
    docker compose up -d --build --remove-orphans

    echo ""
    echo "✅ Деплой завершён!"
    echo "   Web UI:  http://${HOSTNAME:-VPS_IP}:5000"
    echo "   noVNC:   http://${HOSTNAME:-VPS_IP}:6080/vnc.html"
    echo ""
    docker compose ps
ENDSSH

echo ""
echo "============================================"
echo "  Готово! Следующий шаг: войти в YouTube"
echo "  http://$VPS_IP:6080/vnc.html → Connect"
echo "  → открыть браузер → войти в YouTube Studio"
echo "  → в Web UI: Сохранить сессию"
echo "============================================"

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
