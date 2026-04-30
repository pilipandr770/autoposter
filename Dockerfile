# Базовый образ с Playwright + Chromium
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Системные пакеты:
# xvfb       — виртуальный дисплей
# x11vnc     — VNC сервер
# novnc + websockify — веб-клиент для VNC
# yt-dlp зависимости
RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    fonts-dejavu-core \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем Playwright браузеры
RUN playwright install chromium

COPY . .

# Создаём папки для данных
RUN mkdir -p /app/data/{sessions,media,db}

EXPOSE 5000 6080

CMD ["python", "main.py"]
