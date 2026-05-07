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
    xdotool \
    wget \
    gnupg2 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome (includes H.264 codec support, unlike Playwright's Chromium)
RUN wget -qO- https://dl-ssl.google.com/linux/linux_signing_key.pub | \
        gpg --dearmor -o /usr/share/keyrings/google-chrome-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome-keyring.gpg] \
        http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list && \
    DEBIAN_FRONTEND=noninteractive apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Устанавливаем Playwright браузеры
RUN playwright install chromium

COPY . .

# Создаём папки для данных
RUN mkdir -p /app/data/{sessions,media,db}

EXPOSE 5000 6080

CMD ["python", "main.py"]
